"""Selling an asset through a destination chain whose steps carry tax
characters (#199): a basis step recovers the sold lot's basis, later steps
characterize the gain, and basis a sale can't recover is a capital loss the
tax accumulator nets, limits and carries forward -- or, for a home, a
personal loss that isn't deductible."""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.chain_edit import CHAIN_COLUMNS
from analysis.sim.engine import EngineError, _capital_gains, run_scenario
from analysis.sim.loader import LoadError, LoadResult

SALE = {
    "date": "2030-06-30", "kind": "transfer_balance",
    "account_1": "Asset_Home", "account_2": "home_sale",
    "description": "sell the home",
}


def _chain(retire_alice_csv: Callable[[str], Path], role: str, name: str,
           steps: list[dict[str, str]]) -> None:
    path = retire_alice_csv(f"chains/{role}/{name}.csv")
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CHAIN_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(steps)


def _home_sale_chain(retire_alice_csv: Callable[[str], Path], exclusion: str = "150000.00") -> None:
    _chain(retire_alice_csv, "destination", "sale_proceeds",
           [{"account_name": "Liability_Mortgage"}, {"account_name": "Asset_Checking"}])
    _chain(retire_alice_csv, "destination", "home_sale", [
        {"account_name": "sale_proceeds", "tax_character": "home_sale", "basis_of": "Asset_Home"},
        {"account_name": "sale_proceeds", "maximum": exclusion, "tax_character": "excluded_gain"},
        {"account_name": "sale_proceeds", "tax_character": "ltcg"},
    ])


def _set_value(retire_alice_csv: Callable[[str], Path], account: str, **columns: str) -> None:
    path = retire_alice_csv("account_values.csv")
    values = pd.read_csv(path, dtype=str, keep_default_na=False)
    for column, value in columns.items():
        values.loc[values["account_name"] == account, column] = value
    values.to_csv(path, index=False)


def _add_rule(retire_alice_csv: Callable[[str], Path], **fields: str) -> None:
    path = retire_alice_csv("scheduled.csv")
    rules = pd.read_csv(path, dtype=str, keep_default_na=False)
    pd.concat([rules, pd.DataFrame([fields])], ignore_index=True).fillna("").to_csv(path, index=False)


def _run(load_test_copy: Callable[[], LoadResult], runs_dir: Path, name: str = "run") -> Path:
    run_path = runs_dir / f"{name}.sqlite3"
    run_scenario(load_test_copy().intent_path, run_path, run_by="test", allow_dirty_data=True)
    return run_path


def _legs_out_of(run: Path, account: str) -> dict[str, int]:
    with sqlite3.connect(run) as con:
        return dict(con.execute(
            "SELECT te.tax_character, SUM(te.amount) FROM transfer_event te "
            "JOIN account fa ON fa.id = te.from_account WHERE fa.name = ? "
            "GROUP BY te.tax_character",
            (account,),
        ).fetchall())


def _last_state(run: Path, account: str, month: str = "9999-12-31") -> tuple[int, int | None]:
    """``account``'s balance and cost basis at the end of ``month`` (default:
    of the run)."""
    with sqlite3.connect(run) as con:
        return con.execute(
            "SELECT ls.balance, ls.cost_basis FROM lot_state ls "
            "JOIN simulation_step s ON s.id = ls.step_id "
            "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
            "WHERE a.name = ? AND s.month <= ? ORDER BY ls.step_id DESC LIMIT 1",
            (account, month),
        ).fetchone()


def _capital_losses(run: Path) -> list[tuple[str, int, str]]:
    with sqlite3.connect(run) as con:
        return con.execute(
            "SELECT a.name, cl.amount, cl.tax_character FROM capital_loss cl "
            "JOIN account_lot m ON m.id = cl.lot_id JOIN account a ON a.id = m.account_id"
        ).fetchall()


