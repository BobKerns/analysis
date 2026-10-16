"""Renaming a scenario, or a name scenarios share, everywhere it is used (issue #177).

A name an inheriting scenario can refer to -- an account, a chain, a
scheduled rule, a calendar row, a series, a lot -- is renamed in every
scenario that shares it, not just the one a command was run against. A
child that still said the old name would no longer load, or would quietly
stop overriding its parent's row. :func:`scope` works out which scenarios
share it; the ``rename_*`` helpers rewrite one scenario folder each. The CLI
runs them inside a staged edit (``staging.staged_edit``), so a rename that
would leave any scenario unable to load is never written.

A scenario itself is renamed with :func:`rename_scenario`: its folder, its
``scenario.csv`` name, every child's ``parent_scenario``, the data repo's
``.current_scenario``, and the links to it in the data repo's README files.
"""

from __future__ import annotations

import csv
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path

from analysis.sim import chain_edit, registries
from analysis.sim.lineage import load_order, parse_parents
from analysis.sim.loader import CURRENT_SCENARIO_FILE

__all__ = [
    "ACCOUNT_COLUMNS", "CALENDAR_ROW_COLUMNS", "SCHEDULED_COLUMNS", "SERIES_COLUMNS", "SERIES_KINDS",
    "account_defined", "calendar_row_defined", "clashes", "describe_clashes", "relink_markdown", "rename_account",
    "name_problem", "rename_calendar_row", "rename_scenario", "rename_scheduled", "rename_series", "scenario_folders",
    "scheduled_defined", "scope", "series_mentioned", "shared_series_files", "symbol_series",
]

Defines = Callable[[Path], bool]
"""Whether one scenario folder has its own definition of a name."""


def scenario_folders(scenarios_dir: Path) -> list[str]:
    return sorted(path.name for path in scenarios_dir.iterdir() if path.is_dir())


def scope(scenarios_dir: Path, scenario: str, defines: Defines) -> list[str]:
    """The scenarios a rename of a name ``scenario`` uses must rewrite, sorted;
    [] if nothing in ``scenario``'s load order defines it.

    That is every scenario in ``scenario``'s load order that defines the
    name, every scenario inheriting from one of those, and, since an
    inheriting scenario's mix-in may define the same name too, the same
    again for each of those, until nothing new turns up. Renaming any less
    would leave some scenario getting the name from a folder the rename
    missed. Two lineages that each define the name but share no scenario are
    separate names, and only ``scenario``'s is renamed.
    """
    orders = {name: load_order(scenarios_dir, name) for name in scenario_folders(scenarios_dir)}
    pending = [each for each in load_order(scenarios_dir, scenario) if defines(scenarios_dir / each)]
    affected: set[str] = set()
    while pending:
        current = pending.pop()
        if current in affected:
            continue
        affected.add(current)
        pending += [name for name, order in orders.items() if current in order and name not in affected]
        pending += [each for each in orders.get(current, [current])
                    if each not in affected and defines(scenarios_dir / each)]
    return sorted(affected)


def clashes(scenarios_dir: Path, affected: Iterable[str], defines: Defines) -> dict[str, str]:
    """For each affected scenario that already gets a name from somewhere in
    its load order, the scenario it gets it from: renaming onto that name
    would merge two things into one, or override one with the other."""
    found: dict[str, str] = {}
    for each in affected:
        for source in reversed(load_order(scenarios_dir, each)):
            if defines(scenarios_dir / source):
                found[each] = source
                break
    return found


def describe_clashes(found: dict[str, str]) -> str:
    """``kid, other (from mix)``: where a name already applies."""
    return ", ".join(each if source == each else f"{each} (from {source})" for each, source in found.items())


def name_problem(what: str, name: str) -> str | None:
    """Why ``name`` can't be a new ``what`` name used in a filename, or None."""
    if not name or any(c.isspace() for c in name):
        return f"a {what} name is one word, without spaces"
    if any(c in name for c in ":/\\") or name.startswith("."):
        return f"a {what} name can't contain ':', '/' or '\\', or start with '.': {name!r}"
    return None


# ---------- reading and rewriting cells ----------

def _cells(path: Path, columns: Iterable[str]) -> set[str]:
    """Every value in ``columns`` of one CSV, stripped; empty if there's no file."""
    if not path.is_file() or path.stat().st_size == 0:
        return set()
    with path.open(newline="", encoding="utf-8") as fh:
        return {(row.get(column) or "").strip() for row in csv.DictReader(fh) for column in columns}


