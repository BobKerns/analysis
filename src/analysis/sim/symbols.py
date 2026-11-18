"""The securities the data repo knows about, and adding one (issue #127).

A symbol touches three shared files at the data repo root: its name in
``securities.csv`` (ADR-003 §4.1a), its asset-class weights in
``allocations.csv`` (§4.1b), and its prices in
``prices/security_price__<SYMBOL>.csv`` (§4.4). ``symbol_table`` gathers what
each file says about every symbol, with the lots that hold it, so a gap (no
weights, no prices) shows; ``add_symbol`` writes the first two, validated by
the loader's own checks so a row it writes always loads.
"""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from analysis.sim import registries, units
from analysis.sim.asset_classes import is_lot_key
from analysis.sim.prices import no_fetch_symbols, price_files
from analysis.sim.dates import month_end_iso
from analysis.sim.loader import _validate_allocations, _validate_securities

__all__ = [
    "DeletePlan", "SymbolAdded", "SymbolEdited", "add_symbol", "delete_symbol", "edit_symbol",
    "fetch_text", "has_weights", "holders", "latest_set", "leftovers", "no_fetch_symbols",
    "normalize_symbol", "parse_weights", "plan_delete", "security_name", "symbol_table", "weights_text",
]

SECURITIES = "securities.csv"
ALLOCATIONS = "allocations.csv"
_SECURITY_COLUMNS = ["symbol", "name"]
# #640: optional; written only once some symbol is marked.
NO_FETCH = "no_fetch"
_ALLOCATION_COLUMNS = ["symbol", "as_of", "asset_class", "weight"]
# A file to write: its path, header and rows (see _write_all).
type _Write = tuple[Path, list[str], list[dict[str, str]]]


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """A CSV's header and its non-blank rows, cells trimmed as the loader trims them."""
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = [
            {k: (v or "").strip() for k, v in row.items()}
            for row in reader
            if any((v or "").strip() for v in row.values())
        ]
        return list(reader.fieldnames or []), rows


def _month_key(text: str) -> str:
    """An ``as_of`` in the loader's stored month-end form; blank stays blank,
    and one that doesn't parse is left as written for the loader to report."""
    if not text:
        return ""
    try:
        return month_end_iso(text)
    except ValueError:
        return text


def _holders(scenarios_dir: Path) -> dict[str, list[str]]:
    """Symbol to the ``scenario: account (lot)`` rows that name it, as each
    scenario's own ``account_lots.csv`` writes them (an inherited lot is
    listed once, where it is defined)."""
    held: dict[str, list[str]] = {}
    if not scenarios_dir.is_dir():
        return held
    for values in sorted(scenarios_dir.glob("*/account_lots.csv")):
        for row in _read(values)[1]:
            if symbol := row.get("symbol", ""):
                lot = row.get("lot") or registries.DEFAULT_LOT
                held.setdefault(symbol, []).append(
                    f"{values.parent.name}: {row.get('account_name', '')} ({lot})"
                )
    return held


def _latest_weights(rows: list[dict[str, str]]) -> dict[str, tuple[str, list[tuple[str, str]], int]]:
    """Symbol to ``(as_of, [(asset_class, weight)], set count)`` of its latest set."""
    sets: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for row in rows:
        sets.setdefault(row["symbol"], {}).setdefault(row.get("as_of", ""), []).append(
            (row.get("asset_class", ""), row.get("weight", ""))
        )
    latest = {}
    for symbol, by_date in sets.items():
        # A blank as_of holds at every date, so it sorts before any dated set.
        as_of = max(by_date, key=_month_key)
        latest[symbol] = (as_of, by_date[as_of], len(by_date))
    return latest


def latest_set(data_dir: Path, symbol: str) -> tuple[str, list[tuple[str, str]]] | None:
    """``symbol``'s latest set of weights in ``allocations.csv`` as
    ``(as_of, [(asset_class, weight)])``, or None when it has none."""
    rows = [r for r in _read(data_dir / ALLOCATIONS)[1] if r.get("symbol") == symbol]
    found = _latest_weights(rows).get(symbol)
    return (found[0], found[1]) if found else None