def test_a_home_sale_recovers_the_basis_then_excludes_then_taxes_the_gain(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """The prototype home: $600,000, with a $400,000 basis. The mortgage is
    paid off from the proceeds and the rest lands in checking."""
    _home_sale_chain(retire_alice_csv)
    append_calendar_row(**SALE)
    run = _run(load_test_copy, runs_dir)
    assert _legs_out_of(run, "Asset_Home") == {
        "home_sale": 400_000_00, "excluded_gain": 150_000_00, "ltcg": 50_000_00,
    }
    assert _last_state(run, "Asset_Home") == (0, 0)
    assert _last_state(run, "Liability_Mortgage")[0] == 0
    assert _capital_losses(run) == []


def test_a_home_sold_at_a_loss_writes_off_its_basis(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """A personal residence's loss isn't deductible: the basis step takes
    the whole sale, and nothing is recorded for the tax accumulator."""
    _set_value(retire_alice_csv, "Asset_Home", opening_balance="300000.00")
    _home_sale_chain(retire_alice_csv)
    append_calendar_row(**SALE)
    run = _run(load_test_copy, runs_dir)
    assert _legs_out_of(run, "Asset_Home") == {"home_sale": 300_000_00}
    assert _last_state(run, "Asset_Home") == (0, 0)
    assert _capital_losses(run) == []


def test_an_investment_sold_at_a_loss_records_the_basis_it_could_not_recover(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    _set_value(retire_alice_csv, "Asset_Brokerage_Joint", opening_cost_basis="400000.00")
    _chain(retire_alice_csv, "destination", "brokerage_sale", [
        {"account_name": "Asset_Savings", "tax_character": "return_of_capital",
         "basis_of": "Asset_Brokerage_Joint", "basis_of_lot": "default"},
        {"account_name": "Asset_Savings", "tax_character": "ltcg"},
    ])
    append_calendar_row(date="2027-01-31", kind="transfer_balance",
                        account_1="Asset_Brokerage_Joint", lot_1="default",
                        account_2="brokerage_sale", description="sell the brokerage lot")
    run = _run(load_test_copy, runs_dir)
    legs = _legs_out_of(run, "Asset_Brokerage_Joint")
    assert set(legs) == {"return_of_capital"}
    [(account, loss, character)] = _capital_losses(run)
    assert (account, character) == ("Asset_Brokerage_Joint", "ltcg")
    assert loss + legs["return_of_capital"] == 400_000_00
    assert _last_state(run, "Asset_Brokerage_Joint", "2027-01-31") == (0, 0)


def test_a_lot_swept_through_a_chain_without_a_basis_step_is_split_as_usual(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    """With no basis step to size the basis, the chain's ltcg would tax the
    lot's basis as gain; the withdrawal's pro-rata split stands instead."""
    _set_value(retire_alice_csv, "Asset_Brokerage_Joint", maximum_balance="250000.00",
               overflow_chain="brokerage_trim")
    _chain(retire_alice_csv, "destination", "brokerage_trim", [
        {"account_name": "Asset_Savings", "tax_character": "ltcg"},
    ])
    legs = _legs_out_of(_run(load_test_copy, runs_dir), "Asset_Brokerage_Joint")
    assert legs.get("return_of_capital", 0) > 0
    assert legs.get("ltcg", 0) > 0


def test_a_capital_improvement_adds_to_the_basis(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    _chain(retire_alice_csv, "destination", "remodel", [
        {"account_name": "Expense_Housing", "tax_character": "capital_improvement",
         "basis_of": "Asset_Home"},
    ])
    append_calendar_row(date="2027-03-31", kind="one_off_transfer", account_1="Asset_Savings",
                        account_2="remodel", amount="15000.00", tax_character="none",
                        description="new kitchen")
    run = _run(load_test_copy, runs_dir)
    assert _last_state(run, "Asset_Home") == (600_000_00, 415_000_00)
    with sqlite3.connect(run) as con:
        assert con.execute(
            "SELECT tax_character FROM transfer_event te JOIN account ta ON ta.id = te.to_account "
            "WHERE ta.name = 'Expense_Housing' AND te.date = '2027-03-31' AND amount = 1500000"
        ).fetchall() == [("capital_improvement",)]


def test_a_basis_step_for_another_lot_fails_the_run(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    _home_sale_chain(retire_alice_csv)
    append_calendar_row(date="2030-06-30", kind="transfer_balance", account_1="Asset_Savings",
                        account_2="home_sale", description="not the home")
    with pytest.raises(EngineError, match="recovers the basis of 'Asset_Home'"):
        _run(load_test_copy, runs_dir)


def test_a_basis_step_an_earlier_step_starves_fails_the_run(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    _chain(retire_alice_csv, "destination", "home_sale", [
        {"account_name": "Asset_Checking", "tax_character": "ltcg"},
        {"account_name": "Asset_Checking", "tax_character": "home_sale", "basis_of": "Asset_Home"},
        {"account_name": "Asset_Checking", "tax_character": "ltcg"},
    ])
    append_calendar_row(**SALE)
    with pytest.raises(EngineError, match="a basis step must take the sale's first money"):
        _run(load_test_copy, runs_dir)


def test_a_basis_step_no_money_reaches_fails_the_run(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    _chain(retire_alice_csv, "destination", "basis_first", [
        {"account_name": "Asset_Checking", "tax_character": "home_sale", "basis_of": "Asset_Home"},
        {"account_name": "Asset_Checking", "tax_character": "ltcg"},
    ])
    _chain(retire_alice_csv, "destination", "home_sale", [
        {"account_name": "Asset_Checking"}, {"account_name": "basis_first"},
    ])
    append_calendar_row(**SALE)
    with pytest.raises(EngineError, match="its basis step for 'Asset_Home' was never reached"):
        _run(load_test_copy, runs_dir)


def _gain(con: sqlite3.Connection, year: int, character: str, amount: int) -> None:
    con.execute(
        "INSERT INTO transfer_event(step_id, date, from_account, to_account, from_lot_id, "
        "to_lot_id, amount, requested_cents, remainder_cents, tax_character, reason_kind) "
        "VALUES (1, ?, 1, 2, 1, 2, ?, ?, 0, ?, 'rule')",
        (f"{year}-06-30", amount, amount, character),
    )


def _loss(con: sqlite3.Connection, year: int, character: str, amount: int) -> None:
    con.execute(
        "INSERT INTO capital_loss(step_id, date, lot_id, amount, tax_character, reason_kind) "
        "VALUES (1, ?, 1, ?, ?, 'rule')",
        (f"{year}-06-30", amount, character),
    )


def test_a_capital_loss_offsets_three_thousand_a_year_and_carries_the_rest(
    ledger_con: sqlite3.Connection,
) -> None:
    _loss(ledger_con, 2026, "ltcg", 10_000_00)
    _gain(ledger_con, 2027, "ltcg", 2_000_00)

    def gains(year: int) -> tuple[int, int, int, int]:
        result = _capital_gains(ledger_con, tax_year=year, loss_limit=lambda _y: 3_000_00)
        return result.ltcg, result.loss_deduction, result.carryover_st, result.carryover_lt

    assert gains(2026) == (0, 3_000_00, 0, 7_000_00)
    # The carryover first absorbs 2027's gain, then $3,000 more of income.
    assert gains(2027) == (0, 3_000_00, 0, 2_000_00)
    assert gains(2028) == (0, 2_000_00, 0, 0)
    assert gains(2029) == (0, 0, 0, 0)


def test_short_and_long_term_results_net_before_the_limit(ledger_con: sqlite3.Connection) -> None:
    """A short-term loss offsets a long-term gain; a loss left over keeps the
    character of the side that had it, and the deduction uses short-term first."""
    _gain(ledger_con, 2026, "ltcg", 4_000_00)
    _loss(ledger_con, 2026, "stcg", 1_000_00)
    netted = _capital_gains(ledger_con, tax_year=2026, loss_limit=lambda _y: 3_000_00)
    assert (netted.stcg, netted.ltcg, netted.loss_deduction) == (0, 3_000_00, 0)

    _loss(ledger_con, 2027, "stcg", 2_000_00)
    _gain(ledger_con, 2027, "ltcg", -5_000_00)  # a withdrawal at a loss posts a negative leg
    both = _capital_gains(ledger_con, tax_year=2027, loss_limit=lambda _y: 3_000_00)
    assert (both.loss_deduction, both.carryover_st, both.carryover_lt) == (3_000_00, 0, 4_000_00)


def test_the_tax_summary_shows_the_deduction_and_the_carryover(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """A federal tax_accumulator for 2027 sees the brokerage loss through the
    capital_loss_limit table."""
    _set_value(retire_alice_csv, "Asset_Brokerage_Joint", opening_cost_basis="400000.00")
    _chain(retire_alice_csv, "destination", "brokerage_sale", [
        {"account_name": "Asset_Savings", "tax_character": "return_of_capital",
         "basis_of": "Asset_Brokerage_Joint", "basis_of_lot": "default"},
        {"account_name": "Asset_Savings", "tax_character": "ltcg"},
    ])
    append_calendar_row(date="2027-01-31", kind="transfer_balance",
                        account_1="Asset_Brokerage_Joint", lot_1="default",
                        account_2="brokerage_sale", description="sell the brokerage lot")
    _add_rule(retire_alice_csv, name="federal_tax", from_account="Asset_Checking",
              to_account="Expense_Taxes_Federal", cadence="annual", month_of_year="4",
              amount_rule_kind="tax_accumulator", tax_character="none", start="2028-04-30",
              priority="400", meta_json="{}", params_json='{"year": "previous"}')
    run = _run(load_test_copy, runs_dir)
    with sqlite3.connect(run) as con:
        summary = dict(con.execute(
            "SELECT bucket, amount FROM tax_year_summary WHERE year = 2027 "
            "AND bucket LIKE 'federal_capital_loss_%'"
        ).fetchall())
    [(_account, loss, _character)] = _capital_losses(run)
    assert summary == {
        "federal_capital_loss_deduction": 3_000_00,
        "federal_capital_loss_carryover": loss - 3_000_00,
    }


@pytest.mark.parametrize(("steps", "message"), [
    ([{"account_name": "Asset_Checking", "basis_of": "Asset_Home"}],
     "basis_of needs tax_character"),
    ([{"account_name": "Asset_Checking", "tax_character": "home_sale"}],
     "tax_character home_sale needs basis_of"),
    ([{"account_name": "Asset_Checking", "tax_character": "home_sale", "basis_of": "Expense_Living"}],
     "basis_of 'Expense_Living' is not an asset account"),
    ([{"account_name": "Asset_Checking", "tax_character": "home_sale", "basis_of": "Asset_Home",
       "maximum": "100.00"}],
     "maximum must be blank"),
    ([{"account_name": "Asset_Checking", "tax_character": "home_sale", "basis_of": "Asset_Home"},
      {"account_name": "Asset_Savings"}],
     "the step after a basis step must set its own tax_character"),
    ([{"account_name": "Asset_Checking", "tax_character": "ltcg", "basis_of_lot": "default"}],
     "basis_of_lot needs basis_of"),
    ([{"account_name": "Asset_Checking", "tax_character": "bogus"}],
     "unknown tax_character 'bogus'"),
    ([{"account_name": "Asset_Checking", "tax_character": "home_sale",
       "basis_of": "Asset_Brokerage_Joint"}],
     "basis_of 'Asset_Brokerage_Joint' is an investment account and needs a basis_of_lot"),
])
def test_the_loader_rejects_a_malformed_tax_step(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
    steps: list[dict[str, str]], message: str,
) -> None:
    _chain(retire_alice_csv, "destination", "bad", steps)
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_a_source_chain_takes_no_tax_columns(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    _chain(retire_alice_csv, "source", "bad", [
        {"account_name": "Asset_Savings", "tax_character": "ltcg"},
    ])
    with pytest.raises(LoadError, match="belong to destination chains"):
        load_test_copy()


def test_the_home_sale_rule_kind_names_its_replacement(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    _add_rule(retire_alice_csv, name="sell_home", from_account="Asset_Home",
              to_account="Asset_Checking", cadence="one_off", amount_rule_kind="home_sale",
              amount="600000.00", tax_character="none", start="2030-06-30", priority="500",
              meta_json="{}")
    with pytest.raises(LoadError, match="home_sale rule kind is gone"):
        load_test_copy()


def test_a_home_sale_exclusion_table_names_its_replacement(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("home_sale_exclusion.csv", "effective_date,filing_status,amount\n1997-05-31,MFJ,500000.00\n")
    with pytest.raises(LoadError, match="the home-sale exclusion is now the maximum"):
        load_test_copy()


def test_chain_commands_write_and_keep_the_tax_columns(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    def sim(*args: str) -> str:
        result = CliRunner().invoke(main, [
            "sim", "chain", *args, "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
        ])
        assert result.exit_code == 0, result.output
        return result.output

    sim("add", "home_sale", "Asset_Checking", "--role", "destination",
        "--tax-character", "home_sale", "--basis-of", "Asset_Home")
    added = sim("step", "add", "home_sale", "Asset_Checking", "--tax-character", "ltcg")
    assert "taxed as ltcg" in added
    sim("step", "edit", "home_sale", "1", "--maximum", "")
    with retire_alice_csv("chains/destination/home_sale.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["tax_character"], r["basis_of"]) for r in rows] == [
        ("home_sale", "Asset_Home"), ("ltcg", ""),
    ]
    load_test_copy()


def _sim_ok(data_dir: Path, *args: str) -> None:
    result = CliRunner().invoke(main, [
        "sim", *args, "--data-dir", str(data_dir), "--scenario", "retire_alice",
    ])
    assert result.exit_code == 0, result.output


def _steps(path: Path) -> list[tuple[str, str]]:
    with path.open() as fh:
        return [(row["basis_of"], row["basis_of_lot"]) for row in csv.DictReader(fh)]


def test_renaming_an_account_or_lot_follows_the_basis_columns(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    _home_sale_chain(retire_alice_csv)
    _chain(retire_alice_csv, "destination", "brokerage_sale", [
        {"account_name": "Asset_Savings", "tax_character": "return_of_capital",
         "basis_of": "Asset_Brokerage_Joint", "basis_of_lot": "default"},
        {"account_name": "Asset_Savings", "tax_character": "ltcg"},
    ])
    _sim_ok(test_data_dir, "account", "rename", "Asset_Home", "Asset_House")
    _sim_ok(test_data_dir, "account", "lot", "rename", "Asset_Brokerage_Joint", "default", "core")
    assert _steps(retire_alice_csv("chains/destination/home_sale.csv"))[0] == ("Asset_House", "")
    assert _steps(retire_alice_csv("chains/destination/brokerage_sale.csv"))[0] == (
        "Asset_Brokerage_Joint", "core",
    )
    load_test_copy()
