"""What a scenario sets up, read from its CSVs for the ``sim`` listings (#185):
its calendar, persons and lifespans, year-to-date rows, itemizations, and a
summary of the scenario itself.

Everything is read straight from the data repo rather than loaded, so a
scenario that doesn't load can still be listed, and every row is merged down
the load order with where it is defined, by the same overlay the edit
commands use (:mod:`analysis.sim.overlay`): a listing and an edit agree on
which row is which.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from analysis.sim import readme, registries, units
from analysis.sim.lineage import load_order, read_parents
from analysis.sim.loader import (
    _MoneyReferenceResolver,
    latest_ensemble_batch,
    latest_run_path,
)
from analysis.sim.overlay import CalendarRow, overlay_rows, scenario_calendar_rows

if TYPE_CHECKING:
    from analysis.sim.scenario import ScenarioDate, ScenarioValue

__all__ = [
    "THIS_SCENARIO", "SHARED",
    "CalendarListing", "calendar_listing", "calendar_target", "vary_summary",
    "PersonListing", "person_listing",
    "YtdListing", "ytd_listing",
    "Itemization", "ItemizationReference", "itemizations", "itemization_references",
    "ScenarioShow", "scenario_show",
]

THIS_SCENARIO = "this scenario"
"""``defined_in`` of a row the scenario itself writes and inherits from nowhere."""

SHARED = "shared"
"""``defined_in`` of a file in the data repo's shared folder, outside any scenario."""


def defined_in(index: int | None, inherited_from: str | None) -> str:
    """Where a row comes from, as the listings show it: :data:`THIS_SCENARIO`,
    the ancestor it is inherited from, or ``overrides <ancestor>`` for the
    scenario's own row replacing an inherited one."""
    if index is None:
        return inherited_from or ""
    return f"overrides {inherited_from}" if inherited_from else THIS_SCENARIO


# ---------- calendar ----------


@dataclass(frozen=True)
class CalendarListing:
    number: int
    """The row's number for ``sim calendar edit``/``delete``: the scenario's
    own rows first, in file order, then those it inherits."""
    row: dict[str, str]
    defined_in: str


def calendar_listing(scenario_dir: Path) -> list[CalendarListing]:
    """Every calendar row of the scenario, merged, numbered as ``sim calendar
    edit`` numbers them, in date order (latent factors, which have no date,
    first)."""
    overlaid = scenario_calendar_rows(scenario_dir)
    ordered: list[CalendarRow] = [
        *sorted((e for e in overlaid if e.index is not None), key=lambda e: e.index or 0),
        *(e for e in overlaid if e.index is None),
    ]
    listed = [
        CalendarListing(number, entry.row, defined_in(entry.index, entry.inherited_from))
        for number, entry in enumerate(ordered, start=1)
    ]
    return sorted(listed, key=lambda e: (e.row["date"], e.number))


def _with_lot(account: str, lot: str) -> str:
    return f"{account}:{lot}" if account and lot else account


def calendar_target(row: dict[str, str]) -> str:
    """What one calendar row acts on: ``from -> to`` for a transfer, the
    scheduled row it changes or retires, or the series it sets (with its aux
    kind when that isn't ``rate``, and the table a swap puts in its place)."""
    if row["account_1"] or row["account_2"]:
        return (f"{_with_lot(row['account_1'], row['lot_1'])} -> "
                f"{_with_lot(row['account_2'], row['lot_2'])}")
    if row["scheduled_name"]:
        return row["scheduled_name"]
    if not row["series"]:
        return ""
    try:
        params = json.loads(row["params_json"]) if row["params_json"] else {}
    except ValueError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    target = row["series"]
    aux_kind = params.get("aux_kind", "rate")
    if aux_kind != "rate":
        target += f" ({aux_kind})"
    if params.get("source_series"):
        target += f" <- {params['source_series']}"
    return target


_VARY_COLUMNS = [c for c in registries.CALENDAR_HEADER if c.startswith("vary_")]


