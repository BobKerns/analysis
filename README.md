# Financial Analysis and Planning

This project provides tools for personal financial analysis and planning, supporting both CLI and Jupyter notebook workflows. It uses Python 3.14+, uv for package management, and modern Python best practices.

## Directory Structure
- `src/analysis/` — Core package code
- `tests/` — Unit tests
- `notebooks/` — Jupyter notebooks for exploratory analysis
- `data/` — User input data (ignored in git)
- `out/` — Output data (ignored in git)
- `state/` — Persistent state (ignored in git)

This repository is a single top-level Python project. There are no nested repos or subprojects.

## Setup
1. Install [uv](https://github.com/astral-sh/uv)
2. Ensure Python 3.14+ is available
3. Configure the pre-commit hook (one-time):
    ```sh
    git config core.hooksPath .githooks
    ```
4. Run `uv sync --group dev`

## Usage
- CLI tools: `python -m analysis ...`
- Notebooks: Open and run notebooks in `notebooks/`

Notebook helper API (same logic as CLI):

```python
from analysis.amazon.notebook import load_categorization_state, run_amazon_categorization

result, annotated, summary = run_amazon_categorization("data/amazon_2026_01.csv")
categories, mapping = load_categorization_state("state")
```

### Shell Autocomplete (Click)

This CLI uses Click shell completion.

One-time for current shell session:

```sh
eval "$(_ANALYSIS_COMPLETE=zsh_source analysis)"   # zsh
eval "$(_ANALYSIS_COMPLETE=bash_source analysis)"  # bash
```

If `analysis` is not on your `PATH`, use `uv run`:

```sh
eval "$(_ANALYSIS_COMPLETE=zsh_source uv run analysis)"   # zsh
eval "$(_ANALYSIS_COMPLETE=bash_source uv run analysis)"  # bash
```

Persist in your shell config:

```sh
# zsh
echo 'eval "$(_ANALYSIS_COMPLETE=zsh_source analysis)"' >> ~/.zshrc

# bash
echo 'eval "$(_ANALYSIS_COMPLETE=bash_source analysis)"' >> ~/.bashrc
```

Reload your shell after adding it:

```sh
exec zsh   # or: exec bash
```

### Amazon Expense Categorizer

Run the interactive categorizer on an Amazon CSV covering any date range:

```sh
uv run python -m analysis amazon categorize data/amazon_2026_01.csv
```

During interactive category prompts, press `Tab` to complete from known categories.

Non-interactive mode assigns unknown items to `UNCATEGORIZED`:

```sh
uv run python -m analysis amazon categorize data/amazon_2026_01.csv --no-interactive
```

Review and re-categorize existing mapped items for a category:

```sh
uv run python -m analysis amazon category review
```

Delete a category and remove mappings assigned to it:

```sh
uv run python -m analysis amazon category delete
```

Both commands prompt you to select a category (with Tab completion).

Outputs:
- `out/Amazon-YYYY-MM_annotated.csv`
- `out/Amazon-YYYY-MM_summary.csv`

Why this exists:
- These monthly files are designed to align with Amex billing cycles, not strict calendar months.
- This makes reconciliation and statement matching easier.

Monthly files use a billing cutoff on day 3:
- Dates on the 1st-3rd are grouped into that month.
- Dates on the 4th+ are grouped into the next month.

Use `--cutoff-day` to override the boundary (1-28):

```sh
uv run python -m analysis amazon categorize data/amazon_2026_01.csv --cutoff-day 3
```

If a generated billing month is still in progress for today, the monthly filenames include `_pending`:
- `out/Amazon-YYYY-MM_pending_annotated.csv`
- `out/Amazon-YYYY-MM_pending_summary.csv`

Pending rule details:
- For month `YYYY-MM`, the billing interval is from `(previous cutoff + 1 day)` through `(this month cutoff)`.
- A file is marked `_pending` only if today's date falls inside that interval.
- If there are no rows in the open interval, no `_pending` files are written.

Persistent memory:
- `state/state.sqlite3`

Schema guide:
- `docs/sqlite-guide.md`

## Data Safety
All files in `data/`, `out/`, and `state/` are ignored by git and protected by a pre-commit hook.

## Spell Checking
Spell checking is handled by cspell (see `cspell.json` and `.cspell/project-words.txt`). Only documentation, markdown, and comments are checked.

## Coding Standards

**Tabs are forbidden in all text files.**

Use spaces for all indentation and alignment. Hard tab characters (\t) are not allowed anywhere in the repository, including code, markdown, and documentation. This prevents formatting issues and ensures consistency across all editors and platforms.
