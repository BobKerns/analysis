"""analysis.historical (``analysis historical``): parsing each source and the statistics,
all offline (downloads are stubbed)."""

from __future__ import annotations

# cspell:ignore CUUR CWUR SUUR SEEB DPCCRG DPCERG DPCERGM PCEPI histret Asize

import csv
import io
import math
import re
import ssl
import urllib.error
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any

import click
import numpy as np
import pandas as pd
import pytest
import truststore
from click.testing import CliRunner

import analysis.historical.calibration as calibration
from analysis.historical.cli import historical_command

@pytest.fixture
def hc():
    return calibration


def invoke(*args: str):
    return CliRunner().invoke(historical_command, list(args))


@pytest.fixture
def downloads(hc, monkeypatch):
    """Stub every download with canned bytes by URL; records what was asked."""
    served: dict[str, bytes] = {}
    asked: list[str] = []

    def fake(self, url):
        asked.append(url)
        if url not in served:
            raise hc.DataError(f"no canned response for {url}")
        return served[url]

    monkeypatch.setattr(hc.Fetcher, "_download", fake)
    return served, asked


def monthly_index(hc, levels_by_year: dict[int, list[float]], kind="index"):
    points = {
        pd.Timestamp(year, month, 1): value
        for year, values in levels_by_year.items()
        for month, value in enumerate(values, start=1)
    }
    return hc.Series(label="x", spec="test", kind=kind, values=pd.Series(points, dtype=float), monthly=True)


# ------------------------------------------------------------------ specs


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("CUUR0000SA0", ("CUUR0000SA0", "bls", "CUUR0000SA0", None)),
        ("CPI-U=bls:CUUR0000SA0", ("CPI-U", "bls", "CUUR0000SA0", None)),
        ("rcpie:Medical care", ("Medical care", "rcpie", "Medical care", None)),
        ("Yield=fred:DGS10@rate", ("Yield", "fred", "DGS10", "rate")),
        ("csv:a=b.csv", ("a=b.csv", "csv", "a=b.csv", None)),
    ],
)
def test_parse_spec(hc, arg, expected):
    assert hc.parse_spec(arg) == expected


def test_parse_spec_rejects_unknown_source(hc):
    with pytest.raises(hc.DataError, match="unknown source"):
        hc.parse_spec("yahoo:SPY")


@pytest.mark.parametrize(
    ("arg", "expected"),
    [("Calm=1984-2007", ("Calm", 1984, 2007)), ("Recent=2021-", ("Recent", 2021, None)),
     ("All=-", ("All", None, None))],
)
def test_parse_period(hc, arg, expected):
    assert hc.parse_period(arg) == expected


def test_parse_period_rejects_a_reversed_span(hc):
    with pytest.raises(hc.DataError, match="ends before it starts"):
        hc.parse_period("Bad=2025-1984")


def test_parse_period_rejects_bad_span(hc):
    with pytest.raises(hc.DataError, match="NAME=FIRST-LAST"):
        hc.parse_period("1984-2007")


# ------------------------------------------------------------------- BLS

BLS_DATA = (
    "series_id        \tyear\tperiod\t       value\tfootnote_codes\n"
    "CUUR0000SA0      \t2024\tM12\t      100.0\t\n"
    "CUUR0000SA0      \t2025\tM10\t          -\t\n"
    "CUUR0000SA0      \t2025\tM12\t      103.0\t\n"
    "CUUR0000SAM      \t2025\tM12\t      110.0\t\n"
)


def test_parse_bls_data_keeps_wanted_series_and_skips_missing_values(hc):
    rows = hc.parse_bls_data(BLS_DATA, {"CUUR0000SA0"})
    assert rows == {"CUUR0000SA0": [(2024, "M12", 100.0), (2025, "M12", 103.0)]}


def test_bls_data_files_skips_current_and_searches_regional_last(hc):
    listing = " ".join(
        f'HREF="/pub/time.series/cu/cu.data.{n}"'
        for n in ["0.Current", "1.AllItems", "10.OtherWest", "11.USFoodBeverage", "2.Summaries", "3.AsizeNorthEast"]
    )
    assert hc.bls_data_files(listing, "cu") == [
        "cu.data.1.AllItems", "cu.data.2.Summaries", "cu.data.11.USFoodBeverage",
        "cu.data.3.AsizeNorthEast", "cu.data.10.OtherWest",
    ]


