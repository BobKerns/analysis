---
name: analysis-results
description: Answer the user's questions about their finance simulator results on their REAL data — net worth over time, a scenario's configuration, how an account is affected, how money flows through chains, guard rails, comparing scenarios, ensembles — showing charts inline in their terminal, or opening the right notebook. Use for "what does scenario X show", "why did ... go negative", "compare A and B", "are we past a guard rail", "show me ...". Not for creating or editing scenarios.
---

# Analysis results

Answer questions about the user's simulation results, on their **real** data
repo, with checkable numbers and charts they can see. The tools are the `sim`
CLI, the notebooks, and the helpers in this folder. Paths are relative to the
repo root.

## Ground rules

- **Real figures and names stay out of the code repo**: never in a commit,
  issue, PR, test, or file here. They may appear in your answer, in the view
  pane, in notebooks, and (when the user agrees) in a private artifact.
- **Never edit the data repo.** Creating or changing scenarios is a
  different job. You may *run* scenarios (`sim run X`, `sim all`) when a
  result is missing or stale; say so first if it's an ensemble (minutes).
- **Don't move the user's current scenario** with your own lookups:
  `env.sh` sets `ANALYSIS_KEEP_SCENARIO`. Commands you type into the user's
  view pane *should* switch it, as their own commands would.
- **Close stdin**: run every `sim` command with `</dev/null`. A command
  missing an argument asks for it, and would otherwise wait forever
  (`sim compare X` with no variant does).

## Start of every answer

```sh
source .claude/skills/analysis-results/env.sh
```

That activates the real data through the main checkout (its `sim` and
`python`, which the runs and the background builder use) and prints the data
dir. Every shell command starts fresh, so begin each with it.

Read `$ANALYSIS_DATA_DIR/AI-NOTES.md` if it exists: the household's own notes
on what its scenarios and accounts mean and what it usually asks. If you learn
something worth keeping there, propose the wording to the user; don't write
it yourself.

Before opening a scenario's results, check whether that would wait for a
background rebuild:

```sh
python .claude/skills/analysis-results/pending.py base retire_alice
```

It prints `current` or `rebuild pending`, and whether each scenario is a
single run or an ensemble. Opening a pending one waits, printing `waiting for
the background build of X` and then progress. A single run takes seconds to
about half a minute; an ensemble of 1000 can take about 5 minutes, and a
pooled ensemble a multiple of that. Tell the user before such a wait, run the
command in the background, and pass on its progress lines.

## Showing results: the view pane

Your commands have no terminal, so `--graph` can't draw for you. `show.py`
opens a pane beside your session (once; it is reused) and types into it, so
charts draw inline there and the user can scroll or type in it too.

```sh
uv run -q .claude/skills/analysis-results/show.py --cmd "source '$ANALYSIS_DATA_DIR/activate-analysis'"
uv run -q .claude/skills/analysis-results/show.py --cmd 'sim compare base retire_alice --section net-worth --graph'
uv run -q .claude/skills/analysis-results/show.py --read 20
uv run -q .claude/skills/analysis-results/show.py chart.png
uv run -q .claude/skills/analysis-results/show.py --fallback chart.png
```

