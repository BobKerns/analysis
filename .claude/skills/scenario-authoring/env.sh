# shellcheck shell=bash
# Source me (bash or zsh) before each command: the agent's shell starts fresh
# every time.
#
#   source .claude/skills/scenario-authoring/env.sh
#
# Activates the user's real data repo through the MAIN checkout (from a
# worktree too): its `sim` is the code the runs and the background builder
# use. Sets ANALYSIS_KEEP_SCENARIO, so looking at other scenarios, and even
# creating one, never moves the user's current scenario; `sim switch NAME`
# at the end makes the new scenario current on purpose. Sets COLUMNS so
# tables aren't squeezed to 80 columns. Prints one line saying what it
# activated, or why it couldn't.

_sa_main="$(git worktree list --porcelain 2>/dev/null | sed -n '1s/^worktree //p')"
if [ -z "${ANALYSIS_DATA_DIR-}" ]; then
    if [ -n "$_sa_main" ] && [ -f "$_sa_main/data/shared/sim/activate-analysis" ]; then
        unset VIRTUAL_ENV
        # shellcheck disable=SC1091
        . "$_sa_main/data/shared/sim/activate-analysis" </dev/null >/dev/null 2>&1
    fi
fi
if [ -n "${ANALYSIS_DATA_DIR-}" ]; then
    export ANALYSIS_KEEP_SCENARIO=1
    export COLUMNS=160
    echo "analysis: data $ANALYSIS_DATA_DIR, user ${ANALYSIS_USER-?}, code ${ANALYSIS_REPO_DIR-?}"
else
    echo "analysis: no data repo found (expected $_sa_main/data/shared/sim/activate-analysis)" >&2
fi
unset _sa_main