def vary_summary(row: dict[str, str]) -> str:
    """A varying row's or latent factor's ``vary_*`` cells on one line, the
    distribution first and the other filled cells as ``key=value``; "" for a
    row that doesn't vary. A latent factor's correlation reads ``~ parent rho``."""
    parts: list[str] = []
    if row.get("vary_distribution"):
        parts.append(row["vary_distribution"])
    for column in _VARY_COLUMNS:
        value = row.get(column, "")
        if not value or column in ("vary_distribution", "vary_correlate_with", "vary_correlation"):
            continue
        parts.append(f"{column.removeprefix('vary_')}={value}")
    if row.get("vary_correlate_with"):
        rho = f" {row['vary_correlation']}" if row.get("vary_correlation") else ""
        parts.append(f"~ {row['vary_correlate_with']}{rho}")
    return " ".join(parts)


# ---------- persons ----------


@dataclass(frozen=True)
class PersonListing:
    row: dict[str, str]
    """persons.csv's row: id, display, birthdate, role, meta_json."""
    final_month: str
    """The lifespan.csv ``final_month`` the scenario sets, or ""."""
    lifespan_defined_in: str


def person_listing(data_dir: Path, scenario_dir: Path) -> list[PersonListing]:
    """``persons.csv`` (shared, at the data repo root) with each person's
    lifespan as the scenario's load order merges ``lifespan.csv``. A lifespan
    for a person persons.csv doesn't have is listed too, with only its id,
    so the mistake the loader reports is visible here."""
    lifespans = overlay_rows(scenario_dir, "lifespan.csv", key="person")
    listed: list[PersonListing] = []
    for row in _read_rows(data_dir / "persons.csv"):
        found = lifespans.pop(row.get("id", ""), None)
        listed.append(PersonListing(
            row,
            found.row.get("final_month", "") if found else "",
            defined_in(found.index, found.inherited_from) if found else "",
        ))
    for person, found in lifespans.items():
        listed.append(PersonListing(
            {"id": person}, found.row.get("final_month", ""),
            defined_in(found.index, found.inherited_from),
        ))
    return listed


# ---------- year to date ----------


@dataclass(frozen=True)
class YtdListing:
    source: str
    """``year_to_date`` for a year_to_date.csv row, ``scheduled`` for a
    scheduled row's ``ytd_amount``/``ytd_preroll``."""
    name: str
    from_account: str
    to_account: str
    amount: str
    """As authored: decimal dollars or an ``@itemization``; ``preroll`` for a
    scheduled row whose first-year amount is pre-rolled."""
    tax_character: str
    year: str
    description: str
    defined_in: str


def ytd_listing(scenario_dir: Path) -> list[YtdListing]:
    """The merged ``year_to_date.csv`` rows, then the merged scheduled rows
    that carry a year-to-date amount or pre-roll (ADR-003 §4.10)."""
    listed: list[YtdListing] = []
    for name, found in overlay_rows(scenario_dir, "year_to_date.csv").items():
        row = found.row
        listed.append(YtdListing(
            "year_to_date", name,
            _with_lot(row.get("from_account", ""), row.get("from_lot", "")),
            _with_lot(row.get("to_account", ""), row.get("to_lot", "")),
            row.get("amount", ""), row.get("tax_character", ""), row.get("year", ""),
            row.get("description", ""), defined_in(found.index, found.inherited_from),
        ))
    for name, found in overlay_rows(scenario_dir, "scheduled.csv").items():
        row = found.row
        amount = row.get("ytd_amount", "")
        if not amount and row.get("ytd_preroll", "") not in ("", "0"):
            amount = "preroll"
        if not amount:
            continue
        listed.append(YtdListing(
            "scheduled", name,
            _with_lot(row.get("from_account", ""), row.get("from_lot", "")),
            _with_lot(row.get("to_account", ""), row.get("to_lot", "")),
            amount, row.get("tax_character", ""), row.get("tax_year", ""),
            row.get("description", ""), defined_in(found.index, found.inherited_from),
        ))
    return listed


# ---------- itemizations ----------


