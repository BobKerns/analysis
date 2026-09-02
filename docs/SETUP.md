# Setup

Run the setup script (idempotent — safe to re-run):

```sh
./scripts/setup.sh
```

It installs [uv](https://github.com/astral-sh/uv) if missing, configures the
`.githooks` pre-commit hook, runs `uv sync --group dev` (installing the Python
version pinned in `.python-version` only if `uv sync` can't find a usable
interpreter), and installs the notebook-cleaning `nbstripout` filter (strips
outputs and per-run/per-machine metadata churn like `kernelspec.display_name`
— see [notebooks/README.md](../notebooks/README.md)).

## What the script does, step by step (for reference or manual setup)

1. Install [uv](https://github.com/astral-sh/uv)
2. Configure the pre-commit hook:
    ```sh
    git config core.hooksPath .githooks
    ```
3. Run `uv sync --group dev` (installing the pinned Python from
   `.python-version` first with `uv python install` only if this fails)
4. Install the notebook-cleaning filter:
    ```sh
    uv run nbstripout --install
    git config filter.nbstripout.clean '"$(git rev-parse --show-toplevel)/.venv/bin/python" -m nbstripout --extra-keys "metadata.kernelspec.display_name metadata.language_info.version"'
    ```
    (`--install --extra-keys` in one call does *not* persist the extra keys in
    this nbstripout version — the second command is required.)
