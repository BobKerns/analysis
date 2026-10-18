"""``sim invest`` (#217): a new investment in one command -- a new lot (and
account, and symbol, if new) and the transfer that buys it."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadResult

BROKERAGE = "Asset_Brokerage_Joint"


@pytest.fixture
def invest(test_data_dir: Path) -> Callable[..., Result]:
    def run(*args: str, answers: tuple[str, ...] = ()) -> Result:
        return CliRunner().invoke(
            main, ["sim", "invest", *args, "--scenario", "retire_alice", "--data-dir", str(test_data_dir)],
            input="".join(f"{answer}\n" for answer in answers),
        )
    return run


def _rows(path: Path, **match: str) -> list[dict[str, str]]:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    for column, value in match.items():
        frame = frame[frame[column] == value]
    return frame.to_dict("records")


def _legs_into(run: Path, account: str, lot: str) -> list[tuple[str, str, int, str]]:
    with sqlite3.connect(run) as con:
        return con.execute(
            "SELECT te.date, fa.name, te.amount, te.tax_character FROM transfer_event te "
            "JOIN account fa ON fa.id = te.from_account JOIN account_lot tm ON tm.id = te.to_lot_id "
            "JOIN account ta ON ta.id = tm.account_id "
            "WHERE ta.name = ? AND tm.lot = ? AND fa.kind = 'asset' ORDER BY te.id",
            (account, lot),
        ).fetchall()


def _run(load: Callable[[], LoadResult], runs_dir: Path) -> Path:
    return run_scenario(load().intent_path, runs_dir / "invest.sqlite3", run_by="T",
                        allow_dirty_data=True, allow_dirty_code=True)


def test_invest_writes_the_lot_and_the_transfer_that_buys_it(
    invest: Callable[..., Result], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    result = invest("Asset_Savings", BROKERAGE, "bonds", "--amount", "10000", "--date", "2027-03")

    assert result.exit_code == 0, result.output
    [lot] = _rows(retire_alice_csv("account_lots.csv"), account_name=BROKERAGE, lot="bonds")
    assert (lot["acquired"], lot["opening_balance"]) == ("2027-03-31", "0.00")
    [row] = _rows(retire_alice_csv("calendar.csv"), kind="one_off_transfer", lot_2="bonds")
    assert (row["date"], row["account_1"], row["lot_2"], row["amount"]) == (
        "2027-03", "Asset_Savings", "bonds", "10000.00")
    run = _run(load_test_copy, runs_dir)
    assert _legs_into(run, BROKERAGE, "bonds") == [("2027-03-31", "Asset_Savings", 1_000_000, "none")]
    with sqlite3.connect(run) as con:
        basis = con.execute(
            "SELECT ls.cost_basis FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
            "JOIN account_lot m ON m.id = ls.lot_id WHERE m.lot = 'bonds' AND s.month = '2027-03-31'"
        ).fetchone()[0]
    assert basis == 1_000_000  # bought: its basis is what it cost


def test_a_roth_conversion_is_one_command(
    invest: Callable[..., Result], load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """A conversion into its own Roth lot, dated: the traditional money is
    taxed as it leaves, and the new lot's basis is what was converted."""
    result = invest("Asset_TraditionalIRA_Alice", "Asset_Roth_Alice", "conv_2027",
                    "--subkind", "roth_ira", "--amount", "20000", "--date", "2027-01")

    assert result.exit_code == 0, result.output
    assert _legs_into(_run(load_test_copy, runs_dir), "Asset_Roth_Alice", "conv_2027") == [
        ("2027-01-31", "Asset_TraditionalIRA_Alice", 2_000_000, "tax_deferred_out"),
    ]


def test_invest_adds_a_new_account_and_symbol(
    invest: Callable[..., Result], retire_alice_csv: Callable[[str], Path], test_data_dir: Path,
) -> None:
    result = invest("Asset_Savings", "Asset_Brokerage_Bill", "vti", "--account-subkind", "brokerage_taxable",
                    "--owner", "bill", "--symbol", "vti", "--symbol-name", "Total market",
                    "--weight", "stock_us=100%", "--no-fetch", "--amount", "5000", "--date", "2028-01")

    assert result.exit_code == 0, result.output
    [account] = _rows(retire_alice_csv("accounts.csv"), name="Asset_Brokerage_Bill")
    # It grows at VTI's blend, funded as a taxable account's gains.
    assert (account["subkind"], account["owner"], account["tax_treatment"], account["tax_character"]) == (
        "brokerage_taxable", "bill", "taxable", "gains")
    [lot] = _rows(retire_alice_csv("account_lots.csv"), account_name="Asset_Brokerage_Bill")
    assert (lot["lot"], lot["symbol"]) == ("vti", "VTI")
    assert _rows(test_data_dir / "securities.csv", symbol="VTI") == [{"symbol": "VTI", "name": "Total market"}]
    assert _rows(test_data_dir / "allocations.csv", symbol="VTI")[0]["asset_class"] == "stock_us"


def test_guided_invest_asks_and_prints_the_command(
    invest: Callable[..., Result], retire_alice_csv: Callable[[str], Path],
) -> None:
    # source, account, lot, lot subkind, symbol, amount, date
    result = invest(answers=(
        "Asset_Savings", "Asset_Roth_Bill", "contrib_2027", "roth_ira", "", "7000", "2027-02",
    ))

    assert result.exit_code == 0, result.output
    assert ("sim invest Asset_Savings Asset_Roth_Bill contrib_2027 --amount 7000.00 --date 2027-02 "
            "--subkind roth_ira") in result.output
    [lot] = _rows(retire_alice_csv("account_lots.csv"), account_name="Asset_Roth_Bill", lot="contrib_2027")
    assert lot["subkind"] == "roth_ira"


@pytest.mark.parametrize(("args", "error"), [
    ((BROKERAGE, "default", "--amount", "1"), "a new investment is a new lot"),
    (("Asset_Roth_Alice", "x", "--amount", "1"), "a retirement account's lot needs --subkind"),
    ((BROKERAGE, "x", "--subkind", "roth_ira", "--amount", "1"), "--subkind is only for"),
    ((BROKERAGE, "x", "--amount", "0"), "more than zero"),
])
def test_invest_refuses(
    invest: Callable[..., Result], retire_alice_csv: Callable[[str], Path],
    args: tuple[str, ...], error: str,
) -> None:
    before = retire_alice_csv("calendar.csv").read_text()
    result = invest("Asset_Savings", *args, answers=("",))
    assert result.exit_code != 0
    assert error in result.output
    assert retire_alice_csv("calendar.csv").read_text() == before