def _rename_in(scenario_dir: Path, files: dict[str, tuple[str, ...]], old: str, new: str) -> list[str]:
    return [filename for filename, columns in files.items()
            if chain_edit.rename_cells(scenario_dir / filename, columns, old, new)]


def _rename_in_chains(scenario_dir: Path, column: str, old: str, new: str) -> list[str]:
    return [f"chains/{path.parent.name}/{path.name}" for path in chain_edit.chain_files(scenario_dir)
            if chain_edit.rename_cells(path, (column,), old, new)]


# ---------- accounts ----------

ACCOUNT_COLUMNS: dict[str, tuple[str, ...]] = {
    "accounts.csv": ("name",),
    "account_values.csv": ("account_name", "refill_chain", "overflow_chain"),
    "scheduled.csv": ("from_account", "to_account", "interest_account"),
    "calendar.csv": ("account_1", "account_2"),
    "quicken_account_map.csv": ("account_name",),
    "year_to_date.csv": ("from_account", "to_account"),
}
"""The columns, per scenario file, that may name an account. A refill or
overflow chain, or a transfer's end, may be an account's own leaf chain,
which has the account's name; chain files' steps are handled with them."""


def account_defined(name: str) -> Defines:
    return lambda scenario_dir: name in _cells(scenario_dir / "accounts.csv", ("name",))


def rename_account(scenario_dir: Path, old: str, new: str) -> list[str]:
    """Rename account ``old`` to ``new`` throughout one scenario folder;
    returns the files changed, relative to it."""
    return [*_rename_in(scenario_dir, ACCOUNT_COLUMNS, old, new),
            *_rename_in_chains(scenario_dir, "account_name", old, new)]


# ---------- scheduled rules ----------

SCHEDULED_COLUMNS: dict[str, tuple[str, ...]] = {
    "scheduled.csv": ("name",),
    "calendar.csv": ("scheduled_name",),
}
"""A scheduled rule's own name, and the calendar rows (``set_amount``,
``retire_scheduled``) that name it."""


def scheduled_defined(name: str) -> Defines:
    return lambda scenario_dir: name in _cells(scenario_dir / "scheduled.csv", ("name",))


def rename_scheduled(scenario_dir: Path, old: str, new: str) -> list[str]:
    return _rename_in(scenario_dir, SCHEDULED_COLUMNS, old, new)


# ---------- calendar rows ----------

CALENDAR_ROW_COLUMNS: dict[str, tuple[str, ...]] = {
    "calendar.csv": ("name", "vary_correlate_with"),
}
"""A calendar row's name (how an inheriting scenario overrides it), and the
rows naming it as their correlation parent (ADR-008)."""


def calendar_row_defined(name: str) -> Defines:
    return lambda scenario_dir: name in _cells(scenario_dir / "calendar.csv", ("name",))


def rename_calendar_row(scenario_dir: Path, old: str, new: str) -> list[str]:
    return _rename_in(scenario_dir, CALENDAR_ROW_COLUMNS, old, new)


# ---------- series ----------

SERIES_COLUMNS: dict[str, tuple[str, ...]] = {
    "accounts.csv": ("series",),
    "account_values.csv": ("series",),
    "scheduled.csv": ("series",),
    "calendar.csv": ("series",),
}
"""The columns, per scenario file, that name a series. Chain files' steps
have a ``series`` column too."""

_SERIES_FOLDERS = ("series", "aux")


SERIES_KINDS = ("rate", "inflation")
"""The aux kinds whose ``<kind>__<name>.csv`` files are a series' rates.
Other kinds' suffixes (a symbol's prices, a state's brackets) aren't series."""


def _series_files(folder: Path, name: str) -> list[Path]:
    """Series ``name``'s rate files in one folder. Names compared, not
    globbed: a name may hold '[' or '*'."""
    if not folder.is_dir():
        return []
    return sorted(path for kind in SERIES_KINDS for path in folder.glob(f"{kind}__*.csv")
                  if path.stem == f"{kind}__{name}")


def shared_series_files(data_dir: Path, name: str) -> list[Path]:
    """Files for series ``name`` in the data repo's shared ``aux/``, which
    every scenario reads, whatever its lineage."""
    return _series_files(data_dir / "aux", name)