def test_load_bls_stops_downloading_once_every_series_is_found(hc, downloads, calibration_cache_dir):
    served, asked = downloads
    base = hc.BLS_BASE + "/cu"
    served[f"{base}/"] = b'"cu.data.1.AllItems" "cu.data.2.Summaries"'
    served[f"{base}/cu.series"] = (
        b"series_id\tarea_code\tseries_title\nCUUR0000SA0\t0000\tAll items\n"
    )
    served[f"{base}/cu.data.1.AllItems"] = BLS_DATA.encode()
    got = hc.load_bls(hc.Fetcher(calibration_cache_dir, "x@example.com"), ["CUUR0000SA0"])
    assert f"{base}/cu.data.2.Summaries" not in asked
    s = got["CUUR0000SA0"]
    assert (s.title, s.kind, s.monthly) == ("All items", "index", True)
    assert hc.annual_rates(s).to_dict() == {2025: pytest.approx(3.0)}


@pytest.fixture
def serve_bls_rows(hc, downloads, calibration_cache_dir):
    """Serve one BLS cu series with the given (year, period, value) rows."""
    served, _ = downloads
    base = hc.BLS_BASE + "/cu"
    served[f"{base}/"] = b'"cu.data.1.AllItems"'
    served[f"{base}/cu.series"] = b"series_id\tseries_title\nCUUR0000SX\tX\n"

    def serve(rows):
        served[f"{base}/cu.data.1.AllItems"] = (
            "series_id\tyear\tperiod\tvalue\tfootnote_codes\n"
            + "".join(f"CUUR0000SX\t{y}\t{p}\t{v}\t\n" for y, p, v in rows)
        ).encode()
        return hc.load_bls(hc.Fetcher(calibration_cache_dir, "x@example.com"), ["CUUR0000SX"])["CUUR0000SX"]

    return serve


def test_load_bls_reads_an_annual_only_series(hc, serve_bls_rows):
    s = serve_bls_rows([(2020, "A01", 100.0), (2021, "A01", 104.0)])
    assert not s.monthly
    assert hc.annual_rates(s).to_dict() == {2021: pytest.approx(4.0)}


def test_load_bls_refuses_semiannual_series(hc, serve_bls_rows):
    with pytest.raises(hc.DataError, match="only S01, S02 periods"):
        serve_bls_rows([(2020, "S01", 100.0), (2020, "S02", 101.0)])


def test_load_bls_rejects_unknown_series(hc, downloads, calibration_cache_dir):
    served, _ = downloads
    base = hc.BLS_BASE + "/cu"
    served[f"{base}/"] = b""
    served[f"{base}/cu.series"] = b"series_id\tseries_title\nCUUR0000SA0\tAll items\n"
    with pytest.raises(hc.DataError, match="no series CUUR0000XXX"):
        hc.load_bls(hc.Fetcher(calibration_cache_dir, "x@example.com"), ["CUUR0000XXX"])


# -------------------------------------------------------------------- BEA

BEA_REGISTER = (
    "%SeriesCode,SeriesLabel,MetricName,CalculationType,DefaultScale,TableId:LineNo,SeriesCodeParents\n"
    'DPCERG,"Personal consumption expenditures","Fisher Price Index","Level",0,T20804:1,NONE\n'
    'DPCERGM,"Personal consumption expenditures (PCE)","Fisher Price Index","Percent change",0,T20807:1,NONE\n'
)
BEA_DATA = '%SeriesCode,Period,Value\nA015RC,2024M12,"9,288"\nDPCERG,2024M12,"1,000.0"\nDPCERG,2025M12,"1,025.0"\n'


def test_load_bea_reads_a_monthly_level(hc, downloads, calibration_cache_dir):
    served, _ = downloads
    served[f"{hc.BEA_BASE}/SeriesRegister.txt"] = BEA_REGISTER.encode()
    served[f"{hc.BEA_BASE}/NipaDataM.txt"] = BEA_DATA.encode()
    (s,) = hc.load_series(hc.Fetcher(calibration_cache_dir, "x@example.com"), ["PCE=bea:DPCERG"])
    assert s.title == "BEA Personal consumption expenditures, Fisher Price Index"
    assert hc.annual_rates(s).to_dict() == {2025: pytest.approx(2.5)}


