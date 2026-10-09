"""Pick the person a data repo is used as (ADR-004 §4).

``scripts/link-data.sh`` and ``activate-analysis`` both run
``python -m analysis.sim.choose_user <sim-dir>``: it lists the ``primary``
persons in ``persons.csv``, reads an id or display name, and prints the chosen
id on stdout. The list and prompt go to stderr, so the shell can capture the id
with ``$(...)``. Kept in Python so one ``csv`` reader handles quoted display
names in both callers.
"""

from __future__ import annotations

import csv
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO


def primary_persons(sim_dir: Path) -> list[tuple[str, str]]:
    """``(id, display)`` for each ``role=primary`` row of ``persons.csv``, in file order."""
    with (sim_dir / "persons.csv").open(newline="", encoding="utf-8") as f:
        return [
            (row["id"], row.get("display") or "")
            for row in csv.DictReader(f)
            if row.get("id") and (row.get("role") or "").strip() == "primary"
        ]


def match_person(answer: str, persons: Sequence[tuple[str, str]]) -> str | None:
    """The id ``answer`` names, by id or display name; ``None`` if it names none.

    An exact id wins over a display name, then display names are compared
    ignoring case, since they are typed from the list rather than copied.
    """
    answer = answer.strip()
    for pid, _ in persons:
        if answer == pid:
            return pid
    folded = answer.casefold()
    for pid, display in persons:
        if display and folded == display.casefold():
            return pid
    return None


def choose(sim_dir: Path, stdin: TextIO, stderr: TextIO) -> str | None:
    """Prompt until an answer names a primary person; ``None`` on EOF or none to pick."""
    persons = primary_persons(sim_dir)
    if not persons:
        print(f"No person with role 'primary' in {sim_dir / 'persons.csv'}.", file=stderr)
        return None
    width = max(len("id"), *(len(pid) for pid, _ in persons))
    print(f"  {'id':<{width}}  display", file=stderr)
    for pid, display in persons:
        print(f"  {pid:<{width}}  {display}", file=stderr)
    while True:
        print("Which of these are you (id or display)? ", end="", file=stderr, flush=True)
        line = stdin.readline()
        if not line:
            print(file=stderr)
            return None
        pid = match_person(line, persons)
        if pid is not None:
            return pid
        print(f"'{line.strip()}' is not in the list.", file=stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print("usage: python -m analysis.sim.choose_user <sim-dir>", file=sys.stderr)
        return 2
    pid = choose(Path(args[0]), sys.stdin, sys.stderr)
    if pid is None:
        return 1
    print(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
