"""A sole proprietor's business (#231): ``business_income`` into and
``business_expense`` out of a ``business`` account net into Schedule C. A loss
offsets the household's other income and carries forward as a net operating
loss when it's larger; a business ruled a hobby deducts none of its expenses.

The ``business_scenarios`` fixture's ``alice_business`` is the example, and
``alice_business_hobby`` the same business as a hobby."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.engine import EngineError, _business_result, run_scenario
from analysis.sim.loader import LoadError


@pytest.fixture
def run_file(
    business_scenarios: Path, load_test_scenario: Callable[[str], Path], runs_dir: Path,
) -> Callable[[str], Path]:
    """Run a scenario of the private data copy; its run file."""
    def _run(name: str) -> Path:
        run = runs_dir / f"{name}.sqlite3"
        run_scenario(load_test_scenario(name), run, run_by="test", allow_dirty_data=True)
        return run
    return _run


@pytest.fixture
def run_business(run_file: Callable[[str], Path]) -> Callable[[str], dict[int, dict[str, int]]]:
    """Run a scenario of the private data copy; its federal tax summary by year."""
    def _run(name: str) -> dict[int, dict[str, int]]:
        with sqlite3.connect(run_file(name)) as con:
            summary: dict[int, dict[str, int]] = {}
            for year, bucket, amount in con.execute(
                "SELECT year, bucket, amount FROM tax_year_summary WHERE bucket LIKE 'federal_%'"
            ):
                summary.setdefault(year, {})[bucket.removeprefix("federal_")] = amount
            return summary
    return _run


def _edit(path: Path, rows: list[dict[str, str]], *, replace: bool = False) -> None:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    if replace:
        frame = frame.iloc[0:0]
    pd.concat([frame, pd.DataFrame(rows)], ignore_index=True).fillna("").to_csv(path, index=False)


def test_a_business_loss_offsets_other_income_unless_it_is_a_hobby(
    run_business: Callable[[str], dict[int, dict[str, int]]],
) -> None:
    business, hobby = run_business("alice_business")[2029], run_business("alice_business_hobby")[2029]
    # 2029 is a year of expenses and no sales.
    expenses = -business["schedule_c_net"]
    assert expenses > 0
    assert business["hobby_expense_disallowed"] == 0
    assert (hobby["schedule_c_net"], hobby["hobby_expense_disallowed"]) == (0, expenses)
    # Interest differs a little too: the hobby's higher tax leaves less cash.
    assert hobby["ordinary_income"] - business["ordinary_income"] == pytest.approx(expenses, rel=0.01)
    assert hobby["tax_liability"] > business["tax_liability"]


def test_a_hobby_still_pays_tax_on_its_sales(
    run_business: Callable[[str], dict[int, dict[str, int]]],
) -> None:
    hobby = run_business("alice_business_hobby")[2030]
    assert hobby["schedule_c_net"] > 0  # the sales, with no expense against them
    assert hobby["hobby_expense_disallowed"] > hobby["schedule_c_net"]


def test_the_tax_report_shows_the_business(run_file: Callable[[str], Path]) -> None:
    result = CliRunner().invoke(
        main, ["sim", "report", "tax", "--runfile", str(run_file("alice_business_hobby"))],
    )
    assert result.exit_code == 0, result.output
    # A hobby has sales and undeductible expenses, but no loss to carry.
    for column in ("business_net", "hobby_disallowed"):
        assert column in result.output
    assert "nol_carryover" not in result.output


def test_the_expense_report_has_a_business_category(run_file: Callable[[str], Path]) -> None:
    result = CliRunner().invoke(
        main, ["sim", "report", "expenses", "--runfile", str(run_file("alice_business"))],
    )
    assert result.exit_code == 0, result.output
    assert "business" in result.output


def test_a_loss_larger_than_the_years_income_carries_forward(
    business_scenarios: Path, run_business: Callable[[str], dict[int, dict[str, int]]],
) -> None:
    """The loss carries less only the income above the deduction; a later
    year uses it up to 80% of its taxable income."""
    one_off = {"cadence": "one_off", "amount_rule_kind": "fixed_amount", "amount": "250000.00",
               "start": "2030-03-31", "end": "2030-03-31", "meta_json": "{}"}
    _edit(business_scenarios / "alice_business" / "scheduled.csv", [{
        # An inheritance pays for it, so the household stays solvent.
        **one_off, "name": "funding", "from_account": "Income_Inheritance",
        "to_account": "Asset_Business_Alice", "tax_character": "none", "priority": "100",
    }, {
        **one_off, "name": "big_build", "from_account": "Asset_Business_Alice",
        "to_account": "Expense_Business_Alice", "tax_character": "business_expense",
        "priority": "200",
    }])
    summary = run_business("alice_business")
    loss_year, next_year = summary[2030], summary[2031]
    nonbusiness = (loss_year["ordinary_income"] - loss_year["schedule_c_net"]
                   + loss_year["capital_loss_deduction"] + loss_year["ltcg_income"]
                   + loss_year["taxable_ss"])
    carried = -loss_year["schedule_c_net"] - max(0, nonbusiness - loss_year["deduction_used"])
    assert loss_year["nol_carryover"] == carried > 0
    assert loss_year["taxable_income"] == 0

    before_nol = next_year["taxable_income"] + next_year["nol_used"]
    assert next_year["nol_used"] == min(carried, before_nol * 80 // 100)
    assert next_year["nol_carryover"] == carried - next_year["nol_used"] + max(
        0, -next_year["schedule_c_net"] - max(0, (
            next_year["ordinary_income"] - next_year["schedule_c_net"]
            + next_year["capital_loss_deduction"] + next_year["ltcg_income"]
            + next_year["taxable_ss"]) - next_year["deduction_used"]))


@pytest.mark.parametrize(("csv", "rows", "message"), [
    ("scheduled.csv", [{
        "name": "personal_pays", "from_account": "Asset_Checking",
        "to_account": "Expense_Business_Alice", "cadence": "monthly",
        "amount_rule_kind": "fixed_amount", "amount": "10.00", "tax_character": "business_expense",
        "start": "2029-01-31", "priority": "200", "meta_json": "{}",
    }], "business_expense must be paid out of a business account, named directly "
        "(from: 'Asset_Checking')"),
    ("scheduled.csv", [{
        "name": "sales_to_checking", "from_account": "Income_Business_Alice",
        "to_account": "Asset_Checking", "cadence": "monthly",
        "amount_rule_kind": "fixed_amount", "amount": "10.00", "tax_character": "business_income",
        "start": "2029-01-31", "priority": "200", "meta_json": "{}",
    }], "business_income must be paid into a business account"),
    ("accounts.csv", [{
        "name": "Asset_Checking", "kind": "asset", "subkind": "cash", "owner": "joint",
        "tax_treatment": "taxable", "opened": "2020-01-31", "meta_json": '{"hobby": true}',
    }], "meta_json hobby belongs on a business account"),
    ("accounts.csv", [{
        "name": "Asset_Business_Alice", "kind": "asset", "subkind": "business", "owner": "alice",
        "tax_treatment": "taxable", "opened": "2028-07-31", "meta_json": '{"hobby": "yes"}',
    }], "meta_json hobby must be true or false"),
    ("accounts.csv", [{
        "name": "Asset_Business_Alice", "kind": "asset", "subkind": "business", "owner": "alice",
        "tax_treatment": "taxable", "opened": "2028-07-31", "meta_json": "[]",
    }], "(Asset_Business_Alice): meta_json must be a JSON object"),
])
def test_the_loader_checks_business_rows(
    business_scenarios: Path, load_test_scenario: Callable[[str], Path],
    csv: str, rows: list[dict[str, str]], message: str,
) -> None:
    path = business_scenarios / "alice_business_hobby" / csv
    if not path.exists():
        pd.DataFrame(columns=pd.read_csv(business_scenarios / "alice_business" / csv).columns
                     ).to_csv(path, index=False)
    _edit(path, rows, replace=csv == "accounts.csv")
    with pytest.raises(LoadError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load_test_scenario("alice_business_hobby")


def test_a_chain_step_takes_no_business_character(
    business_scenarios: Path, load_test_scenario: Callable[[str], Path],
) -> None:
    chain = business_scenarios / "alice_business" / "chains" / "destination" / "to_business.csv"
    chain.parent.mkdir(parents=True)
    chain.write_text("account_name,tax_character\nAsset_Business_Alice,business_income\n")
    with pytest.raises(LoadError, match="isn't for a chain step"):
        load_test_scenario("alice_business")


def test_the_nol_limit_must_be_a_percentage(
    business_scenarios: Path, run_file: Callable[[str], Path],
) -> None:
    path = business_scenarios / "alice_business" / "scheduled.csv"
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.loc[frame["name"] == "federal_tax", "params_json"] = (
        '{"year": "previous", "nol_limit_percent": 150}')
    frame.to_csv(path, index=False)
    with pytest.raises(EngineError, match="nol_limit_percent must be 0 to 100, got 150"):
        run_file("alice_business")


def _account(con: sqlite3.Connection, account_id: int, subkind: str, meta: str = "{}") -> None:
    kind = "expense" if subkind == "other_expense" else "asset"
    con.execute(
        "INSERT INTO account(id, name, kind, subkind, meta_json) VALUES (?, ?, ?, ?, ?)",
        (account_id, f"A{account_id}", kind, subkind, meta),
    )


def _event(con: sqlite3.Connection, frm: int, to: int, amount: int, character: str) -> None:
    con.execute(
        "INSERT INTO transfer_event(step_id, date, from_account, to_account, from_lot_id, "
        "to_lot_id, amount, requested_cents, remainder_cents, tax_character, reason_kind) "
        "VALUES (1, '2030-06-30', ?, ?, ?, ?, ?, ?, 0, ?, 'rule')",
        (frm, to, frm, to, amount, amount, character),
    )


def test_each_business_is_judged_by_its_own_account(ledger_con: sqlite3.Connection) -> None:
    """Two businesses, one a hobby: only the hobby's expenses are disallowed."""
    _account(ledger_con, 1, "business")
    _account(ledger_con, 2, "business", '{"hobby": true}')
    _account(ledger_con, 3, "other_expense")
    _event(ledger_con, 1, 3, 500_00, "business_expense")
    _event(ledger_con, 2, 3, 300_00, "business_expense")
    _event(ledger_con, 3, 2, 100_00, "business_income")
    assert _business_result(ledger_con, tax_year=2030) == (100_00 - 500_00, 300_00)
    assert _business_result(ledger_con, tax_year=2031) == (0, 0)


def test_the_engine_rejects_a_business_character_off_a_business_account(
    ledger_con: sqlite3.Connection,
) -> None:
    _account(ledger_con, 1, "cash")
    _account(ledger_con, 3, "other_expense")
    _event(ledger_con, 1, 3, 500_00, "business_expense")
    with pytest.raises(EngineError, match="must be paid out of a business account, not 'A1'"):
        _business_result(ledger_con, tax_year=2030)
