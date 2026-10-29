---
name: run-analysis
description: Run, smoke-test, or screenshot the analysis finance simulator (the `sim` CLI) and its Jupyter notebooks on throwaway Alice-and-Bill data. Use to run a scenario, check that a change works in the real CLI or a notebook, draw a chart to PNG, or execute notebooks headless and catch the errors their widgets hide.
---

# Run analysis

`analysis` is a CLI (`sim`, `amazon`) plus Jupyter notebooks over a separate
**data repo**. To drive it, build a throwaway data repo from `prototype_data/`
(Alice and Bill, no real data), then run `sim` commands against it. Charts come
out as PNGs from `--graph-file`, or from notebooks executed by `nbrun.py`.

Paths are relative to the repo root. Work in a worktree (see `CLAUDE.md`).
Never point this at the user's real data repo or `data/shared`.

## Prerequisites

`uv`, plus `node` for the lint gates. Once per worktree:

```sh
env -u VIRTUAL_ENV uv sync && npm ci
```

## Run (agent path): the smoke driver

```sh
.claude/skills/run-analysis/smoke.sh "$TMPDIR/analysis-smoke" --fresh
```

This takes about 10 seconds, and its exit status is 0 only if every step passed. It:

1. builds `OUT/datarepo/`: a git repo with `sim/` copied from `prototype_data/`
   and `state/` beside it, the same shape as the real data repo;
2. activates it as user `alice`, then runs `sim check --all`, `sim run base`,
   `sim run retire_alice`, `sim list`, `sim explain`, and `sim networth`;
3. writes `OUT/png/compare.png` (base vs. retire_alice net worth) and
   `OUT/png/alice.png` (lifespan);
4. executes `net_worth`, `accounts`, and `scenario_overview` notebooks with
   `nbrun.py`, writing `OUT/nb/*.html` and `OUT/nb/<notebook>-NNN.png` (one per
   chart).

Without `--fresh`, it reuses `datarepo/`. Use `--no-notebooks` to skip step 4.
**Look at the PNGs** with Read; a blank or $0 chart means the wrong scenario.

Then drive anything else against the same data:

```sh
unset VIRTUAL_ENV
source "$TMPDIR/analysis-smoke/datarepo/sim/activate-analysis" </dev/null
sim run retire_alice
sim report income
sim compare base retire_alice --graph-file "$TMPDIR/analysis-smoke/png/cmp.png"
```

Commands that draw in the terminal take `--graph-file FILE.png` instead of
`--graph`. The commands that have it are `compare`, `person show`,
`chain show`, and the `ensemble` subcommands.

### Notebooks: `nbrun.py`

```sh
python .claude/skills/run-analysis/nbrun.py --timeout 20 "$TMPDIR/analysis-smoke/nb" notebooks/net_worth.ipynb notebooks/lifespan.ipynb 2>/dev/null
```

Run it with a data repo activated. It prints `ok`/`FAILED` and the number of
charts for each notebook, plus each error with the deepest `src/analysis` frame. It
never writes the `.ipynb`. `2>/dev/null` hides the kernel's TCP warnings.
`--timeout` (default 120) is per cell; a cell over it fails, naming the cell.
Some cells wait out the whole timeout for no reason (see Gotchas), so keep it
short (`--timeout 20`) for notebooks that only read finished runs. An ensemble
is different: a run can take nearly 5 minutes, and a pooled ensemble (an
ensemble of ensembles) a multiple of that. Build those with `sim run <recipe>`
*before* running the notebook, so the notebook only opens finished batches. If
a notebook has to build one itself, pass a timeout longer than the build.

Baseline on `main` (61dc10c) with the smoke data, current scenario
`retire_alice`. Anything new in this list is yours:

| Notebook | Result |
| --- | --- |
| `accounts`, `net_worth`, `scenario_compare`, `scenario_overview`, `ensemble`, `sensitivity`, `events`, `lifespan`, `amazon_playground` | ok |
| `amazon` | `FileNotFoundError: State database not found at: <repo>/state/state.sqlite3` (needs real Amazon data) |
| `expense_baseline` | `FileNotFoundError: Quicken export not found at: <repo>/data/shared/quicken/…` (needs real data) |

`ensemble` and `sensitivity` pass but show nothing much: `prototype_data` has
no ensemble recipe.

### Direct invocation (no notebook, no CLI)

For a PR that touches an internal function, import it with the data repo
activated:

```sh
python - </dev/null <<'EOF'
from analysis.sim.notebook_ui.pickers import scenario_picker
from analysis.sim.accounts import accounts_dataframe
widget, conns, dropdown = scenario_picker()   # opens the current scenario
print(dropdown.value)
print(accounts_dataframe(conns.scenario).head())
EOF
```

