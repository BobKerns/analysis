#!/bin/sh
# Fail if blocked keywords appear in:
#   --staged        the staged version of every file in the pending commit
#   --all           every tracked file, as it would be committed: files with
#                   a diff textconv (notebooks, via nbstripout) are searched
#                   through it, so a run notebook's outputs don't count
#   --message FILE  a commit message being written ('#' comment lines
#                   skipped, as git does)
#   --text FILE     any other text, every line checked (a stored commit
#                   message, or a pull request's title and body)
#
# Keywords come from two lists:
#   public   .githooks/blocked-keywords.txt, committed here
#   private  never committed here: the BLOCKED_KEYWORDS_PRIVATE environment
#            variable (a secret in CI and cloud sessions), plus the file
#            blocked-keywords.txt at the top of the data repo, found through
#            $ANALYSIS_DATA_DIR or else the main checkout's data/shared link
# Both use the same format; the private one may also separate keywords with
# commas, for environments that can't hold a multi-line value. A hit on a
# private keyword reports only where it is, never the text, so the list
# doesn't reach a log.
# Set ALLOW_BLOCKED_KEYWORDS=1 to let a deliberate use through.
# cspell:ignore ACMR
set -eu

usage() {
    echo "usage: $0 --staged | --all | --message FILE | --text FILE" >&2
    exit 2
}

[ $# -ge 1 ] || usage
mode=$1
case $mode in
    --staged|--all) ;;
    --message|--text) [ $# -eq 2 ] || usage; input=$2 ;;
    *) usage ;;
esac

top=$(git rev-parse --show-toplevel)
cd "$top"
list=.githooks/blocked-keywords.txt
# Not searched: the public list itself, and the lock files, which tools write
# and which hold only package names, versions, URLs and checksums. A short
# keyword turns up in a checksum's base64 text by chance, where '+', '/' and
# '-' stand as word boundaries around it.
skip_list=":!$list"
skip_npm_lock=':!package-lock.json'
skip_uv_lock=':!uv.lock'

# The private list file: in the data repo named by $ANALYSIS_DATA_DIR (which
# points into its sim/ subtree), or else the one data/shared links to in the
# main checkout, so a worktree without the link and a hook run without the
# environment both still find it.
private_file() {
    if [ -n "${ANALYSIS_DATA_DIR:-}" ] && [ -d "$ANALYSIS_DATA_DIR" ]; then
        data_top=$(git -C "$ANALYSIS_DATA_DIR" rev-parse --show-toplevel 2>/dev/null) || data_top=$ANALYSIS_DATA_DIR
        echo "$data_top/blocked-keywords.txt"
        return
    fi
    common=$(git rev-parse --path-format=absolute --git-common-dir)
    echo "$(dirname "$common")/data/shared/blocked-keywords.txt"
}

# One alternation of the keywords read on stdin, each delimited by a
# non-alphanumeric; empty when there are none.
pattern_of() {
    keywords=$(tr ',' '\n' \
        | grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' \
        | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
              -e 's/[][\\.^$*+?(){}|]/\\&/g' -e 's/  */[ _-]+/g' \
        | paste -s -d '|' -) || true
    if [ -n "$keywords" ]; then
        printf '(^|[^[:alnum:]])(%s)([^[:alnum:]]|$)\n' "$keywords"
    fi
}

# Every hit on pattern $1 in what the mode checks, as path:line:text (files)
# or line:text (a message or text).
search() {
    case $mode in
        --staged)
            # Checked first: with no file names, git grep would search everything.
            if git diff --cached --quiet --diff-filter=ACMR -- . "$skip_list" "$skip_npm_lock" "$skip_uv_lock"; then
                return 0
            fi
            git diff --cached --name-only -z --diff-filter=ACMR -- . "$skip_list" "$skip_npm_lock" "$skip_uv_lock" \
                | xargs -0 git grep --cached -n -I -i -E -e "$1" -- || true
            ;;
        --all)
            # git grep exits 1 for no match; anything higher is an error, such
            # as a textconv that can't run (a worktree without .venv), and
            # must not pass as clean.
            status=0
            git grep --textconv -n -I -i -E -e "$1" -- . "$skip_list" "$skip_npm_lock" "$skip_uv_lock" || status=$?
            if [ "$status" -gt 1 ]; then
                echo "git grep failed (exit $status); files were not all checked." >&2
                exit 2
            fi
            ;;
        --message)
            grep -v '^#' "$input" | grep -n -i -E -e "$1" || true
            ;;
        --text)
            grep -n -i -E -e "$1" "$input" || true
            ;;
    esac
}

found=0

public_pattern=$(pattern_of < "$list")
if [ -n "$public_pattern" ]; then
    hits=$(search "$public_pattern")
    if [ -n "$hits" ]; then
        echo "Blocked keywords found (see $list):" >&2
        printf '%s\n' "$hits" >&2
        found=1
    fi
fi

file=$(private_file)
private_pattern=$( { printf '%s\n' "${BLOCKED_KEYWORDS_PRIVATE:-}"; if [ -f "$file" ]; then cat "$file"; fi; } | pattern_of)
if [ -n "$private_pattern" ]; then
    hits=$(search "$private_pattern")
    if [ -n "$hits" ]; then
        # Where only: the matched text would show the private keyword.
        case $mode in
            --staged|--all) where=$(printf '%s\n' "$hits" | cut -d: -f1,2) ;;
            *) where=$(printf '%s\n' "$hits" | cut -d: -f1 | sed 's/^/line /') ;;
        esac
        echo "Blocked keywords from the private list found at:" >&2
        printf '%s\n' "$where" >&2
        found=1
    fi
fi

if [ "$found" = 0 ]; then
    exit 0
fi

if [ "${ALLOW_BLOCKED_KEYWORDS:-}" = 1 ]; then
    echo "ALLOW_BLOCKED_KEYWORDS=1 is set; continuing." >&2
    exit 0
fi
echo "These usually mean real data is leaking into this repo. Use the prototype" >&2
echo "names instead, or set ALLOW_BLOCKED_KEYWORDS=1 if the use is deliberate." >&2
exit 1
