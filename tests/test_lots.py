"""Lot-keyed state (ADR-005): one account_model_member row per lot."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from analysis.sim.engine import _post_transfer
from analysis.sim.loader import LoadError, LoadResult


def test_every_account_has_a_default_lot_and_leaf_chain(retire_alice_intent_file: Path) -> None:
    con = sqlite3.connect(retire_alice_intent_file)
    try:
        accounts = con.execute("SELECT COUNT(*) FROM account").fetchone()[0]
        lots = con.execute(
            "SELECT lot, COUNT(*) FROM account_model_member GROUP BY lot"
        ).fetchall()
        assert lots == [("default", accounts)]
        unlinked = con.execute(
            "SELECT COUNT(*) FROM chain c "
            "LEFT JOIN account_model_member m ON m.id = c.lot_id AND m.account_id = c.account_id "
            "WHERE c.account_id IS NOT NULL AND m.id IS NULL"
        ).fetchone()[0]
        assert unlinked == 0
    finally:
        con.close()


def test_account_state_view_sums_lots_and_nulls_partial_basis(ledger_con: sqlite3.Connection) -> None:
    ledger_con.execute(
        "INSERT INTO account_model_member(id, model_id, account_id, lot, opening_balance) "
        "VALUES (200, 1, 5, 'second', 0)"
    )
    ledger_con.execute(
        "INSERT INTO simulation_step(id, step_index, month) VALUES (1, 0, '2026-05-31')"
    )
    ledger_con.executemany(
        "INSERT INTO lot_state(step_id, lot_id, balance, cost_basis) VALUES (1, ?, ?, ?)",
        [(5, 100, 40), (200, 50, 30)],
    )
    assert ledger_con.execute(
        "SELECT account_id, balance, cost_basis FROM account_state"
    ).fetchall() == [(5, 150, 70)]

    ledger_con.execute("UPDATE lot_state SET cost_basis = NULL WHERE lot_id = 200")
    assert ledger_con.execute(
        "SELECT balance, cost_basis FROM account_state"
    ).fetchall() == [(150, None)]


def test_post_transfer_records_lot_and_owning_account(ledger_con: sqlite3.Connection) -> None:
    ledger_con.execute(
        "INSERT INTO account_model_member(id, model_id, account_id, lot, opening_balance) "
        "VALUES (200, 1, 5, 'second', 0)"
    )
    balances = {200: 1_000, 2: 0}
    _post_transfer(
        ledger_con, balances, {200: "asset", 2: "asset"}, 1, "2026-05-31",
        200, 2, 400, "none", 99,
    )
    assert balances == {200: 600, 2: 400}
    assert ledger_con.execute(
        "SELECT from_account, to_account, from_lot_id, to_lot_id FROM transfer_event"
    ).fetchone() == (5, 2, 200, 2)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path.name, old)
    path.write_text(text.replace(old, new, 1))


def test_duplicate_account_and_lot_rejected(
    add_account_value: Callable[..., None], load_test_copy: Callable[[], LoadResult]
) -> None:
    add_account_value("Asset_Checking", opening_balance="1.00")
    with pytest.raises(LoadError, match="duplicate account_name and lot"):
        load_test_copy()


def test_lot_label_may_not_contain_colon(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _edit(retire_alice_csv("account_values.csv"), "Asset_Roth_Bob,default,", "Asset_Roth_Bob,a:b,")
    with pytest.raises(LoadError, match="may not contain ':'"):
        load_test_copy()


def test_investment_account_row_requires_explicit_lot(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _edit(retire_alice_csv("account_values.csv"), "Asset_Roth_Bob,default,", "Asset_Roth_Bob,,")
    with pytest.raises(LoadError, match=r"Asset_Roth_Bob\): investment account requires an explicit lot"):
        load_test_copy()


def test_investment_reference_without_a_lot_lists_its_lots(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _edit(retire_alice_csv("calendar.csv"),
          ",Asset_Brokerage_Joint,default,", ",Asset_Brokerage_Joint,,")
    with pytest.raises(LoadError, match=r"needs a lot_2 \(its lots: default\)"):
        load_test_copy()


def test_unknown_lot_reference_rejected(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _edit(retire_alice_csv("calendar.csv"),
          ",Asset_Brokerage_Joint,default,", ",Asset_Brokerage_Joint,nope,")
    with pytest.raises(LoadError, match="has no lot 'nope'"):
        load_test_copy()


def test_rmd_on_multi_lot_account_rejected(
    add_account_value: Callable[..., None], load_test_copy: Callable[[], LoadResult]
) -> None:
    add_account_value("Asset_TraditionalIRA_Alice", lot="b", opening_balance="10000.00",
                      prior_year_end_balance="9000.00")
    with pytest.raises(LoadError, match="rmd on multi-lot account 'Asset_TraditionalIRA_Alice'"):
        load_test_copy()


def test_prior_year_end_balance_is_all_or_none_per_account(
    add_account_value: Callable[..., None], update_account_value: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_account_value("Asset_Roth_Alice", lot="b", opening_balance="10000.00")
    update_account_value("Asset_Roth_Alice", "default", prior_year_end_balance="90000.00")
    with pytest.raises(LoadError, match="prior_year_end_balance must be set on every lot"):
        load_test_copy()


def test_cash_management_only_on_single_lot_accounts(
    add_account_value: Callable[..., None], load_test_copy: Callable[[], LoadResult]
) -> None:
    add_account_value("Asset_Checking", lot="b", opening_balance="10.00")
    with pytest.raises(LoadError, match=r"Asset_Checking\): minimum_balance.*single-lot accounts"):
        load_test_copy()


def test_two_lot_account_has_a_lot_state_row_per_lot(two_lot_run_file: Path) -> None:
    con = sqlite3.connect(two_lot_run_file)
    try:
        lots = dict(con.execute(
            "SELECT m.lot, m.id FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = 'Asset_Brokerage_Joint'"
        ).fetchall())
        assert set(lots) == {"a", "b"}
        steps = con.execute("SELECT COUNT(*) FROM simulation_step").fetchone()[0]
        for lot_id in lots.values():
            assert con.execute(
                "SELECT COUNT(*) FROM lot_state WHERE lot_id = ?", (lot_id,)
            ).fetchone()[0] == steps
        # The account-level view is the sum of its lots.
        sums = con.execute(
            "SELECT ls.step_id, SUM(ls.balance) FROM lot_state ls "
            "WHERE ls.lot_id IN (?, ?) GROUP BY ls.step_id ORDER BY ls.step_id",
            tuple(lots.values()),
        ).fetchall()
        viewed = con.execute(
            "SELECT ast.step_id, ast.balance FROM account_state ast "
            "JOIN account a ON a.id = ast.account_id "
            "WHERE a.name = 'Asset_Brokerage_Joint' ORDER BY ast.step_id"
        ).fetchall()
        assert sums == viewed
    finally:
        con.close()


def test_each_lot_grows_at_its_own_series(two_lot_run_file: Path) -> None:
    con = sqlite3.connect(two_lot_run_file)
    try:
        rules = dict(con.execute(
            "SELECT name, series FROM scheduled_transaction WHERE name LIKE 'growth_asset_brokerage_joint%'"
        ).fetchall())
        assert rules == {
            "growth_asset_brokerage_joint_a": "AAA",
            "growth_asset_brokerage_joint_b": "brokerage growth",
        }
    finally:
        con.close()


def test_sale_from_one_lot_leaves_the_other_lots_basis_alone(two_lot_run_file: Path) -> None:
    con = sqlite3.connect(two_lot_run_file)
    try:
        lots = dict(con.execute(
            "SELECT m.lot, m.id FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = 'Asset_Brokerage_Joint'"
        ).fetchall())
        def basis(lot: str) -> list[int]:
            return [r[0] for r in con.execute(
                "SELECT cost_basis FROM lot_state WHERE lot_id = ? ORDER BY step_id",
                (lots[lot],))]
        assert set(basis("a")) == {6_000_000}
        b = basis("b")
        assert b[0] < 14_000_000 and b == sorted(b, reverse=True) and b[-1] > 0

        basis_returned = con.execute(
            "SELECT SUM(amount) FROM transfer_event "
            "WHERE from_lot_id = ? AND tax_character = 'return_of_capital'",
            (lots["b"],),
        ).fetchone()[0]
        assert 14_000_000 - b[-1] == basis_returned
        assert con.execute(
            "SELECT COUNT(*) FROM transfer_event "
            "WHERE from_lot_id = ? AND tax_character = 'return_of_capital'",
            (lots["a"],),
        ).fetchone()[0] == 0

        account_basis = con.execute(
            "SELECT cost_basis FROM account_state ast JOIN account a ON a.id = ast.account_id "
            "WHERE a.name = 'Asset_Brokerage_Joint' ORDER BY ast.step_id DESC LIMIT 1"
        ).fetchone()[0]
        assert account_basis == 6_000_000 + b[-1]
    finally:
        con.close()


def test_sale_reduces_only_that_lots_quantity(two_lot_run_file: Path) -> None:
    con = sqlite3.connect(two_lot_run_file)
    try:
        def quantities(lot: str) -> list[int]:
            return [r[0] for r in con.execute(
                "SELECT ls.quantity_micros FROM lot_state ls "
                "JOIN account_model_member m ON m.id = ls.lot_id "
                "JOIN account a ON a.id = m.account_id "
                "WHERE a.name = 'Asset_Brokerage_Joint' AND m.lot = ? "
                "ORDER BY ls.step_id", (lot,))]
        assert set(quantities("a")) == {1_000_000_000}
        b = quantities("b")
        assert b == sorted(b, reverse=True)
        assert 0 < b[-1] < b[0] <= 2_000_500_000
    finally:
        con.close()


def test_post_transfer_reduces_quantity_pro_rata_and_zeroes_on_full_drain(
    ledger_con: sqlite3.Connection,
) -> None:
    from analysis.sim.engine import ChainGraph, RefillContext

    ctx = RefillContext(
        ChainGraph({}, {}, {}, {}, {}, {}, {}, {}), {}, date(2026, 5, 31), {},
        {}, {}, {}, {1: 500_00}, {1: 1_000_000_000},
    )
    balances = {1: 1_000_00, 2: 0}
    kinds = {1: "asset", 2: "asset"}
    _post_transfer(ledger_con, balances, kinds, 1, "2026-05-31", 1, 2, 250_00, "none", 9,
                   refill_ctx=ctx)
    assert ctx.quantities[1] == 750_000_000
    _post_transfer(ledger_con, balances, kinds, 1, "2026-06-30", 1, 2, 750_00, "none", 9,
                   refill_ctx=ctx)
    assert ctx.quantities[1] == 0


def _liquidity_chain(retire_alice_csv: Callable[[str], Path]) -> Path:
    return retire_alice_csv("chains") / "asset__asset_checking_liquidity.csv"


def test_chain_file_lot_column_matches_account_lot_spelling(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _liquidity_chain(retire_alice_csv).write_text(
        "account_name,lot,maximum,percent,series,cadence\n"
        "Asset_Savings,default,,,,\n"
        "Asset_TraditionalIRA_Alice,default,,,,\n"
    )
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        draws = [r[0] for r in con.execute(
            "SELECT c.name FROM chain_steps cs JOIN chain c ON c.id = cs.draw_from_chain_id "
            "JOIN chain owner ON owner.id = cs.chain_id "
            "WHERE owner.name = 'asset checking liquidity' ORDER BY cs.step_order"
        )]
        assert draws == ["Asset_Savings", "Asset_TraditionalIRA_Alice:default"]
    finally:
        con.close()


def test_chain_file_investment_row_without_lot_is_rejected(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _liquidity_chain(retire_alice_csv).write_text(
        "account_name,lot,maximum,percent,series,cadence\n"
        "Asset_TraditionalIRA_Alice,,,,,\n"
    )
    with pytest.raises(LoadError, match="needs a lot"):
        load_test_copy()


def test_colon_spelling_is_not_a_reference(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _liquidity_chain(retire_alice_csv).write_text(
        "account_name,lot,maximum,percent,series,cadence\n"
        "Asset_TraditionalIRA_Alice:default,,,,,\n"
    )
    with pytest.raises(LoadError, match="'Asset_TraditionalIRA_Alice:default' is not a known account"):
        load_test_copy()


def _append_scheduled_row(path: Path, **fields: str) -> None:
    import pandas as pd

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.loc[len(df)] = {**dict.fromkeys(df.columns, ""), **fields}
    df.to_csv(path, index=False)


def test_scheduled_row_to_investment_account_needs_a_to_lot(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _append_scheduled_row(
        retire_alice_csv("scheduled.csv"), name="sweep", from_account="Asset_Checking",
        to_account="Asset_Brokerage_Joint", cadence="monthly",
        amount_rule_kind="fixed_amount", amount="10.00", tax_character="none",
        start="2026-05-31", priority="500", meta_json="{}",
    )
    with pytest.raises(LoadError, match=r"sweep\): to_account 'Asset_Brokerage_Joint' .* needs a to_lot"):
        load_test_copy()


def test_scheduled_row_lot_column_selects_that_lot(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _append_scheduled_row(
        retire_alice_csv("scheduled.csv"), name="sweep", from_account="Asset_Checking",
        to_account="Asset_Brokerage_Joint", to_lot="default", cadence="monthly",
        amount_rule_kind="fixed_amount", amount="10.00", tax_character="none",
        start="2026-05-31", priority="500", meta_json="{}",
    )
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        assert con.execute(
            "SELECT c.name FROM scheduled_transaction s JOIN chain c ON c.id = s.to_account "
            "WHERE s.name = 'sweep'"
        ).fetchone() == ("Asset_Brokerage_Joint:default",)
    finally:
        con.close()


def test_year_to_date_row_lot_column_is_checked(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    retire_alice_csv("year_to_date.csv").write_text(
        "name,from_account,to_account,to_lot,amount,tax_character\n"
        "growth,Income_CapitalGain,Asset_Brokerage_Joint,nope,1.00,ltcg\n"
    )
    with pytest.raises(LoadError, match="has no lot 'nope'"):
        load_test_copy()


def test_scheduled_list_and_accounts_view_handle_multi_lot_accounts(
    two_lot_run_file: Path, test_data_dir: Path
) -> None:
    from click.testing import CliRunner

    from analysis.cli import main
    from analysis.sim.accounts import load_accounts_view
    from analysis.sim.connection import open_scenario

    result = CliRunner().invoke(main, [
        "sim", "scheduled", "list", "--scenario", "retire_alice",
        "--data-dir", str(test_data_dir),
    ], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    assert "growth_asset_brokerage_joint_a" in result.output
    assert "Asset_Brokerage_Joint:b" in result.output

    with open_scenario(test_data_dir, "retire_alice") as sc:
        brokerage = next(r for r in load_accounts_view(sc) if r.name == "Asset_Brokerage_Joint")
    assert brokerage.opening_balance == 300_000.0
    assert brokerage.cost_basis == 200_000.0
    assert brokerage.rate_series is not None


def test_shipped_account_values_files_carry_every_lot_column(
    repo_root: Path, prototype_data: Path
) -> None:
    from analysis.sim.registries import ACCOUNT_VALUES_CSV_HEADER

    for path in (
        prototype_data / "scenarios" / "retire_alice" / "account_values.csv",
        repo_root / "templates" / "scenarios" / "template" / "account_values.csv",
    ):
        assert tuple(path.read_text().splitlines()[0].split(",")) == ACCOUNT_VALUES_CSV_HEADER, path


def test_explain_lists_each_lot_with_its_symbol_and_quantity(two_lot_run_file: Path) -> None:
    from click.testing import CliRunner

    from analysis.cli import main

    result = CliRunner().invoke(main, ["sim", "explain", str(two_lot_run_file)])
    assert result.exit_code == 0, result.output
    section = result.output.split("lots at the final step:")[1].split("year-end net worth:")[0]
    a = next(line for line in section.splitlines() if "Asset_Brokerage_Joint (a)" in line)
    b = next(line for line in section.splitlines() if "Asset_Brokerage_Joint (b)" in line)
    assert "AAA" in a and " 1,000 " in a
    assert "BBB" in b and "2,000.5" not in b  # part of it was sold
    assert "Asset_TraditionalIRA_Alice (default)" in section  # investment accounts always listed


def test_items_can_be_limited_to_one_lot(two_lot_run_file: Path) -> None:
    from click.testing import CliRunner

    from analysis.cli import main

    def items(lot: str):
        return CliRunner().invoke(main, [
            "sim", "items", "--account", "Asset_Brokerage_Joint", "--lot", lot,
            "--start", "2026-05-31", "--months", "3", "--runfile", str(two_lot_run_file),
        ])

    lot_b, lot_a = items("b"), items("a")
    assert lot_b.exit_code == 0, lot_b.output
    assert "(lot b)" in lot_b.output and "return_of_capital" in lot_b.output
    assert lot_a.exit_code == 0, lot_a.output
    assert "return_of_capital" not in lot_a.output
    assert "growth_asset_brokerage_joint_a" in lot_a.output
    assert items("nope").exit_code != 0


def test_lot_balance_item_and_member_columns(two_lot_run_file: Path, test_data_dir: Path) -> None:
    from analysis.sim.connection import open_run, open_scenario

    with open_scenario(test_data_dir, "retire_alice") as sc:
        with open_run(two_lot_run_file, sc, scenarios_dir=test_data_dir / "scenarios") as run:
            held = run.items("lot_balance", account="Asset_Brokerage_Joint")
            assert set(held["lot"]) == {"a", "b"}
            last = held[held["month"] == held["month"].max()].set_index("lot")
            assert last.loc["a", "symbol"] == "AAA" and last.loc["a", "quantity_micros"] == 1_000_000_000
            members = run.items("account_model_member", symbol="BBB")
            assert list(members["account"]) == ["Asset_Brokerage_Joint"]
            assert members.iloc[0]["quantity_micros"] == 2_000_500_000