def test_load_bea_refuses_a_percent_change_series(hc, downloads, calibration_cache_dir):
    served, _ = downloads
    served[f"{hc.BEA_BASE}/SeriesRegister.txt"] = BEA_REGISTER.encode()
    with pytest.raises(hc.DataError, match="use its Level series"):
        hc.load_bea(hc.Fetcher(calibration_cache_dir, "x@example.com"), ["DPCERGM"])


# ---------------------------------------------------------------- R-CPI-E


def xlsx(rows: Sequence[Mapping[str, str | float]]) -> bytes:
    """A one-sheet workbook with inline numbers and shared strings."""
    strings: list[str] = []
    cells_xml = []
    for i, row in enumerate(rows, start=1):
        cells = []
        for col, value in row.items():
            if isinstance(value, str):
                strings.append(value)
                cells.append(f'<c r="{col}{i}" t="s"><v>{len(strings) - 1}</v></c>')
            else:
                cells.append(f'<c r="{col}{i}"><v>{value}</v></c>')
        cells_xml.append(f'<row r="{i}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml",
                   f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml",
                   f"<worksheet {ns}><sheetData>{''.join(cells_xml)}</sheetData></worksheet>")
    return buf.getvalue()


RCPIE_ROWS = [
    {"A": "Table 1. Research consumer price index"},
    {"A": "Month", "B": "All items", "C": "Recreation (1)"},
    {"A": 30286.0, "B": 100.0, "C": "--"},  # 1982-12-01
    {"A": 30651.0, "B": 103.0, "C": 100.0},  # 1983-12-01
    {"A": "Table 2. Percent changes"},
    {"A": 30651.0, "B": 3.0},
]


def test_parse_rcpie_reads_table_one_by_column(hc):
    table = hc.parse_rcpie(xlsx(RCPIE_ROWS))
    assert set(table) == {"All items", "Recreation"}  # "(1)" footnote mark dropped
    assert table["All items"].to_dict() == {pd.Timestamp(1982, 12, 1): 100.0, pd.Timestamp(1983, 12, 1): 103.0}
    assert list(table["Recreation"].index) == [pd.Timestamp(1983, 12, 1)]  # "--" skipped


def test_parse_rcpie_without_header_fails(hc):
    with pytest.raises(hc.DataError, match="'Month' header"):
        hc.parse_rcpie(xlsx([{"A": "nothing here"}]))


# -------------------------------------------------------------- Damodaran

DAMODARAN_HEADER = " ".join([
    "S&amp;P 500 (includes dividends)", "US Small cap (bottom decile)", "3-month T.Bill",
    "US T. Bond (10-year)", "Baa Corporate Bond", "Real Estate", "Gold*",
])


def test_parse_damodaran_reads_annual_returns(hc):
    page = (f"<p>Value of $100 invested at start of 1928 in</p><tr>{DAMODARAN_HEADER}</tr>"
            "<tr><td>1928</td>" + "".join(f"<td>{v}%</td>" for v in [43.81, 62.15, 3.08, 0.84, 3.22, 1.49, 0.10])
            + "<td>$ 143.81</td></tr>"
            "<tr><td>1929</td>" + "".join(f"<td>{v}%</td>" for v in [-8.3, -46.08, 3.16, 4.2, 3.02, -2.06, -0.15])
            + "</tr>")
    table = hc.parse_damodaran(page)
    assert table["tbill"].to_dict() == {pd.Timestamp(1928, 1, 1): 3.08, pd.Timestamp(1929, 1, 1): 3.16}
    assert table["sp500"].iloc[1] == -8.3


def test_parse_damodaran_rejects_a_changed_header(hc):
    page = "<tr>Year Stocks Bonds</tr><tr>1928 " + " ".join(["1.0%"] * 7) + "</tr>"
    with pytest.raises(hc.DataError, match="header has changed"):
        hc.parse_damodaran(page)


# ------------------------------------------------------------ CSV and FRED


