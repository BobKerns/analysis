"""Lot-keyed state (ADR-005): one account_model_member row per lot."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from analysis.sim.engine import _post_transfer
from analysis.sim.loader import LoadError, LoadResult, growth_rule_name

BROKERAGE = "Asset_Brokerage_Joint"


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
            "SELECT name, series FROM scheduled_transaction WHERE name LIKE 'growth_%'"
        ).fetchall())
        assert rules[growth_rule_name(BROKERAGE, "a")] == "AAA"
        assert rules[growth_rule_name(BROKERAGE, "b")] == "brokerage_growth"
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
        # Lot a is never sold from; its only change is the 2029 inheritance
        # (+$50,000), a purchase into it.
        assert sorted(set(basis("a"))) == [6_000_000, 11_000_000]
        assert basis("a") == sorted(basis("a"))
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
        assert account_basis == basis("a")[-1] + b[-1]
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
        # Lot a is only ever bought into (the inheritance), so its shares only rise.
        a = quantities("a")
        assert a[0] == 1_000_000_000 and a == sorted(a) and a[-1] > a[0]
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
            "WHERE owner.name = 'asset_checking_liquidity' ORDER BY cs.step_order"
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
    assert growth_rule_name(BROKERAGE, "a") in result.output
    assert "Asset_Brokerage_Joint:" not in result.output  # lots are their own column, not spelled as a name

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
    assert "AAA" in a and "110,000.00" in a  # basis: $60,000 plus the $50,000 inheritance
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
    assert growth_rule_name(BROKERAGE, "a") in lot_a.output
    assert items("nope").exit_code != 0


def test_lot_balance_item_and_member_columns(two_lot_run_file: Path, test_data_dir: Path) -> None:
    from analysis.sim.connection import open_run, open_scenario

    with open_scenario(test_data_dir, "retire_alice") as sc:
        with open_run(two_lot_run_file, sc, scenarios_dir=test_data_dir / "scenarios") as run:
            held = run.items("lot_balance", account="Asset_Brokerage_Joint")
            assert set(held["lot"]) == {"a", "b"}
            last = held[held["month"] == held["month"].max()].set_index("lot")
            assert last.loc["a", "symbol"] == "AAA" and last.loc["a", "quantity_micros"] > 1_000_000_000
            members = run.items("account_model_member", symbol="BBB")
            assert list(members["account"]) == ["Asset_Brokerage_Joint"]
            assert members.iloc[0]["quantity_micros"] == 2_000_500_000


def _ctx(cost_bases, quantities, growth_sources=frozenset(), acquired=None):
    from analysis.sim.engine import ChainGraph, RefillContext

    return RefillContext(
        ChainGraph({}, {}, {}, {}, {}, {}, {}, {}), {}, date(2026, 5, 31), {},
        {}, {}, {}, cost_bases, quantities, growth_sources, acquired,
    )


def test_money_into_a_basis_tracked_lot_raises_basis_and_shares(ledger_con: sqlite3.Connection) -> None:
    ctx = _ctx({2: 500_00}, {2: 100_000_000})  # basis $500, 100 shares
    balances = {1: 1_000_00, 2: 800_00}
    kinds = {1: "asset", 2: "asset"}
    _post_transfer(ledger_con, balances, kinds, 1, "2026-05-31", 1, 2, 200_00, "none", 9,
                   refill_ctx=ctx)
    assert ctx.cost_bases[2] == 700_00
    assert ctx.quantities[2] == 125_000_000  # 200 / 800 more: 25 shares at the implied $8


def test_appreciation_from_a_growth_counterparty_changes_neither(ledger_con: sqlite3.Connection) -> None:
    ctx = _ctx({2: 500_00}, {2: 100_000_000}, growth_sources=frozenset({1}))
    balances = {1: 0, 2: 800_00}
    kinds = {1: "income", 2: "asset"}
    _post_transfer(ledger_con, balances, kinds, 1, "2026-05-31", 1, 2, 200_00, "gains", 9,
                   refill_ctx=ctx)
    assert (ctx.cost_bases[2], ctx.quantities[2]) == (500_00, 100_000_000)
    assert balances[2] == 1_000_00


def test_untracked_basis_and_liabilities_are_left_alone(ledger_con: sqlite3.Connection) -> None:
    ctx = _ctx({2: None, 3: 100_00}, {})
    balances = {1: 1_000_00, 2: 0, 3: 300_00}
    kinds = {1: "asset", 2: "asset", 3: "liability"}
    _post_transfer(ledger_con, balances, kinds, 1, "2026-05-31", 1, 2, 50_00, "none", 9, refill_ctx=ctx)
    _post_transfer(ledger_con, balances, kinds, 1, "2026-05-31", 1, 3, 50_00, "none", 9, refill_ctx=ctx)
    assert ctx.cost_bases == {2: None, 3: 100_00}


def test_a_lot_declared_for_a_future_purchase_accumulates_basis(
    retire_alice_csv: Callable[[str], Path], add_account_value: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    from analysis.sim.engine import run_scenario

    add_account_value("Asset_Brokerage_Joint", lot="new", acquired="2027-01-31", opening_balance="0")
    _append_scheduled_row(
        retire_alice_csv("scheduled.csv"), name="buy_new", from_account="Asset_Checking",
        to_account="Asset_Brokerage_Joint", to_lot="new", cadence="monthly",
        amount_rule_kind="fixed_amount", amount="100.00", tax_character="none",
        start="2027-01-31", priority="500", meta_json="{}",
    )
    run = run_scenario(load_test_copy().intent_path, runs_dir / "buy.sqlite3", run_by="T",
                       allow_dirty_data=True, allow_dirty_code=True)
    con = sqlite3.connect(run)
    try:
        rows = con.execute(
            "SELECT s.month, ls.balance, ls.cost_basis FROM lot_state ls "
            "JOIN simulation_step s ON s.id = ls.step_id "
            "JOIN account_model_member m ON m.id = ls.lot_id "
            "JOIN account a ON a.id = m.account_id "
            "WHERE a.name = 'Asset_Brokerage_Joint' AND m.lot = 'new' ORDER BY s.step_index"
        ).fetchall()
    finally:
        con.close()
    assert rows[0][2] == 0                               # empty before the first purchase
    months = [r for r in rows if r[0] >= "2027-01-31"]
    for n, (_month, balance, basis) in enumerate(months[:12], start=1):
        assert basis == n * 10_000                       # each $100 purchase adds basis
        assert balance >= basis                          # growth adds balance, never basis
    assert months[11][1] > months[11][2]                 # ... so a gain builds up


@pytest.mark.parametrize(("acquired", "sale", "long_term"), [
    (None, "2026-05-31", True),                # no acquisition date: long-term
    ("2025-01-31", "2026-01-31", False),       # exactly one year is not more than a year
    ("2025-01-31", "2026-02-01", True),
    ("2025-06-15", "2026-05-31", False),
    ("2024-02-29", "2025-02-28", False),       # a Feb 29 acquisition anniversaries on Feb 28
    ("2024-02-29", "2025-03-31", True),
])
def test_holding_period_is_more_than_a_year(acquired: str | None, sale: str, long_term: bool) -> None:
    from analysis.sim.engine import _is_long_term

    parsed = date.fromisoformat(acquired) if acquired else None
    assert _is_long_term(parsed, date.fromisoformat(sale)) is long_term


def _sell_from_lot(ledger_con: sqlite3.Connection, acquired: date | None, month: str):
    from analysis.sim.engine import _post_withdrawal

    ctx = _ctx({3: 60_000_00}, {}, acquired={3: acquired})
    balances = {3: 100_000_00, 4: 0}
    _post_withdrawal(
        ledger_con, balances, {3: "asset", 4: "asset"}, {3: "taxable"},
        {3: "brokerage_taxable"}, {3: "Brokerage"}, ctx.cost_bases,
        step_id=1, month_s=month, from_id=3, to_id=4, amount=10_000_00, reason_id=9,
        refill_ctx=ctx,
    )
    return ledger_con.execute("SELECT amount, tax_character FROM transfer_event ORDER BY id").fetchall()


def test_gain_on_a_lot_held_a_year_or_less_is_short_term(ledger_con: sqlite3.Connection) -> None:
    assert _sell_from_lot(ledger_con, date(2026, 1, 15), "2026-12-31") == [
        (6_000_00, "return_of_capital"), (4_000_00, "stcg"),
    ]


def test_gain_on_a_lot_held_longer_is_long_term(ledger_con: sqlite3.Connection) -> None:
    assert _sell_from_lot(ledger_con, date(2026, 1, 15), "2027-01-31")[1] == (4_000_00, "ltcg")
    assert _sell_from_lot(ledger_con, None, "2026-12-31")[1] == (4_000_00, "ltcg")


def test_short_term_gains_count_as_ordinary_income_in_the_tax_scan(ledger_con: sqlite3.Connection) -> None:
    from analysis.sim.engine import _tax_year_income_buckets

    for character, amount in (("ordinary", 100), ("stcg", 50), ("ltcg", 30), ("ss_benefit", 7)):
        ledger_con.execute(
            "INSERT INTO transfer_event(step_id, date, from_account, to_account, from_lot_id, "
            "to_lot_id, amount, requested_cents, remainder_cents, tax_character, reason_kind) "
            "VALUES (1, '2026-06-30', 1, 2, 1, 2, ?, ?, 0, ?, 'rule')",
            (amount, amount, character),
        )
    assert _tax_year_income_buckets(ledger_con, tax_year=2026) == (150, 30, 7)


def test_short_term_sales_then_long_term_sales_from_the_same_lot(two_lot_run_file: Path) -> None:
    con = sqlite3.connect(two_lot_run_file)
    try:
        legs = con.execute(
            "SELECT date, tax_character FROM transfer_event te "
            "JOIN account_model_member m ON m.id = te.from_lot_id "
            "WHERE m.lot = 'b' AND te.tax_character IN ('stcg', 'ltcg')"
        ).fetchall()
    finally:
        con.close()
    assert legs
    for month, character in legs:
        # Lot b was acquired 2026-01-15, so it turns long-term after 2027-01-15.
        assert character == ("stcg" if month <= "2027-01-15" else "ltcg"), (month, character)
    assert {c for _, c in legs} == {"stcg", "ltcg"}


def _tax_by_bracket(income: int, brackets: list[tuple[int, int]]) -> int:
    """Independent progressive tax: brackets are (floor_cents, rate_micros)."""
    tax = 0
    for i, (floor, rate) in enumerate(brackets):
        upper = brackets[i + 1][0] if i + 1 < len(brackets) else income
        tax += max(0, min(income, upper) - floor) * rate // 1_000_000
    return tax


def _bracket_rows(path: Path, status: str) -> list[tuple[int, int]]:
    import csv

    with path.open() as f:
        rows = [r for r in csv.DictReader(f)
                if r["filing_status"] == status and r["effective_date"] == "2026-01-31"]
    return sorted((round(float(r["floor"]) * 100), int(r["marginal_rate_micros"])) for r in rows)


def test_long_term_gains_are_taxed_at_the_preferential_rates(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
    fund_asset_savings: Callable[..., None],
) -> None:
    from analysis.sim.engine import run_scenario

    fund_asset_savings()
    _append_scheduled_row(
        retire_alice_csv("scheduled.csv"), name="tax_federal_prev_year",
        from_account="Asset_Checking", to_account="Expense_Taxes_Federal",
        cadence="annual", month_of_year="4", amount_rule_kind="tax_accumulator",
        tax_character="none", start="2027-04-30", priority="400", meta_json="{}",
        params_json='{"year":"previous","scope":"federal"}',
    )
    retire_alice_csv("year_to_date.csv").write_text(
        "name,from_account,to_account,amount,tax_character\n"
        "gain,Income_CapitalGain,Asset_Checking,100000.00,ltcg\n"
    )

    def federal(name: str) -> dict[str, int]:
        run = run_scenario(load_test_copy().intent_path, runs_dir / name, run_by="T",
                           allow_dirty_data=True, allow_dirty_code=True)
        con = sqlite3.connect(run)
        try:
            return dict(con.execute(
                "SELECT bucket, amount FROM tax_year_summary WHERE year = 2026"
            ).fetchall())
        finally:
            con.close()

    aux = test_data_dir / "aux"
    ltcg_file = aux / "ltcg_brackets.csv"
    kept = ltcg_file.read_text()
    ltcg_file.unlink()
    as_ordinary = federal("ordinary.sqlite3")
    ltcg_file.write_text(kept)
    preferential = federal("preferential.sqlite3")

    assert as_ordinary["federal_ltcg_income"] == 10_000_000 == preferential["federal_ltcg_income"]
    taxable = preferential["federal_taxable_income"]
    ordinary_brackets = _bracket_rows(aux / "federal_brackets.csv", "MFJ")
    ltcg_brackets = _bracket_rows(aux / "ltcg_brackets.csv", "MFJ")
    assert as_ordinary["federal_tax_liability"] == _tax_by_bracket(taxable, ordinary_brackets)

    gain = min(10_000_000, taxable)
    base = taxable - gain
    expected = (
        _tax_by_bracket(base, ordinary_brackets)
        + _tax_by_bracket(taxable, ltcg_brackets) - _tax_by_bracket(base, ltcg_brackets)
    )
    assert preferential["federal_tax_liability"] == expected
    assert preferential["federal_tax_liability"] < as_ordinary["federal_tax_liability"]


def test_state_tax_still_treats_gains_as_ordinary(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """The preferential table is federal-only: state liability ignores it."""
    import pandas as pd

    from analysis.sim.engine import run_scenario

    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    accounts.loc[len(accounts)] = {
        **dict.fromkeys(accounts.columns, ""), "name": "Expense_Taxes_State",
        "kind": "expense", "subkind": "taxes_state", "owner": "joint", "tax_treatment": "none",
        "meta_json": "{}",
    }
    accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)
    _append_scheduled_row(
        retire_alice_csv("scheduled.csv"), name="tax_state_prev_year",
        from_account="Asset_Checking", to_account="Expense_Taxes_State", cadence="annual",
        month_of_year="4", amount_rule_kind="tax_accumulator", tax_character="none",
        start="2027-04-30", priority="400", meta_json="{}",
        params_json='{"year":"previous","scope":"state"}',
    )
    retire_alice_csv("year_to_date.csv").write_text(
        "name,from_account,to_account,amount,tax_character\n"
        "gain,Income_CapitalGain,Asset_Checking,100000.00,ltcg\n"
    )

    def state_liability(name: str) -> int:
        run = run_scenario(load_test_copy().intent_path, runs_dir / name, run_by="T",
                           allow_dirty_data=True, allow_dirty_code=True)
        con = sqlite3.connect(run)
        try:
            return con.execute(
                "SELECT amount FROM tax_year_summary "
                "WHERE year = 2026 AND bucket = 'state_tax_liability'"
            ).fetchone()[0]
        finally:
            con.close()

    with_table = state_liability("with.sqlite3")
    (test_data_dir / "aux" / "ltcg_brackets.csv").unlink()
    assert state_liability("without.sqlite3") == with_table > 0


@pytest.mark.parametrize(("account", "lot", "expected"), [
    ("Bob Vanguard Rollover", "settlement", "growth_bob_vang_roll_sett"),
    ("Asset_Checking", "default", "growth_asse_chec"),      # the default lot adds nothing
    ("Asset_Brokerage_Joint", "2024-03", "growth_asse_brok_join_2024_03"),
    ("IRA", "a", "growth_ira_a"),                           # short words are kept whole
])
def test_generated_growth_rule_names_trim_each_word(account: str, lot: str, expected: str) -> None:
    assert growth_rule_name(account, lot) == expected


def test_a_taken_growth_rule_name_widens_until_it_is_free() -> None:
    taken: set[str] = set()
    names = []
    for _ in range(7):
        names.append(growth_rule_name("Vanguard Rollover", "settlement", taken))
        taken.add(names[-1])
    assert names[:3] == [
        "growth_vang_roll_sett", "growth_vangu_rollo_settl", "growth_vangua_rollov_settle",
    ]
    assert names[-1] == "growth_vanguard_rollover_settlement"   # the untrimmed names, at the widest
    assert len(set(names)) == len(names)


def test_lots_whose_names_abbreviate_alike_still_get_distinct_rules(
    add_account_value: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    # 'settlement' and 'settled' both trim to 'sett'.
    add_account_value("Asset_Brokerage_Joint", lot="settlement", opening_balance="10.00")
    add_account_value("Asset_Brokerage_Joint", lot="settled", opening_balance="10.00")
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM scheduled_transaction "
            "WHERE name LIKE 'growth_asse_brok%' OR name LIKE 'growth_asset_broke%'"
        )]
    finally:
        con.close()
    assert sorted(names) == sorted([
        growth_rule_name(BROKERAGE, "default"),
        "growth_asse_brok_join_sett",
        "growth_asset_broke_joint_settl",   # the second lot to want 'sett' is trimmed less
    ])