def series_mentioned(name: str) -> Defines:
    """A series has no single defining row: rates for it may come from a
    file, a calendar row, or both, and it is named in several columns. Any
    of those ties a scenario to it."""
    def mentioned(scenario_dir: Path) -> bool:
        return (any(_series_files(scenario_dir / folder, name) for folder in _SERIES_FOLDERS)
                or any(name in _cells(scenario_dir / f, cols) for f, cols in SERIES_COLUMNS.items())
                or any(name in _cells(path, ("series",)) for path in chain_edit.chain_files(scenario_dir)))
    return mentioned


def rename_series(scenario_dir: Path, old: str, new: str) -> list[str]:
    """Rename series ``old`` to ``new`` throughout one scenario folder: its
    rate files in ``series/`` and ``aux/``, and every column naming it."""
    changed: list[str] = []
    for folder in _SERIES_FOLDERS:
        for path in _series_files(scenario_dir / folder, old):
            target = path.with_name(f"{path.stem.split('__', 1)[0]}__{new}.csv")  # kind kept
            if target.exists():
                raise FileExistsError(f"{scenario_dir.name} already has {folder}/{target.name}")
            path.rename(target)
            changed.append(f"{folder}/{path.name} -> {folder}/{target.name}")
    return [*changed, *_rename_in(scenario_dir, SERIES_COLUMNS, old, new),
            *_rename_in_chains(scenario_dir, "series", old, new)]


# ---------- scenarios ----------

def _rewrite_scenario_csv(path: Path, edit: Callable[[dict[str, str]], None]) -> bool:
    """Apply ``edit`` to each row of a scenario.csv; whether it changed."""
    if not path.is_file():
        return False
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    before = [dict(row) for row in rows]
    for row in rows:
        edit(row)
    if rows == before:
        return False
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return True


def _default_description(parents: list[str]) -> str:
    # What ``scenario.create_scenario`` writes when given no description.
    return f"Derived from {' + '.join(parents)}"


def _repoint_parents(old: str, new: str) -> Callable[[dict[str, str]], None]:
    def edit(row: dict[str, str]) -> None:
        parents = parse_parents(row.get("parent_scenario"))
        if old not in parents:
            return
        renamed = [new if parent == old else parent for parent in parents]
        row["parent_scenario"] = ":".join(renamed)
        # A description `scenario create` made up says which scenarios it
        # derives from, so it follows them; one somebody wrote is left be.
        if (row.get("description") or "").strip() == _default_description(parents):
            row["description"] = _default_description(renamed)
    return edit


def _markdown_files(data_dir: Path) -> list[Path]:
    """The README and other markdown files a scenario rename may need to
    relink: everything under ``scenarios/``, and the data repo's own
    top-level files. Other top-level folders (aux/, prices/...) describe
    shared data, not scenarios, and a staged edit can't write them."""
    files = sorted((data_dir / "scenarios").rglob("*.md"))
    files += sorted(path for path in data_dir.glob("*.md") if path.is_file() and not path.is_symlink())
    return files


# A link's destination, inline (`[text](dest "title")`) or as a reference
# definition (`[label]: dest`). Destinations in <angle brackets> or with
# spaces are left alone; scenario paths don't need them.
_INLINE_LINK_RE = re.compile(r"(\]\()([^)\s]+)")
_REFERENCE_LINK_RE = re.compile(r"^(\s{0,3}\[[^\]]+\]:\s*)(\S+)", re.MULTILINE)


def _relink(dest: str, md_dir: Path, new_md_dir: Path, old_dir: Path, new_dir: Path) -> str:
    """``dest`` pointed at the renamed folder from a file that now lives in
    ``new_md_dir``; unchanged unless it pointed into ``old_dir``."""
    if "://" in dest or dest.startswith(("#", "/", "mailto:")):
        return dest
    cut = min((i for i in (dest.find("#"), dest.find("?")) if i >= 0), default=len(dest))
    path, rest = dest[:cut], dest[cut:]
    if not path:
        return dest
    target = Path(os.path.normpath(md_dir / path))
    if target != old_dir and old_dir not in target.parents:
        return dest
    moved = new_dir / target.relative_to(old_dir)
    # Keep the link as written, the folder's name swapped, when that still
    # reaches it; otherwise work out a fresh relative path.
    parts = path.split("/")
    swapped = "/".join(new_dir.name if part == old_dir.name else part for part in parts)
    if Path(os.path.normpath(new_md_dir / swapped)) == moved:
        return swapped + rest
    relinked = Path(os.path.relpath(moved, new_md_dir)).as_posix()
    if path.endswith("/"):
        relinked += "/"
    return relinked + rest