def test_two_column_csv_kind_comes_from_its_header(hc):
    kind, s = hc.parse_two_column_csv("observation_date,PCEPI\n2024-01-01,100\n2024-02-01,.\n")
    assert kind == "index" and len(s) == 1
    kind, _ = hc.parse_two_column_csv("date,rate\n2024-01-01,3.5\n")
    assert kind == "rate"


def test_fred_series_load_through_the_cache(hc, downloads, calibration_cache_dir):
    served, asked = downloads
    url = hc.FRED_URL.format("PCEPI")
    served[url] = b"observation_date,PCEPI\n" + b"".join(
        f"{y}-12-01,{v}\n".encode() for y, v in [(2023, 100.0), (2024, 102.0)])
    fetcher = hc.Fetcher(calibration_cache_dir, "x@example.com")
    for _ in range(2):
        (s,) = hc.load_series(fetcher, ["PCE=fred:PCEPI"])
    assert asked == [url]  # second load came from the cache
    assert s.label == "PCE" and not s.monthly
    assert hc.annual_rates(s).to_dict() == {2024: pytest.approx(2.0)}
    assert (calibration_cache_dir / "fred" / "PCEPI.csv.retrieved").exists()


def test_csv_rate_series_is_used_as_is(hc, calibration_rate_csv, calibration_cache_dir):
    (s,) = hc.load_series(hc.Fetcher(calibration_cache_dir, None), [f"Part B=csv:{calibration_rate_csv}"])
    assert hc.annual_rates(s).to_dict() == {2020: 3.0, 2021: 5.0, 2022: 4.0}


@pytest.fixture
def sent_agents(hc, monkeypatch):
    """Stub urlopen, recording the User-Agent and SSL context of each request."""
    agents: dict[str, Any] = {}

    def fake_urlopen(request, timeout, context):
        agents[request.full_url] = request.get_header("User-agent")
        agents.setdefault("contexts", []).append(context)
        return io.BytesIO(b"data")

    monkeypatch.setattr(hc.urllib.request, "urlopen", fake_urlopen)
    return agents


def test_weekly_rates_average_to_monthly_then_annual(hc):
    days = pd.date_range("2020-01-02", "2021-12-31", freq="7D")
    text = "date,rate\n" + "".join(f"{d.date()},{3.0 if d.year == 2020 else 5.0}\n" for d in days)
    s = hc.load_csv_like("csv:w", text, "w", "w")
    assert s.monthly and len(s.values) == 24
    assert hc.annual_rates(s).to_dict() == {2020: pytest.approx(3.0), 2021: pytest.approx(5.0)}


@pytest.mark.parametrize("text", ["date,rate\n2020-01-01,abc\n", "date,rate\nnot-a-date,1.0\n"])
def test_malformed_csv_is_a_data_error(hc, text):
    with pytest.raises(hc.DataError, match="malformed CSV"):
        hc.load_csv_like("csv:x", text, "x", "x")


def test_markdown_table_escapes_pipes_in_labels(hc):
    table = hc.markdown_table(["Series", "N"], [["Food|Energy", "3"]])
    assert table.splitlines()[2] == "| Food\\|Energy |   3 |"


def test_rate_override_applies_before_reducing_to_monthly(hc):
    text = "observation_date,DGS10\n2020-01-02,1.0\n2020-01-31,3.0\n2020-02-03,2.0\n"
    s = hc.load_csv_like("fred:DGS10", text, "y", "y", "rate")
    assert s.kind == "rate" and s.values.tolist() == [2.0, 2.0]


def test_quarterly_data_is_refused(hc):
    text = "date,rate\n" + "".join(f"{y}-{m:02d}-01,2.0\n" for y in (2020, 2021) for m in (1, 4, 7, 10))
    with pytest.raises(hc.DataError, match="less frequent than monthly"):
        hc.load_csv_like("csv:q", text, "q", "q")


def test_weekly_levels_take_each_months_last_value(hc):
    text = "date,value\n2020-12-03,100\n2020-12-31,101\n2021-12-02,103\n2021-12-30,104.03\n"
    s = hc.load_csv_like("csv:w", text, "w", "w")
    assert hc.annual_rates(s).to_dict() == {2021: pytest.approx(3.0)}


