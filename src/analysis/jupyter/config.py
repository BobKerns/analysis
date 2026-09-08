"""Local ``.mcp.json`` (Jupyter MCP server) config: find, load, or generate.

``.mcp.json`` is gitignored and per-machine (see ``.gitignore`` and
``docs/SETUP.md``); the shared, committed MCP config lives in
``.github/mcp.json``. This module owns only the ``jupyter`` server entry, so
that ``analysis jupyter`` and the MCP server always agree on the same token
and port.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from git import InvalidGitRepositoryError, NoSuchPathError, Repo

MCP_CONFIG_FILENAME = ".mcp.json"
DEFAULT_JUPYTER_URL = "http://localhost:8888"
DEFAULT_JUPYTER_PORT = 8888


class McpConfigError(RuntimeError):
    """Raised when ``.mcp.json`` exists but has no usable ``jupyter`` server entry."""


@dataclass(frozen=True)
class JupyterMcpSettings:
    url: str
    token: str

    @property
    def port(self) -> int:
        return urlparse(self.url).port or DEFAULT_JUPYTER_PORT


def find_repo_root(start: Path | None = None) -> Path:
    """Find the enclosing git repository root, falling back to ``start``.

    ``start`` defaults to the current directory. Falling back (rather than
    raising) lets ``analysis jupyter`` still work outside a git checkout.
    """
    search_from = start or Path.cwd()
    try:
        repo = Repo(search_from, search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError):
        return search_from.resolve()
    return Path(repo.working_tree_dir)


def ensure_jupyter_mcp_config(repo_root: Path) -> tuple[JupyterMcpSettings, bool]:
    """Load the ``jupyter`` MCP server settings from ``.mcp.json``, creating it if missing.

    Returns the settings and whether the file was just created. An existing
    file is never rewritten, so a token generated once survives across runs
    (and across a running JupyterLab instance a coding agent is already
    connected to).
    """
    config_path = repo_root / MCP_CONFIG_FILENAME
    if not config_path.exists():
        settings = JupyterMcpSettings(url=DEFAULT_JUPYTER_URL, token=secrets.token_hex(32))
        config_path.write_text(json.dumps(_config_document(settings), indent=2) + "\n")
        return settings, True

    try:
        data = json.loads(config_path.read_text())
        env = data["mcpServers"]["jupyter"]["env"]
        settings = JupyterMcpSettings(url=env["JUPYTER_URL"], token=env["JUPYTER_TOKEN"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise McpConfigError(
            f"{config_path} exists but has no usable 'jupyter' MCP server entry "
            f"(expected mcpServers.jupyter.env.{{JUPYTER_URL,JUPYTER_TOKEN}}). "
            "Fix or remove it, then rerun."
        ) from exc
    return settings, False


def _config_document(settings: JupyterMcpSettings) -> dict:
    return {
        "mcpServers": {
            "jupyter": {
                "type": "local",
                "command": "uvx",
                "args": ["jupyter-mcp-server"],
                "env": {
                    "JUPYTER_URL": settings.url,
                    "JUPYTER_TOKEN": settings.token,
                },
                "tools": ["*"],
            }
        }
    }
