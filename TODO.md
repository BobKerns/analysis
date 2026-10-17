# TODO

To read a formatted `.md` file:
- In Jupyter: right-click on it and select `Open With / Markdown Preview`
- In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

## Review for missing CLI functionality, periodically

Every notebook view or widget should have a terminal equivalent where one makes sense:
it needs no kernel or browser, works over SSH and in scripts, and is what to suggest on a
machine too small for a notebook. Every so often (say after each batch of notebook work),
walk the notebooks and `analysis.sim.notebook_ui`, check each view against `analysis sim
--help`, and add the missing command or note why there isn't one. Update the table in
[notebooks/README.md](notebooks/README.md) when a command is added.

Known gaps (last reviewed 2026-10-17; each has an issue):

- account and lot balance history (`notebooks/accounts.ipynb`): no terminal equivalent (#187)
- income, expenses by category, realized and unrealized gains, tax by year
  (`notebooks/net_worth.ipynb`): no terminal equivalent (#187)
- plain-run scenario comparison (account and lot definition differences, rule and event
  differences, net worth differences, break-even): only `sim ensemble compare`, which is
  ensembles' risk only (#188)
- events (`notebooks/events.ipynb`): filtering the full `transfer_event` ledger by reason
  kind/tax character/account/date range; `sim items` covers one account and a date range
  only (#189)