def has_weights(data_dir: Path, symbol: str, as_of: str) -> bool:
    """Whether ``allocations.csv`` already has a set for ``symbol`` as of ``as_of``."""
    key = _month_key(as_of)
    return any(
        r.get("symbol") == symbol and _month_key(r.get("as_of", "")) == key
        for r in _read(data_dir / ALLOCATIONS)[1]
    )


def security_name(data_dir: Path, symbol: str) -> str | None:
    """``symbol``'s name in ``securities.csv``, or None when it isn't there."""
    return next((r.get("name", "") for r in _read(data_dir / SECURITIES)[1] if r.get("symbol") == symbol), None)


def _scenario_price_files(scenarios_dir: Path) -> dict[str, list[Path]]:
    """Symbol to each scenario's own ``prices/`` file for it, which replaces
    the shared one for that scenario and its children."""
    found: dict[str, list[Path]] = {}
    if scenarios_dir.is_dir():
        for folder in sorted(p for p in scenarios_dir.iterdir() if p.is_dir()):
            for symbol, path in price_files(folder / "prices").items():
                found.setdefault(symbol, []).append(path)
    return found


def weights_text(as_of: str, classes: list[tuple[str, str]]) -> str:
    """A set of weights as ``symbol list`` and ``symbol edit`` show it:
    ``stock_us 60%, bond 40% (as of 2026-01)``."""
    return ", ".join(f"{cls} {weight}" for cls, weight in classes) + (f" (as of {as_of})" if as_of else "")


def fetch_text(marked: bool) -> str:
    """What a symbol's ``no_fetch`` mark means, for a status line."""
    return "marked no_fetch" if marked else "fetched and researched"


def symbol_table(data_dir: Path) -> pd.DataFrame:
    """One row per symbol named anywhere: its name, latest weights, prices and
    holders. Columns: ``symbol``, ``name``, ``allocation``, ``allocation_sets``,
    ``prices``, ``latest_price``, ``latest_date`` (the shared ``prices/`` file),
    ``scenario_prices`` (the scenarios with their own), ``held_by``,
    ``no_fetch`` (#640)."""
    names = {row["symbol"]: row.get("name", "") for row in _read(data_dir / SECURITIES)[1] if row.get("symbol")}
    marked = no_fetch_symbols(data_dir)
    weights = _latest_weights([  # account:lot keys class a lot, not a symbol (#326)
        r for r in _read(data_dir / ALLOCATIONS)[1] if r.get("symbol") and not is_lot_key(r["symbol"])
    ])
    prices = price_files(data_dir / "prices")
    holders = _holders(data_dir / "scenarios")
    overrides = {symbol: [path.parent.parent.name for path in paths]
                 for symbol, paths in _scenario_price_files(data_dir / "scenarios").items()}
    records = []
    for symbol in sorted({*names, *weights, *prices, *overrides, *holders}):
        allocation, sets = "", 0
        if symbol in weights:
            as_of, classes, sets = weights[symbol]
            allocation = weights_text(as_of, classes)
        count, latest_price, latest_date = 0, "", ""
        if symbol in prices:
            rows = [r for r in _read(prices[symbol])[1] if r.get("effective_date")]
            count = len(rows)
            if rows:
                last = max(rows, key=lambda r: r["effective_date"])
                latest_price, latest_date = last.get("price", ""), last["effective_date"]
        records.append({
            "symbol": symbol, "name": names.get(symbol, ""),
            "allocation": allocation, "allocation_sets": sets,
            "prices": count, "latest_price": latest_price, "latest_date": latest_date,
            "scenario_prices": overrides.get(symbol, []),
            "held_by": holders.get(symbol, []), "no_fetch": symbol in marked,
        })
    columns = ["symbol", "name", "allocation", "allocation_sets", "prices",
               "latest_price", "latest_date", "scenario_prices", "held_by", "no_fetch"]
    return pd.DataFrame(records, columns=columns)


