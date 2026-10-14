# Notebooks

> To read a formatted `.md` file:
> - In Jupyter: right-click on it and select `Open With / Markdown Preview`
> - In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

Exploratory notebooks live here.

Notes:
- Keep reusable logic in the `src/analysis/` package, not in notebooks.
- Read inputs from `../data/` and write generated artifacts to `../out/`.
- Treat `../state/` as persistent runtime state used by CLI and notebooks.

## A table is clipped or a notebook is slow

VS Code's notebook renderer clips a table that is wider than the window instead of
scrolling it, and VS Code plus a Python kernel needs a fair amount of memory. If a
notebook is not satisfactory there, run `analysis jupyter` and open it in JupyterLab
instead: wide tables scroll sideways, and it can use less memory. (It may still be too
much for a small machine with many browser tabs open.)

Several views also have terminal equivalents that need no notebook or kernel:

| notebook view | terminal |
| --- | --- |
| accounts and opening balances | `analysis sim account list` |
| the lots in an account | `analysis sim account holdings <account>` |
| the portfolio by asset class | `analysis sim allocation` |
| the accounts holding a symbol | `analysis sim account holders <symbol>` |
| chains, as a table and one chain at a time as a flowchart | `analysis sim chain list`, `analysis sim chain show CHAIN [--graph \| --mermaid]` |
| scheduled transactions | `analysis sim scheduled list` |
| one-time transfers | `analysis sim transfer list` |
| a run's year-end net worth, and lots | `analysis sim explain <runfile>` |
| every event, filtered by reason/tax/account/date | none yet ([TODO.md](../TODO.md)) |
| an ensemble's mean/median/SD/range by year | `analysis sim ensemble summary <recipe>` |
| which varying rows matter, and when (`sensitivity.ipynb`) | `analysis sim ensemble sensitivity <recipe>` |

Views with no terminal equivalent yet are listed in [TODO.md](../TODO.md), which also asks for a periodic review of what is missing.

## Git status shows a notebook as modified after running it

Notebook outputs are private (they can contain real financial figures), so
this repo strips them from what actually gets committed via an `nbstripout`
git filter. That filter also strips `kernelspec.display_name` and
`language_info.version`, which VS Code's Python/Jupyter extension otherwise
rewrites inconsistently between runs (e.g. toggling `.venv (3.14.1)` vs
`.venv (3.14.1.final.0)`) even though the interpreter hasn't changed. See
[README.md](../README.md#setup) for the one-time filter setup. The filter
only runs when content is staged or diffed, not for `git status`'s quick
check, so a notebook you just ran will show as
"modified" even if nothing meaningful changed. Run `git diff` or `git add`
to see the real (usually empty) diff. See
[nbstripout](https://github.com/kynan/nbstripout) for details.

## Writing a widget for a notebook

Show a widget with `CONNECTIONS.show(fn, ...)` or `CONNECTIONS.show_scenario(fn, ...)`
(`analysis.sim.notebook_ui.pickers`); they own the draw and redraw logic, so a widget
function should not.

- **Return the widget** (or return `None` if the function displays things itself). Do not
  also `display()` the box you return: `show` displays the result, so doing both puts the
  widget on the page twice.
- **Don't worry about the first draw.** `show` runs the function after the cell that called
  it has finished (IPython's `post_execute` hook), like any later redraw. Drawing while the
  cell is still running, before the widget's view exists in the browser, could come out
  clipped or blank under Jupyter. Nothing else needs to be deferred.
- Plots go in an `Output` widget as before: `plt.show()`, or `display(fig)` followed by
  `plt.close(fig)`. Redraws triggered by a control are drawn immediately.
- If a plot sits alongside other output in the same cell (a table above it, say), give it
  its own `Output` widget rather than sharing one; under Jupyter a plot has needed both
  `plt.show()` and `display(fig, clear=True)`, in that order, to draw reliably every time
  (`portfolio_allocation_widget`'s pie is the example) — sharing the `Output` with other
  content caused the two calls to reserve then misplace space.
- Fill a picker's options before you observe it, so setting them does not trigger a draw.
