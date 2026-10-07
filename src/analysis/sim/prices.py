"""Fetching security prices into ``prices/security_price__<SYMBOL>.csv`` (ADR-005 §6).

Prices are only used to derive a lot's opening balance (``quantity`` x the latest
price on or before the scenario start), so what is stored is a sparse history:
the last close of each month from the December before the earliest scenario start's year,
plus the most recent close. Files are merged, never rewritten: a date already in
a file keeps its price unless ``refresh`` is set. Only ticker symbols leave the
machine.
"""

from __future__ import annotations

import csv
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pandas as pd

from analysis.sim.dates import parse_month
from analysis.frames import named_rows
from analysis.tls import ssl_context, verify_reason

if TYPE_CHECKING:
    from analysis.sim.connection import ScenarioConnection

__all__ = [
    "FetchResult", "PRICE_PROVIDERS", "PriceProvider", "PriceUse", "YahooProvider",
    "check_prices", "default_since", "fetch_prices", "format_price", "merge_price_rows",
    "month_end_closes", "prices_in_use",
]

PRICE_COLUMNS = ("effective_date", "price")
NO_PRICE_SYMBOLS = frozenset({"CASH"})  # dollar-denominated: nothing to look up


class PriceError(Exception):
    """A provider could not supply prices for a symbol."""


class PriceProvider(Protocol):
    name: str

    def closes(self, symbol: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        """Unadjusted daily closes for ``symbol`` from ``start`` to ``end``, oldest first."""


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (analysis sim)"})
    with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as response:
        return response.read()


class YahooProvider:
    """Daily closes from Yahoo Finance's public chart endpoint (no account or key)."""

    name = "yahoo"
    _URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={p1}&period2={p2}&interval=1d"

    def __init__(self, http_get: Callable[[str], bytes] = _http_get) -> None:
        self._get = http_get

    def closes(self, symbol: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp())
        p2 = int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp()) + 86_400
        url = self._URL.format(symbol=urllib.parse.quote(symbol, safe=""), p1=p1, p2=p2)
        try:
            payload = json.loads(self._get(url))
        except urllib.error.HTTPError as exc:
            raise PriceError(
                "not found at Yahoo" if exc.code == 404 else f"Yahoo returned HTTP {exc.code}"
            ) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise PriceError(
                    f"could not verify Yahoo's HTTPS certificate: {verify_reason(exc.reason)}"
                ) from None
            raise PriceError(f"could not reach Yahoo ({exc.reason})") from None
        except (TimeoutError, json.JSONDecodeError) as exc:
            raise PriceError(f"could not reach Yahoo ({exc})") from None
        chart = payload.get("chart", {})
        if chart.get("error"):
            raise PriceError(chart["error"].get("description") or "Yahoo reported an error")
        results = chart.get("result") or []
        if not results:
            raise PriceError("no prices returned")
        if not results[0].get("timestamp"):
            last = results[0].get("meta", {}).get("regularMarketTime")
            if last:
                when = datetime.fromtimestamp(last, UTC).date()
                raise PriceError(
                    f"no prices since {start} (last traded {when}; the fund may be closed or merged)"
                )
            raise PriceError("no prices returned")
        result = results[0]
        offset = result.get("meta", {}).get("gmtoffset", 0)
        closes = result["indicators"]["quote"][0]["close"]
        out = []
        for stamp, close in zip(result["timestamp"], closes, strict=False):
            if close is None:
                continue
            day = datetime.fromtimestamp(stamp + offset, UTC).date()
            out.append((day, Decimal(repr(round(close, 4)))))  # Yahoo's float noise is past 4 places
        return out


PRICE_PROVIDERS: dict[str, Callable[[], PriceProvider]] = {"yahoo": YahooProvider}


def format_price(price: Decimal) -> str:
    """Decimal dollars with at least two and at most six places."""
    text = f"{price.quantize(Decimal('0.000001')):f}".rstrip("0")
    whole, _, cents = text.partition(".")
    return f"{whole}.{cents.ljust(2, '0')}"


