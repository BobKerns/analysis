"""Washington's taxes (#489): a tax on long-term capital gains apart from
the income tax (``state_gains_tax``), on the federal net long-term gain less
a home's, and from 2028 a 9.9% income tax over $1 million that credits it."""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from analysis.sim import registries
from analysis.sim.engine import TaxRule, _drawdown_tax, _prepare_tax, run_scenario
from analysis.sim.loader import LoadResult
from analysis.sim.tax_api import NO_EXTRA, Extra, Return
from analysis.sim.taxes.common import progressive_tax
from analysis.sim.lineage import TEMPLATE_AUX_DIR

Aux = dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]]

CHECKING, BROKERAGE, HOME, SALARY = 1, 2, 3, 4

MILLION = 1_000_000_00
DEDUCTION = 278_000_00


def _rows(date: str, **row: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(date, {"filing_status": "MFJ", **row})]


def _aux() -> Aux:
    """A household in Washington, with Washington's law (its 2025 gains
    deduction, not indexed here) and a 10% flat federal tax."""
    zero = _rows("2022-01-31", floor=0, marginal_rate_micros=0)
    return {
        ("filing_status", None): [("2026-01-31", {"filing_status": "MFJ"})],
        ("federal_brackets", None): _rows("2026-01-31", floor=0, marginal_rate_micros=100_000),
        ("standard_deduction", None): _rows("2026-01-31", amount=10_000_00),
        ("capital_loss_limit", None): _rows("2026-01-31", amount=3_000_00),
        ("state_brackets", "WA"): [
            *zero, *_rows("2028-01-31", floor=0, marginal_rate_micros=99_000)],
        (registries.STATE_STANDARD_DEDUCTION, "WA"): [
            *_rows("2022-01-31", amount=0), *_rows("2028-01-31", amount=MILLION)],
        (registries.STATE_SOCIAL_SECURITY, "WA"): _rows("2022-01-31", exclusion_micros=0),
        (registries.STATE_GAINS_TAX, "WA"): [
            *_rows("2025-01-31", floor=0, marginal_rate_micros=70_000),
            *_rows("2025-01-31", floor=MILLION, marginal_rate_micros=99_000)],
        (registries.STATE_GAINS_DEDUCTION, "WA"): _rows("2025-01-31", amount=DEDUCTION),
        (registries.STATE_ADJUSTMENTS, "WA"): [("2028-01-31", {
            "name": "capital_gains_tax_credit", "step": "credit", "base": "gains_tax"})],
    }


@pytest.fixture
def ledger(ledger_con: sqlite3.Connection,
           set_residence: Callable[..., None]) -> sqlite3.Connection:
    set_residence(ledger_con, ("2022-01-31", "WA", None))
    ledger_con.executemany(
        "INSERT INTO account(id, name, kind, subkind) VALUES (?, ?, ?, ?)",
        [(CHECKING, "Asset_Checking", "asset", "cash"),
         (BROKERAGE, "Asset_Brokerage", "asset", "brokerage_taxable"),
         (HOME, "Asset_Home", "asset", "real_estate"),
         (SALARY, "Income_Salary", "income", "salary")],
    )
    return ledger_con


def _post(con: sqlite3.Connection, year: int, frm: int, amount: int, character: str) -> None:
    con.execute(
        "INSERT INTO transfer_event(step_id, date, from_account, to_account, from_lot_id, "
        "to_lot_id, amount, requested_cents, remainder_cents, tax_character, reason_kind) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'rule')",
        (f"{year}-06-30", frm, CHECKING, frm, CHECKING, amount, amount, character),
    )


def _state(con: sqlite3.Connection, year: int = 2026, extra: Extra = NO_EXTRA) -> Return:
    return _prepare_tax(
        con, {"scope": "state"}, aux=_aux(), tax_year=year, tid=1, kind="state_tables",
        jurisdictions={"federal": TaxRule("federal", {"scope": "federal"}, 1)}).compute(extra)


