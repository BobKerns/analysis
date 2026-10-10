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
from analysis.sim.dates import month_end_iso
from analysis.sim.loader import _validate_allocations, _validate_securities

__all__ = ["SymbolAdded", "add_symbol", "parse_weights", "symbol_table"]

SECURITIES = "securities.csv"
ALLOCATIONS = "allocations.csv"
_SECURITY_COLUMNS = ["symbol", "name"]
_ALLOCATION_COLUMNS = ["symbol", "as_of", "asset_class", "weight"]
_PRICE_PREFIX = "security_price__"


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


def _price_files(prices_dir: Path) -> dict[str, Path]:
    if not prices_dir.is_dir():
        return {}
    return {
        path.stem.removeprefix(_PRICE_PREFIX): path
        for path in prices_dir.glob(f"{_PRICE_PREFIX}*.csv")
    }


def _holders(scenarios_dir: Path) -> dict[str, list[str]]:
    """Symbol to the ``scenario: account (lot)`` rows that name it, as each
    scenario's own ``account_values.csv`` writes them (an inherited lot is
    listed once, where it is defined)."""
    held: dict[str, list[str]] = {}
    if not scenarios_dir.is_dir():
        return held
    for values in sorted(scenarios_dir.glob("*/account_values.csv")):
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


def symbol_table(data_dir: Path) -> pd.DataFrame:
    """One row per symbol named anywhere: its name, latest weights, prices and
    holders. Columns: ``symbol``, ``name``, ``allocation``, ``allocation_sets``,
    ``prices``, ``latest_price``, ``latest_date``, ``held_by``."""
    names = {row["symbol"]: row.get("name", "") for row in _read(data_dir / SECURITIES)[1] if row.get("symbol")}
    weights = _latest_weights([r for r in _read(data_dir / ALLOCATIONS)[1] if r.get("symbol")])
    prices = _price_files(data_dir / "prices")
    holders = _holders(data_dir / "scenarios")
    records = []
    for symbol in sorted({*names, *weights, *prices, *holders}):
        allocation, sets = "", 0
        if symbol in weights:
            as_of, classes, sets = weights[symbol]
            allocation = ", ".join(f"{cls} {weight}" for cls, weight in classes)
            if as_of:
                allocation += f" (as of {as_of})"
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
            "held_by": holders.get(symbol, []),
        })
    columns = ["symbol", "name", "allocation", "allocation_sets", "prices",
               "latest_price", "latest_date", "held_by"]
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
                f"unknown asset class {asset_class!r}; allowed: {', '.join(registries.ASSET_CLASSES)}"
            )
        if asset_class in weights:
            raise ValueError(f"asset class {asset_class} is given twice")
        micros = units.percent_to_micros(percent, require_sign=False)
        weights[asset_class] = units.micros_to_percent(micros)
    return weights


@dataclass
class SymbolAdded:
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
) -> SymbolAdded:
    """Name ``symbol`` in ``securities.csv`` and give it one set of weights in
    ``allocations.csv``. Either may be left out; neither file is touched for
    what isn't given. Raises ``ValueError``, writing nothing, when a row would
    not load (a duplicate name, a set that doesn't add up to 100%, a second set
    for the same ``as_of``)."""
    symbol = symbol.strip()
    if not symbol or any(ch.isspace() for ch in symbol) or "/" in symbol:
        # It becomes part of a filename, prices/security_price__<SYMBOL>.csv.
        raise ValueError(f"symbol {symbol!r} must be one word, with no spaces or '/'")
    if as_of and not weights:
        raise ValueError("--as-of dates a set of weights; give the weights too")
    writes: list[tuple[Path, list[str], list[dict[str, str]]]] = []

    if name:
        path = data_dir / SECURITIES
        header, old, rows = _appended(path, _SECURITY_COLUMNS, [{"symbol": symbol, "name": name.strip()}])
        existing = next((r for r in old if r.get("symbol") == symbol), None)
        if existing is not None:
            raise ValueError(f"{symbol} is already in {SECURITIES}, named {existing.get('name')!r}")
        issues: list[str] = []
        _validate_securities(rows, issues)
        if issues:
            raise ValueError("; ".join(issues))
        writes.append((path, header, rows))

    if weights:
        path = data_dir / ALLOCATIONS
        when = as_of.strip() if as_of else ""
        if when:
            try:
                month_end_iso(when)
            except ValueError:
                raise ValueError(f"--as-of {as_of!r} is not a YYYY-MM month") from None
        new = [{"symbol": symbol, "as_of": when, "asset_class": cls, "weight": w} for cls, w in weights.items()]
        header, old, rows = _appended(path, _ALLOCATION_COLUMNS, new)
        if any(r.get("symbol") == symbol and _month_key(r.get("as_of", "")) == _month_key(when) for r in old):
            dated = f" as of {when}" if when else " with a blank as_of"
            raise ValueError(f"{symbol} already has weights{dated} in {ALLOCATIONS}; edit that file to change them")
        issues = []
        # The loader's checks read as_of in its stored month-end form.
        _validate_allocations(
            [{**r, "as_of": _month_key(r.get("as_of", ""))} for r in rows], issues,
        )
        if issues:
            raise ValueError("; ".join(issues))
        writes.append((path, header, rows))

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
    return SymbolAdded(named=bool(name), weighted=bool(weights))
