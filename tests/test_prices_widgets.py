"""The prices section of accounts.ipynb (#436): the price check, the symbol
table and one symbol's price history, read as the CLI reads them."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import matplotlib
import numpy as np
import pytest

matplotlib.use("Agg")

from analysis.sim.connection import ScenarioConnection
from analysis.sim.notebook_ui.prices_widgets import (
    price_check_view,
    price_check_widget,
    price_history_figure,
    price_history_widget,
    symbol_table_view,
    symbol_table_widget,
)
from analysis.sim import prices

QQQ_PRICES = "effective_date,price\n2025-12-31,400.00\n2026-09-30,580.00\n2026-10-13,609.00\n"


@pytest.fixture(scope="function")
def qqq_prices(test_data_dir: Path) -> Path:
    """The private copy's shared prices/ gets QQQ, and securities.csv names it."""
    (test_data_dir / "prices").mkdir(exist_ok=True)
    (test_data_dir / "prices" / "security_price__QQQ.csv").write_text(QQQ_PRICES)
    (test_data_dir / "securities.csv").write_text("symbol,name\nQQQ,Nasdaq 100\nABCDX,A fund\n")
    return test_data_dir


def test_check_view_puts_the_flagged_lots_first(
    priced_lots: None, test_data_dir: Path, load_test_copy,
) -> None:
    load_test_copy()
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        heading, frame = price_check_view(scenario)
        loose_heading, loose = price_check_view(scenario, 0.5)
    assert [lot for _account, lot in frame.index] == ["old", "bare", "ok"]
    assert list(frame["flag"]) == ["stale", "no price", ""]
    assert heading.startswith("**2 of 3 lot(s) flagged**")
    assert list(loose["flag"]) == ["no price", "", ""]  # 18% is inside 50%
    assert "1 of 3" in loose_heading


def test_check_view_says_so_when_no_lot_has_a_quantity(test_retire_alice_scenario: ScenarioConnection) -> None:
    heading, frame = price_check_view(test_retire_alice_scenario)
    assert frame.empty and "no lot with a symbol and a quantity" in heading


def test_check_widget_reruns_when_the_tolerance_changes(
    priced_lots: None, test_data_dir: Path, load_test_copy,
) -> None:
    load_test_copy()
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        widget = price_check_widget(scenario)
        tolerance, _out = widget.children
        assert tolerance.value == 1.0
        tolerance.value = 50.0  # must not raise


def test_symbol_table_names_each_symbol_with_its_prices(
    qqq_prices: Path, test_retire_alice_scenario: ScenarioConnection,
) -> None:
    frame = symbol_table_view(test_retire_alice_scenario)
    assert frame.loc["QQQ", "name"] == "Nasdaq 100"
    assert frame.loc["QQQ", "prices"] == "3"
    assert (frame.loc["QQQ", "latest_price"], frame.loc["QQQ", "latest_date"]) == ("609.00", "2026-10-13")
    assert frame.loc["ABCDX", "prices"] == "none"
    assert len(symbol_table_widget(test_retire_alice_scenario).children) == 1


def test_history_widget_offers_the_symbols_with_prices_and_limits_by_date(
    qqq_prices: Path, test_retire_alice_scenario: ScenarioConnection,
) -> None:
    widget = price_history_widget(test_retire_alice_scenario)
    selectors, _out = widget.children
    picker, since = selectors.children
    assert list(picker.options) == [("QQQ - Nasdaq 100", "QQQ")]
    since.value = date(2026, 10, 1)  # must not raise
    since.value = date(2030, 1, 1)  # nothing that late: says so


def test_history_widget_says_so_with_no_prices(test_retire_alice_scenario: ScenarioConnection) -> None:
    assert len(price_history_widget(test_retire_alice_scenario).children) == 1


def test_history_figure_draws_every_stored_price(tmp_path: Path) -> None:
    (tmp_path / "security_price__QQQ.csv").write_text(QQQ_PRICES)
    history = prices.price_history(tmp_path, "QQQ")
    assert history is not None
    fig = price_history_figure(history, "QQQ")
    line = fig.axes[0].get_lines()[0]
    assert np.asarray(line.get_ydata()).tolist() == [400.0, 580.0, 609.0]


def test_symbol_table_counts_a_scenarios_own_prices_as_present(
    test_data_dir: Path, add_account_value, retire_alice_csv,
    test_retire_alice_scenario: ScenarioConnection,
) -> None:
    own = retire_alice_csv("prices")
    own.mkdir(exist_ok=True)
    (own / "security_price__QQQX.csv").write_text("effective_date,price\n2026-05-29,10.00\n")
    add_account_value("Asset_Roth_Alice", lot="x", symbol="QQQX", quantity="10", opening_balance="1.00")
    frame = symbol_table_view(test_retire_alice_scenario)
    assert frame.loc["QQQX", "prices"] == "0 (own file in retire_alice)"  # not "none", so not red
