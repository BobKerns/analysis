import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from analysis.cli import main
from analysis.jupyter.config import (
    McpConfigError,
    ensure_jupyter_mcp_config,
    find_repo_root,
)


def test_ensure_jupyter_mcp_config_creates_file_with_generated_token(tmp_path: Path) -> None:
    settings, created = ensure_jupyter_mcp_config(tmp_path)

    assert created is True
    assert settings.url == "http://localhost:8888"
    assert settings.port == 8888
    assert len(settings.token) == 64  # secrets.token_hex(32)

    config_path = tmp_path / ".mcp.json"
    data = json.loads(config_path.read_text())
    assert data["mcpServers"]["jupyter"]["env"]["JUPYTER_TOKEN"] == settings.token


def test_ensure_jupyter_mcp_config_reuses_existing_token(tmp_path: Path) -> None:
    first, created_first = ensure_jupyter_mcp_config(tmp_path)
    second, created_second = ensure_jupyter_mcp_config(tmp_path)

    assert created_first is True
    assert created_second is False
    assert first == second


def test_ensure_jupyter_mcp_config_rejects_malformed_file(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}')

    try:
        ensure_jupyter_mcp_config(tmp_path)
        raise AssertionError("expected McpConfigError")
    except McpConfigError as exc:
        assert "jupyter" in str(exc)


def test_find_repo_root_falls_back_outside_git(tmp_path: Path) -> None:
    outside_git = tmp_path / "no-git-here"
    outside_git.mkdir()

    from git import InvalidGitRepositoryError

    with patch("analysis.jupyter.config.Repo", side_effect=InvalidGitRepositoryError):
        root = find_repo_root(outside_git)

    assert root == outside_git.resolve()


def test_cli_jupyter_generates_config_and_execs_jupyter_lab(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_exec(jupyter_bin: str, argv: list[str]) -> None:
        captured["jupyter_bin"] = jupyter_bin
        captured["argv"] = argv

    runner = CliRunner()
    with (
        patch("analysis.jupyter.cli.find_repo_root", return_value=tmp_path),
        patch("analysis.jupyter.cli.shutil.which", return_value="/usr/bin/jupyter"),
        patch("analysis.jupyter.cli._exec_jupyter_lab", side_effect=fake_exec),
    ):
        result = runner.invoke(main, ["jupyter", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert "Generated" in result.output
    assert captured["jupyter_bin"] == "/usr/bin/jupyter"
    argv = captured["argv"]
    assert argv[0] == "/usr/bin/jupyter"
    assert argv[1] == "lab"
    assert argv[2] == "--port=8888"
    assert argv[3].startswith("--IdentityProvider.token=")
    assert argv[4] == f"--notebook-dir={tmp_path / 'notebooks'}"
    assert argv[5:] == ["--no-browser"]

    config_path = tmp_path / ".mcp.json"
    assert config_path.exists()
    token = json.loads(config_path.read_text())["mcpServers"]["jupyter"]["env"]["JUPYTER_TOKEN"]
    assert argv[3] == f"--IdentityProvider.token={token}"


def test_cli_jupyter_reports_missing_jupyter_binary(tmp_path: Path) -> None:
    runner = CliRunner()
    with (
        patch("analysis.jupyter.cli.find_repo_root", return_value=tmp_path),
        patch("analysis.jupyter.cli.shutil.which", return_value=None),
    ):
        result = runner.invoke(main, ["jupyter"])

    assert result.exit_code != 0
    assert "jupyter executable not found" in result.output
