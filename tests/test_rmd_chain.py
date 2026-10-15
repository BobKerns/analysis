"""RMD from an explicit chain over the subject account's lots (ADR-005 §8)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult

IRA = "Asset_TraditionalIRA_Alice"


@pytest.fixture
def set_rmd_source(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Point the retire_alice ``rmd_alice`` rule at another source, and optionally
    write a chain file for it."""
    def _set(from_account: str, from_lot: str = "", params: dict | None = None,
             chain: tuple[str, list[tuple[str, str]]] | None = None) -> None:
        path = retire_alice_csv("scheduled.csv")
        scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
        row = scheduled["name"] == "rmd_alice"
        scheduled.loc[row, "from_account"] = from_account
        scheduled.loc[row, "from_lot"] = from_lot
        scheduled.loc[row, "params_json"] = json.dumps(params) if params else ""
        scheduled.to_csv(path, index=False)
        if chain:
            name, members = chain
            lines = "".join(f"{account},{lot},,,,\n" for account, lot in members)
            (retire_alice_csv("chains/source") / f"{name}.csv").write_text(
                "account_name,lot,maximum,percent,series,cadence\n" + lines)
    return _set


def _rmd_legs(run: Path) -> list[tuple[str, str, int, str]]:
    con = sqlite3.connect(run)
    try:
        return con.execute(
            "SELECT te.date, m.lot, te.amount, te.tax_character FROM transfer_event te "
            "JOIN scheduled_transaction st ON st.id = te.reason_id AND te.reason_kind = 'rule' "
            "JOIN account_lot m ON m.id = te.from_lot_id "
            "WHERE st.name = 'rmd_alice' ORDER BY te.id"
        ).fetchall()
    finally:
        con.close()


def _run(load: Callable[[], LoadResult], runs_dir: Path, name: str) -> Path:
    return run_scenario(load().intent_path, runs_dir / name, run_by="T",
                        allow_dirty_data=True, allow_dirty_code=True)


def test_a_single_lot_ira_through_a_chain_gives_the_same_rmds(
    set_rmd_source: Callable[..., None], load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    direct = _rmd_legs(_run(load_test_copy, runs_dir, "direct.sqlite3"))
    set_rmd_source("ira_rmd", params={"subject_account": IRA}, chain=("ira_rmd", [(IRA, "default")]))
    chained = _rmd_legs(_run(load_test_copy, runs_dir, "chained.sqlite3"))
    assert [amount for _d, _l, amount, _c in direct] == [681_542, 558_190, 454_889]
    assert chained == direct


def _two_lot_ira_run(
    set_rmd_source: Callable[..., None], add_account_value: Callable[..., None],
    update_account_value: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> Path:
    # A small lot b listed first in the chain, so the RMD exhausts it and falls through.
    update_account_value(IRA, "default", opening_balance="500000.00", prior_year_end_balance="480000.00")
    add_account_value(IRA, lot="b", opening_balance="2000.00", prior_year_end_balance="1900.00")
    set_rmd_source("ira_rmd", params={"subject_account": IRA},
                   chain=("ira_rmd", [(IRA, "b"), (IRA, "default")]))
    return _run(load_test_copy, runs_dir, "two_lot.sqlite3")


def test_a_two_lot_ira_is_drawn_in_the_chains_order(
    set_rmd_source: Callable[..., None], add_account_value: Callable[..., None],
    update_account_value: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    run = _two_lot_ira_run(set_rmd_source, add_account_value, update_account_value,
                           load_test_copy, runs_dir)
    con = sqlite3.connect(run)
    try:
        aggregate = con.execute(
            "SELECT SUM(ls.balance) FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
            "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
            "WHERE a.name = ? AND s.month = '2037-12-31'", (IRA,),
        ).fetchone()[0]
        factor = next(
            json.loads(payload)["factor_micros"]
            for (payload,) in con.execute("SELECT payload_json FROM aux_table WHERE kind = 'rmd_factors'")
            if json.loads(payload)["age"] == 73
        )
    finally:
        con.close()
    legs = _rmd_legs(run)
    rmd_2038 = aggregate * 1_000_000 // factor
    # The amount is set by both lots together...
    assert sum(amount for date, _l, amount, _c in legs if date.startswith("2038")) == rmd_2038
    # ...and drawn from lot b first, then the rest from the default lot.
    assert [(date[:4], lot, amount) for date, lot, amount, _c in legs[:2]] == [
        ("2038", "b", 200_000), ("2038", "default", rmd_2038 - 200_000),
    ]
    assert {c for _d, _l, _a, c in legs} == {"tax_deferred_out"}
    assert {lot for date, lot, _a, _c in legs if date > "2039"} == {"default"}  # b is spent


def test_the_chains_order_decides_which_lot_pays(
    set_rmd_source: Callable[..., None], add_account_value: Callable[..., None],
    update_account_value: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    update_account_value(IRA, "default", opening_balance="500000.00", prior_year_end_balance="480000.00")
    add_account_value(IRA, lot="b", opening_balance="2000.00", prior_year_end_balance="1900.00")
    set_rmd_source("ira_rmd", params={"subject_account": IRA},
                   chain=("ira_rmd", [(IRA, "default"), (IRA, "b")]))
    legs = _rmd_legs(_run(load_test_copy, runs_dir, "reversed.sqlite3"))
    assert {lot for _d, lot, _a, _c in legs} == {"default"}  # the big lot covers it; b is never touched


def test_a_chain_rmd_needs_a_subject_account(
    set_rmd_source: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    set_rmd_source("ira_rmd", chain=("ira_rmd", [(IRA, "default")]))
    with pytest.raises(LoadError, match=r"rmd_alice\): an rmd drawing from chain 'ira_rmd' needs params_json"):
        load_test_copy()


def test_the_chain_may_only_draw_from_the_subject_accounts_lots(
    set_rmd_source: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    set_rmd_source("ira_rmd", params={"subject_account": IRA},
                   chain=("ira_rmd", [(IRA, "default"), ("Asset_Roth_Alice", "default")]))
    with pytest.raises(LoadError, match="draws from Asset_Roth_Alice:default, which are not lots of the subject"):
        load_test_copy()


def test_a_multi_lot_subject_needs_a_chain_not_one_lot(
    set_rmd_source: Callable[..., None], add_account_value: Callable[..., None],
    update_account_value: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    update_account_value(IRA, "default", prior_year_end_balance="480000.00")
    add_account_value(IRA, lot="b", opening_balance="10.00", prior_year_end_balance="9.00")
    set_rmd_source(IRA, "default")
    with pytest.raises(LoadError, match="needs a chain as from_account, listing the lots"):
        load_test_copy()


def test_subject_account_must_exist_and_match_a_single_source(
    set_rmd_source: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    set_rmd_source(IRA, "default", params={"subject_account": "Nope"})
    with pytest.raises(LoadError, match="subject_account 'Nope' is not an account"):
        load_test_copy()
    set_rmd_source(IRA, "default", params={"subject_account": "Asset_Roth_Alice"})
    with pytest.raises(LoadError,
                       match="subject_account 'Asset_Roth_Alice' is not the account 'Asset_TraditionalIRA_Alice'"):
        load_test_copy()