@dataclass(frozen=True)
class Itemization:
    name: str
    path: Path
    defined_in: str
    """:data:`THIS_SCENARIO`, the ancestor whose ``itemizations/`` has it, or
    :data:`SHARED`."""
    rows: list[dict[str, str]]
    total_cents: int | None
    """The sum the loader would use for ``@name``, nested references
    included; None when it can't be computed (``error`` says why)."""
    error: str


def _lineage_dirs(scenario_dir: Path) -> list[Path]:
    """The scenario's folder and its ancestors', root first (the load order)."""
    return [scenario_dir.parent / name for name in load_order(scenario_dir.parent, scenario_dir.name)]


def itemizations(data_dir: Path, scenario_dir: Path) -> list[Itemization]:
    """Every ``itemizations/<name>.csv`` the scenario can reference, by name:
    its own, its ancestors', and the shared folder's, a nearer one hiding a
    farther one of the same name, as the loader looks them up."""
    tiers: list[tuple[Path, str]] = [(data_dir, SHARED)]
    for folder in _lineage_dirs(scenario_dir):
        tiers.append((folder, THIS_SCENARIO if folder == scenario_dir else folder.name))
    found: dict[str, tuple[Path, str]] = {}
    for folder, label in tiers:
        directory = folder / "itemizations"
        if directory.is_dir():
            for path in sorted(directory.glob("*.csv")):
                found[path.stem] = (path, label)
    resolver = _MoneyReferenceResolver(data_dir, _lineage_dirs(scenario_dir)[::-1])
    listed: list[Itemization] = []
    for name in sorted(found):
        path, label = found[name]
        try:
            total: int | None = resolver.resolve(name)
            error = ""
        except ValueError as exc:
            total, error = None, str(exc)
        listed.append(Itemization(name, path, label, _read_rows(path), total, error))
    return listed


@dataclass(frozen=True)
class ItemizationReference:
    path: Path
    line: int
    """The CSV line, counting the header as line 1."""
    column: str


# The columns whose cells the loader reads as money, by file name, so an
# ``@name`` there is an itemization reference. Anywhere else ``@name`` is
# plain text to the loader, not a reference.
_LOT_MONEY_COLUMNS = frozenset({
    "opening_balance", "opening_cost_basis", "prior_year_end_balance",
    "minimum_balance", "maximum_balance", "refill_target", "refill_amount",
})
_MONEY_COLUMNS_BY_FILE: dict[str, frozenset[str]] = {
    "accounts.csv": _LOT_MONEY_COLUMNS,
    "account_values.csv": _LOT_MONEY_COLUMNS,
    "scheduled.csv": frozenset({"amount", "ytd_amount"}),
    "year_to_date.csv": frozenset({"amount"}),
}


def _money_columns(path: Path) -> frozenset[str]:
    """The columns of ``path`` the loader reads ``@name`` in: a lot's
    balances, a rule's and a year-to-date row's amounts, a chain step's
    ``maximum``, an aux table's money columns, an itemization's ``Value``."""
    folder = path.parent.name
    if folder in ("aux", "series", "prices"):
        kind = path.stem.split("__", 1)[0]
        return registries.AUX_MONEY_COLUMNS.get(kind, frozenset())
    if folder == "itemizations":
        return frozenset({"Value"})
    if path.parent.parent.name == "chains":
        return frozenset({"maximum"})
    return _MONEY_COLUMNS_BY_FILE.get(path.name, frozenset())


def _referencing_files(data_dir: Path, scenario_dir: Path) -> Iterable[Path]:
    for folder in _lineage_dirs(scenario_dir):
        yield from sorted(folder.rglob("*.csv"))
    for shared in ("aux", "itemizations"):
        if (data_dir / shared).is_dir():
            yield from sorted((data_dir / shared).rglob("*.csv"))