def _gains_tax(taxable: int) -> int:
    return progressive_tax(taxable, [(0, 70_000), (MILLION, 99_000)])


def test_a_long_term_gain_over_the_deduction_is_taxed_at_7_and_over_a_million_at_9_9(
    ledger: sqlite3.Connection,
) -> None:
    _post(ledger, 2026, BROKERAGE, 1_500_000_00 + DEDUCTION, "ltcg")
    state = _state(ledger)
    assert state["gains_base"] == 1_500_000_00 + DEDUCTION
    assert state["gains_deduction"] == DEDUCTION
    assert state["gains_tax"] == 70_000_00 + 49_500_00
    # Before 2028 there is no income tax: the gains tax is the whole tax.
    assert state.tax == 119_500_00


def test_a_short_term_loss_does_not_offset_the_long_term_gain(ledger: sqlite3.Connection) -> None:
    _post(ledger, 2026, BROKERAGE, 400_000_00, "ltcg")
    _post(ledger, 2026, BROKERAGE, -100_000_00, "stcg")
    state = _state(ledger)
    assert state["gains_base"] == 400_000_00
    assert state.tax == _gains_tax(400_000_00 - DEDUCTION)


def test_a_long_term_loss_carried_in_offsets_it(ledger: sqlite3.Connection) -> None:
    """Schedule D line 15 nets the long-term loss carried from 2026."""
    _post(ledger, 2026, BROKERAGE, -100_000_00, "ltcg")
    _post(ledger, 2027, BROKERAGE, 400_000_00, "ltcg")
    assert _state(ledger, 2026)["gains_base"] == 0
    # 2026's $3,000 of the loss offset ordinary income; the rest carried.
    assert _state(ledger, 2027)["gains_base"] == 400_000_00 - 97_000_00


def test_a_homes_gain_is_exempt(ledger: sqlite3.Connection) -> None:
    _post(ledger, 2026, HOME, 500_000_00, "ltcg")
    _post(ledger, 2026, BROKERAGE, 300_000_00, "ltcg")
    state = _state(ledger)
    assert state["gains_base"] == 300_000_00
    assert state.tax == _gains_tax(300_000_00 - DEDUCTION)


def test_before_2028_ordinary_income_and_what_ifs_of_it_are_not_taxed(
    ledger: sqlite3.Connection,
) -> None:
    _post(ledger, 2026, SALARY, 2_000_000_00, "ordinary")
    assert _state(ledger).tax == 0
    # A Roth conversion on top costs nothing; a gain does, unless a home's.
    assert _state(ledger, extra=Extra(500_000_00)).tax == 0
    gain = Extra(0, 300_000_00)
    assert _state(ledger, extra=gain).tax == _gains_tax(300_000_00 - DEDUCTION)
    assert _state(ledger, extra=Extra(0, 300_000_00, 300_000_00)).tax == 0


def test_from_2028_income_over_a_million_is_taxed_and_credits_the_gains_tax(
    ledger: sqlite3.Connection,
) -> None:
    """ESSB 6346: the income tax's base is federal AGI with the gains tax's
    base in place of the long-term gain, so a home's gain stays out; the
    gains tax paid is credited against it."""
    _post(ledger, 2028, SALARY, 1_500_000_00, "ordinary")
    _post(ledger, 2028, BROKERAGE, 778_000_00, "ltcg")
    _post(ledger, 2028, HOME, 400_000_00, "ltcg")
    state = _state(ledger, 2028)
    gains_tax = _gains_tax(500_000_00)
    assert state["gains_tax"] == gains_tax
    assert state["ltcg_for_gains_base"] == 400_000_00
    assert state.agi == 1_500_000_00 + 778_000_00
    income_tax = (state.agi - MILLION) * 99 // 1000
    # The credit takes the gains tax off the income tax, and the gains tax
    # is added back: the gain is taxed once, at the higher rate.
    assert state["capital_gains_tax_credit"] == gains_tax
    assert state.tax == income_tax


