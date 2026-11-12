"""Applying generated expense rules to a scenario's ``scheduled.csv`` (issue #527).

The rules themselves come from ``analysis.expenses.baseline``; this is what
every flow import shares after that: which rules a rerun adds, changes and
removes, for the before/after diff.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["RuleChange", "file_digest", "plan"]

_COMPARED = ("from_account", "from_lot", "to_account", "amount", "series", "start", "cadence")
"""A rule is unchanged when these are: its provenance alone (a new export of
the same numbers) isn't a change worth showing."""


@dataclass(frozen=True)
class RuleChange:
    name: str
    to_account: str
    before: str
    """The amount now; blank for a new rule."""
    after: str
    """The amount after; blank for a rule the rerun removes."""
    status: str
    """``added``, ``changed``, ``removed`` or ``unchanged``."""


def file_digest(paths: Sequence[Path]) -> str:
    """A short hash of the imported files, for a generated rule's provenance."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()[:16]


def plan(scheduled_path: Path, rows: Sequence[Mapping[str, str]], tag: str) -> list[RuleChange]:
    """How ``rows`` differ from the rules tagged ``tag`` now in
    ``scheduled_path``, which they would replace."""
    existing: dict[str, dict[str, str]] = {}
    if scheduled_path.is_file() and scheduled_path.stat().st_size:
        with scheduled_path.open(newline="", encoding="utf-8-sig") as handle:
            existing = {row["name"]: row for row in csv.DictReader(handle)
                        if tag in (row.get("tags") or "").split()}
    changes: list[RuleChange] = []
    for row in rows:
        old = existing.pop(row["name"], None)
        if old is None:
            status = "added"
        elif any((old.get(column) or "") != row[column] for column in _COMPARED):
            status = "changed"
        else:
            status = "unchanged"
        changes.append(RuleChange(row["name"], row["to_account"],
                                  (old or {}).get("amount", ""), row["amount"], status))
    changes.extend(RuleChange(name, old.get("to_account", ""), old.get("amount", ""), "", "removed")
                   for name, old in existing.items())
    return changes
