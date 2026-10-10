"""`sim symbol add|list`: a stock or fund's name, allocation and prices (issue #127)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import prices, symbols
from analysis.sim.prices import PriceError


class _Provider:
    name = "fake"

    def __init__(self, known: set[str]) -> None:
        self.known = known

    def closes(self, symbol, start, end):  # noqa: ARG002
        if symbol not in self.known:
            raise PriceError("not found")
        return [(date(2026, 5, 29), Decimal("11.50")), (date(2026, 6, 2), Decimal("12.50"))]


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(prices.PRICE_PROVIDERS, "yahoo", lambda: _Provider({"ABCDX"}))


def _sim(data_dir: Path, *args: str, input: str | None = None):
    return CliRunner().invoke(main, ["sim", "symbol", *args, "--data-dir", str(data_dir)],
                              input=input, terminal_width=200)


def test_add_writes_name_weights_and_prices(test_data_dir: Path, provider: None, load_test_copy) -> None:
    result = _sim(test_data_dir, "add", "ABCDX", "--name", "A Balanced Fund",
                  "--weight", "stock_us=60", "--weight", "bond=40%")
    assert result.exit_code == 0, result.output
    assert "ABCDX,A Balanced Fund" in (test_data_dir / "securities.csv").read_text()
    allocations = (test_data_dir / "allocations.csv").read_text().splitlines()
    assert allocations[1:] == ["ABCDX,,stock_us,60%", "ABCDX,,bond,40%"]  # % added on the way in
    assert (test_data_dir / "prices" / "security_price__ABCDX.csv").exists()
    load_test_copy()  # what it wrote loads


def test_add_without_weights_points_to_the_guide(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "add", "ABCDX", "--no-fetch")
    assert result.exit_code == 0, result.output
    assert "unclassified" in result.output and "Part 4.11" in result.output
    assert not (test_data_dir / "prices" / "security_price__ABCDX.csv").exists()


@pytest.mark.parametrize(("args", "message"), [
    (["--weight", "stock_us=60%", "--weight", "bond=30%"], "add up to 90%, not 100%"),
    (["--weight", "stocks=100%"], "unknown asset class 'stocks'"),
    (["--weight", "stock_us"], "must be CLASS=PERCENT"),
    (["--as-of", "2026-01"], "give the weights too"),
    (["--weight", "bond=100%", "--as-of", "soon"], "not a YYYY-MM month"),
])
def test_add_refuses_rows_that_would_not_load(test_data_dir: Path, args: list[str], message: str) -> None:
    before = (test_data_dir / "allocations.csv").read_text()
    result = _sim(test_data_dir, "add", "ABCDX", "--no-fetch", *args)
    assert result.exit_code != 0
    assert message in result.output
    assert (test_data_dir / "allocations.csv").read_text() == before


def test_add_refuses_a_second_name_or_a_second_set_for_one_date(test_data_dir: Path) -> None:
    first = _sim(test_data_dir, "add", "ABCDX", "--no-fetch", "--name", "A Fund",
                 "--weight", "bond=100%", "--as-of", "2026-01")
    assert first.exit_code == 0, first.output
    renamed = _sim(test_data_dir, "add", "ABCDX", "--no-fetch", "--name", "Another")
    assert renamed.exit_code != 0 and "already in securities.csv, named 'A Fund'" in renamed.output
    same_month = _sim(test_data_dir, "add", "ABCDX", "--no-fetch", "--weight", "cash=100%",
                      "--as-of", "2026-01-15")
    assert same_month.exit_code != 0 and "already has weights as of 2026-01-15" in same_month.output
    later = _sim(test_data_dir, "add", "ABCDX", "--no-fetch", "--weight", "cash=100%", "--as-of", "2027-01")
    assert later.exit_code == 0, later.output  # a later set follows a glide path


def test_add_keeps_the_rows_when_the_fetch_fails(test_data_dir: Path, provider: None) -> None:
    result = _sim(test_data_dir, "add", "ZZZ", "--weight", "other=100%")
    assert result.exit_code != 0
    assert "ZZZ was added, but its prices could not be fetched" in result.output
    assert "ZZZ,,other,100%" in (test_data_dir / "allocations.csv").read_text()


def test_guided_add_asks_and_prints_the_command(test_data_dir: Path) -> None:
    answers = "ABCDX\nA Fund\nstock_us=50% bond=40%\nstock_us=50% bond=50%\n2026-01\n"
    result = _sim(test_data_dir, "add", "--no-fetch", input=answers)
    assert result.exit_code == 0, result.output
    assert "these add up to 90%, not 100%" in result.output  # asked again
    assert ("equivalent command: sim symbol add ABCDX --name 'A Fund' --weight stock_us=50% "
            "--weight bond=50% --as-of 2026-01 --no-fetch") in result.output
    assert "ABCDX,2026-01,bond,50%" in (test_data_dir / "allocations.csv").read_text()


def test_list_shows_every_symbol_and_its_gaps(
    test_data_dir: Path, provider: None, add_account_value,
) -> None:
    assert _sim(test_data_dir, "add", "ABCDX", "--name", "A Fund", "--weight", "bond=100%").exit_code == 0
    add_account_value("Asset_Roth_Alice", lot="x", symbol="QQQX", quantity="10", opening_balance="1.00")
    frame = symbols.symbol_table(test_data_dir).set_index("symbol")
    assert frame.loc["ABCDX", "allocation"] == "bond 100%"
    assert frame.loc["ABCDX", "prices"] == 2 and frame.loc["ABCDX", "latest_date"] == "2026-06-02"
    assert frame.loc["QQQX", "allocation"] == "" and frame.loc["QQQX", "prices"] == 0
    assert frame.loc["QQQX", "held_by"] == ["retire_alice: Asset_Roth_Alice (x)"]
    result = _sim(test_data_dir, "list")
    assert result.exit_code == 0, result.output
    assert "ABCDX" in result.output and "QQQX" in result.output and "none" in result.output
