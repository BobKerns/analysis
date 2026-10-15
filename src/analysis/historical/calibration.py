"""Download public price and return series and summarize them for calibrating
an ensemble's varying rates (docs/guides/historical-calibration.md).

For each series and each period, ``analysis historical`` reports the statistics a varying
`set-rate` row needs: the compound (geometric) mean rate, median, SD, range,
year-to-year persistence and the yearly shock a ``vary_process`` row needs,
and the correlation with a reference series, both
Pearson and the copula rho implied by Kendall's tau. ``--window N`` summarizes
rolling N-year compound rates instead of single years, which is the spread of
a rate held for N years. ``--runs-above PCT`` lists the stretches of
consecutive years above a threshold: the episodes, with their start years and
lengths.

Series are named by source:

- ``bls:CUUR0000SAM`` (or a bare BLS id): any BLS series from the LABSTAT
  flat files, e.g. CPI-U (``CU``), CPI-W (``CW``) and chained CPI (``SU``).
- ``rcpie:Medical care``: a column of BLS's R-CPI-E research spreadsheet
  (CPI for Americans 62 and older), from December 1982.
- ``damodaran:tbill``: a column of Damodaran's annual returns since 1928
  (``sp500``, ``small_cap``, ``tbill``, ``tbond``, ``baa_corp``,
  ``real_estate``, ``gold``).
- ``bea:DPCERG``: a monthly NIPA level series from BEA's flat files, e.g.
  the PCE price index (``DPCERG``) and core PCE (``DPCCRG``).
- ``fred:PCEPI``: a FRED series, through its keyless CSV download.
- ``csv:path/to/file.csv``: a two-column CSV, ``date,value`` for an index
  level or ``date,rate`` for a rate already in percent.

``LABEL=SOURCE`` names a series in the output; a ``@rate`` or ``@index``
suffix overrides whether its values are levels (rates are computed from
them) or rates in percent. Downloads are cached under the repository's
``historical_data/`` with their retrieval date, so the committed copies are
used offline and a new series is captured there.

BLS refuses requests without contact details, so the User-Agent carries an
email address: ``--contact``, ``$HISTORICAL_CALIBRATION_CONTACT``, or
``git config user.email``.

Example::

    uv run analysis historical --reference bls:CUUR0000SA0L1E \\
        --period "Great Inflation=1966-1982" \\
        CPI-U=bls:CUUR0000SA0 Medical=bls:CUUR0000SAM Shelter=bls:CUUR0000SAH1
"""

from __future__ import annotations

# cspell:ignore CUUR CWUR SUUR SEEB DPCCRG DPCERG DPCERGM PCEPI histret Asize

import csv
import dataclasses
import html
import io
import math
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import pandas as pd
from scipy import stats

from analysis.frames import datetime_index, index_months, index_years
from analysis.tls import ssl_context, verify_reason

# The committed data beside the source tree; an editable install (uv sync)
# finds it, while a wheel installed elsewhere has none and needs --cache.
DEFAULT_CACHE = Path(__file__).resolve().parents[3] / "historical_data"

BLS_BASE = "https://download.bls.gov/pub/time.series"
RCPIE_URL = "https://www.bls.gov/cpi/research-series/r-cpi-e-data.xlsx"
DAMODARAN_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/histretSP.html"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
BEA_BASE = "https://apps.bea.gov/national/Release/TXT"

# Column order of Damodaran's annual-returns table, checked against its
# header so a reshuffled page fails loudly instead of mislabeling columns.
DAMODARAN_COLUMNS = {
    "sp500": "S&P 500 (includes dividends)",
    "small_cap": "US Small cap (bottom decile)",
    "tbill": "3-month T.Bill",
    "tbond": "US T. Bond (10-year)",
    "baa_corp": "Baa Corporate Bond",
    "real_estate": "Real Estate",
    "gold": "Gold",
}

# The episodes the reference doc tabulates, by the calendar year each annual
# rate ends in. The calm baseline starts in 1984, the first December-to-
# December year wholly after CPI-U switched to rental equivalence for owners'
# housing in January 1983.
DEFAULT_PERIODS = {
    "Great Inflation": (1966, 1982),
    "Calm baseline": (1984, 2007),
    "Financial crisis and aftermath": (2008, 2019),
    "Tariffs 2018-2019": (2018, 2019),
    "Post-pandemic": (2021, 2023),
    "Tariffs 2025-": (2025, None),
    "Full history": (None, None),
}


class DataError(Exception):
    """A source couldn't be fetched or didn't have the expected shape."""


@dataclass
class Series:
    """One input series: its values, and whether they are levels or rates."""

    label: str
    spec: str
    # "index": price levels (rates are computed from them); "rate": percent.
    kind: str
    # Indexed by period start: month starts for monthly data, Jan 1 for annual.
    values: pd.Series
    monthly: bool
    title: str = ""
    # BLS publishes its own annual averages (period M13); prefer them.
    annual_average: pd.Series | None = None


# ---------------------------------------------------------------- fetching


