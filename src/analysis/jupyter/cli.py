"""``analysis jupyter`` — launch JupyterLab using the local Jupyter MCP config's token."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import click

from analysis.jupyter.config import McpConfigError, ensure_jupyter_mcp_config, find_repo_root


def _exec_jupyter_lab(jupyter_bin: str, argv: list[str]) -> None:
    """Replace this process with ``jupyter lab`` so signals and the TTY behave normally."""
    os.execv(jupyter_bin, argv)  # noqa: S606 (intentional exec, not user-controlled input)


@click.command(name="jupyter", context_settings={"ignore_unknown_options": True})
@click.argument("jupyter_args", nargs=-1, type=click.UNPROCESSED)
@click.option("--notebook-dir", type=Path)
def jupyter_command(jupyter_args: tuple[str, ...], notebook_dir: Path | None = None) -> None:
    """Launch JupyterLab, generating .mcp.json (Jupyter MCP server config) if needed.

    Reuses the token already in .mcp.json across runs, so the Jupyter MCP
    server used by coding agents keeps talking to the same running
    JupyterLab instance. Any JUPYTER_ARGS are passed through to `jupyter lab`
    verbatim, after the generated --port and --IdentityProvider.token.
    """
    repo_root = find_repo_root()
    if notebook_dir is None:
        notebook_dir = repo_root / "notebooks"

    try:
        settings, created = ensure_jupyter_mcp_config(repo_root)
    except McpConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    config_path = repo_root / ".mcp.json"
    if created:
        click.echo(f"Generated {config_path} with a new Jupyter token.")

    jupyter_bin = shutil.which("jupyter")
    if jupyter_bin is None:
        raise click.ClickException(
            "jupyter executable not found on PATH; is jupyterlab installed in this environment?"
        )

    argv = [
        jupyter_bin,
        "lab",
        f"--port={settings.port}",
        f"--IdentityProvider.token={settings.token}",
        f"--notebook-dir={notebook_dir}",
        *jupyter_args,
    ]
    click.echo(f"Launching JupyterLab on port {settings.port} (token from {config_path})...")
    _exec_jupyter_lab(jupyter_bin, argv)
