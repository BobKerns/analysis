"""The blocked-keyword check (issue #49): names from the private data repo
must not reach this repo's files or commit messages.

Samples are built from the keyword list at run time, so this file never
contains a keyword itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = Path("scripts") / "check-blocked-keywords.sh"
KEYWORD_LIST = Path(".githooks") / "blocked-keywords.txt"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(),
    reason="needs a git checkout",
)


def _keywords() -> list[str]:
    lines = (REPO / KEYWORD_LIST).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _check(repo: Path, *args: str, allow: bool = False) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if allow:
        env["ALLOW_BLOCKED_KEYWORDS"] = "1"
    return subprocess.run(
        [str(repo / SCRIPT), *args], cwd=repo, env=env, capture_output=True, text=True,
    )


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    """An empty git repo carrying only the check script and the keyword list."""
    repo = tmp_path / "repo"
    for relative in (SCRIPT, KEYWORD_LIST):
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, repo / relative)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True)


@pytest.mark.slow
def test_no_tracked_file_contains_a_blocked_keyword() -> None:
    result = _check(REPO, "--all")

    assert result.returncode == 0, result.stderr


@pytest.mark.slow
def test_all_ignores_notebook_outputs_but_not_source(scratch_repo: Path) -> None:
    # A run notebook holds outputs that nbstripout keeps out of commits, so
    # --all reads notebooks through the same textconv `nbstripout --install`
    # sets up. Configured here so the test doesn't depend on local config.
    subprocess.run(
        ["git", "config", "diff.ipynb.textconv", f"{sys.executable} -m nbstripout -t"],
        cwd=scratch_repo, check=True,
    )
    keyword = _keywords()[0]

    def notebook(source: str, output: str) -> str:
        cell = {
            "cell_type": "code", "execution_count": 1, "id": "c1", "metadata": {},
            "outputs": [{"name": "stdout", "output_type": "stream", "text": [output]}],
            "source": [source],
        }
        return json.dumps({"cells": [cell], "metadata": {}, "nbformat": 4, "nbformat_minor": 5})

    _stage(scratch_repo, ".gitattributes", "*.ipynb diff=ipynb\n")
    _stage(scratch_repo, "run.ipynb", notebook("print(name)", f"{keyword}\n"))
    assert _check(scratch_repo, "--all").returncode == 0

    _stage(scratch_repo, "run.ipynb", notebook(f"print('{keyword}')", "plain\n"))
    result = _check(scratch_repo, "--all")

    assert result.returncode == 1
    assert "run.ipynb:" in result.stderr


def test_all_fails_when_a_textconv_cannot_run(scratch_repo: Path) -> None:
    subprocess.run(
        ["git", "config", "diff.ipynb.textconv", str(scratch_repo / "missing")],
        cwd=scratch_repo, check=True,
    )
    _stage(scratch_repo, ".gitattributes", "*.ipynb diff=ipynb\n")
    _stage(scratch_repo, "run.ipynb", "{}\n")

    result = _check(scratch_repo, "--all")

    assert result.returncode == 2
    assert "not all checked" in result.stderr


def test_a_staged_file_naming_a_real_account_is_blocked(scratch_repo: Path) -> None:
    first, second = _keywords()[:2]
    _stage(scratch_repo, "accounts.csv", f"name\n{first}_{second.lower()}_Checking\n")

    result = _check(scratch_repo, "--staged")

    assert result.returncode == 1
    assert "accounts.csv:2:" in result.stderr


def test_keywords_inside_longer_words_are_not_matched(scratch_repo: Path) -> None:
    first, second = _keywords()[:2]
    _stage(scratch_repo, "notes.txt", f"x{first}\n{second}9\nplain text\n")

    assert _check(scratch_repo, "--staged").returncode == 0


def test_a_keyword_with_a_space_matches_underscores_and_hyphens(scratch_repo: Path) -> None:
    spaced = next(keyword for keyword in _keywords() if " " in keyword)
    _stage(scratch_repo, "a.txt", spaced.replace(" ", "_") + "\n")
    _stage(scratch_repo, "b.txt", spaced.replace(" ", "-").upper() + "\n")

    result = _check(scratch_repo, "--staged")

    assert result.returncode == 1
    assert "a.txt:1:" in result.stderr and "b.txt:1:" in result.stderr


def test_the_keyword_list_itself_and_an_empty_commit_pass(scratch_repo: Path) -> None:
    assert _check(scratch_repo, "--staged").returncode == 0

    subprocess.run(["git", "add", str(KEYWORD_LIST)], cwd=scratch_repo, check=True)

    assert _check(scratch_repo, "--staged").returncode == 0


def test_a_commit_message_is_checked_but_its_comment_lines_are_not(scratch_repo: Path) -> None:
    keyword = _keywords()[0]
    message = scratch_repo / "message.txt"

    message.write_text(f"Fix import\n\n# {keyword} appears in a comment line\n", encoding="utf-8")
    assert _check(scratch_repo, "--message", str(message)).returncode == 0

    message.write_text(f"Fix the {keyword} import\n", encoding="utf-8")
    assert _check(scratch_repo, "--message", str(message)).returncode == 1


def test_pull_request_text_is_checked_including_markdown_headings(scratch_repo: Path) -> None:
    keyword = _keywords()[0]
    text = scratch_repo / "pr.txt"

    text.write_text("Fix import\n\n## Summary\nplain text\n", encoding="utf-8")
    assert _check(scratch_repo, "--text", str(text)).returncode == 0

    text.write_text(f"Fix import\n\n## The {keyword} import\n", encoding="utf-8")
    result = _check(scratch_repo, "--text", str(text))

    assert result.returncode == 1
    assert "3:" in result.stderr


def test_the_override_lets_a_deliberate_use_through(scratch_repo: Path) -> None:
    _stage(scratch_repo, "notes.txt", _keywords()[0] + "\n")

    result = _check(scratch_repo, "--staged", allow=True)

    assert result.returncode == 0
    assert "notes.txt:1:" in result.stderr