class Fetcher:
    """Downloads into a cache directory, recording each file's retrieval date."""

    def __init__(self, cache: Path, contact: str | None, refresh: bool = False):
        self.cache = cache
        self.contact = contact
        self.refresh = refresh
        self.retrieved: dict[str, str] = {}

    def get(self, url: str, name: str) -> bytes:
        path = self.cache / name
        stamp = path.with_name(path.name + ".retrieved")
        if self.refresh or not path.exists():
            data = self._download(url)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            stamp.write_text(datetime.now(UTC).date().isoformat() + "\n")
        self.retrieved[url] = stamp.read_text().strip() if stamp.exists() else "unknown"
        return path.read_bytes()

    def retrieved_by_place(self) -> dict[str, str]:
        """Retrieval dates by directory, oldest first when files differ, so a
        survey's dozen data files cite as one source."""
        places: dict[str, str] = {}
        for url, day in self.retrieved.items():
            place = url.rsplit("/", 1)[0] + "/" if "?" not in url else url
            places[place] = min(day, places.get(place, day))
        return places

    def _download(self, url: str) -> bytes:
        # Only BLS gets a custom User-Agent: it refuses requests without a
        # contact address, while FRED's bot filter silently drops unfamiliar
        # agents and accepts urllib's default one.
        headers = {}
        if (urllib.parse.urlparse(url).hostname or "").endswith("bls.gov"):
            if not self.contact:
                raise DataError(
                    "no contact email for the User-Agent (BLS refuses requests without one): "
                    "pass --contact, set $HISTORICAL_CALIBRATION_CONTACT, or set git's user.email"
                )
            headers["User-Agent"] = f"analysis historical (contact: {self.contact})"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120, context=ssl_context()) as response:
                return response.read()
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise DataError(f"couldn't verify the HTTPS certificate for {url}: "
                                f"{verify_reason(exc.reason)}") from exc
            raise DataError(f"couldn't download {url}: {exc}") from exc
        except OSError as exc:
            raise DataError(f"couldn't download {url}: {exc}") from exc


