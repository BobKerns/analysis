"""Editing chain files: the CSV side of ``sim chain add|step|rename|delete`` (issue #129).

A chain is one file, ``chains/<role>/<name>.csv`` (ADR-003 §4.7), where the
role is ``source`` or ``destination``, and a scenario inheriting it may
override the whole file. These helpers read and
write those files and the references to a chain by name; the CLI runs them
inside a staged edit (``staging.staged_edit``), so a change that would not
load is never written.
"""

from __future__ import annotations

import csv
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from analysis.sim import registries
from analysis.sim.lineage import load_order

__all__ = [
    "ACCOUNT_COLUMNS", "CHAIN_COLUMNS", "LOT_COLUMNS", "REFERENCE_COLUMNS", "TAX_COLUMNS", "chain_file",
    "chain_files", "defining_scenario", "descendants", "new_chain_file", "read_steps", "rename_cells",
    "rename_references", "shares_to_percents", "write_steps",
]

TAX_COLUMNS = ("tax_character", "basis_of", "basis_of_lot")
"""A destination chain step's tax columns (#199): written only to a file
whose steps use them, so other chain files keep their header."""

CHAIN_COLUMNS = ("account_name", "lot", "maximum", "percent", "series", "cadence", *TAX_COLUMNS)
"""A chain file's columns (ADR-003 §4.7), in the order they are written."""

ACCOUNT_COLUMNS = ("account_name", "basis_of")
"""A chain file's columns that may name an account, for renames and deletes."""

LOT_COLUMNS = (("account_name", "lot"), ("basis_of", "basis_of_lot"))
"""A chain file's (account, lot) column pairs, for lot renames and deletes."""

REFERENCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "scheduled.csv": ("from_account", "to_account"),
    "calendar.csv": ("account_1", "account_2"),
    "account_lots.csv": ("refill_chain", "overflow_chain"),
}
"""The columns, per scenario file, that may name a chain. Chain files' own
``account_name`` steps are handled with the chain files."""


class AmbiguousChain(ValueError):
    """A scenario folder has more than one file for the same chain name."""


def chain_files(scenario_dir: Path) -> list[Path]:
    """The scenario folder's own chain files, in both role folders. The
    loader skips a '_' file (a template such as ``_example.csv``), so this
    does too."""
    chains = scenario_dir / "chains"
    return [
        path
        for role in registries.CHAIN_ROLES
        for path in sorted((chains / role).glob("*.csv"))
        if not path.stem.startswith("_")
    ]


def new_chain_file(scenario_dir: Path, role: str, name: str) -> Path:
    """Where a new ``role`` chain ``name`` is written in a scenario folder."""
    return scenario_dir / "chains" / role / f"{name}.csv"


def chain_file(scenario_dir: Path, name: str) -> Path | None:
    """The scenario folder's own file for chain ``name``, if it has one.
    Two files for one name (``source/x.csv`` and ``destination/x.csv``)
    raise ``AmbiguousChain``: the loader refuses the scenario, and an edit
    to either could miss the one meant."""
    # Compare names, not a glob: a chain name may hold '[' or '*'.
    found = [path for path in chain_files(scenario_dir) if path.stem == name]
    if len(found) > 1:
        raise AmbiguousChain(
            f"{scenario_dir.name} has {len(found)} files for chain {name!r} "
            f"({', '.join(f'{p.parent.name}/{p.name}' for p in found)}); remove all but one first")
    return found[0] if found else None


def defining_scenario(scenarios_dir: Path, scenario: str, name: str) -> str | None:
    """The scenario in ``scenario``'s load order whose file for chain ``name``
    is the one loaded: the last to have one, since a file replaces the whole
    chain."""
    found = None
    for each in load_order(scenarios_dir, scenario):
        if chain_file(scenarios_dir / each, name) is not None:
            found = each
    return found


def descendants(scenarios_dir: Path, scenario: str) -> list[str]:
    """The scenarios that inherit from ``scenario``, directly or not."""
    return [
        path.name for path in sorted(scenarios_dir.iterdir())
        if path.is_dir() and path.name != scenario and scenario in load_order(scenarios_dir, path.name)
    ]


def read_steps(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        columns = [c for c in CHAIN_COLUMNS if c not in TAX_COLUMNS or c in (reader.fieldnames or ())]
        return [
            {column: (row.get(column) or "").strip() for column in columns}
            for row in reader
            if any((v or "").strip() for v in row.values())
        ]


def write_steps(path: Path, steps: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    taxed = any(step.get(column) for step in steps for column in TAX_COLUMNS)
    columns = [c for c in CHAIN_COLUMNS if taxed or c not in TAX_COLUMNS]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows({column: step.get(column, "") for column in columns} for step in steps)


def rename_cells(path: Path, columns: tuple[str, ...], old: str, new: str) -> bool:
    """Replace ``old`` with ``new`` in ``columns`` of one CSV; whether it changed.
    Other columns, and the file's own column order, are kept."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    changed = False
    for row in rows:
        for column in columns:
            if column in row and (row[column] or "").strip() == old:
                row[column] = new
                changed = True
    if changed:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    return changed


def name_problem(name: str) -> str | None:
    """Why ``name`` can't be a new chain's name, or None. The name becomes
    part of a filename in ``chains/<role>/``, so it must stay one path
    segment the loader's ``<name>.csv`` glob finds."""
    if not name or any(c.isspace() for c in name):
        return "a chain name is one word, without spaces"
    if any(c in name for c in ":/\\") or name.startswith("."):
        return f"a chain name can't contain ':', '/' or '\\', or start with '.': {name!r}"
    return None


def rename_references(scenario_dir: Path, old: str, new: str) -> list[str]:
    """Rename chain ``old`` to ``new`` throughout one scenario folder: its own
    file for the chain (kept in the same role folder), the steps of its other
    chains, and every column that may name a chain. Returns what changed, as
    paths relative to the folder."""
    if problem := name_problem(new):
        raise ValueError(problem)
    changed: list[str] = []
    own = chain_file(scenario_dir, old)
    if own is not None:
        role = own.parent.name
        if (taken := chain_file(scenario_dir, new)) is not None:
            raise FileExistsError(f"{scenario_dir.name} already has chains/{taken.parent.name}/{taken.name}")
        own.rename(own.with_name(f"{new}.csv"))
        changed.append(f"chains/{role}/{own.name} -> chains/{role}/{new}.csv")
    for filename, columns in REFERENCE_COLUMNS.items():
        if rename_cells(scenario_dir / filename, columns, old, new):
            changed.append(filename)
    for path in chain_files(scenario_dir):
        if rename_cells(path, ("account_name",), old, new):
            changed.append(f"chains/{path.parent.name}/{path.name}")
    return changed


def shares_to_percents(shares: list[Decimal]) -> list[str]:
    """Shares of a *total*, adding up to 100, as the ``percent`` cells a chain
    needs, where each step's percent is of what is still outstanding: 50, 30,
    20 becomes ``50%``, ``60%`` and a plain last step. The last step is always
    left plain, so rounding to the four places a percent may carry never
    strands a remainder."""
    if sum(shares) != 100:
        raise ValueError(f"the shares add up to {sum(shares):g}%, not 100%")
    if any(share <= 0 for share in shares):
        raise ValueError("every share must be more than 0%")
    percents: list[str] = []
    left = Decimal(100)
    for share in shares[:-1]:
        percent = (share * 100 / left).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        percents.append(f"{percent.normalize():f}%")
        left -= share
    return [*percents, ""]
