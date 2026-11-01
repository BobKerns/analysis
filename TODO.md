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
notebook chart wants a `--chart`/`--chart-file` on its terminal equivalent, with the
plot in a widget-free module both share (as `ensemble_plots.py` and `compare_charts.py`
are). Update the table in [notebooks/README.md](notebooks/README.md) when a command is
added.

Check the other direction at the same time: information a read-only `sim` command shows
(a warning, a table, a setup fact) that no notebook makes visible. Commands that create
or change data are out of scope there; the notebooks only view.

Known CLI gaps (last reviewed 2026-11-01; each has an issue):

- events (`notebooks/events.ipynb`): filtering the full `transfer_event` ledger by reason
  kind/tax character/account/date range; `sim items` covers one account and a date range
  only (#189)
- auxiliary tools: summaries for `amazon.ipynb` and `expense_baseline.ipynb` (#333)

Scenario editing still missing from the CLI: `sim symbol edit|delete` (#191) and
`sim series list|show|set` (#194). Machine-readable `--csv`/`--json` output is #196.

Known notebook gaps, shown by the CLI only (last reviewed 2026-11-01; each has an issue):

- a run's negative-balance warnings (`sim explain`) (#431)
- load warnings and not-yet-read definitions (`sim check`, `sim load`) (#432)
- where each row is defined, and the scenario's lineage, seed, size and reserve
  (`calendar list`, `chain list`, `scenario show`) (#433)
- basis step-ups at a death (`sim explain`) (#434)
- year-to-date rows, itemizations and tags in `scenario_overview.ipynb` (`ytd list`,
  `itemization list|show`, `sim tags`) (#435)
- stored prices, the price check and symbols (`prices show|check`, `symbol list`) (#436)
- which run file is open, and a newer failed run (`sim list`) (#437)
- partial rank correlations on a Monte Carlo batch (`ensemble sensitivity`) (#438)
