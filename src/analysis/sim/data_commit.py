"""Commit a scenario's uncommitted data for someone who doesn't use git
(#308, ADR-004 §3).

A run is reproducible only from committed data, and the background builder
builds only committed trees. So ``sim run``/``load``/``all`` offer to commit
the changes that make their scenarios dirty, and ``sim commit`` does it on
its own. Only those changes are committed (the scenarios' scoped paths,
``provenance.scoped_changes``); anything else in the data repo, staged or
not, is left as it is. The message is written from the paths, so the data
repo's history reads as a list of edits.

Never the code repository: a commit there would diverge from GitHub's
``main`` and stop the next update from fast-forwarding.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from analysis.sim.choose_user import primary_persons

# The subject line stays readable in `git log --oneline` up to about this.
_SUBJECT_WIDTH = 72
_EMAIL_DOMAIN = "analysis.local"


class CommitError(Exception):
    """The data can't be committed now; the message says why."""


def _git(
    root: Path, *args: str, check: bool = False, stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    # Literal pathspecs: a file name with * or : in it is just a name.
    done = subprocess.run(
        ["git", "--literal-pathspecs", "-C", str(root), *args],
        input=stdin, capture_output=True, text=True, check=False,
    )
    if check and done.returncode:
        raise CommitError((done.stderr or done.stdout).strip() or f"git {args[0]} failed")
    return done


def repo_top(data_dir: Path) -> Path:
    done = _git(data_dir, "rev-parse", "--show-toplevel")
    if done.returncode:
        raise CommitError(f"{data_dir} isn't in a git repository")
    return Path(done.stdout.strip())


def _git_path(root: Path, name: str) -> Path:
    path = Path(_git(root, "rev-parse", "--git-path", name, check=True).stdout.strip())
    return path if path.is_absolute() else root / path


def _refuse_unless_committable(root: Path) -> None:
    """Refuse mid-merge, mid-rebase and on a detached HEAD: a commit there
    belongs to an operation the person is in the middle of, or to no
    branch at all."""
    for name, what in (("MERGE_HEAD", "a merge"), ("rebase-merge", "a rebase"),
                       ("rebase-apply", "a rebase"), ("CHERRY_PICK_HEAD", "a cherry-pick"),
                       ("REVERT_HEAD", "a revert")):
        if _git_path(root, name).exists():
            raise CommitError(f"the data repo is in the middle of {what}; finish it first")
    if _git(root, "symbolic-ref", "-q", "HEAD").returncode:
        raise CommitError("the data repo isn't on a branch (detached HEAD)")


def operator(data_dir: Path) -> tuple[str, str]:
    """``(id, display)`` of the person using this data repo: $ANALYSIS_USER,
    else ``.analysis_user``, else $USER; display from persons.csv."""
    person = os.environ.get("ANALYSIS_USER", "").strip()
    if not person:
        try:
            person = (data_dir / ".analysis_user").read_text(encoding="utf-8").strip()
        except OSError:
            person = ""
    person = person or os.environ.get("USER") or "analysis"
    try:
        display = dict(primary_persons(data_dir)).get(person, "")
    except (OSError, KeyError):
        display = ""
    return person, display or person


def ensure_identity(root: Path, person_id: str, display: str) -> bool:
    """Give the repository at ``root`` a committer when git has none, from
    the person: ``display`` and ``<id>@analysis.local``. Only in that
    repository's own config, never the global one. True if it set one."""
    have_name = not _git(root, "config", "user.name").returncode
    have_email = not _git(root, "config", "user.email").returncode
    if have_name and have_email:
        return False
    if not have_name:
        _git(root, "config", "user.name", display or person_id, check=True)
    if not have_email:
        _git(root, "config", "user.email", f"{person_id}@{_EMAIL_DOMAIN}", check=True)
    return True


def describe(data_dir: Path, root: Path, path: str) -> tuple[str | None, str]:
    """``(scenario, name)`` for a changed ``path`` (relative to ``root``):
    the scenario whose folder holds it and the path within that folder, or
    ``None`` and the path relative to the data dir."""
    try:
        rel = (root / path).resolve().relative_to(data_dir.resolve())
    except ValueError:
        return None, path
    parts = rel.parts
    if len(parts) > 2 and parts[0] == "scenarios":
        return parts[1], Path(*parts[2:]).as_posix()
    return None, rel.as_posix()


def message(data_dir: Path, root: Path, changes: Sequence[tuple[str, str]], command: str) -> str:
    """A commit message for ``changes``: a subject naming what changed,
    ``Edit base: accounts.csv, calendar.csv`` for one scenario, else
    ``Edit base, retire_early, persons.csv``, then why it was committed and
    every file."""
    scenarios: dict[str, list[str]] = {}
    shared: list[str] = []
    lines = []
    for status, path in changes:
        scenario, name = describe(data_dir, root, path)
        if scenario is None:
            shared.append(name)
            shown = name
        else:
            scenarios.setdefault(scenario, []).append(name)
            shown = f"scenarios/{scenario}/{name}"
        note = " (new)" if status == "??" or "A" in status else (
            " (deleted)" if "D" in status else "")
        lines.append(f"- {shown}{note}")
    if len(scenarios) == 1 and not shared:
        (scenario, names), = scenarios.items()
        subject = _subject(f"Edit {scenario}: ", sorted(dict.fromkeys(names)))
    else:
        subject = _subject("Edit ", [*sorted(scenarios), *sorted(dict.fromkeys(shared))])
    why = f"Committed by `{command}` so its runs are reproducible." if command else ""
    return "\n".join([subject, "", *([why, ""] if why else []), *lines]) + "\n"


def _subject(lead: str, items: Sequence[str]) -> str:
    """``lead`` and ``items``, as many as fit the width, then "and N more"."""
    for shown in range(len(items), 0, -1):
        rest = len(items) - shown
        text = lead + ", ".join(items[:shown]) + (f" and {rest} more" if rest else "")
        if len(text) <= _SUBJECT_WIDTH or shown == 1:
            return text
    return lead.rstrip(": ")


def commit(
    data_dir: Path, changes: Sequence[tuple[str, str]], *, command: str = "",
    subject: str | None = None,
) -> tuple[str, str]:
    """Commit ``changes`` (``provenance.scoped_changes``'s pairs) in the data
    repo and nothing else. ``subject`` replaces the generated first line.
    Returns ``(commit hash, subject)``."""
    if not changes:
        raise CommitError("nothing to commit")
    root = repo_top(data_dir)
    _refuse_unless_committable(root)
    ensure_identity(root, *operator(data_dir))
    text = message(data_dir, root, changes, command)
    if subject:
        text = subject.strip() + "\n" + text.partition("\n")[2]
    paths = sorted({path for _status, path in changes})
    # -A records a deletion too; a path already gone from the index as well
    # (a renamed file's old name) has nothing to add, and `add` would fail
    # on it. --only commits just these paths, whatever else is staged.
    indexed = set(_git(root, "ls-files", "-z", "--", *paths, check=True).stdout.split("\0"))
    if to_add := [p for p in paths if p in indexed or (root / p).exists()]:
        _git(root, "add", "-A", "--", *to_add, check=True)
    _git(root, "commit", "-q", "--only", "-F", "-", "--", *paths, check=True, stdin=text)
    return _head(root), text.partition("\n")[0]


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD", check=True).stdout.strip()
