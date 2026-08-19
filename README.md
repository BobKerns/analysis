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

Three CLI entry points are installed:

| Script | Equivalent | Purpose |
| -------- | ------------ | --------- |
| `analysis` | — | Top-level; all subcommands |
| `amazon` | `analysis amazon` | Amazon expense categoriser |
| `sim` | `analysis sim` | Scenario simulator |

All examples below use the short scripts (`amazon ...`, `sim ...`). You can also use `analysis amazon ...` / `analysis sim ...`, or `uv run amazon ...` if the venv is not on your `PATH`.

Notebook helper API (same logic as CLI):

```python
from analysis.amazon.notebook import load_categorization_state, run_amazon_categorization

result, annotated, summary = run_amazon_categorization("data/amazon_2026_01.csv")
categories, mapping = load_categorization_state("state")
```

### Shell Autocomplete (Click)

This CLI uses Click shell completion. Completions are available for all three scripts.

One-time for current shell session:

```sh
eval "$(_ANALYSIS_COMPLETE=zsh_source analysis)"   # zsh
eval "$(_AMAZON_COMPLETE=zsh_source amazon)"        # zsh
eval "$(_SIM_COMPLETE=zsh_source sim)"              # zsh
```

If the scripts are not on your `PATH`, use `uv run`:

```sh
eval "$(_ANALYSIS_COMPLETE=zsh_source uv run analysis)"   # zsh
eval "$(_AMAZON_COMPLETE=zsh_source uv run amazon)"        # zsh
eval "$(_SIM_COMPLETE=zsh_source uv run sim)"              # zsh
```

Persist in your shell config:

```sh
# zsh
echo 'eval "$(_ANALYSIS_COMPLETE=zsh_source analysis)"' >> ~/.zshrc
echo 'eval "$(_AMAZON_COMPLETE=zsh_source amazon)"' >> ~/.zshrc
echo 'eval "$(_SIM_COMPLETE=zsh_source sim)"' >> ~/.zshrc

# bash — replace zsh_source with bash_source above
```

Reload your shell after adding it:

```sh
exec zsh   # or: exec bash
```

### Amazon Expense Categorizer

Run the interactive categorizer on one or more Amazon CSVs covering any date range:

```sh
amazon categorize data/amazon_2026_01.csv
```

Multiple files in one run:

```sh
amazon categorize data/Amazon-2026-03.csv data/Amazon-2026-04.csv
```

During interactive category prompts, press `Tab` to complete from known categories.

Non-interactive mode assigns unknown items to `UNCATEGORIZED`:

```sh
amazon categorize data/amazon_2026_01.csv --no-interactive
```

Review and re-categorize existing mapped items for a category:

```sh
amazon category review
```

Delete a category and remove mappings assigned to it:

```sh
amazon category delete
```

Both commands prompt you to select a category (with Tab completion).

Outputs:
- `out/Amazon-YYYY-MM_annotated.csv` in an `out/` subdirectory of the input directory by default
- `out/Amazon-YYYY-MM_summary.csv` in an `out/` subdirectory of the input directory by default
- Use `--output-dir` to choose another directory.

Why this exists:
- These monthly files are designed to align with Amex billing cycles, not strict calendar months.
- This makes reconciliation and statement matching easier.

Monthly files use a billing cutoff on day 3:
- Dates on the 1st-3rd are grouped into that month.
- Dates on the 4th+ are grouped into the next month.

Use `--cutoff-day` to override the boundary (1-28):

```sh
amazon categorize data/amazon_2026_01.csv --cutoff-day 3
```

If a generated billing month is still in progress for today, the monthly filenames include `_pending`:
- `out/Amazon-YYYY-MM_pending_annotated.csv`
- `out/Amazon-YYYY-MM_pending_summary.csv`

Pending rule details:
- For month `YYYY-MM`, the billing interval is from `(previous cutoff + 1 day)` through `(this month cutoff)`.
- A file is marked `_pending` only if today's date falls inside that interval.
- If there are no rows in the open interval, no `_pending` files are written.

Safe reruns:
- It is safe to run `analysis amazon categorize` multiple times on the same input data.
- If the category mapping is unchanged, outputs and stored item assignments remain unchanged.
- If the category mapping changes, existing rows are updated in place to reflect the new mapping.

Persistent memory:
- `state/state.sqlite3`

### Importing the Expense Baseline

The baseline importer combines canonicalized Quicken expenses with Amazon
monthly summaries and replaces only rows it previously generated:

```sh
sim scheduled import-baseline --scenario current --dry-run
sim scheduled import-baseline --scenario current
```

Generated rows are tagged `generated:expense_baseline` and carry provenance in
`meta_json`, so rerunning removes stale generated categories after the private
category maps change while preserving manually authored scheduled rows.

The command expects `quicken/expense_account_map.csv` with columns
`category,from_account,to_account`. Amounts are monthly averages over the
observed baseline months and use the scenario start date by default.

Schema guide:
- `docs/sqlite-guide.md`

## Data Safety
All files in `data/`, `out/`, and `state/` are ignored by git and protected by a pre-commit hook.

## Spell Checking
Spell checking is handled by cspell (see `cspell.json` and `.cspell/project-words.txt`). Only documentation, markdown, and comments are checked.

## Coding Standards

**Tabs are forbidden in all text files.**

Use spaces for all indentation and alignment. Hard tab characters (\t) are not allowed anywhere in the repository, including code, markdown, and documentation. This prevents formatting issues and ensures consistency across all editors and platforms.
