#!/bin/sh
# Fail if blocked keywords (.githooks/blocked-keywords.txt) appear in:
#   --staged        the staged version of every file in the pending commit
#   --all           every tracked file
#   --message FILE  a commit message
# Set ALLOW_BLOCKED_KEYWORDS=1 to let a deliberate use through.
# cspell:ignore ACMR
set -eu

usage() {
    echo "usage: $0 --staged | --all | --message FILE" >&2
    exit 2
}

[ $# -ge 1 ] || usage
mode=$1

top=$(git rev-parse --show-toplevel)
cd "$top"
list=.githooks/blocked-keywords.txt

# One alternation of the keywords, each delimited by a non-alphanumeric.
keywords=$(grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$list" \
    | sed -e 's/[[:space:]]*$//' -e 's/[][\\.^$*+?(){}|]/\\&/g' -e 's/  */[ _-]+/g' \
    | paste -s -d '|' -)
if [ -z "$keywords" ]; then
    exit 0
fi
pattern="(^|[^[:alnum:]])($keywords)([^[:alnum:]]|\$)"

case $mode in
    --staged)
        # Checked first: with no file names, git grep would search everything.
        if git diff --cached --quiet --diff-filter=ACMR -- . ":!$list"; then
            exit 0
        fi
        hits=$(git diff --cached --name-only -z --diff-filter=ACMR -- . ":!$list" \
            | xargs -0 git grep --cached -n -I -i -E -e "$pattern" -- || true)
        ;;
    --all)
        hits=$(git grep -n -I -i -E -e "$pattern" -- . ":!$list" || true)
        ;;
    --message)
        [ $# -eq 2 ] || usage
        hits=$(grep -v '^#' "$2" | grep -n -i -E -e "$pattern" || true)
        ;;
    *)
        usage
        ;;
esac

if [ -z "$hits" ]; then
    exit 0
fi

echo "Blocked keywords found (see $list):" >&2
printf '%s\n' "$hits" >&2
if [ "${ALLOW_BLOCKED_KEYWORDS:-}" = 1 ]; then
    echo "ALLOW_BLOCKED_KEYWORDS=1 is set; continuing." >&2
    exit 0
fi
echo "These usually mean real data is leaking into this repo. Use the prototype" >&2
echo "names instead, or set ALLOW_BLOCKED_KEYWORDS=1 if the use is deliberate." >&2
exit 1