- `--cmd` types a command and Return. Activate the data in the pane once
  (the first line above, in double quotes so the path expands in your shell,
  not the pane's); after that, type the same `sim` command you used, with
  `--graph`.
- `--read N` returns the pane's last N lines of text. Check it after `--cmd`:
  a typo shows up there, not to you. Images are blank lines in it.
- An image file (from `--graph-file`) is drawn in the pane, or opened in the
  browser where there's no pane. `--fallback` forces the browser.
- iTerm2 is tested. kitty, WezTerm and tmux backends exist but are
  **untested**: if one fails, the script says so (exit 3); tell the user and
  ask whether to use `--fallback`. With no pane at all (exit 4 on `--cmd`),
  give the user the command to run themselves.

Look at a chart yourself (Read the PNG from `--graph-file`) before
describing it.

## How to answer

- **Simple question** (a number, a year, one comparison): two or three
  lines of answer with the figures, plus the matching chart in the view pane
  when there is one. Name the scenario and the run (its timestamp) the
  figures come from, and say CPI-constant or nominal, after-tax or pre-tax
  (the defaults are CPI-constant and after-tax).
- **Complex question** (several scenarios, "why", exploring): don't write
  a wall of text. Give the short conclusion, then put the detail where the
  user can work with it: the notebook that shows it (below), the pane, or,
  for a self-contained interactive view, offer a private claude.ai artifact.
  An artifact puts real figures on claude.ai (private to the user), so ask
  first.
- **Numbers come from commands, not arithmetic in your head.** If you
  combine figures, say how.

## Which command answers what

All take `--scenario X` (or X as an argument where shown) and default to its
latest run; add `</dev/null`. Charts marked † take `--graph` (in the pane) or
`--graph-file F.png`.

| Question | Commands |
| --- | --- |
| What scenarios are there; what is X | `sim scenario list`, `sim scenario show X` (description, lineage, dates, latest run) |
| Net worth over time | `sim networth X [--monthly] [--pre-tax] [--dollars nominal]`; `sim explain X` (year-end net worth, lots, negative balances, years past a guard rail) |
| Compare scenarios | `sim compare BASE V1 [V2...] [--section net-worth\|break-even\|guard-rails\|differences\|runs\|account] [--account A]` † |
| How X is set up | `sim account list`, `sim scheduled list`, `sim calendar list` (each row says where it's defined in the lineage), `sim person list`, `sim guard-rail list` |
| How an account is affected | `sim account history A [--yearly] [--lot L]`, `sim account holdings A`, `sim items --account A --start YYYY-MM [--months N]` (each movement with the rule that made it) |
| How money flows | `sim chain list`, `sim chain show A [--rules]` † (a flowchart of where a debit draws from, refills and overflows) |
| Income, spending, tax | `sim report income`, `sim report expenses [--by category\|account\|tag:ROLE]`, `sim report gains`, `sim report tax [--reconciliation]` |
| Guard rails | `sim report guard-rails [--carried]` (each year's margin, cumulative, and a summary), `sim compare ... --section guard-rails`, `sim ensemble guard-rails R` |
| Lifespans | `sim person show P` † |
| Ensembles (ranges of outcomes) | `sim ensemble summary R` †, `sim ensemble compare R1 R2` †, `sim ensemble sensitivity R`, `sim ensemble draws R` |

`sim networth`, `account history`, the reports and `report guard-rails` have
no chart yet (#327): for those pictures, open the notebook.

**"Why did X happen"**: find the month (`sim explain` lists negative
balances; `account history` shows the change), list that month's movements
with `sim items`, which names the rule behind each, then find that rule in
`sim scheduled list` or `sim calendar list`, whose "Defined in" column says
which scenario in the lineage sets it.

## Notebooks

| Notebook | Shows |
| --- | --- |
| `net_worth.ipynb` | net worth, income, expenses, gains, economic income, guard rails, with charts |
| `accounts.ipynb` | an account's balance history, setup, lots and holdings, allocation |
| `scenario_compare.ipynb` | scenarios side by side, break-even |
| `scenario_overview.ipynb` | a scenario's setup, without a run |
| `events.ipynb` | every movement, filterable; each refill linked to its debit |
| `ensemble.ipynb`, `sensitivity.ipynb` | ensemble bands; which varying rows matter |
| `lifespan.ipynb` | lifespan distributions |

```sh
.claude/skills/analysis-results/notebook.sh net_worth.ipynb
```

It starts JupyterLab (as `analysis jupyter`, from the main checkout, in the
background) if it isn't running, and opens the notebook in the browser. Tell
the user which widget or section to look at, and which scenario to pick in
it (`net_worth` and `events` open on `base`; the others on the current
scenario).

**Untested:** with JupyterLab running and the `jupyter` MCP server from
`.mcp.json` loaded (Claude Code started in the main checkout, server
approved), you could add and run cells in an open notebook directly.

## Gotchas

- Every command naming a scenario makes it current unless
  `ANALYSIS_KEEP_SCENARIO` is set; `env.sh` sets it. Needs #350.
- `sim compare X` with one scenario prompts for a variant. With
  `</dev/null` it aborts (`variant: Aborted!`); without, it hangs.
- Run `pending.py` and `sim` from the **main checkout's** venv (`env.sh`
  does). A worktree's changed code makes every run look stale.
- The builder may be rebuilding scenarios when you start (`sim builder
  status`). A scenario's state can change between two checks.
- `show.py --read` text shows images as blank lines; the prompt line after
  them means the command finished.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `analysis: no data repo found` from `env.sh` | The main checkout has no `data/shared`; ask the user where their data repo is |
| A command sits silent | It's waiting for input: rerun with `</dev/null` and name what it asked for |
| `waiting for the background build of X` | Expected for a stale scenario; let it finish (Ctrl-C would open the old run) |
| `show: the kitty backend is UNTESTED and failed` | Tell the user; ask whether to use `--fallback` |
| `notebook.sh: JupyterLab didn't start` | Read the log it names; port 8888 may be taken |