def test_missing_csv_is_a_data_error(hc, calibration_cache_dir, tmp_path):
    with pytest.raises(hc.DataError, match="couldn't read"):
        hc.load_series(hc.Fetcher(calibration_cache_dir, None), [f"csv:{tmp_path / 'nope.csv'}"])


def test_download_from_bls_needs_a_contact(hc, calibration_cache_dir, sent_agents):
    with pytest.raises(hc.DataError, match="no contact email"):
        hc.Fetcher(calibration_cache_dir, None).get("https://download.bls.gov/x", "x")
    assert sent_agents == {}


def test_only_bls_is_sent_a_custom_agent(hc, calibration_cache_dir, sent_agents):
    fetcher = hc.Fetcher(calibration_cache_dir, "x@example.com")
    fetcher.get("https://download.bls.gov/pub/a", "a")
    fetcher.get("https://fred.stlouisfed.org/b", "b")
    assert "x@example.com" in sent_agents["https://download.bls.gov/pub/a"]
    assert sent_agents["https://fred.stlouisfed.org/b"] is None  # urllib's default


def test_downloads_verify_against_the_system_trust_store(hc, calibration_cache_dir, sent_agents):
    hc.Fetcher(calibration_cache_dir, None).get("https://fred.stlouisfed.org/b", "b")
    assert isinstance(sent_agents["contexts"][0], truststore.SSLContext)


