#!/bin/sh
# Install the simulator with one line, no git knowledge needed (#308):
#
#   curl -fsSL https://raw.githubusercontent.com/BobKerns/analysis/main/scripts/install.sh | sh
#
# Gets git if it's missing, clones the code into ~/retirement/analysis (or
# updates the clone already there), runs scripts/setup.sh, and links the data
# repo at ~/retirement/data, creating it with `sim init` if there's none yet.
# Run the same line again to update. Options go after `sh -s --`:
#
#   --code DIR       where the code goes (default ~/retirement/analysis)
#   --data DIR       the data repo, to link or create (default ~/retirement/data)
#   --branch NAME    a branch to track instead of main, for testing
#   --yes            take the defaults without asking
#   --id ID, --display NAME, --birthdate YYYY-MM-DD, --sex f|m,
#   --filing-status MFJ|Single|MFS
#                    you, for a new data repo, as `sim init` takes them;
#                    asked for at a terminal, needed without one
#
# Everything is in functions, called on the last line, so a download cut off
# part way runs nothing. Under `curl | sh` the script itself is this shell's
# input, so questions are read from /dev/tty, never from stdin.
set -eu

REPO_URL=${ANALYSIS_REPO_URL:-https://github.com/BobKerns/analysis.git}

say() { printf '%s\n' "$*"; }
die() {
    printf 'install: %s\n' "$*" >&2
    exit 1
}

usage() {
    sed -n '2,19s/^# \{0,1\}//p' "$0" 2>/dev/null >&2 \
        || say "See the comment at the top of scripts/install.sh." >&2
    exit 2
}

# Ask $1 with default $2; the answer (or the default) on stdout.
ask() {
    if [ "$yes" ] || [ "$tty" = /dev/null ]; then
        printf '%s\n' "$2"
        return
    fi
    printf '%s [%s]: ' "$1" "$2" >&2
    answer=""
    read -r answer <"$tty" || answer=""
    printf '%s\n' "${answer:-$2}"
}

# $1 as an absolute path: setup.sh runs from the code's folder, so a
# relative --data would otherwise mean a different place there.
full_path() {
    case $1 in
        \~) printf '%s\n' "$HOME" ;;
        \~/*) printf '%s\n' "$HOME/${1#\~/}" ;;
        /*) printf '%s\n' "$1" ;;
        *) printf '%s\n' "$PWD/$1" ;;
    esac
}

need_git() {
    if [ "$(uname -s)" = Darwin ]; then
        # /usr/bin/git exists even without the command-line tools; it only
        # offers to install them. setup.sh needs them for a compiler anyway.
        if ! xcode-select -p >/dev/null 2>&1; then
            xcode-select --install >/dev/null 2>&1 || true
            die "macOS's command-line tools (which include git) are needed. Finish installing
  them in the window that opened, then run the same install line again."
        fi
        return
    fi
    command -v git >/dev/null 2>&1 && return
    if [ "$(id -u)" -eq 0 ]; then sudo=""; else sudo="sudo "; fi
    if command -v apt-get >/dev/null 2>&1; then
        install="${sudo}apt-get update && ${sudo}apt-get install -y git"
    elif command -v dnf >/dev/null 2>&1; then
        install="${sudo}dnf install -y git"
    elif command -v zypper >/dev/null 2>&1; then
        install="${sudo}zypper install -y git"
    elif command -v pacman >/dev/null 2>&1; then
        install="${sudo}pacman -S --needed --noconfirm git"
    elif command -v apk >/dev/null 2>&1; then
        install="${sudo}apk add git"
    else
        die "git is needed; install it with your system's package manager, then run this again."
    fi
    say "git not found; installing it with: $install"
    # sudo may ask for a password, so it reads the terminal too.
    if ! sh -c "$install" <"$tty" || ! command -v git >/dev/null 2>&1; then
        die "couldn't install git; run this, then the install line again:
    $install"
    fi
}

# Clone the code into $code, or bring an existing clone up to date.
get_code() {
    if [ -d "$code/.git" ]; then
        current=$(git -C "$code" symbolic-ref --short -q HEAD || true)
        if [ "$current" != "$branch" ]; then
            say "$code is on ${current:-no branch}, not $branch; leaving it as it is."
        elif [ -n "$(git -C "$code" status --porcelain)" ]; then
            say "$code has local changes; not updating it."
        else
            say "Updating the code in $code..."
            git -C "$code" pull --ff-only --quiet \
                || say "  couldn't update it; carrying on with the code as it is." >&2
        fi
        return
    fi
    if [ -e "$code" ] && [ -n "$(ls -A "$code" 2>/dev/null)" ]; then
        die "$code exists and isn't a copy of the code; pick another place with --code DIR."
    fi
    say "Downloading the code into $code..."
    mkdir -p "$(dirname "$code")"
    git clone --quiet --branch "$branch" "$REPO_URL" "$code"
}

main() {
    code="" data="" branch=main yes=""
    id="" display="" birthdate="" sex="" sex_set="" filing=""
    while [ $# -gt 0 ]; do
        case $1 in
            --code|--data|--branch|--id|--display|--birthdate|--sex|--filing-status)
                [ $# -ge 2 ] || die "$1 needs a value"
                case $1 in
                    --code) code=$2 ;;
                    --data) data=$2 ;;
                    --branch) branch=$2 ;;
                    --id) id=$2 ;;
                    --display) display=$2 ;;
                    --birthdate) birthdate=$2 ;;
                    --sex) sex=$2; sex_set=1 ;;
                    --filing-status) filing=$2 ;;
                esac
                shift 2
                ;;
            --yes) yes=1; shift ;;
            -h|--help) usage ;;
            *) die "unknown option: $1 (--help lists them)" ;;
        esac
    done

    # A terminal for questions, even when stdin is this script.
    if (: </dev/tty) 2>/dev/null; then tty=/dev/tty; else tty=/dev/null; fi

    say "This installs the retirement simulator."
    code=$(full_path "${code:-$(ask "Where should the code go?" "$HOME/retirement/analysis")}")
    need_git
    get_code

    set --
    if [ -e "$code/data/shared" ] || [ -L "$code/data/shared" ]; then
        : # linked already; link-data.sh says so and leaves it alone
    else
        data=$(full_path "${data:-$(ask "Where is your data, or where should it go?" \
            "$HOME/retirement/data")}")
        if [ -f "$data/sim/persons.csv" ]; then
            set -- "$data"
        else
            if [ "$tty" = /dev/null ] || [ "$yes" ]; then
                missing=""
                for pair in "id:$id" "display:$display" "birthdate:$birthdate" \
                        "filing-status:$filing"; do
                    [ -n "${pair#*:}" ] || missing="$missing --${pair%%:*}"
                done
                [ -z "$missing" ] || die "a new data repo needs:$missing"
            fi
            set -- --new "$data"
            [ -z "$id" ] || set -- "$@" --id "$id"
            [ -z "$display" ] || set -- "$@" --display "$display"
            [ -z "$birthdate" ] || set -- "$@" --birthdate "$birthdate"
            [ -z "$sex_set" ] || set -- "$@" --sex "$sex"
            [ -z "$filing" ] || set -- "$@" --filing-status "$filing"
        fi
    fi

    say "Setting up (this takes a few minutes the first time)..."
    # --yes means no questions, from setup.sh and what it runs as well.
    if [ "$yes" ]; then input=/dev/null; else input=$tty; fi
    (cd "$code" && scripts/setup.sh "$@" <"$input")

    shared=$(cd "$code/data/shared" 2>/dev/null && pwd -P) || shared="$code/data/shared"
    if [ "$branch" != main ] && [ -f "$shared/state/builder/config.json" ]; then
        # setup.sh turned background builds on, for main; they build only
        # from code matching the branch they record.
        "$code/.venv/bin/sim" builder install "$branch" --data-dir "$shared/sim" || true
    fi
    say ""
    say "Installed. Open a new terminal window, then:"
    say "    source $shared/sim/activate-analysis"
    say "    sim run"
    say "To update later, run the same install line again."
}

main "$@"; exit
