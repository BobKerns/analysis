"""Applying generated expense rules to a scenario's ``scheduled.csv`` (issue #527).

The rules themselves come from ``analysis.expenses.baseline``; this is what
every flow import shares after that: which rules a rerun adds, changes and
removes, for the before/after diff.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["RuleChange", "file_digest", "plan"]

_NOT_COMPARED = frozenset({"meta_json"})
"""A rule is unchanged when every other cell is: its provenance alone (a new
export of the same numbers) isn't a change worth showing. Every other cell
counts, since the write resets a generated rule's hand edits too."""


@dataclass(frozen=True)
class RuleChange:
    name: str
    to_account: str
    before: str
    """The amount now; blank for a new rule."""
    after: str
    """The amount after; blank for a rule the rerun removes."""
    status: str
    """``added``, ``changed``, ``removed``, ``unchanged`` or ``kept``: a rule
    the rerun no longer produces, which the scenario inherits and so still has."""


def file_digest(paths: Sequence[Path]) -> str:
    """A short hash of the imported files, for a generated rule's provenance."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()[:16]


def plan(
    existing: Mapping[str, Mapping[str, str]], rows: Sequence[Mapping[str, str]], tag: str,
    *, inherited: Collection[str] = (),
) -> list[RuleChange]:
    """How ``rows`` differ from the rules tagged ``tag`` the scenario has
    now, which they would replace. ``existing`` is every rule it loads, by
    name, its lineage's included; ``inherited`` names those a parent
    scenario has, which replacing this scenario's own rows can't remove."""
    current = {name: row for name, row in existing.items() if tag in (row.get("tags") or "").split()}
    changes: list[RuleChange] = []
    for row in rows:
        old = current.pop(row["name"], None)
        if old is None:
            status = "added"
        elif any((old.get(column) or "") != value
                 for column, value in row.items() if column not in _NOT_COMPARED):
            status = "changed"
        else:
            status = "unchanged"
        changes.append(RuleChange(row["name"], row["to_account"],
                                  (old or {}).get("amount", ""), row["amount"], status))
    changes.extend(
        RuleChange(name, old.get("to_account", ""), old.get("amount", ""),
                   *((old.get("amount", ""), "kept") if name in inherited else ("", "removed")))
        for name, old in current.items())
    return changes