def parse_weights(specs: list[str]) -> dict[str, str]:
    """``CLASS=PERCENT`` arguments to ``{asset_class: weight}``, each weight in
    its CSV form (``60%``). The ``%`` may be left off on the command line."""
    weights: dict[str, str] = {}
    for spec in specs:
        asset_class, sep, percent = spec.partition("=")
        asset_class = asset_class.strip()
        if not sep or not asset_class or not percent.strip():
            raise ValueError(f"weight {spec!r} must be CLASS=PERCENT, e.g. stock_us=60%")
        if asset_class not in registries.ASSET_CLASSES:
            raise ValueError(
                f"unknown asset class {asset_class!r}; allowed: {registries.asset_class_list()}"
            )
        if asset_class in weights:
            raise ValueError(f"asset class {asset_class} is given twice")
        micros = units.percent_to_micros(percent, require_sign=False)
        weights[asset_class] = units.micros_to_percent(micros)
    return weights


def normalize_symbol(symbol: str) -> str:
    """``symbol`` as written to the files: upper case, like the tickers it
    names (an ``<account>:<lot>`` key keeps its case). Raises ``ValueError`` for one that can't be part of a filename,
    as ``prices/security_price__<SYMBOL>.csv`` and ``research/<SYMBOL>/`` are."""
    symbol = symbol.strip()
    # An <account>:<lot> key classes a lot with no symbol and must match the
    # names as written, so only a ticker is upper-cased.
    if not is_lot_key(symbol):
        symbol = symbol.upper()
    if not symbol or any(ch.isspace() for ch in symbol) or "/" in symbol or "\\" in symbol or symbol in (".", ".."):
        raise ValueError(f"symbol {symbol!r} must be one word, with no spaces or '/'")
    return symbol


@dataclass
class SymbolAdded:
    symbol: str
    """The symbol as written: tickers are upper case, so ``vti`` is ``VTI``."""
    named: bool
    """A ``securities.csv`` row was written."""
    weighted: bool
    """``allocations.csv`` rows were written."""


