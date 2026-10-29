# Source me (bash or zsh) before each lookup: the agent's shell starts fresh
# every command.
#
#   source .claude/skills/analysis-results/env.sh
#
# Activates the user's real data repo through the MAIN checkout (from a
# worktree too): its `sim` and `python` are the code the runs and the
# background builder use, so freshness is judged right. Then sets
# ANALYSIS_KEEP_SCENARIO so lookups never move the user's current scenario.
# Prints one line saying what it activated, or why it couldn't.

_ar_main="$(git worktree list --porcelain 2>/dev/null | sed -n '1s/^worktree //p')"
if [ -z "${ANALYSIS_DATA_DIR-}" ]; then
    if [ -n "$_ar_main" ] && [ -f "$_ar_main/data/shared/sim/activate-analysis" ]; then
        unset VIRTUAL_ENV
        # shellcheck disable=SC1091
        . "$_ar_main/data/shared/sim/activate-analysis" </dev/null >/dev/null 2>&1
    fi
fi
if [ -n "${ANALYSIS_DATA_DIR-}" ]; then
    export ANALYSIS_KEEP_SCENARIO=1
    echo "analysis: data $ANALYSIS_DATA_DIR, user ${ANALYSIS_USER-?}, code ${ANALYSIS_REPO_DIR-?}"
else
    echo "analysis: no data repo found (expected $_ar_main/data/shared/sim/activate-analysis)" >&2
fi
unset _ar_main
