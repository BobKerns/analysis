"""Fetching security prices into aux files (ADR-005 §6)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import prices
from analysis.sim.prices import PriceError, YahooProvider, fetch_prices, format_price, month_end_closes


class FakeProvider:
    name = "fake"

    def __init__(self, series: dict[str, list[tuple[str, str]]]) -> None:
        self.series = series
        self.asked: list[tuple[str, date, date]] = []

    def closes(self, symbol, start, end):
        self.asked.append((symbol, start, end))
        if symbol not in self.series:
            raise PriceError("not found")
        return [(date.fromisoformat(d), Decimal(p)) for d, p in self.series[symbol]]


DAILY = [
    ("2026-04-29", "10.10"), ("2026-04-30", "10.00"),
    ("2026-05-28", "11.00"), ("2026-05-29", "11.50"),
    ("2026-06-01", "12.25"), ("2026-06-02", "12.5"),
]


@pytest.mark.parametrize(("price", "text"), [
    ("12.5", "12.50"), ("12", "12.00"), ("12.345678", "12.345678"),
    ("0.1000004", "0.10"), ("1234.5", "1234.50"),
])
def test_prices_are_written_with_two_to_six_places(price: str, text: str) -> None:
    assert format_price(Decimal(price)) == text


def test_the_last_close_of_each_month_is_kept_and_the_final_month_is_the_latest() -> None:
    kept = month_end_closes((date.fromisoformat(d), Decimal(p)) for d, p in DAILY)
    assert [(d.isoformat(), str(p)) for d, p in kept] == [
        ("2026-04-30", "10.00"), ("2026-05-29", "11.50"), ("2026-06-02", "12.5"),
    ]


def test_a_new_file_gets_the_month_ends_and_an_existing_one_is_only_added_to(tmp_path: Path) -> None:
    provider = FakeProvider({"AAA": DAILY})
    aux = tmp_path / "aux"
    first = fetch_prices(aux, ["AAA"], provider, since=date(2026, 4, 1), today=date(2026, 6, 2))
    assert (first[0].added, first[0].changed) == (3, 0)
    path = aux / "security_price__AAA.csv"
    assert path.read_text() == (
        "effective_date,price\n2026-04-30,10.00\n2026-05-29,11.50\n2026-06-02,12.50\n"
    )
    path.write_text(path.read_text().replace("2026-05-29,11.50", "2026-05-29,99.00"))
    again = fetch_prices(aux, ["AAA"], provider, since=date(2026, 4, 1), today=date(2026, 6, 2))
    assert (again[0].added, again[0].changed) == (0, 0)
    assert "2026-05-29,99.00" in path.read_text()  # an existing price is not overwritten
    refreshed = fetch_prices(aux, ["AAA"], provider, since=date(2026, 4, 1),
                             today=date(2026, 6, 2), refresh=True)
    assert refreshed[0].changed == 1 and "2026-05-29,11.50" in path.read_text()


def test_dry_run_writes_nothing_and_failures_are_reported_per_symbol(tmp_path: Path) -> None:
    provider = FakeProvider({"AAA": DAILY})
    results = fetch_prices(tmp_path, ["AAA", "ZZZ"], provider, since=date(2026, 4, 1),
                           today=date(2026, 6, 2), dry_run=True)
    assert [(r.symbol, r.added, r.error) for r in results] == [("AAA", 3, None), ("ZZZ", 0, "not found")]
    assert not list(tmp_path.glob("*.csv"))


def _yahoo_payload(closes: list[float | None]) -> bytes:
    stamps = [1_780_000_000 + i * 86_400 for i in range(len(closes))]
    return json.dumps({"chart": {"result": [{
        "meta": {"gmtoffset": -14_400}, "timestamp": stamps,
        "indicators": {"quote": [{"close": closes}]},
    }], "error": None}}).encode()


def test_yahoo_provider_reads_closes_and_skips_missing_ones() -> None:
    urls: list[str] = []

    def get(url: str) -> bytes:
        urls.append(url)
        return _yahoo_payload([10.0, None, 10.5])

    closes = YahooProvider(get).closes("BRK.B", date(2026, 5, 1), date(2026, 6, 1))
    assert [str(p) for _, p in closes] == ["10.0", "10.5"]
    assert "BRK.B" in urls[0] and "period1=" in urls[0]


@pytest.mark.parametrize("payload", [
    b'{"chart": {"result": null, "error": {"description": "No data found"}}}',
    b'{"chart": {"result": [{"timestamp": []}], "error": null}}',
    b"not json",
])
def test_yahoo_provider_turns_bad_responses_into_price_errors(payload: bytes) -> None:
    with pytest.raises(PriceError):
        YahooProvider(lambda url: payload).closes("X", date(2026, 5, 1), date(2026, 6, 1))


def test_prices_in_use_lists_symbols_with_a_quantity_and_the_earliest_start(tmp_path: Path) -> None:
    for name, start, rows in [
        ("base", "2026-05-31", "Brok,a,AAA,,10,,,,\nBrok,b,BBB,,,,100.00,,\nCash,,CASH,,5,,,,\n"),
        ("child", "", "Brok,c,CCC,,3,,,,\n"),
    ]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "scenario.csv").write_text(f"name,start\n{name},{start}\n")
        (folder / "account_values.csv").write_text(
            "account_name,lot,symbol,acquired,quantity,series,opening_balance,opening_cost_basis,x\n" + rows)
    use = prices.prices_in_use(tmp_path)
    assert use.symbols == ["AAA", "CCC"]  # BBB has no quantity, CASH is skipped
    assert use.earliest_start == date(2026, 5, 31)
    assert prices.default_since(use.earliest_start, date(2026, 9, 1)) == date(2026, 4, 1)


def test_fetch_command_writes_the_aux_files_for_the_symbols_in_use(
    test_data_dir: Path, add_account_value, monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_account_value("Asset_Roth_Alice", lot="x", symbol="AAA", quantity="10", opening_balance="1.00")
    provider = FakeProvider({"AAA": DAILY})
    monkeypatch.setitem(prices.PRICE_PROVIDERS, "yahoo", lambda: provider)
    runner = CliRunner()
    result = runner.invoke(main, ["sim", "prices", "fetch", "--data-dir", str(test_data_dir)])
    assert result.exit_code == 0, result.output
    assert "AAA: 3 added (latest 12.50 on 2026-06-02)" in result.output
    assert (test_data_dir / "aux" / "security_price__AAA.csv").exists()
    assert provider.asked[0][1] == date(2026, 4, 1)  # the month before the 2026-05-31 start
    missing = runner.invoke(main, ["sim", "prices", "fetch", "ZZZ", "--data-dir", str(test_data_dir)])
    assert missing.exit_code != 0 and "ZZZ: not found" in missing.output


def test_fetched_files_feed_the_opening_balance(
    test_data_dir: Path, add_account_value, monkeypatch: pytest.MonkeyPatch,
    load_test_copy,
) -> None:
    add_account_value("Asset_Roth_Alice", lot="x", symbol="AAA", quantity="10", opening_balance="")
    monkeypatch.setitem(prices.PRICE_PROVIDERS, "yahoo", lambda: FakeProvider({"AAA": DAILY}))
    result = CliRunner().invoke(main, ["sim", "prices", "fetch", "--data-dir", str(test_data_dir)])
    assert result.exit_code == 0, result.output
    import sqlite3
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        balance = con.execute(
            "SELECT opening_balance FROM account_model_member WHERE lot = 'x'").fetchone()[0]
    finally:
        con.close()
    assert balance == 11500  # 10 shares at the 2026-05-29 close, the last on or before the start


@pytest.fixture(scope="function")
def priced_lots(add_account_value, write_aux, update_account_value):
    """Roth_Alice gains three lots holding 10 shares of AAA: one whose balance matches
    the price at the 2026-05-31 start ($11.50 on 05-29), one that is stale, one
    whose symbol has no prices. A later price ($20) is in the file too."""
    write_aux("security_price__AAA.csv",
              "effective_date,price\n2026-05-29,11.50\n2026-09-24,20.00\n")
    add_account_value("Asset_Roth_Alice", lot="ok", symbol="AAA", quantity="10", opening_balance="115.00")
    add_account_value("Asset_Roth_Alice", lot="old", symbol="AAA", quantity="10", opening_balance="140.00")
    add_account_value("Asset_Roth_Alice", lot="bare", symbol="ZZZ", quantity="10", opening_balance="50.00")


def test_check_compares_balances_with_the_start_price_and_the_latest_price(
    priced_lots: None, test_data_dir: Path, load_test_copy,
) -> None:
    from analysis.sim.connection import ScenarioConnection
    load_test_copy()
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        frame = prices.check_prices(scenario).set_index("lot")
    assert frame.loc["ok", "value"] == pytest.approx(115.0) and pd.isna(frame.loc["ok", "flag"])
    assert frame.loc["ok", "latest_value"] == pytest.approx(200.0)  # what it is worth now
    assert frame.loc["old", "flag"] == "stale"
    assert frame.loc["old", "difference"] == pytest.approx((115 - 140) / 140)
    assert frame.loc["bare", "flag"] == "no price"
    assert list(frame.index) == ["ok", "old", "bare"]  # lots without a quantity are left out


def test_check_command_flags_stale_lots_and_exits_nonzero(
    priced_lots: None, test_data_dir: Path,
) -> None:
    runner = CliRunner()
    args = ["sim", "prices", "check", "--scenario", "retire_alice", "--data-dir", str(test_data_dir)]
    result = runner.invoke(main, args, env={"COLUMNS": "220"})
    assert result.exit_code != 0 and "2 lot(s) flagged" in result.output
    assert "stale" in result.output and "no price" in result.output
    loose = runner.invoke(main, [*args, "--tolerance", "50"], env={"COLUMNS": "220"})
    assert "stale" not in loose.output  # 18% is inside a 50% tolerance
