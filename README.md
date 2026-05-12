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
3. Run `uv sync --group dev`

## Usage
- CLI tools: `python -m analysis ...`
- Notebooks: Open and run notebooks in `notebooks/`

## Data Safety
All files in `data/`, `out/`, and `state/` are ignored by git and protected by a pre-commit hook.

## Spell Checking
Spell checking is handled by cspell (see `.github/cspell.json` and `.cspell/project-words.txt`). Only documentation, markdown, and comments are checked.