def relink_markdown(text: str, md_path: Path, old_dir: Path, new_dir: Path) -> str:
    """``text``, a markdown file at ``md_path``, with its references to
    scenario folder ``old_dir`` pointed at ``new_dir`` instead: links into the
    folder (relative ones, resolved from the file, wherever the file now
    lives), a ``scenarios/<old>`` path written out, and the scenario's bare
    name in `code` or as a link's text."""
    old, new = old_dir.name, new_dir.name
    new_md_dir = (new_dir / md_path.parent.relative_to(old_dir)
                  if md_path.parent == old_dir or old_dir in md_path.parent.parents else md_path.parent)

    def fix(match: re.Match[str]) -> str:
        return match.group(1) + _relink(match.group(2), md_path.parent, new_md_dir, old_dir, new_dir)

    text = _INLINE_LINK_RE.sub(fix, text)
    text = _REFERENCE_LINK_RE.sub(fix, text)
    text = re.sub(rf"(?<![\w-])scenarios/{re.escape(old)}(?![\w-])", f"scenarios/{new}", text)
    text = text.replace(f"`{old}`", f"`{new}`")
    return text.replace(f"[{old}](", f"[{new}](")


def rename_scenario(data_dir: Path, old: str, new: str) -> list[str]:
    """Rename scenario ``old`` to ``new`` in data repo ``data_dir`` (a staged
    copy, in the CLI). Returns what changed, as paths relative to
    ``data_dir``, named as they are after the rename."""
    if not registries.SCENARIO_NAME_RE.match(new):
        raise ValueError(
            f"invalid scenario name: {new!r} (lowercase letters, digits, '_' and '-', "
            "starting with a letter or digit)")
    scenarios = data_dir / "scenarios"
    old_dir, new_dir = scenarios / old, scenarios / new
    if not old_dir.is_dir():
        raise FileNotFoundError(f"scenario not found: {old_dir}")
    if new_dir.exists():
        raise FileExistsError(f"scenario already exists: {new_dir}")

    def after(path: Path) -> str:
        if path == old_dir or old_dir in path.parents:
            path = new_dir / path.relative_to(old_dir)
        return path.relative_to(data_dir).as_posix()

    changed: list[str] = []
    # Links are worked out against where each file is now, before the move.
    for path in _markdown_files(data_dir):
        text = path.read_text(encoding="utf-8")
        relinked = relink_markdown(text, path, old_dir, new_dir)
        if relinked != text:
            path.write_text(relinked, encoding="utf-8")
            changed.append(after(path))
    old_dir.rename(new_dir)
    changed.insert(0, f"scenarios/{old}/ -> scenarios/{new}/")

    def rename_self(row: dict[str, str]) -> None:
        if (row.get("name") or "").strip() == old:
            row["name"] = new
    if _rewrite_scenario_csv(new_dir / "scenario.csv", rename_self):
        changed.append(f"scenarios/{new}/scenario.csv")
    for each in scenario_folders(scenarios):
        if _rewrite_scenario_csv(scenarios / each / "scenario.csv", _repoint_parents(old, new)):
            if f"scenarios/{each}/scenario.csv" not in changed:
                changed.append(f"scenarios/{each}/scenario.csv")
    current = data_dir / CURRENT_SCENARIO_FILE
    if current.is_file() and not current.is_symlink() and current.read_text(encoding="utf-8").strip() == old:
        current.write_text(f"{new}\n", encoding="utf-8")
        changed.append(CURRENT_SCENARIO_FILE)
    return changed


def symbol_series(data_dir: Path, name: str) -> str | None:
    """Why series ``name`` is read by name rather than through a column, or
    None. A lot holding a symbol grows at the series named for it, or for
    each asset class it holds at ``<symbol>.<class>`` (ADR-005 §5), with no
    cell naming the series; renaming one would quietly cut it off."""
    symbols = _cells(data_dir / "securities.csv", ("symbol",))
    scenarios = data_dir / "scenarios"
    for each in scenario_folders(scenarios):
        symbols |= _cells(scenarios / each / "account_values.csv", ("symbol",))
    symbols.discard("")
    if name in symbols:
        return f"{name!r} is a symbol, and lots holding it read its series by that name"
    symbol, _, asset_class = name.rpartition(".")
    if symbol and asset_class in registries.ASSET_CLASSES:
        return f"{name!r} is a <symbol>.<asset class> series, which fund lots read by that name"
    return None
