"""Which scenarios would wait for a background rebuild if opened now.

    python .claude/skills/analysis-results/pending.py [SCENARIO ...]

Run it with the activated data repo's `python` (the main checkout's venv):
freshness compares each run's code with the code asking, so a worktree's
changed code would call every run stale.

Prints one line per scenario (all of them by default): `current`, or
`rebuild pending` (opening it waits for the builder, saying so as it goes),
marking ensembles, whose rebuild can take minutes. Asks without waiting or
moving anything up the builder's queue.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from analysis.sim.builder import build_pending
from analysis.sim.cli import _resolve_runs_dir  # the CLI's own default
from analysis.sim.loader import list_scenario_names
from analysis.sim.scenario import ensemble_settings


def main(names: list[str]) -> int:
    data = os.environ.get("ANALYSIS_DATA_DIR")
    if not data:
        print("pending: no data repo activated ($ANALYSIS_DATA_DIR)", file=sys.stderr)
        return 2
    data_dir = Path(data)
    runs = _resolve_runs_dir(None, None)
    for name in names or list_scenario_names(data_dir):
        setting = ensemble_settings(data_dir, name)[1]
        size = setting.value if setting is not None else 0
        kind = f"ensemble of {size}" if size else "single run"
        state = "rebuild pending" if build_pending(runs, name) else "current"
        print(f"{name:24} {state:16} {kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