def test_the_credit_never_takes_more_than_the_income_tax(ledger: sqlite3.Connection) -> None:
    """Below the $1 million deduction there's no income tax to credit
    against, and the gains tax is still paid in full."""
    _post(ledger, 2028, BROKERAGE, 778_000_00, "ltcg")
    state = _state(ledger, 2028)
    assert state["capital_gains_tax_credit"] == 0
    assert state.tax == _gains_tax(500_000_00)


def test_the_drawdown_passes_a_homes_gain_apart(ledger: sqlite3.Connection) -> None:
    """#489: a gains tax leaves a home's unrealized gain out of the
    after-tax drawdown, so the drawdown passes it as its own part."""
    ledger.execute(
        "INSERT INTO scenario(name, calendar_id, start, end, parents_json, load_order_json, "
        "mixin_only, created_at, created_by, meta_json) "
        "VALUES ('s', 1, '2026-01', '2040-12', '[]', '[]', 0, '', '', '{}')")
    ledger.execute("INSERT INTO simulation_step(id, step_index, month) VALUES (1, 0, '2030-12')")
    ledger.execute("UPDATE account_lot SET tax_treatment = 'taxable'")
    ledger.executemany(
        "INSERT INTO lot_state(step_id, lot_id, balance, cost_basis) VALUES (1, ?, ?, ?)",
        [(BROKERAGE, 150_000_00, 50_000_00), (HOME, 900_000_00, 400_000_00)])
    seen: list[tuple[int, int, int]] = []

    def tax_with(ordinary: int, gains: int, home: int = 0) -> int:
        seen.append((ordinary, gains, home))
        return 0

    base, _ = _drawdown_tax(ledger, 2030, lambda _year: tax_with)
    assert base == 600_000_00
    shares = [s for s in seen if s != (0, 0, 0)]
    assert sum(g for _, g, _ in shares) == 600_000_00
    assert sum(h for _, _, h in shares) == 500_000_00


def test_the_shipped_washington_tables_carry_its_law() -> None:
    def read(kind: str) -> list[dict[str, str]]:
        with open(TEMPLATE_AUX_DIR / f"{kind}__WA.csv", newline="") as fh:
            return list(csv.DictReader(fh))

    gains = [(r["floor"], r["marginal_rate"]) for r in read(registries.STATE_GAINS_TAX)
             if r["effective_date"] == "2025-01-31" and r["filing_status"] == "MFJ"]
    assert gains == [("0.00", "7%"), ("1000000.00", "9.9%")]
    deduction = {r["effective_date"]: r["amount"] for r in read(registries.STATE_GAINS_DEDUCTION)
                 if r["filing_status"] == "MFJ"}
    assert deduction["2025-01-31"] == "278000.00"
    income = {r["effective_date"]: r["marginal_rate"] for r in read("state_brackets")
              if r["filing_status"] == "MFJ"}
    assert income == {"2022-01-31": "0%", "2028-01-31": "9.9%"}


def test_a_move_to_washington_reads_its_shipped_tables(
    add_state_tax: Callable[..., None], add_residence: Callable[..., Path],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """A move from California needs no tables of the household's own: from
    the year of the move the state tax is Washington's gains tax."""
    add_state_tax("CA")
    add_residence("2027-03", "WA")
    run = runs_dir / "move.sqlite3"
    run_scenario(load_test_copy().intent_path, run, run_by="test", allow_dirty_data=True)
    with closing(sqlite3.connect(run)) as con:
        buckets = dict(con.execute(
            "SELECT bucket, amount FROM tax_year_summary WHERE year = 2027 "
            "AND bucket IN ('state_tax', 'state_gains_tax', 'state_gains_deduction')").fetchall())
        assert "state_gains_deduction" not in dict(con.execute(
            "SELECT bucket, amount FROM tax_year_summary WHERE year = 2026").fetchall())
    assert buckets["state_gains_deduction"] > 278_000_00  # 2026's figure, grown a year
    assert buckets["state_tax"] == buckets["state_gains_tax"]