def default_contact() -> str | None:
    if contact := os.environ.get("HISTORICAL_CALIBRATION_CONTACT"):
        return contact
    try:
        out = subprocess.run(
            ["git", "config", "user.email"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return out.stdout.strip() or None


# ------------------------------------------------------------------- BLS


def parse_bls_data(text: str, wanted: set[str]) -> dict[str, list[tuple[int, str, float]]]:
    """Rows of a LABSTAT ``<survey>.data.*`` file for the wanted series ids.

    Missing observations (``-``, as in October 2025 when collection lapsed)
    are skipped rather than read as zero."""
    found: dict[str, list[tuple[int, str, float]]] = {}
    for line in text.splitlines()[1:]:
        fields = [f.strip() for f in line.split("\t")]
        if len(fields) < 4 or fields[0] not in wanted:
            continue
        try:
            value = float(fields[3])
        except ValueError:
            continue
        found.setdefault(fields[0], []).append((int(fields[1]), fields[2], value))
    return found


def bls_data_files(listing: str, survey: str) -> list[str]:
    """The survey's data files in search order, from its directory listing.

    ``data.0.Current`` holds only recent years, so it's skipped; the
    national files come first, in BLS's order (all items, summaries, then
    each expenditure group)."""
    names = sorted(set(re.findall(rf"{survey}\.data\.(\d+)\.(\w+)", listing)),
                   key=lambda m: (_is_regional(m[1]), int(m[0])))
    return [f"{survey}.data.{n}.{name}" for n, name in names if n != "0"]


def _is_regional(name: str) -> bool:
    # CPI's regional files (AsizeNorthEast, OtherWest, ...) hold no national
    # series, so they're searched last and usually never downloaded.
    return name.startswith(("Asize", "Other"))


def bls_titles(text: str) -> dict[str, str]:
    lines = text.splitlines()
    header = [h.strip() for h in lines[0].split("\t")]
    col = header.index("series_title")
    return {
        fields[0].strip(): fields[col].strip()
        for fields in (line.split("\t") for line in lines[1:])
        if len(fields) > col
    }


def load_bls(fetcher: Fetcher, ids: list[str]) -> dict[str, Series]:
    by_survey: dict[str, list[str]] = {}
    for sid in ids:
        by_survey.setdefault(sid[:2].lower(), []).append(sid)
    result: dict[str, Series] = {}
    for survey, sids in by_survey.items():
        base = f"{BLS_BASE}/{survey}"
        listing = fetcher.get(f"{base}/", f"bls/{survey}.listing.html").decode("latin-1")
        titles = bls_titles(fetcher.get(f"{base}/{survey}.series", f"bls/{survey}.series").decode("latin-1"))
        unknown = [s for s in sids if s not in titles]
        if unknown:
            raise DataError(f"BLS survey {survey!r} has no series {', '.join(unknown)}")
        rows: dict[str, list[tuple[int, str, float]]] = {}
        for name in bls_data_files(listing, survey):
            missing = set(sids) - set(rows)
            if not missing:
                break
            text = fetcher.get(f"{base}/{name}", f"bls/{name}").decode("latin-1")
            rows.update(parse_bls_data(text, missing))
        for sid in sids:
            if sid not in rows:
                raise DataError(f"BLS series {sid} wasn't in any {survey}.data file")
            monthly = {date(y, int(p[1:]), 1): v for y, p, v in rows[sid] if p.startswith("M") and p != "M13"}
            # M13 is a monthly series' annual average; A01 is an annual-only
            # series' value.
            annual = {date(y, 1, 1): v for y, p, v in rows[sid] if p in ("M13", "A01")}
            if not monthly and not annual:
                periods = sorted({p for _, p, _ in rows[sid]})
                raise DataError(f"BLS series {sid} has only {', '.join(periods)} periods; "
                                "only monthly and annual series are supported")
            result[sid] = Series(
                label=sid, spec=f"bls:{sid}", kind="index", monthly=bool(monthly),
                values=_series(monthly or annual), title=titles[sid],
                annual_average=_series(annual) if monthly and annual else None,
            )
    return result


def _series(points: dict[date, float]) -> pd.Series:
    s = pd.Series(points, dtype=float)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


# ---------------------------------------------------------------- R-CPI-E

_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_xlsx_rows(data: bytes, sheet: int = 1) -> list[dict[str, str]]:
    """A worksheet's cells by column letter, read with the standard library
    so reading it needs no Excel package."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{{{_XLSX_NS['m']}}}t")) for si in root]
        root = ElementTree.fromstring(z.read(f"xl/worksheets/sheet{sheet}.xml"))
    rows = []
    for row in root.iter(f"{{{_XLSX_NS['m']}}}row"):
        cells = {}
        for c in row.findall("m:c", _XLSX_NS):
            v = c.find("m:v", _XLSX_NS)
            if v is None or v.text is None:
                continue
            ref = re.match(r"[A-Z]+", c.get("r", ""))
            if ref is None:
                raise DataError(f"spreadsheet cell without a cell reference: {c.attrib}")
            column = ref.group(0)
            cells[column] = shared[int(v.text)] if c.get("t") == "s" else v.text
        rows.append(cells)
    return rows


def parse_rcpie(data: bytes) -> dict[str, pd.Series]:
    """R-CPI-E's monthly indexes by column name (Table 1: all items and the
    major groups). Dates are Excel serial days; a blank cell is a month BLS
    didn't publish."""
    rows = read_xlsx_rows(data)
    header_at = next((i for i, r in enumerate(rows) if r.get("A") == "Month"), None)
    if header_at is None:
        raise DataError("R-CPI-E spreadsheet has no 'Month' header row")
    names = {col: re.sub(r"\s*\(\d+\)$", "", name).strip() for col, name in rows[header_at].items() if col != "A"}
    points: dict[str, dict[date, float]] = {n: {} for n in names.values()}
    for row in rows[header_at + 1:]:
        try:
            serial = float(row.get("A", ""))
        except ValueError:
            if points["All items"]:
                break  # past Table 1's data
            continue
        day = date(1899, 12, 30) + timedelta(days=serial)
        for col, name in names.items():
            try:
                points[name][day.replace(day=1)] = float(row[col])
            except (KeyError, ValueError):
                continue  # blank or "--": not published that month
    return {name: _series(p) for name, p in points.items() if p}


def load_rcpie(fetcher: Fetcher, column: str) -> Series:
    table = parse_rcpie(fetcher.get(RCPIE_URL, "bls/r-cpi-e-data.xlsx"))
    if column not in table:
        raise DataError(f"R-CPI-E has no column {column!r}; it has {', '.join(table)}")
    return Series(label=f"R-CPI-E {column}", spec=f"rcpie:{column}", kind="index",
                  values=table[column], monthly=True,
                  title=f"R-CPI-E, {column}, Americans 62 and older, Dec 1982=100")


# -------------------------------------------------------------- Damodaran


def parse_damodaran(text: str) -> dict[str, pd.Series]:
    """Annual returns (percent) by column from Damodaran's histretSP page."""
    flat = html.unescape(re.sub(r"<[^>]+>", " ", text))
    flat = re.sub(r"\s+", " ", flat)
    pct = r"(-?\d+(?:\.\d+)?)%"
    pattern = re.compile(r"\b(19\d\d|20\d\d) " + " ".join([pct] * len(DAMODARAN_COLUMNS)))
    first = pattern.search(flat)
    if first is None:
        raise DataError("found no annual returns on Damodaran's page")
    header = flat[: first.start()]
    positions = [header.find(title) for title in DAMODARAN_COLUMNS.values()]
    if -1 in positions or positions != sorted(positions):
        raise DataError("Damodaran's returns table header has changed; check DAMODARAN_COLUMNS")
    columns: dict[str, dict[date, float]] = {k: {} for k in DAMODARAN_COLUMNS}
    for m in pattern.finditer(flat):
        for key, value in zip(DAMODARAN_COLUMNS, m.groups()[1:], strict=True):
            columns[key][date(int(m.group(1)), 1, 1)] = float(value)
    return {k: _series(v) for k, v in columns.items()}


def load_damodaran(fetcher: Fetcher, column: str) -> Series:
    if column not in DAMODARAN_COLUMNS:
        raise DataError(f"Damodaran has no column {column!r}; use one of {', '.join(DAMODARAN_COLUMNS)}")
    table = parse_damodaran(fetcher.get(DAMODARAN_URL, "damodaran/histretSP.html").decode("latin-1"))
    return Series(label=column, spec=f"damodaran:{column}", kind="rate", values=table[column],
                  monthly=False, title=f"Damodaran annual return, {DAMODARAN_COLUMNS[column]}")


# -------------------------------------------------------------------- BEA


def parse_bea_register(text: str) -> dict[str, tuple[str, str, str]]:
    """BEA's SeriesRegister: code -> (label, metric, calculation type)."""
    frame = pd.read_csv(io.StringIO(text), dtype=str)
    return {row.iloc[0]: (row["SeriesLabel"], row["MetricName"], row["CalculationType"])
            for _, row in frame.iterrows()}


def parse_bea_monthly(text: str, wanted: set[str]) -> dict[str, pd.Series]:
    """Rows of BEA's ``NipaDataM.txt`` (``code,1959M01,"1,234.5"``) for the
    wanted series codes."""
    points: dict[str, dict[date, float]] = {}
    for line in text.splitlines()[1:]:
        code, _, rest = line.partition(",")
        if code not in wanted:
            continue
        period, _, value = rest.partition(",")
        m = re.fullmatch(r"(\d{4})M(\d\d)", period)
        if m:
            points.setdefault(code, {})[date(int(m[1]), int(m[2]), 1)] = float(value.strip('"').replace(",", ""))
    return {code: _series(p) for code, p in points.items()}


def load_bea(fetcher: Fetcher, codes: list[str]) -> dict[str, Series]:
    if not codes:
        return {}
    register = parse_bea_register(fetcher.get(f"{BEA_BASE}/SeriesRegister.txt", "bea/SeriesRegister.txt").decode())
    for code in codes:
        if code not in register:
            raise DataError(f"BEA has no series {code}")
        if register[code][2] != "Level":
            # A "Percent change" series is already a monthly annualized rate,
            # which isn't the annual rate this script derives; use the level.
            raise DataError(f"BEA series {code} is a {register[code][2]!r} series; use its Level series")
    data = parse_bea_monthly(fetcher.get(f"{BEA_BASE}/NipaDataM.txt", "bea/NipaDataM.txt").decode(), set(codes))
    out = {}
    for code in codes:
        if code not in data:
            raise DataError(f"BEA series {code} has no monthly data")
        label, metric, _ = register[code]
        out[code] = Series(label=code, spec=f"bea:{code}", kind="index", values=data[code], monthly=True,
                           title=f"BEA {label}, {metric}")
    return out


# ------------------------------------------------------------ FRED and CSV


def parse_two_column_csv(text: str) -> tuple[str, pd.Series]:
    """``date,value`` or ``date,rate`` (FRED's ``observation_date,ID`` reads
    as a level). Returns the kind and the series; FRED's ``.`` for a missing
    observation is dropped."""
    try:
        frame = pd.read_csv(io.StringIO(text), na_values=["."])
        if frame.shape[1] != 2:
            raise DataError(f"expected two columns, got {list(frame.columns)}")
        kind = "rate" if frame.columns[1].strip().lower() == "rate" else "index"
        s = pd.Series(frame.iloc[:, 1].to_numpy(dtype=float), index=pd.to_datetime(frame.iloc[:, 0])).dropna()
    except (ValueError, pd.errors.ParserError) as exc:
        raise DataError(f"malformed CSV: {exc}") from exc
    return kind, s.sort_index()


def to_monthly(kind: str, values: pd.Series) -> tuple[pd.Series, bool]:
    """Annual data as is; anything more frequent becomes one value per month.

    Weekly or daily data (a mortgage-rate survey, daily yields) is reduced to
    monthly first: a rate to its monthly mean, a level to the month's last
    value, the way BLS and BEA monthly series already are. Returns the values
    and whether they're monthly."""
    if len(values) < 2 or not pd.Series(index_years(values)).duplicated().any():
        return values, False
    # Quarterly data would leave most months empty, and annual_rates() would
    # then drop every year as incomplete; refuse it rather than print nothing.
    if values.index.to_series().diff().median() > pd.Timedelta(days=45):
        raise DataError("data is less frequent than monthly (quarterly?); reduce it to one row per year")
    by_month = values.groupby(datetime_index(values).to_period("M"))
    monthly = by_month.mean() if kind == "rate" else by_month.last()
    monthly.index = pd.PeriodIndex(monthly.index).to_timestamp()
    return monthly, True


def load_csv_like(spec: str, text: str, label: str, title: str, kind: str | None = None) -> Series:
    """``kind`` overrides the header's before resampling, since a rate and a
    level reduce to monthly differently."""
    header_kind, values = parse_two_column_csv(text)
    kind = kind or header_kind
    values, monthly = to_monthly(kind, values)
    return Series(label=label, spec=spec, kind=kind, values=values, monthly=monthly, title=title)


# ------------------------------------------------------------------ specs


def parse_spec(arg: str) -> tuple[str, str, str, str | None]:
    """``[LABEL=]SOURCE:ID[@kind]`` -> (label, source, id, kind override)."""
    label, _, spec = arg.partition("=") if "=" in arg.split(":", 1)[0] else ("", "", arg)
    kind = None
    if spec.endswith(("@rate", "@index")):
        spec, _, kind = spec.rpartition("@")
    source, sep, ident = spec.partition(":")
    if not sep:
        source, ident = "bls", spec
    if source not in {"bls", "bea", "rcpie", "damodaran", "fred", "csv"}:
        raise DataError(f"unknown source {source!r} in {arg!r}")
    return label or ident, source, ident, kind


def load_series(fetcher: Fetcher, args: list[str]) -> list[Series]:
    parsed = [parse_spec(a) for a in args]
    bls = load_bls(fetcher, sorted({i for _, src, i, _ in parsed if src == "bls"}))
    bea = load_bea(fetcher, sorted({i for _, src, i, _ in parsed if src == "bea"}))
    out = []
    for label, source, ident, kind in parsed:
        if source == "bls":
            s = bls[ident]
        elif source == "bea":
            s = bea[ident]
        elif source == "rcpie":
            s = load_rcpie(fetcher, ident)
        elif source == "damodaran":
            s = load_damodaran(fetcher, ident)
        elif source == "fred":
            url = FRED_URL.format(ident)
            text = fetcher.get(url, f"fred/{ident}.csv").decode()
            s = load_csv_like(f"fred:{ident}", text, ident, f"FRED {ident}", kind)
        else:
            try:
                text = Path(ident).read_text()
            except OSError as exc:
                raise DataError(f"couldn't read {ident}: {exc}") from exc
            s = load_csv_like(f"csv:{ident}", text, Path(ident).stem, ident, kind)
        # A copy, since a BLS or BEA series used twice (say, also as the
        # reference) is one shared object.
        out.append(dataclasses.replace(s, label=label, kind=kind or s.kind))
    return out


# ------------------------------------------------------------------ rates


def annual_rates(s: Series, basis: str = "dec") -> pd.Series:
    """Percent rate for each calendar year, indexed by year.

    A level's rate is December over the previous December (``dec``, BLS's
    headline convention) or annual average over annual average
    (``average``). A monthly rate (a yield) is its annual average; an annual
    rate is used as is. A year missing a needed month is dropped, not
    guessed."""
    v = s.values
    years = index_years(v)
    if s.kind == "rate":
        if not s.monthly:
            return pd.Series(v.to_numpy(), index=years, dtype=float)
        by_year = v.groupby(years)
        means = by_year.mean()[by_year.count() == 12]
        return means.astype(float)
    if not s.monthly:
        levels = pd.Series(v.to_numpy(), index=years)
    elif basis == "dec":
        dec = v[index_months(v) == 12]
        levels = pd.Series(dec.to_numpy(), index=index_years(dec))
    else:
        by_year = v.groupby(years)
        levels = by_year.mean()[by_year.count() == 12]
        if s.annual_average is not None:
            published = s.annual_average
            levels = pd.Series(published.to_numpy(), index=index_years(published)).combine_first(levels)
    levels = levels.sort_index()
    prev = levels.shift(1)
    consecutive = pd.Series(levels.index, index=levels.index).diff() == 1
    return ((levels / prev - 1) * 100)[consecutive].astype(float)


def compound_rate(rates: pd.Series) -> float:
    """The single annual rate that compounds to the same total: the
    geometric mean of the growth factors, in percent."""
    if rates.empty:
        return math.nan
    return (float(np.prod(1 + rates.to_numpy() / 100)) ** (1 / len(rates)) - 1) * 100


def rolling_compound(rates: pd.Series, window: int) -> pd.Series:
    """Compound rate over each run of ``window`` consecutive years, indexed
    by the window's last year."""
    out = {}
    years = set(rates.index)
    for end in rates.index:
        span = range(end - window + 1, end + 1)
        if all(y in years for y in span):
            out[end] = compound_rate(rates.loc[list(span)])
    return pd.Series(out, dtype=float)


def in_period(rates: pd.Series, first: int | None, last: int | None) -> pd.Series:
    keep = pd.Series(True, index=rates.index)
    if first is not None:
        keep &= rates.index >= first
    if last is not None:
        keep &= rates.index <= last
    return rates[keep]


def tau_rho(tau: float) -> float:
    """Copula rho with the given Kendall's tau: the inverse of
    tau = (2/pi) asin(rho), which holds for the simulator's Gaussian and t
    copulas alike (ADR-008)."""
    return math.sin(math.pi * tau / 2)


@dataclass
class Summary:
    label: str
    years: str
    n: int
    compound: float
    median: float
    sd: float
    low: float
    high: float
    persistence: float
    shock_sd: float
    rho_tau: float
    pearson: float
    gap: float


# Fewer points than this make a correlation or persistence estimate noise
# (with three years, tau can only be -1, -1/3, 1/3 or 1), so it's left blank.
MIN_YEARS_FOR_CORRELATION = 8


def summarize(label: str, annual: pd.Series, reference: pd.Series | None, window: int = 1) -> Summary:
    """Statistics of one series' annual rates over one period.

    With ``window`` > 1 the median, SD, range and correlation describe the
    rolling ``window``-year compound rates instead of single years, while the
    compound rate and gap still cover every year. Persistence is left blank
    then: overlapping windows share most of their years, so consecutive ones
    are nearly equal by construction. So is the shock SD: the SD of one
    year's shock to ``log(1 + rate)`` around its persistent part, what a
    ``vary_process`` row's ``vary_innovation_sd`` means (ADR-006 §4a),
    ``sd(log(1 + rate)) * sqrt(1 - persistence**2)``, with a negative
    persistence counted as 0, as a process row can't take one. Correlations
    and the gap use only the years both series have."""
    values = rolling_compound(annual, window) if window > 1 else annual
    n = len(values)
    sd = float(values.std(ddof=1)) if n > 1 else math.nan
    persistence = shock_sd = math.nan
    if window == 1:
        # Pair each year with the calendar year before it, so a gap in the
        # data isn't counted as one year's persistence.
        prior = pd.Series(values.to_numpy(), index=values.index + 1)
        pairs = pd.concat([values, prior], axis=1, join="inner").dropna()
        if len(pairs) >= MIN_YEARS_FOR_CORRELATION:
            persistence = float(pairs.iloc[:, 0].corr(pairs.iloc[:, 1]))
            log_sd = float(np.log1p(values / 100).std(ddof=1)) * 100
            shock_sd = log_sd * math.sqrt(1 - max(persistence, 0.0) ** 2)
    rho_tau = pearson = gap = math.nan
    if reference is not None:
        ref_values = rolling_compound(reference, window) if window > 1 else reference
        both = pd.concat([values, ref_values], axis=1, join="inner").dropna()
        if (len(both) >= MIN_YEARS_FOR_CORRELATION
                and both.iloc[:, 0].std() > 0 and both.iloc[:, 1].std() > 0):
            tau = stats.kendalltau(both.iloc[:, 0], both.iloc[:, 1]).statistic
            rho_tau = tau_rho(float(tau))
            pearson = float(both.iloc[:, 0].corr(both.iloc[:, 1]))
        common = pd.concat([annual, reference], axis=1, join="inner").dropna()
        if len(common):
            gap = compound_rate(common.iloc[:, 0]) - compound_rate(common.iloc[:, 1])
    # The year span and compound rate cover every year, even when a window
    # longer than the period leaves no rolling values.
    years = f"{annual.index.min()}–{annual.index.max()}" if len(annual) else "—"
    return Summary(label, years, n, compound_rate(annual),
                   float(values.median()) if n else math.nan, sd,
                   float(values.min()) if n else math.nan, float(values.max()) if n else math.nan,
                   persistence, shock_sd, rho_tau, pearson, gap)


# A process row's level SD is bracketed by these: the spread of rolling
# compound rates over this many years is an upper bound (ADR-006 §4a: the
# level is uncertainty about the long run, which a decade's average
# overstates), and the standard error of the long-run mean a lower one.
PROCESS_LEVEL_WINDOW = 10
DEFAULT_HOLD_YEARS = 30


@dataclass
class RowValues:
    """What one series' history suggests for a varying ``set_rate`` row's
    ``calendar.csv`` columns, held or as a ``vary_process=ar1`` path."""
    label: str
    rate: float
    held_sd: float
    level_sd_low: float
    level_sd_high: float
    persistence: float
    shock_sd: float


def row_values(label: str, annual: pd.Series, hold_years: int = DEFAULT_HOLD_YEARS) -> RowValues:
    """Suggested row values from single-year rates, whatever ``--window``
    the summary uses. A held row's ``vary_sd`` is the spread of rolling
    ``hold_years`` compound rates, since a held draw stands for the average
    over the span it holds. A process row's level ``vary_sd`` is a range, the
    standard error of the mean (single-year SD / sqrt(N)) up to the rolling
    ``PROCESS_LEVEL_WINDOW``-year SD, because how far the future's long-run
    rate may differ from the past's is a judgment the data can't make.
    Persistence is floored at 0, as a process row can't take a negative one.

    A rolling SD is left blank unless the period spans at least two whole
    windows: overlapping windows over fewer years share most of their years,
    so their SD comes out far too small (a 30-year SD from 42 years of CPI
    is 0.1 points)."""
    single = summarize(label, annual, None)

    def rolling_sd(window: int) -> float:
        return summarize(label, annual, None, window=window).sd if len(annual) >= 2 * window else math.nan

    se = single.sd / math.sqrt(single.n) if single.n > 1 else math.nan
    persistence = max(single.persistence, 0.0) if not math.isnan(single.persistence) else math.nan
    shock_sd = single.shock_sd
    # A series that only ever rises (or never moves) estimates persistence 1
    # and no shock, which the loader rejects (0 <= φ < 1, shock > 0). Suggest
    # no process then, judged on the printed values, so nothing unpasteable
    # is shown.
    if not (round(persistence, 2) < 1 and round(shock_sd, 1) > 0):
        persistence = shock_sd = math.nan
    return RowValues(label, single.compound, rolling_sd(hold_years), se, rolling_sd(PROCESS_LEVEL_WINDOW),
                     persistence, shock_sd)


def runs_above(rates: pd.Series, threshold: float) -> list[tuple[int, int, float]]:
    """Maximal stretches of consecutive years above ``threshold`` percent:
    (first year, last year, compound rate over the stretch)."""
    runs: list[tuple[int, int, float]] = []
    start: int | None = None
    prev = 0
    rates = rates.sort_index()
    for year, rate in zip(rates.index.astype(int), rates, strict=True):
        if rate > threshold and start is not None and year == prev + 1:
            prev = year
            continue
        if start is not None:
            runs.append((start, prev, compound_rate(rates.loc[start:prev])))
            start = None
        if rate > threshold:
            start = prev = year
    if start is not None:
        runs.append((start, prev, compound_rate(rates.loc[start:prev])))
    return runs


# ----------------------------------------------------------------- output


def _fmt(x: float, digits: int = 1) -> str:
    return "—" if x is None or math.isnan(x) else f"{x:.{digits}f}"


SUMMARY_COLUMNS = ["Series", "Years", "N", "Compound %", "Median %", "SD", "Min %", "Max %",
                   "Persistence", "Shock SD", "ρ (from τ)", "Pearson", "Gap vs ref"]


def summary_cells(s: Summary) -> list[str]:
    return [s.label, s.years, str(s.n), _fmt(s.compound), _fmt(s.median), _fmt(s.sd), _fmt(s.low),
            _fmt(s.high), _fmt(s.persistence, 2), _fmt(s.shock_sd), _fmt(s.rho_tau, 2),
            _fmt(s.pearson, 2), _fmt(s.gap, 2)]


def _pct(x: float) -> str:
    """A percent cell as calendar.csv takes it: ``4.5%``."""
    return "—" if math.isnan(x) else f"{x:.1f}%"


ROW_VALUE_COLUMNS = ["Series", "Row", "amount, vary_median", "vary_sd", "vary_persistence",
                     "vary_innovation_sd"]


def row_value_cells(v: RowValues, hold_years: int) -> list[list[str]]:
    """Two rows per series: held, and as an ``ar1`` process."""
    if math.isnan(v.level_sd_low) and math.isnan(v.level_sd_high):
        level = "—"
    elif math.isnan(v.level_sd_high):
        level = f"{_pct(v.level_sd_low)} or more"
    else:
        level = f"{_pct(v.level_sd_low)} to {_pct(v.level_sd_high)}"
    return [[v.label, f"held {hold_years} yr", _pct(v.rate), _pct(v.held_sd), "—", "—"],
            [v.label, "process (ar1)", _pct(v.rate), level, _fmt(v.persistence, 2), _pct(v.shock_sd)]]


# Every name a calibrated value goes by, so a value can be followed from
# this command's tables to the CSV, the CLI and the guides (#156).
LEGEND_COLUMNS = ["calendar.csv column", "sim calendar add flag", "Plain name", "From these tables",
                  "Meaning"]
LEGEND = [
    ["amount", "RATE_PERCENT argument", "the rate", "Compound %",
     "The row's rate; on a varying row, a placeholder the draw replaces."],
    ["vary_median", "--vary-median", "long-run rate (L)", "Compound %",
     "The median draw: the rate a held row keeps, or the rate a process path compounds to "
     "in the median future."],
    ["vary_sd (held row)", "--vary-sd", "spread of the held rate", "held row's vary_sd",
     "SD of the draw. A held rate stands for the average over the years it holds, so this is "
     "the SD of rolling compound rates over that many years (--hold-years), not the "
     "single-year SD. Blank when the period is shorter than two such spans."],
    ["vary_sd (process row)", "--vary-sd", "uncertainty about the long-run rate",
     "process row's vary_sd",
     "SD of the level L only; the path adds the yearly spread. Low end: the standard error "
     "of the mean; high end: the 10-year rolling SD. Where in between is a judgment."],
    ["vary_process", "--vary-process", "year-by-year path", "—",
     "ar1 for a path that moves every year around L; blank to hold one draw."],
    ["vary_persistence", "--vary-persistence", "persistence (φ)", "Persistence",
     "How much of one year's deviation carries into the next. A negative value is entered as 0."],
    ["vary_innovation_sd", "--vary-innovation-sd", "yearly shock size (σ)", "Shock SD",
     "SD of one year's surprise in log growth, written as a percent (18.9 → 18.9%). In a tree "
     "with vary_tail_dependence ν, divide by √(ν / (ν − 2))."],
    ["vary_low, vary_high", "--vary-low, --vary-high", "bounds on the draw", "—",
     "Bound the drawn rate, or a process's level L, not its yearly path. Min % and Max % are "
     "single years' extremes; don't use them here."],
    ["—", "—", "single-year spread", "SD, Median %, Min %, Max %",
     "Of single years (or --window N rolling rates). For reading history; no column takes the "
     "single-year SD directly."],
]


def _md_cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def markdown_table(header: list[str], rows: list[list[str]]) -> str:
    # Labels are free-form, and a bare | would split a cell.
    header = [_md_cell(c) for c in header]
    rows = [[_md_cell(c) for c in r] for r in rows]
    widths = [max(3, len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    numeric = [i > 0 and all(re.fullmatch(r"[-\d.—]+", r[i]) for r in rows) for i in range(len(header))]

    def line(cells):
        return "| " + " | ".join(c.rjust(w) if num else c.ljust(w)
                                 for c, w, num in zip(cells, widths, numeric, strict=True)) + " |"

    rule = "| " + " | ".join(("-" * (w - 1) + ":") if num else "-" * w
                             for w, num in zip(widths, numeric, strict=True)) + " |"
    return "\n".join([line(header), rule, *(line(r) for r in rows)])


def parse_period(arg: str) -> tuple[str, int | None, int | None]:
    """``NAME=FIRST-LAST``; either year may be blank for open-ended."""
    name, sep, span = arg.rpartition("=")
    m = re.fullmatch(r"(\d{4})?-(\d{4})?", span.strip())
    if not sep or not m:
        raise DataError(f"period {arg!r} isn't NAME=FIRST-LAST (e.g. 'Calm=1984-2007', 'Recent=2021-')")
    first, last = (int(g) if g else None for g in m.groups())
    if first is not None and last is not None and first > last:
        raise DataError(f"period {arg!r} ends before it starts")
    return name.strip(), first, last


def run(series_args: Sequence[str], *, reference_arg: str | None = None, period_args: Sequence[str] = (),
        window: int = 1, basis: str = "dec", runs_above_pct: float | None = None,
        hold_years: int = DEFAULT_HOLD_YEARS,
        fmt: str = "markdown", cache: Path = DEFAULT_CACHE, refresh: bool = False,
        contact: str | None = None) -> None:
    """Print the summary tables for ``analysis historical``; raises
    :class:`DataError` when a period or a source is bad."""
    if cache == DEFAULT_CACHE and not cache.is_dir():
        # Don't quietly start a cache inside site-packages.
        raise DataError(f"the committed data isn't at {cache} (is analysis installed from a checkout?); "
                        "pass --cache DIR")
    periods = [parse_period(p) for p in period_args] or [(k, *v) for k, v in DEFAULT_PERIODS.items()]
    fetcher = Fetcher(cache, contact, refresh)
    loaded = load_series(fetcher, list(series_args) + ([reference_arg] if reference_arg else []))
    series = loaded[: len(series_args)]
    reference = loaded[-1] if reference_arg else None

    ref_rates = annual_rates(reference, basis) if reference else None
    csv_rows = []
    for name, first, last in periods:
        ref = in_period(ref_rates, first, last) if ref_rates is not None else None
        annuals = [(s.label, in_period(annual_rates(s, basis), first, last)) for s in series]
        summaries = [summarize(label, annual, ref, window) for label, annual in annuals]
        if fmt == "csv":
            csv_rows += [[name, *summary_cells(s)] for s in summaries]
            continue
        span = f"{first or 'start'}–{last or 'latest'}"
        unit = f", rolling {window}-year compound rates" if window > 1 else ""
        print(f"### {name} ({span}{unit})\n")
        print(markdown_table(SUMMARY_COLUMNS, [summary_cells(s) for s in summaries]))
        print()
        print(f"Suggested calendar.csv values ({name}, from single years; see the legend below):\n")
        print(markdown_table(ROW_VALUE_COLUMNS, [
            cells for label, annual in annuals
            for cells in row_value_cells(row_values(label, annual, hold_years), hold_years)]))
        print()
    if fmt == "csv":
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["Period", *SUMMARY_COLUMNS])
        writer.writerows(csv_rows)
    if runs_above_pct is not None:
        print(f"### Years above {runs_above_pct:g}%\n")
        rows = [[s.label, f"{a}–{b}" if a != b else str(a), str(b - a + 1), _fmt(rate)]
                for s in series for a, b, rate in runs_above(annual_rates(s, basis), runs_above_pct)]
        print(markdown_table(["Series", "Years", "Length", "Compound %"], rows) if rows else "None.")
        print()
    if fmt == "markdown":
        print("### Legend\n")
        print(markdown_table(LEGEND_COLUMNS, LEGEND))
        print()
        print("Sources:\n")
        for spec, s in {s.spec: s for s in loaded}.items():
            print(f"- {s.label}: {s.title or spec}")
        for place, day in fetcher.retrieved_by_place().items():
            print(f"- {place} (retrieved {day})")
        print(f"\nReference: {reference.label if reference else 'none'}. "
              f"Annual basis: {'December over December' if basis == 'dec' else 'annual average'}.")
