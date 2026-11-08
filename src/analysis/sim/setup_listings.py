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
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from analysis.sim import chain_edit, readme, registries, units
from analysis.sim.dates import final_month_for_age
from analysis.sim.lineage import load_order, read_parents
from analysis.sim.lineage import scenario_dir as _scenario_dir
from analysis.sim.loader import (
    _MoneyReferenceResolver,
    latest_ensemble_batch,
    latest_run_path,
)
from analysis.sim.overlay import CalendarRow, overlay_rows, scenario_calendar_rows

if TYPE_CHECKING:
    from analysis.sim.scenario import ScenarioDate, ScenarioValue

__all__ = [
    "THIS_SCENARIO", "SHARED", "BUILT_IN",
    "UNCONFIRMED", "defined_in", "row_origins", "checked_origins", "loader_accounts",
    "chain_origins", "calendar_origins", "stamped", "reserve_text",
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


BUILT_IN = "built in"
"""``defined_in`` of a row no scenario writes but the loader adds itself: a
growth account a lot's gains are booked against, or a lot's growth rule."""


UNCONFIRMED = "-"
"""``defined_in`` where the folders no longer match the loaded intent (the
scenario was edited since its load), shown rather than a wrong guess."""


def defined_in(index: int | None, inherited_from: str | None) -> str:
    """Where a row comes from, as the listings show it: :data:`THIS_SCENARIO`,
    the ancestor it is inherited from, or ``overrides <ancestor>`` for the
    scenario's own row replacing an inherited one."""
    if index is None:
        return inherited_from or ""
    return f"overrides {inherited_from}" if inherited_from else THIS_SCENARIO


def row_origins(scenario_dir: Path, filename: str, key: str = "name") -> dict[str, str]:
    """``defined_in`` of every row of ``filename`` (accounts.csv,
    scheduled.csv) by ``key``, merged down the load order as the loader and
    the edit commands merge it (#433)."""
    return {name: defined_in(found.index, found.inherited_from)
            for name, found in overlay_rows(scenario_dir, filename, key).items()}


def loader_accounts() -> frozenset[str]:
    """The accounts the loader adds itself (growth and payout counterparties),
    which are also the source of every rule it adds: a lot's growth rule or
    payout. What :data:`BUILT_IN` marks."""
    return frozenset(account.name for account in (
        *registries.GROWTH_COUNTERPARTY_ACCOUNTS.values(), *registries.PAYOUT_ACCOUNTS.values()))


def checked_origins(origins: Mapping[str, str], names: Sequence[str],
                    built_in: Collection[str]) -> list[str]:
    """``origins`` (:func:`row_origins`) lined up with an intent's ``names``,
    a name the folders don't have marked :data:`BUILT_IN` when it is in
    ``built_in``. :data:`UNCONFIRMED` throughout when they don't agree -- a
    row the folders have that the intent lacks, or one the intent has that
    neither the folders nor the loader explain -- since the scenario was
    edited after its load and any label could be another row's (#433)."""
    known = set(names)
    if not origins.keys() <= known or any(n not in origins and n not in built_in for n in names):
        return [UNCONFIRMED] * len(names)
    return [origins.get(name, BUILT_IN) for name in names]


def chain_origins(scenarios_dir: Path, scenario: str) -> dict[str, str | chain_edit.AmbiguousChain]:
    """Where each chain some file defines is defined, as ``sim chain list``
    shows it, in one pass over the load order: a chain file replaces the
    whole chain, so the last scenario with a file for it; ``overrides
    <ancestor>`` when that is ``scenario`` itself and an ancestor has one too.
    A chain one folder has two files for maps to the
    ``chain_edit.AmbiguousChain`` the loader would refuse it with."""
    definers: dict[str, list[str]] = {}
    ambiguous: dict[str, chain_edit.AmbiguousChain] = {}
    for each in load_order(scenarios_dir, scenario):
        folder = _scenario_dir(scenarios_dir, each)
        for stem, count in Counter(path.stem for path in chain_edit.chain_files(folder)).items():
            definers.setdefault(stem, []).append(each)
            if count > 1 and stem not in ambiguous:
                try:
                    chain_edit.chain_file(folder, stem)
                except chain_edit.AmbiguousChain as exc:
                    ambiguous[stem] = exc
    origins: dict[str, str | chain_edit.AmbiguousChain] = {
        name: found[-1] if found[-1] != scenario
        else defined_in(0, found[-2] if len(found) > 1 else None)
        for name, found in definers.items()
    }
    return {**origins, **ambiguous}


def calendar_origins(scenario_dir: Path) -> list[tuple[str, str, str]]:
    """``(kind, name, defined_in)`` of every merged calendar row, in the order
    the loader writes them to ``calendar_event`` (the overlay follows the
    load's own merge), so a caller can line them up with the intent's rows
    after checking that the kinds and names agree (#433)."""
    return [(entry.row["kind"], entry.row["name"], defined_in(entry.index, entry.inherited_from))
            for entry in scenario_calendar_rows(scenario_dir)]


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
    lifespan: dict[str, str]
    """The person's lifespan.csv row as the scenario merges it, or {}."""
    lifespan_defined_in: str

    @property
    def age_at_death(self) -> str:
        """The lifespan's ``age_at_death`` as written, or ""."""
        return self.lifespan.get("age_at_death", "")

    @property
    def final_month(self) -> str:
        """The ``YYYY-MM`` the age reaches from the birthdate (#221), or ""
        when either is missing or doesn't parse (the loader reports it)."""
        try:
            return final_month_for_age(
                self.row.get("birthdate", ""), float(self.age_at_death),
            )[:7]
        except ValueError:
            return ""

    @property
    def varies(self) -> str:
        """How the age varies across ensemble members, e.g.
        ``triangular 75..95 ~ lifespan:alice 0.5``, or ""."""
        return lifespan_vary_text(self.lifespan)


def lifespan_vary_text(row: dict[str, str]) -> str:
    """A lifespan.csv row's ``vary_*`` cells in one phrase (#221), or ""."""
    distribution = row.get("vary_distribution", "")
    if not distribution:
        return ""
    parts = [distribution if distribution == registries.LIFE_TABLE_DISTRIBUTION
             else f"{distribution} {row.get('vary_low', '')}..{row.get('vary_high', '')}"]
    if row.get("vary_mode"):
        parts.append(f"mode {row['vary_mode']}")
    if row.get("vary_correlate_with"):
        parts.append(f"~ {row['vary_correlate_with']} {row.get('vary_correlation', '')}".rstrip())
    return " ".join(parts)


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
            dict(found.row) if found else {},
            defined_in(found.index, found.inherited_from) if found else "",
        ))
    for person, found in lifespans.items():
        listed.append(PersonListing(
            {"id": person}, dict(found.row), defined_in(found.index, found.inherited_from),
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
    return [_scenario_dir(scenario_dir.parent, name) for name in load_order(scenario_dir.parent, scenario_dir.name)]


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
    "account_lots.csv": _LOT_MONEY_COLUMNS,
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


_ROW_KEYS: dict[str, tuple[str, ...]] = {
    "accounts.csv": ("name",),
    "account_lots.csv": ("account_name", "lot"),
    "scheduled.csv": ("name",),
    "year_to_date.csv": ("name",),
}
"""The files whose rows a nearer scenario overrides one by one, by these
columns, as the loader merges them; any other file a nearer one of the
same path replaces whole."""


def _row_key(row: dict[str, str], keys: tuple[str, ...]) -> tuple[str, ...]:
    """A row's key as the loader merges on it: a blank lot is the default
    lot (``loader._default_lots``)."""
    return tuple((row.get(column, "") or registries.DEFAULT_LOT) if column == "lot"
                 else row.get(column, "") for column in keys)


def _csv_rows(path: Path) -> tuple[list[str], dict[int, list[str]]]:
    """``path``'s header and its rows by line (the header is line 1)."""
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = [column.strip() for column in next(reader, [])]
            return header, {line: cells for line, cells in enumerate(reader, start=2)}
    except (OSError, UnicodeDecodeError, csv.Error):
        return [], {}


def _overridden(ref: ItemizationReference, data_dir: Path, lineage: list[Path]) -> bool:
    """Whether a nearer scenario replaces what ``ref`` sits in: its whole
    file, or, for a file merged row by row, its row's value in that column."""
    owner = next((i for i, folder in enumerate(lineage) if ref.path.is_relative_to(folder)), None)
    rel = ref.path.relative_to(data_dir if owner is None else lineage[owner])
    later = lineage[(-1 if owner is None else owner) + 1:]
    if owner is not None and rel.parent.parent.name == "chains":
        # A chain is replaced by name, whichever role folder holds it.
        nearer = [folder / "chains" / role / rel.name
                  for folder in later for role in registries.CHAIN_ROLES]
    else:
        nearer = [folder / rel for folder in later]
    nearer = [path for path in nearer if path.is_file()]
    if not nearer:
        return False
    # Only the scenario-level files merge row by row; an itemization or aux
    # table of the same file name is replaced whole.
    keys = _ROW_KEYS.get(ref.path.name) if len(rel.parts) == 1 else None
    if keys is None or owner is None:
        return True
    header, rows = _csv_rows(ref.path)
    row = dict(zip(header, (cell.strip() for cell in rows.get(ref.line, [])), strict=False))
    key = _row_key(row, keys)
    for path in nearer:
        near_header, near_rows = _csv_rows(path)
        if ref.column.strip() not in near_header:
            continue
        for cells in near_rows.values():
            near = dict(zip(near_header, (cell.strip() for cell in cells), strict=False))
            if _row_key(near, keys) == key:
                return True
    return False


def _ignored(ref: ItemizationReference, data_dir: Path, lineage: list[Path]) -> bool:
    """Whether the loader skips ``ref``'s file: a chain file outside a role
    folder, or a template whose name starts with ``_``
    (``loader._read_chain_files``)."""
    owner = next((folder for folder in lineage if ref.path.is_relative_to(folder)), None)
    if owner is None:
        return False
    rel = ref.path.relative_to(owner)
    return rel.parts[0] == "chains" and (
        len(rel.parts) != 3 or rel.parts[1] not in registries.CHAIN_ROLES
        or ref.path.stem.startswith("_"))


def itemization_references(
    data_dir: Path, scenario_dir: Path, *, effective: bool = False,
) -> dict[str, list[ItemizationReference]]:
    """Every ``@name`` in a money column (``_money_columns``) of the CSVs
    the scenario reads -- its lineage's folders and the shared aux/ and
    itemizations/ -- by the name it references. A row a later scenario
    overrides is still reported, in its own file, unless ``effective``:
    then only what the scenario reads, past every override, is."""
    found: dict[str, list[ItemizationReference]] = {}
    lineage = _lineage_dirs(scenario_dir) if effective else []
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
    if effective:
        found = {name: kept for name, refs in found.items()
                 if (kept := [ref for ref in refs if not _ignored(ref, data_dir, lineage)
                           and not _overridden(ref, data_dir, lineage)])}
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
            return reserve_text(meta["reserve"]), folder.name != name
    return None


def reserve_text(raw: object) -> str:
    """``meta_json.reserve`` as ``sim scenario show`` prints it."""
    try:
        return f"${units.json_dollars_to_cents(raw) / 100:,.2f}"
    except ValueError:
        return f"{raw!r} (invalid)"


def stamped(path: Path | None) -> tuple[Path, datetime] | None:
    """``path`` with when it was written; None for no path, or a file gone
    since it was found (a run pruned or rebuilt meanwhile)."""
    if path is None:
        return None
    try:
        return path, datetime.fromtimestamp(path.stat().st_mtime)
    except FileNotFoundError:
        return None


def scenario_show(data_dir: Path, scenario_dir: Path, runs_dir: Path | None) -> ScenarioShow:
    """What ``readme.description_view`` shows in the notebook, read from the
    folders: description, lineage, dates, seed, ensemble size and reserve,
    each as the load would merge it; and the scenario's latest run file and,
    for an ensemble, its latest batch under ``runs_dir``."""
    # Imported here: analysis.sim.scenario imports the CLI, which imports this.
    from analysis.sim.scenario import scenario_summary

    name = scenario_dir.name
    summary = scenario_summary(data_dir, scenario_dir.parent, name)
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
        # Describing a scenario doesn't open its run, so it never waits (#300).
        latest = stamped(latest_run_path(runs_dir, name, wait=False))
        if summary.is_ensemble:
            latest_batch = stamped(latest_ensemble_batch(runs_dir / name, wait=False))
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
