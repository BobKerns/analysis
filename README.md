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

Run the interactive categorizer on a monthly Amazon CSV:

```sh
uv run python -m analysis amazon-categorize data/amazon_2026_01.csv
```

Non-interactive mode assigns unknown items to `UNCATEGORIZED`:

```sh
uv run python -m analysis amazon-categorize data/amazon_2026_01.csv --no-interactive
```

Outputs:
- `out/<input_stem>_annotated.csv`
- `out/<input_stem>_summary.csv`

Persistent memory:
- `state/categories.json`
- `state/item_category_map.json`
- `state/events.jsonl`

## Data Safety
All files in `data/`, `out/`, and `state/` are ignored by git and protected by a pre-commit hook.

## Spell Checking
Spell checking is handled by cspell (see `.github/cspell.json` and `.cspell/project-words.txt`). Only documentation, markdown, and comments are checked.

## Coding Standards

**Tabs are forbidden in all text files.**

Use spaces for all indentation and alignment. Hard tab characters (\t) are not allowed anywhere in the repository, including code, markdown, and documentation. This prevents formatting issues and ensures consistency across all editors and platforms.
