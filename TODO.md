# TODO

To read a formatted `.md` file:
- In Jupyter: right-click on it and select `Open With / Markdown Preview`
- In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

## Review for missing CLI functionality, periodically

Every notebook view or widget should have a terminal equivalent where one makes sense:
it needs no kernel or browser, works over SSH and in scripts, and is what to suggest on a
machine too small for a notebook. Every so often (say after each batch of notebook work),
walk the notebooks and `analysis.sim.notebook_ui`, check each view against `analysis sim
--help`, and add the missing command or note why there isn't one. Charts count too: a
notebook chart wants a `--graph`/`--graph-file` on its terminal equivalent, with the
plot in a widget-free module both share (as `ensemble_plots.py` and `compare_charts.py`
are). Update the table in [notebooks/README.md](notebooks/README.md) when a command is
added.

Known gaps (last reviewed 2026-10-28; each has an issue):

- events (`notebooks/events.ipynb`): filtering the full `transfer_event` ledger by reason
  kind/tax character/account/date range; `sim items` covers one account and a date range
  only (#189)
- charts for plain-run results: net worth, income, expenses, gains, economic income,
  guard rails, account balance history and the allocation pie are tables only in the
  CLI (#327)
- several ensembles against one baseline in one table (`ensemble_table_widget`);
  `sim ensemble compare` takes one variant (#328)
- sensitivity charts (heatmap, tornado, attribution, correlation, R², one row's delta
  over time and response curve) (#329)
- draws charts: a node's histogram and u-plot, a correlated pair's scatter (#330)
- the notebooks' shared Years, Dollars and Scale controls: `--start/--end` on networth,
  report, compare and ensemble commands, `--dollars` on `ensemble summary`, `--log` on
  the ensemble charts (#331)
- `sim explain` still shows pre-tax net worth (#332)
- auxiliary tools: summaries for `amazon.ipynb` and `expense_baseline.ipynb` (#333)

Scenario editing still missing from the CLI: `sim symbol edit|delete` (#191) and
`sim series list|show|set` (#194). Machine-readable `--csv`/`--json` output is #196.