def test_an_untrusted_certificate_says_why(hc, calibration_cache_dir, monkeypatch):
    error = ssl.SSLCertVerificationError("certificate verify failed")
    error.verify_code = 20

    def fake_urlopen(request, timeout, context):
        raise urllib.error.URLError(error)

    monkeypatch.setattr(hc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(hc.DataError, match="couldn't verify the HTTPS certificate"):
        hc.Fetcher(calibration_cache_dir, None).get("https://fred.stlouisfed.org/b", "b")


# ------------------------------------------------------------------ rates


def test_annual_rates_dec_over_dec_drops_a_year_after_a_gap(hc):
    s = monthly_index(hc, {2020: [100.0] * 12, 2021: [104.0] * 12, 2023: [110.0] * 12})
    assert hc.annual_rates(s).to_dict() == {2021: pytest.approx(4.0)}


def test_annual_rates_average_prefers_bls_published_average(hc):
    s = monthly_index(hc, {2020: [100.0] * 12, 2021: [101.0] * 11 + [113.0]})
    assert hc.annual_rates(s, "average").to_dict() == {2021: pytest.approx(2.0)}
    s.annual_average = pd.Series({pd.Timestamp(2020, 1, 1): 100.0, pd.Timestamp(2021, 1, 1): 101.0})
    assert hc.annual_rates(s, "average").to_dict() == {2021: pytest.approx(1.0)}


def test_annual_rates_of_a_monthly_rate_is_its_full_year_average(hc):
    s = monthly_index(hc, {2020: [1.0] * 6 + [3.0] * 6, 2021: [5.0] * 11}, kind="rate")
    assert hc.annual_rates(s).to_dict() == {2020: 2.0}  # 2021 is incomplete


def test_compound_rate_is_the_geometric_mean(hc):
    rates = pd.Series([10.0, -10.0])
    assert hc.compound_rate(rates) == pytest.approx((math.sqrt(1.1 * 0.9) - 1) * 100)
    assert math.isnan(hc.compound_rate(pd.Series([], dtype=float)))


def test_rolling_compound_needs_every_year_of_the_window(hc):
    rates = pd.Series({2000: 2.0, 2001: 4.0, 2002: 6.0, 2004: 1.0})
    out = hc.rolling_compound(rates, 2)
    assert list(out.index) == [2001, 2002]
    assert out[2001] == pytest.approx((math.sqrt(1.02 * 1.04) - 1) * 100)


def test_tau_rho_inverts_the_copula_relation(hc):
    for rho in (-0.7, 0.0, 0.3, 0.9):
        assert hc.tau_rho(2 / math.pi * math.asin(rho)) == pytest.approx(rho)


def test_summarize_leaves_correlations_blank_for_short_samples(hc):
    years = range(2000, 2000 + hc.MIN_YEARS_FOR_CORRELATION)
    ref = pd.Series({y: float(i) for i, y in enumerate(years)})
    rates = ref * 2 + 1
    full = hc.summarize("x", rates, ref)
    assert full.rho_tau == pytest.approx(1.0) and full.pearson == pytest.approx(1.0)
    assert full.gap == pytest.approx(hc.compound_rate(rates) - hc.compound_rate(ref))
    short = hc.summarize("x", rates.iloc[:-1], ref)
    assert math.isnan(short.rho_tau) and math.isnan(short.persistence)
    assert short.median == pytest.approx(float(rates.iloc[:-1].median()))


def test_persistence_pairs_calendar_years_across_a_gap(hc):
    # Alternating years have persistence -1; a gap must not pair 2010 with 2012.
    rates = pd.Series({y: (1.0 if y % 2 else 3.0) for y in [*range(2001, 2011), *range(2012, 2022)]})
    assert hc.summarize("x", rates, None).persistence == pytest.approx(-1.0)


def test_shock_sd_is_the_log_spread_net_of_persistence(hc):
    """ADR-006 §4a: what a process row's vary_innovation_sd means, the SD of
    log(1 + rate) times sqrt(1 - persistence**2)."""
    rates = pd.Series({2000 + i: r for i, r in enumerate(
        [2.0, 3.5, 5.0, 4.0, 2.5, 1.0, 2.0, 4.5, 6.0, 5.5, 3.0, 1.5])})
    s = hc.summarize("x", rates, None)
    log_sd = float(np.log1p(rates / 100).std(ddof=1)) * 100
    assert 0 < s.persistence < 1
    assert s.shock_sd == pytest.approx(log_sd * math.sqrt(1 - s.persistence ** 2))
    assert math.isnan(hc.summarize("x", rates, None, window=2).shock_sd)


def test_shock_sd_counts_a_negative_persistence_as_zero(hc):
    rates = pd.Series({y: (1.0 if y % 2 else 3.0) for y in range(2001, 2021)})
    s = hc.summarize("x", rates, None)
    assert s.persistence == pytest.approx(-1.0)
    assert s.shock_sd == pytest.approx(float(np.log1p(rates / 100).std(ddof=1)) * 100)


def test_summarize_keeps_compound_rate_when_window_exceeds_the_period(hc):
    annual = pd.Series({2000: 2.0, 2001: 4.0})
    s = hc.summarize("x", annual, None, window=5)
    assert s.n == 0 and s.years == "2000–2001"
    assert s.compound == pytest.approx(hc.compound_rate(annual))


def test_summarize_with_a_window_spreads_rolling_rates_but_compounds_every_year(hc):
    annual = pd.Series({2000 + i: r for i, r in enumerate([1.0, 9.0, 1.0, 9.0, 1.0])})
    s = hc.summarize("x", annual, None, window=2)
    assert s.n == 4 and s.years == "2000–2004"
    assert s.compound == pytest.approx(hc.compound_rate(annual))
    assert s.sd == pytest.approx(0.0)  # every 2-year window holds one 1% and one 9% year
    assert math.isnan(s.persistence)


def test_row_values_floor_persistence_and_bracket_the_level_sd(hc):
    # Alternating years: persistence -1, which a process row enters as 0.
    annual = pd.Series({1900 + i: (12.0 if i % 2 else 8.0) for i in range(40)})
    v = hc.row_values("x", annual, hold_years=20)
    single = hc.summarize("x", annual, None)
    assert v.rate == pytest.approx(single.compound)
    assert v.persistence == 0.0
    assert v.shock_sd == pytest.approx(single.shock_sd)
    assert v.level_sd_low == pytest.approx(single.sd / math.sqrt(40))
    assert v.level_sd_high == pytest.approx(hc.summarize("x", annual, None, window=10).sd)
    assert v.held_sd == pytest.approx(hc.summarize("x", annual, None, window=20).sd)


def test_row_values_leave_a_rolling_sd_blank_without_two_whole_windows(hc):
    annual = pd.Series({1984 + i: 2.0 + (i % 3) for i in range(42)})
    v = hc.row_values("x", annual, hold_years=30)
    assert math.isnan(v.held_sd)
    assert not math.isnan(v.level_sd_high)  # 42 years hold two 10-year windows


def test_row_value_cells_write_percents_as_the_csv_takes_them(hc):
    v = hc.RowValues("x", 10.04, 1.26, 1.9, 5.5, 0.0, 18.94)
    held, process = hc.row_value_cells(v, 30)
    assert held == ["x", "held 30 yr", "10.0%", "1.3%", "—", "—"]
    assert process == ["x", "process (ar1)", "10.0%", "1.9% to 5.5%", "0.00", "18.9%"]


def test_legend_names_every_suggested_column(hc):
    named = {name for row in hc.LEGEND for name in re.split(r",? |, ", row[0])}
    for column in hc.ROW_VALUE_COLUMNS[2:]:
        for name in column.split(", "):
            assert name in named, name


def test_runs_above_finds_consecutive_stretches(hc):
    rates = pd.Series({1972: 3.0, 1973: 8.0, 1974: 12.0, 1975: 7.0, 1976: 5.0, 1977: 7.0, 1979: 9.0})
    runs = hc.runs_above(rates, 6.0)
    assert [(a, b) for a, b, _ in runs] == [(1973, 1975), (1977, 1977), (1979, 1979)]
    assert runs[0][2] == pytest.approx(hc.compound_rate(pd.Series([8.0, 12.0, 7.0])))


# ------------------------------------------------------------------- main


def test_main_prints_a_table_per_period(calibration_rate_csv, calibration_cache_dir):
    result = invoke(f"Premium=csv:{calibration_rate_csv}", "--period", "Early=2020-2021", "--period", "All=-",
                    "--cache", str(calibration_cache_dir), "--contact", "x@example.com")
    out = result.stdout
    assert result.exit_code == 0, result.output
    assert "### Early (2020–2021)" in out and "### All (start–latest)" in out
    assert "| Premium | 2020–2021 |   2 |" in out
    assert "Suggested calendar.csv values (Early" in out and "| Premium | process (ar1) |" in out
    assert "### Legend" in out and "--vary-innovation-sd" in out


def test_series_used_twice_keep_their_own_labels(hc, downloads, calibration_cache_dir):
    served, _ = downloads
    served[f"{hc.BEA_BASE}/SeriesRegister.txt"] = BEA_REGISTER.encode()
    served[f"{hc.BEA_BASE}/NipaDataM.txt"] = BEA_DATA.encode()
    a, b = hc.load_series(hc.Fetcher(calibration_cache_dir, None), ["PCE=bea:DPCERG", "Ref=bea:DPCERG@rate"])
    assert (a.label, a.kind, b.label, b.kind) == ("PCE", "index", "Ref", "rate")


def test_main_writes_standard_csv(calibration_rate_csv, calibration_cache_dir):
    result = invoke(f'Premium "B", monthly=csv:{calibration_rate_csv}', "--period", "All=-",
                    "--format", "csv", "--cache", str(calibration_cache_dir))
    rows = list(csv.reader(io.StringIO(result.stdout)))
    assert result.exit_code == 0, result.output
    assert rows[1][:3] == ["All", 'Premium "B", monthly', "2020–2022"]


@pytest.mark.parametrize("extra", [["--window", "0"], ["--runs-above", "5", "--format", "csv"]])
def test_main_rejects_bad_arguments(calibration_rate_csv, extra):
    assert invoke(f"csv:{calibration_rate_csv}", *extra).exit_code == 2


def test_main_reports_source_errors_without_a_traceback(calibration_cache_dir):
    result = invoke("yahoo:SPY", "--cache", str(calibration_cache_dir))
    assert result.exit_code == 1
    assert "unknown source" in result.stderr


def test_historical_is_an_analysis_subcommand():
    from analysis.cli import main
    assert main.get_command(click.Context(main), "historical") is historical_command


def test_a_missing_committed_cache_asks_for_one(hc, calibration_rate_csv, tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "DEFAULT_CACHE", tmp_path / "site-packages" / "historical_data")
    with pytest.raises(hc.DataError, match="pass --cache"):
        hc.run([f"csv:{calibration_rate_csv}"], cache=hc.DEFAULT_CACHE)
    assert not hc.DEFAULT_CACHE.exists()
