# Notebooks

Exploratory notebooks live here.

Notes:
- Keep reusable logic in the `src/analysis/` package, not in notebooks.
- Read inputs from `../data/` and write generated artifacts to `../out/`.
- Treat `../state/` as persistent runtime state used by CLI and notebooks.

## A table is clipped, or a notebook is slow

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
| the accounts holding a symbol | `analysis sim account holders <symbol>` |
| scheduled transactions | `analysis sim scheduled list` |
| one-time transfers | `analysis sim transfer list` |
| a run's year-end net worth, and lots | `analysis sim explain <runfile>` |

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