def itemization_references(data_dir: Path, scenario_dir: Path) -> dict[str, list[ItemizationReference]]:
    """Every ``@name`` in a money column (``_money_columns``) of the CSVs
    the scenario reads -- its lineage's folders and the shared aux/ and
    itemizations/ -- by the name it references. A row a later scenario
    overrides is still reported, in its own file."""
    found: dict[str, list[ItemizationReference]] = {}
    for path in _referencing_files(data_dir, scenario_dir):
        money = _money_columns(path)
        if not money:
            continue
        try:
            with path.open(newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                header = next(reader, [])
                for line, cells in enumerate(reader, start=2):
                    for column, cell in zip(header, cells, strict=False):
                        cell = cell.strip()
                        if column.strip() in money and cell.startswith("@") and len(cell) > 1:
                            found.setdefault(cell[1:], []).append(
                                ItemizationReference(path, line, column),
                            )
        except (OSError, UnicodeDecodeError, csv.Error):
            continue  # the loader reports an unreadable file; a listing skips it
    return found


# ---------- scenario ----------


@dataclass(frozen=True)
class ScenarioShow:
    name: str
    path: Path
    description: str
    """The README.md title and first paragraph, else scenario.csv's own."""
    parent: str | None
    mixins: list[str]
    load_order: list[str]
    start: ScenarioDate | None
    end: ScenarioDate | None
    seed: ScenarioValue | None
    ensemble_size: ScenarioValue | None
    reserve: tuple[str, bool] | None
    """``meta_json.reserve`` as written, and whether it is inherited."""
    latest_run: tuple[Path, datetime] | None
    latest_batch: tuple[Path, datetime] | None


def _merged_reserve(lineage: list[Path], name: str) -> tuple[str, bool] | None:
    for folder in reversed(lineage):
        rows = _read_rows(folder / "scenario.csv")
        try:
            meta = json.loads(rows[0].get("meta_json") or "{}") if rows else {}
        except json.JSONDecodeError:
            continue
        if isinstance(meta, dict) and "reserve" in meta:
            raw = meta["reserve"]
            try:
                text = f"${units.json_dollars_to_cents(raw) / 100:,.2f}"
            except ValueError:
                text = f"{raw!r} (invalid)"
            return text, folder.name != name
    return None


def _stamped(path: Path | None) -> tuple[Path, datetime] | None:
    if path is None:
        return None
    return path, datetime.fromtimestamp(path.stat().st_mtime)


def scenario_show(data_dir: Path, scenario_dir: Path, runs_dir: Path | None) -> ScenarioShow:
    """What ``readme.description_view`` shows in the notebook, read from the
    folders: description, lineage, dates, seed, ensemble size and reserve,
    each as the load would merge it; and the scenario's latest run file and,
    for an ensemble, its latest batch under ``runs_dir``."""
    # Imported here: analysis.sim.scenario imports the CLI, which imports this.
    from analysis.sim.scenario import scenario_summaries

    name = scenario_dir.name
    summary = next(s for s in scenario_summaries(data_dir, scenario_dir.parent) if s.name == name)
    parents = read_parents(scenario_dir)
    lineage = _lineage_dirs(scenario_dir)
    # As the loader has it: the scenario's own README.md, even a blank one,
    # else scenario.csv's description merged field by field down the load order.
    description = readme.readme_description(scenario_dir, data_dir)
    if description is None:
        description = next(
            (desc for folder in reversed(lineage)
             for row in _read_rows(folder / "scenario.csv")[:1]
             if (desc := row.get("description", ""))),
            "",
        )
    latest = latest_batch = None
    if runs_dir is not None:
        latest = _stamped(latest_run_path(runs_dir, name))
        if summary.is_ensemble:
            latest_batch = _stamped(latest_ensemble_batch(runs_dir / name))
    return ScenarioShow(
        name=name, path=scenario_dir, description=description,
        parent=parents[0] if parents else None, mixins=parents[1:],
        load_order=[folder.name for folder in lineage],
        start=summary.start, end=summary.end, seed=summary.seed,
        ensemble_size=summary.ensemble_size,
        reserve=_merged_reserve(lineage, name),
        latest_run=latest, latest_batch=latest_batch,
    )


def _read_rows(path: Path) -> list[dict[str, str]]:
    """A CSV's rows as stripped strings, [] for a missing or empty file."""
    if not path.is_file() or path.stat().st_size == 0:
        return []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [{str(k): str(v).strip() for k, v in record.items()} for record in frame.to_dict("records")]
