# TODO

## Review for missing CLI functionality, periodically

Every notebook view or widget should have a terminal equivalent where one makes sense:
it needs no kernel or browser, works over SSH and in scripts, and is what to suggest on a
machine too small for a notebook. Every so often (say after each batch of notebook work),
walk the notebooks and `analysis.sim.notebook_ui`, check each view against `analysis sim
--help`, and add the missing command or note why there isn't one. Update the table in
[notebooks/README.md](notebooks/README.md) when a command is added.

Known gaps (last reviewed 2026-09-24):

- chains: no `sim chain list` (the notebook shows them)
- persons and lifespans: no listing command
- `year_to_date.csv` rows: no `sim ytd list`
- calendar events other than one-time transfers: no listing command
- scenario comparison (account and lot definition differences, lot balance differences,
  net worth differences): no terminal equivalent
- net worth by month: `sim explain` prints year-end values only
- events (`notebooks/events.ipynb`): filtering the full `transfer_event` ledger by reason
  kind/tax character/account/date range has no terminal equivalent; `sim transfer list`
  only covers one-time transfers from the intent, not a run's actual ledger