def month_end_closes(closes: Iterable[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    """The last close of each calendar month; the final month's is the latest close."""
    last: dict[tuple[int, int], tuple[date, Decimal]] = {}
    for day, price in sorted(closes):
        last[(day.year, day.month)] = (day, price)
    return [last[key] for key in sorted(last)]


def merge_price_rows(
    existing: dict[str, str], fetched: Iterable[tuple[date, Decimal]], *, refresh: bool = False,
) -> tuple[dict[str, str], int, int]:
    """Merge fetched closes into a file's rows (date to price text). Returns the
    merged rows and the counts added and changed."""
    merged = dict(existing)
    added = changed = 0
    for day, price in fetched:
        key, text = day.isoformat(), format_price(price)
        if key not in merged:
            merged[key] = text
            added += 1
        elif refresh and Decimal(merged[key]) != price:
            merged[key] = text
            changed += 1
    return merged, added, changed


def _read_price_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {r["effective_date"]: r["price"] for r in csv.DictReader(fh) if r.get("effective_date")}


def _write_price_file(path: Path, rows: dict[str, str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(PRICE_COLUMNS)
        writer.writerows(sorted(rows.items()))


@dataclass
class PriceUse:
    """Which symbols the scenarios hold with a quantity, and how far back prices are needed."""
    symbols: list[str] = field(default_factory=list)
    earliest_start: date | None = None


def prices_in_use(scenarios_dir: Path, scenario: str | None = None) -> PriceUse:
    """Symbols on lots that have a quantity, across the scenarios (or one), and
    the earliest scenario start. A derived scenario's blank start is skipped."""
    names = [scenario] if scenario else sorted(p.name for p in scenarios_dir.iterdir() if p.is_dir())
    symbols: set[str] = set()
    starts: list[date] = []
    for name in names:
        folder = scenarios_dir / name
        values = folder / "account_values.csv"
        if values.exists():
            with values.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    symbol = (row.get("symbol") or "").strip()
                    if symbol and (row.get("quantity") or "").strip() and symbol not in NO_PRICE_SYMBOLS:
                        symbols.add(symbol)
        meta = folder / "scenario.csv"
        if meta.exists():
            with meta.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if (row.get("start") or "").strip():
                        starts.append(parse_month(row["start"]))
    return PriceUse(sorted(symbols), min(starts) if starts else None)


@dataclass
class FetchResult:
    symbol: str
    added: int = 0
    changed: int = 0
    latest: tuple[date, Decimal] | None = None
    error: str | None = None


def fetch_prices(
    aux_dir: Path, symbols: Iterable[str], provider: PriceProvider, *,
    since: date, today: date | None = None, refresh: bool = False, dry_run: bool = False,
) -> list[FetchResult]:
    """Fetch month-end (and latest) closes for each symbol into its prices file."""
    today = today or datetime.now(UTC).date()
    results = []
    for symbol in symbols:
        result = FetchResult(symbol)
        results.append(result)
        try:
            closes = provider.closes(symbol, since, today)
        except PriceError as exc:
            result.error = str(exc)
            continue
        rows = month_end_closes(closes)
        if not rows:
            result.error = "no prices in the period"
            continue
        result.latest = rows[-1]
        path = aux_dir / f"security_price__{symbol}.csv"
        merged, result.added, result.changed = merge_price_rows(
            _read_price_file(path), rows, refresh=refresh,
        )
        if not dry_run and (result.added or result.changed):
            aux_dir.mkdir(parents=True, exist_ok=True)
            _write_price_file(path, merged)
    return results


def default_since(earliest_start: date | None, today: date) -> date:
    """From the December before the earliest scenario start's year, so the closes
    on or before that start and before the prior year-end (which a lot's
    ``prior_year_end_balance`` can be derived from) are included."""
    anchor = earliest_start or today
    return date(anchor.year - 1, 12, 1)


CHECK_COLUMNS = (
    "account", "lot", "symbol", "quantity", "balance", "price_date", "price",
    "value", "difference", "latest_date", "latest_price", "latest_value", "flag",
)


def check_prices(source: ScenarioConnection, tolerance: float = 0.01) -> pd.DataFrame:
    """Per lot with a quantity and a symbol: the stated ``opening_balance`` against
    ``quantity`` x the price on or before the scenario start (``value``, ``difference``
    as a fraction of the balance) and against the latest price (``latest_value``).
    ``flag`` says why a lot needs a look: ``stale`` when the value at the start price
    is off by more than ``tolerance``, ``no price`` when the symbol has no price file.
    A lot whose balance was derived from a price agrees by construction."""
    start = str(source.items("scenario")["start"].iloc[0])
    aux = source.items("aux_table")
    aux = aux[aux["kind"] == "security_price"]
    history: dict[str, list[tuple[str, float]]] = {}
    for row in named_rows(aux):
        history.setdefault(row.series, []).append(
            (row.effective_date, json.loads(row.payload_json)["price_micros"] / 1_000_000)
        )
    rows = []
    for lot in named_rows(source.items("account_model_member")):
        if not (isinstance(lot.symbol, str) and lot.symbol and pd.notna(lot.quantity_micros)):
            continue
        quantity = lot.quantity_micros / 1_000_000
        balance = lot.opening_balance / 100
        prices = sorted(history.get(lot.symbol, []))
        at_start = [p for p in prices if p[0] <= start]
        row = dict.fromkeys(CHECK_COLUMNS)
        row.update(account=lot.account, lot=lot.lot, symbol=lot.symbol,
                   quantity=quantity, balance=balance)
        if prices:
            row.update(latest_date=prices[-1][0], latest_price=prices[-1][1],
                       latest_value=quantity * prices[-1][1])
        if at_start:
            row.update(price_date=at_start[-1][0], price=at_start[-1][1],
                       value=quantity * at_start[-1][1])
            if balance:
                row["difference"] = (row["value"] - balance) / balance
                if abs(row["difference"]) > tolerance:
                    row["flag"] = "stale"
        else:
            row["flag"] = "no price"
        rows.append(row)
    return pd.DataFrame(rows, columns=list(CHECK_COLUMNS))
