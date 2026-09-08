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
It also installs the `ripwire` CLI if it is missing. The CLI is used to save
token usage and improve quality by creating and using
deterministic repository maps and code-navigation checks; its optional MCP
server is registered separately per coding-agent client below.

## What the script does, step by step (for reference or manual setup)

1. Install [uv](https://github.com/astral-sh/uv)
2. Install and verify `ripwire`:
    ```sh
    RIPWIRE_REPO=redhat-et/ripwire bash -c "$(curl -fsSL https://raw.githubusercontent.com/redhat-et/ripwire/main/scripts/install.sh)"
    export PATH="$HOME/.local/bin:$PATH"
    ripwire --version
    ```
    The installer places the binary in `~/.local/bin`. Add that directory to
    your shell startup file if it is not already on `PATH`.
3. Configure the pre-commit hook:
    ```sh
    git config core.hooksPath .githooks
    ```
4. Run `uv sync --group dev` (installing the pinned Python from
   `.python-version` first with `uv python install` only if this fails)
5. Install the notebook-cleaning filter:
    ```sh
    uv run nbstripout --install
    git config filter.nbstripout.clean '"$(git rev-parse --show-toplevel)/.venv/bin/python" -m nbstripout --extra-keys "metadata.kernelspec.display_name metadata.language_info.version"'
    ```
    (`--install --extra-keys` in one call does *not* persist the extra keys in
    this nbstripout version — the second command is required.)

## Jupyter and filesystem MCP servers

Two project-level MCP servers are configured for coding agents (Copilot CLI,
and any other client that reads `.mcp.json` / `.github/mcp.json`):

- **filesystem** — [`@modelcontextprotocol/server-filesystem`](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem),
  scoped to the repository root. Defined in `.github/mcp.json` (committed —
  no secrets involved).
- **jupyter** — [`jupyter-mcp-server`](https://pypi.org/project/jupyter-mcp-server/),
  which talks to a running JupyterLab instance. Defined in `.mcp.json`
  (gitignored — holds a per-machine token).

Both files are loaded together; server names don't conflict, so no merging
surprises. Run JupyterLab through the CLI rather than starting it directly —
it generates `.mcp.json` with a fresh random token on first run (never
overwriting an existing one), then launches `jupyter lab` with that same
token, so the Jupyter MCP server always talks to the instance you started:

```sh
analysis jupyter
```

Any extra arguments are passed through to `jupyter lab` verbatim, e.g.
`analysis jupyter --no-browser`. If `.mcp.json` gets corrupted, delete it and
rerun `analysis jupyter` to regenerate it.

## Optional MCP registration

The `ripwire` CLI works without MCP. To register its optional stdio MCP server
with a coding agent, ask ripwire for that client's configuration recipe:

```sh
ripwire wrap cursor
ripwire wrap claude
ripwire wrap --all
```

For Cursor, the project-level configuration is:

```json
{
    "mcpServers": {
        "ripwire": { "command": "ripwire", "args": ["--mcp"] }
    }
}
```

Use an absolute path for `command` if the agent does not inherit your shell's
`PATH` (for example, `~/.local/bin/ripwire`). MCP registration is intentionally
not performed by `setup.sh` because it changes user- and client-specific
configuration.