## Run (human path)

`analysis jupyter` starts JupyterLab for the notebooks. It is interactive, so it
is no use to an agent: use `nbrun.py` instead.

## Test

```sh
env -u VIRTUAL_ENV .venv/bin/python -m pytest -q -n auto -m "not slow"
```

This takes about 50s for about 2750 tests. The other gates are in
`.github/copilot-instructions.md`.

## Gotchas

- **The data dir must be its own git repo.** `sim run` stamps provenance with
  `git add` on the data dir. `scripts/link-data.sh --template` builds `data/shared`
  *inside* the code repo, where `data/` is gitignored. So `sim check` passes
  there, but `sim run` dies with `GitCommandError … paths are ignored`. The
  smoke script runs `git init` on its own copy.
- **`state/` sits beside `sim/`, not inside it.** `$ANALYSIS_DATA_DIR` is
  `<repo>/sim`, and runs land in `<repo>/state/runs`. If you copy
  `prototype_data` straight to `<repo>`, the runs land in the parent directory.
- **`prototype_data`'s `base` is empty.** The real one isn't.
  `net_worth.ipynb` hard-codes `scenario_picker(default="base")`, and its charts
  raise on an empty run (#348). The smoke script copies `retire_alice`'s accounts,
  lots, scheduled transactions, calendar, chains, and `rate__` series into the
  scratch `base`.
- **`sim compare`, `sim run X`, and `sim load X` change the current scenario.**
  Most notebooks open the current scenario, so run `sim switch retire_alice`
  before running them; `net_worth` and `events` always open `base`.
- **Notebook charts fail silently.** They draw into `ipywidgets.Output` after
  the cell returns, so `jupyter nbconvert --execute` reports success while the
  chart raised. `nbrun.py` stores the widget state and reads both images and
  errors from it.
- **`nbconvert` rejects the tracked notebooks** with `'display_name' is a
  required property`, because nbstripout strips `kernelspec.display_name`.
  `nbrun.py` puts it back in memory.
- **Activation prompts unless told who you are.** `activate-analysis` asks for
  the repo path and the user unless `sim/.analysis` and `sim/.analysis_user`
  exist. It also writes `sim/.gitignore`, which makes the data dirty until it
  is committed. The smoke script writes both files and commits afterwards.
- **A global `commit.gpgsign`** breaks the throwaway commit (`cannot run gpg`).
  The script passes `-c commit.gpgsign=false`.
- **Uncommitted code still runs.** It is stamped `code: <sha> (dirty)` in
  `sim explain`; no `--allow-dirty-code` needed.
- **`| head` under `set -o pipefail`** kills the smoke script silently (SIGPIPE).
  Use `sed -n 1,20p`.
- **Some notebooks stall for exactly the cell timeout and then pass.**
  `events.ipynb` does every time, and `lifespan.ipynb` does on its first run. nbclient's own
  timestamps show the cell done (`shell.execute_reply`, `iopub.status.idle`) in
  0.2s; nbclient then waits out the rest of the timeout before the next cell.
  The app code isn't waiting (the same calls take 0.1s outside a kernel). So a
  `--timeout 600` costs 10 minutes on `events`. So run ensembles from the CLI
  first, and give the notebooks a short timeout (see "Notebooks" above).
- **The background builder (`sim builder`) must stay off** for the scratch repo:
  it would queue builds this test never asked for, and opening a run waits for
  a pending build. It works through git hooks that `sim builder install` adds
  (`ANALYSIS_NO_BUILDER` only stops `scripts/setup.sh` from installing it), so a
  fresh `git init` has none. `smoke.sh` stops if `sim builder status` says it is
  installed.
- macOS `/bin/bash` 3.2 prints `Shell completion is not supported for Bash
  versions older than 4.4` three times on activation. It's harmless.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `git.exc.GitCommandError: … The following paths are ignored … data/shared` | The data dir isn't its own repo: use the smoke script's datarepo |
| `FileNotFoundError: no runs for 'base' in …/state/runs` (notebook) | `sim run base` first |
| `IndexError: index 0 is out of bounds for axis 0 with size 0` in `networth_plots.py` | Empty `base`: rebuild with `smoke.sh --fresh` (#348) |
| `KeyError: "None of ['name'] are in the columns"` at `accounts.py:143` | Same cause (#348) |
| `NotebookValidationError: 'display_name' is a required property` | Use `nbrun.py`, not `nbconvert` |
| `error: cannot run gpg` while building the data repo | Already handled; if hand-building, add `-c commit.gpgsign=false` |