def _write_atomic(path: Path, header: list[str], rows: list[dict[str, str]]) -> Path:
    """Write ``rows`` to a temporary file beside ``path``; the caller renames it."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return Path(tmp)


def _appended(
    path: Path, default_header: list[str], new_rows: list[dict[str, str]],
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
    """``path``'s header, its rows as written, and the same with ``new_rows``
    added. A file's own extra columns are kept; one missing a column we write
    is refused, so a row never lands half-written."""
    header, rows = _read(path)
    header = header or default_header
    missing = [c for c in default_header if c not in header]
    if missing:
        raise ValueError(f"{path.name} has no {', '.join(missing)} column")
    return header, rows, [*rows, *new_rows]


def add_symbol(
    data_dir: Path, symbol: str, *, name: str | None = None,
    weights: Mapping[str, str] | None = None, as_of: str | None = None,
    replace: bool = False, no_fetch: bool = False,
) -> SymbolAdded:
    """Name ``symbol`` in ``securities.csv`` and give it one set of weights in
    ``allocations.csv``. Either may be left out; neither file is touched for
    what isn't given. Raises ``ValueError``, writing nothing, when a row would
    not load (a duplicate name, a set that doesn't add up to 100%, a second set
    for the same ``as_of``). With ``replace``, a set already written for the
    same ``as_of`` is replaced instead of refused. ``no_fetch`` marks the
    new ``securities.csv`` row (#640), with or without a name."""
    symbol = normalize_symbol(symbol)
    if as_of and not weights:
        raise ValueError("--as-of dates a set of weights; give the weights too")
    writes: list[_Write] = []

    if name or no_fetch:
        path = data_dir / SECURITIES
        row = {"symbol": symbol, "name": (name or "").strip(), **({NO_FETCH: "1"} if no_fetch else {})}
        header, old, rows = _appended(path, _SECURITY_COLUMNS, [row])
        if no_fetch and NO_FETCH not in header:
            header = [*header, NO_FETCH]
        existing = next((r for r in old if r.get("symbol") == symbol), None)
        if existing is not None:
            raise ValueError(f"{symbol} is already in {SECURITIES}, named {existing.get('name')!r}")
        issues: list[str] = []
        _validate_securities(rows, issues)
        if issues:
            raise ValueError("; ".join(issues))
        writes.append((path, header, rows))

    if weights:
        writes.append(_weights_write(data_dir, symbol, weights, as_of, replace=replace))

    _write_all(writes)
    return SymbolAdded(symbol, named=bool(name), weighted=bool(weights))



def _weights_write(
    data_dir: Path, symbol: str, weights: Mapping[str, str], as_of: str | None, *, replace: bool,
) -> _Write:
    """``allocations.csv`` with one set of weights for ``symbol`` added (or,
    with ``replace``, in place of the set for the same ``as_of``), checked as
    the loader checks it. Raises ``ValueError`` for a set that wouldn't load."""
    path = data_dir / ALLOCATIONS
    when = as_of.strip() if as_of else ""
    if when:
        try:
            month_end_iso(when)
        except ValueError:
            raise ValueError(f"--as-of {as_of!r} is not a YYYY-MM month") from None
    new = [{"symbol": symbol, "as_of": when, "asset_class": cls, "weight": w} for cls, w in weights.items()]
    header, old, rows = _appended(path, _ALLOCATION_COLUMNS, new)

    def same_set(r: dict[str, str]) -> bool:
        return r.get("symbol") == symbol and _month_key(r.get("as_of", "")) == _month_key(when)

    if any(same_set(r) for r in old):
        if not replace:
            dated = f" as of {when}" if when else " with a blank as_of"
            raise ValueError(f"{symbol} already has weights{dated} in {ALLOCATIONS}; "
                             "change them with `sim symbol edit --weight`")
        # In place of the old set, so the file's diff reads as an edit.
        at = next(i for i, r in enumerate(old) if same_set(r))
        kept = [r for r in old if not same_set(r)]
        before = sum(1 for r in old[:at] if not same_set(r))
        rows = kept[:before] + new + kept[before:]
    issues = []
    # The loader's checks read as_of in its stored month-end form.
    _validate_allocations(
        [{**r, "as_of": _month_key(r.get("as_of", ""))} for r in rows], issues,
    )
    if issues:
        raise ValueError("; ".join(issues))
    return path, header, rows


def _write_all(writes: list[_Write]) -> None:
    """Write each ``(path, header, rows)``: every file or none."""
    temps: list[tuple[Path, Path]] = []
    try:
        for path, header, rows in writes:
            temps.append((_write_atomic(path, header, rows), path))
    except BaseException:
        for tmp, _path in temps:
            tmp.unlink(missing_ok=True)
        raise
    # Both files or neither: if a later rename fails, put back what the earlier ones replaced.
    originals = [(path, path.read_bytes() if path.exists() else None) for _tmp, path in temps]
    done = 0
    try:
        for tmp, path in temps:
            os.replace(tmp, path)
            done += 1
    except BaseException:
        for path, before in originals[:done]:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(before)
        for tmp, _path in temps[done:]:
            tmp.unlink(missing_ok=True)
        raise


def fill_names(data_dir: Path, names: Mapping[str, str]) -> list[str]:
    """Give each symbol in ``names`` its name in ``securities.csv`` where it
    has none, adding its row when absent (#640). A name already there is
    kept, and CASH, CD and lot keys are never named. Returns the symbols
    named; a file that wouldn't load afterwards is left as it is."""
    path = data_dir / SECURITIES
    header, rows = _read(path)
    header = header or list(_SECURITY_COLUMNS)
    if "symbol" not in header or "name" not in header:
        return []
    by_symbol = {(r.get("symbol") or "").strip(): r for r in rows}
    named = []
    for symbol, name in sorted(names.items()):
        name = " ".join((name or "").split())
        if not name or symbol in registries.SYMBOL_ASSET_CLASS or is_lot_key(symbol):
            continue
        row = by_symbol.get(symbol)
        if row is None:
            row = by_symbol[symbol] = {"symbol": symbol, "name": ""}
            rows.append(row)
        if (row.get("name") or "").strip():
            continue
        row["name"] = name
        named.append(symbol)
    if not named:
        return []
    issues: list[str] = []
    _validate_securities(rows, issues)
    if issues:
        return []
    os.replace(_write_atomic(path, header, rows), path)
    return named


def _security_write(
    data_dir: Path, symbol: str, *, name: str | None, no_fetch: bool | None,
) -> _Write | None:
    """``securities.csv`` with ``symbol``'s name and ``no_fetch`` mark changed
    (``None`` leaves one as it is), checked as the loader checks it; None when
    nothing would change."""
    path = data_dir / SECURITIES
    header, rows = _read(path)
    header = header or list(_SECURITY_COLUMNS)
    if "name" not in header or "symbol" not in header:
        raise ValueError(f"{SECURITIES} has no symbol or name column")
    row = next((r for r in rows if r.get("symbol") == symbol), None)
    if row is None:
        if not name and not no_fetch:
            return None  # an unnamed, fetched symbol needs no row
        row = {"symbol": symbol, "name": ""}
        rows.append(row)
    before = dict(row)
    if name is not None:
        row["name"] = name.strip()
    if no_fetch is not None:
        if no_fetch and NO_FETCH not in header:
            header = [*header, NO_FETCH]
        if NO_FETCH in header:
            row[NO_FETCH] = "1" if no_fetch else "0"
    if row == before and path.exists():
        return None
    issues: list[str] = []
    _validate_securities(rows, issues)
    if issues:
        raise ValueError("; ".join(issues))
    return path, header, rows


@dataclass
class SymbolEdited:
    symbol: str
    named: bool
    """``securities.csv`` changed."""
    as_of: str | None
    """The ``as_of`` of the set of weights written (blank: every date), or
    None when no weights were given."""
    replaced: bool
    """The set replaced one already there."""


def edit_symbol(
    data_dir: Path, symbol: str, *, name: str | None = None, no_fetch: bool | None = None,
    weights: Mapping[str, str] | None = None, as_of: str | None = None,
) -> SymbolEdited:
    """Change ``symbol``: its ``securities.csv`` name (``""`` clears it) and
    ``no_fetch`` mark (#640), and, with ``weights``, a set of weights in
    ``allocations.csv`` (#191). ``None`` leaves a thing as it is.

    With ``as_of``, the set for that month is replaced, or a new dated set is
    added beside the others. Without it, the latest set is corrected in
    place, keeping its ``as_of``; a symbol with no set gets an undated one.
    Both files are written, or neither: raises ``ValueError``, writing
    nothing, when either would not load."""
    symbol = normalize_symbol(symbol)
    if as_of is not None and not weights:
        raise ValueError("--as-of dates a set of weights; give the weights too")
    writes: list[_Write] = []
    if (security := _security_write(data_dir, symbol, name=name, no_fetch=no_fetch)) is not None:
        writes.append(security)
    when, replaced = None, False
    if weights:
        if as_of is None:
            latest = latest_set(data_dir, symbol)
            as_of = latest[0] if latest else ""
        when = as_of.strip()
        replaced = has_weights(data_dir, symbol, when)
        writes.append(_weights_write(data_dir, symbol, weights, when, replace=True))
    _write_all(writes)
    return SymbolEdited(symbol, named=security is not None, as_of=when, replaced=replaced)


def holders(data_dir: Path, symbol: str) -> list[str]:
    """The ``scenario: account (lot)`` lots, in any scenario, holding
    ``symbol``, or, for an ``<account>:<lot>`` key, that are that lot."""
    if not is_lot_key(symbol):
        return _holders(data_dir / "scenarios").get(symbol, [])
    account, _, lot = symbol.partition(":")
    found = []
    for values in sorted((data_dir / "scenarios").glob("*/account_lots.csv")):
        for row in _read(values)[1]:
            if row.get("account_name") == account and (row.get("lot") or registries.DEFAULT_LOT) == lot:
                found.append(f"{values.parent.name}: {account} ({lot})")
    return found


def leftovers(data_dir: Path, symbol: str) -> list[Path]:
    """What ``delete_symbol`` leaves in place but a reader may want to know
    about: its ``research/`` folder, and the rate series named for it
    (``rate__<SYMBOL>.csv``, ``rate__<SYMBOL>.<class>.csv``; ADR-005 §5),
    which nothing reads once no lot holds it."""
    # Imported here: research pulls in the plugin machinery.
    from analysis.sim.research import RESEARCH_DIR

    found = [p for p in [data_dir / RESEARCH_DIR / symbol] if p.is_dir()]
    names = {f"rate__{symbol}.csv", *(f"rate__{symbol}.{c}.csv" for c in registries.ASSET_CLASSES)}
    found += sorted(p for p in (data_dir / "scenarios").glob("*/series/rate__*.csv") if p.name in names)
    return found


@dataclass
class DeletePlan:
    """What ``sim symbol delete`` would do to a symbol, read once."""

    symbol: str
    held: list[str]
    """The lots holding it (or, for an ``<account>:<lot>`` key, that lot):
    while there are any, it can't be deleted."""
    named: bool
    """It has a ``securities.csv`` row."""
    weight_sets: int
    """How many sets of weights it has in ``allocations.csv``."""
    prices: list[Path]
    """Its price files: the shared one, then each scenario's own."""
    left: list[Path]
    """What deleting it leaves in place (:func:`leftovers`)."""


def plan_delete(data_dir: Path, symbol: str) -> DeletePlan:
    """What deleting ``symbol`` would remove, and what stops it."""
    symbol = normalize_symbol(symbol)
    shared = price_files(data_dir / "prices").get(symbol)
    files = [*([shared] if shared else []), *_scenario_price_files(data_dir / "scenarios").get(symbol, [])]
    allocations = [r for r in _read(data_dir / ALLOCATIONS)[1] if r.get("symbol") == symbol]
    return DeletePlan(
        symbol, held=holders(data_dir, symbol),
        named=security_name(data_dir, symbol) is not None,
        weight_sets=len({_month_key(r.get("as_of", "")) for r in allocations}),
        prices=files, left=leftovers(data_dir, symbol),
    )


def delete_symbol(data_dir: Path, plan: DeletePlan, *, prices: bool = False) -> list[Path]:
    """Remove ``plan.symbol`` from the data repo (#191): its ``securities.csv``
    row, its ``allocations.csv`` rows and, with ``prices``, its price files.
    Every file or none. Raises ``ValueError``, changing nothing, while a lot
    holds it, or when that would remove nothing. Returns the price files
    removed."""
    symbol = plan.symbol
    if plan.held:
        raise ValueError(f"{symbol} is held by {len(plan.held)} lot(s); change or delete them first:\n  "
                         + "\n  ".join(plan.held))
    removed = plan.prices if prices else []
    if not plan.named and not plan.weight_sets and not removed:
        kept = "; its price files were kept" if plan.prices else ""
        raise ValueError(f"nothing to delete: {symbol} is not in {SECURITIES} or {ALLOCATIONS}{kept}")
    writes: list[_Write] = []
    for path, named in ((data_dir / SECURITIES, plan.named), (data_dir / ALLOCATIONS, plan.weight_sets)):
        if named:
            header, rows = _read(path)
            writes.append((path, header, [r for r in rows if r.get("symbol") != symbol]))
    # Move the price files aside first, so a file that can't be removed stops
    # the delete before either CSV changes, and a failed write puts them back.
    staged: list[tuple[Path, Path]] = []
    try:
        for path in removed:
            aside = path.with_name(f".{path.name}.deleting")
            os.replace(path, aside)
            staged.append((path, aside))
        _write_all(writes)
    except BaseException:
        for path, aside in staged:
            os.replace(aside, path)
        raise
    for _path, aside in staged:
        aside.unlink(missing_ok=True)
    return removed
