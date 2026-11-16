"""The working set (#322): the scenarios most recently used, newest first,
so the background builder (sim/builder.py) builds what is likely wanted
next before the rest.

``state/builder/working_set``, beside the builder's other files and so
untracked, holds one scenario name per line. Using a scenario (naming it on
a CLI command, or picking it in a notebook) moves it to the top. Every
write drops names that are no longer scenarios or are disabled, and keeps
at most :data:`LIMIT` names; the builder only reads the file, ignoring any
such name it still holds.

Writing is best effort: a data repo that can't be written stops no command,
and two processes racing can at worst lose one move to the top.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

from analysis.sim.builder import BuilderPaths
from analysis.sim.loader import LoadError, list_scenario_names, read_disabled_scenarios
from analysis.sim.locks import BUILDER_CHILD_ENV

__all__ = ["LIMIT", "read_working_set", "remove", "use", "working_set_path"]

# Far enough down, a name says nothing about what is wanted next.
LIMIT = 50


def working_set_path(data_dir: Path | str) -> Path:
    return BuilderPaths.for_data(Path(data_dir)).working_set


def read_working_set(data_dir: Path | str) -> list[str]:
    """The names in the working set file, newest first, as written: a name may
    since have been deleted or disabled."""
    try:
        text = working_set_path(data_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []  # best effort: a damaged file is an empty working set
    seen: dict[str, None] = {}
    for line in text.splitlines():
        if name := line.strip():
            seen.setdefault(name, None)
    return list(seen)


def use(data_dir: Path | str, *names: str | None) -> None:
    """Move ``names`` to the top, the first named topmost. A background
    build (``BUILDER_CHILD_ENV``) never counts as use."""
    if os.environ.get(BUILDER_CHILD_ENV):
        return
    used = [n for n in names if n]
    if used:
        _update(data_dir, lambda names: [*used, *names])


def remove(data_dir: Path | str, name: str) -> None:
    _update(data_dir, lambda names: [n for n in names if n != name])


def _update(data_dir: Path | str, change: Callable[[list[str]], list[str]]) -> None:
    data = Path(data_dir)
    try:
        known = set(list_scenario_names(data)) - read_disabled_scenarios(data)
    except LoadError:
        return  # no scenarios/: nothing to rank
    kept: dict[str, None] = {}
    for name in change(read_working_set(data)):
        if name in known:
            kept.setdefault(name, None)
    _write(data, list(kept)[:LIMIT])


def _write(data_dir: Path, names: Iterable[str]) -> None:
    """Replace the file whole, so a reader never sees half of it."""
    path = working_set_path(data_dir)
    if path.is_symlink():
        return  # never write through a link out of state/
    text = "".join(f"{name}\n" for name in names)
    try:
        if path.is_file() and path.read_bytes() == text.encode("utf-8"):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        pass  # best effort: see the module docstring
