"""The ``fill_bracket`` rule (#252): a yearly Roth conversion sized to keep
taxable income at a target, its loader checks, and ``sim scheduled add``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.registries import SCHEDULED_CSV_HEADER

MFJ_22_TOP = 211_400_00
"""The top of the prototype's 22% MFJ bracket from 2027 (aux/federal_brackets.csv)."""
SINGLE_22_TOP = 105_700_00

Rules = Callable[..., Path]


@pytest.fixture
def fill_run(
    retire_alice_csv: Callable[[str], Path], fund_asset_savings: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> Rules:
    """Run retire_alice with a federal tax rule and the given fill_bracket
    rows (each a dict of scheduled.csv columns over a 22%-bracket
    conversion from Alice's traditional IRA to her Roth, from 2029)."""
    fund_asset_savings("1000000.00")  # the conversions' tax comes from savings
    path = retire_alice_csv("scheduled.csv")
    original = path.read_text()
    runs: list[Path] = []

    def _run(*rows: dict[str, str], tax: bool = True) -> Path:
        path.write_text(original)
        scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
        scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
        blank = dict.fromkeys(SCHEDULED_CSV_HEADER, "")
        if tax:
            scheduled.loc[len(scheduled)] = {
                **blank, "name": "tax_federal", "from_account": "Asset_Checking",
                "to_account": "Expense_Taxes_Federal", "cadence": "annual",
                "month_of_year": "4", "amount_rule_kind": "tax_accumulator",
                "tax_character": "none", "start": "2027-04-30", "meta_json": "{}",
                "params_json": '{"year":"previous","scope":"federal"}',
            }
        for i, row in enumerate(rows):
            scheduled.loc[len(scheduled)] = {
                **blank, "name": f"convert_{i}",
                "from_account": "Asset_TraditionalIRA_Alice", "from_lot": "default",
                "to_account": "Asset_Roth_Alice", "to_lot": "default",
                "cadence": "annual", "month_of_year": "12",
                "amount_rule_kind": "fill_bracket", "rate": "22%",
                "tax_character": "tax_deferred_out", "start": "2029-01-31",
                "meta_json": "{}", **row,
            }
        scheduled.to_csv(path, index=False)
        intent = load_test_copy().intent_path
        runs.append(run_scenario(intent, runs_dir / f"fill{len(runs)}.sqlite3", run_by="TEST",
                                 allow_dirty_data=True, allow_dirty_code=True))
        return runs[-1]
    return _run


def _conversions(run: Path) -> pd.DataFrame:
    """Each fill_bracket conversion: its year, amount, and what it aimed at."""
    with sqlite3.connect(run) as con:
        rows = con.execute(
            "SELECT CAST(substr(te.date, 1, 4) AS INTEGER), te.amount, te.meta_json, te.id "
            "FROM transfer_event te JOIN scheduled_transaction st ON st.id = te.reason_id "
            "WHERE te.reason_kind = 'rule' AND st.amount_rule_kind = 'fill_bracket' "
            "ORDER BY te.id").fetchall()
    return pd.DataFrame(
        [(year, amount, json.loads(meta)["fill_bracket"], event_id)
         for year, amount, meta, event_id in rows],
        columns=["year", "amount", "fill", "event_id"])


def _summary(run: Path) -> pd.DataFrame:
    with sqlite3.connect(run) as con:
        frame = pd.read_sql_query("SELECT year, bucket, amount FROM tax_year_summary", con)
    return frame.pivot_table(index="year", columns="bucket", values="amount", aggfunc="sum")


def _ordinary_taxable(summary: pd.DataFrame, year: int) -> int:
    taxable = summary.loc[year, "federal_taxable_income"]
    return int(taxable - min(summary.loc[year, "federal_ltcg_income"], taxable))


def _ira_balance_after(run: Path, event_id: int) -> int:
    with sqlite3.connect(run) as con:
        return con.execute(
            "SELECT ls.balance FROM lot_state ls JOIN transfer_event te ON te.step_id = ls.step_id "
            "JOIN account_lot l ON l.id = ls.lot_id JOIN account a ON a.id = l.account_id "
            "WHERE te.id = ? AND a.name = 'Asset_TraditionalIRA_Alice'", (event_id,),
        ).fetchone()[0]


def test_a_bracket_target_fills_ordinary_taxable_income_to_the_bracket_top(
    fill_run: Rules,
) -> None:
    run = fill_run({})
    conversions, summary = _conversions(run), _summary(run)
    assert len(conversions) >= 2
    first = conversions.iloc[0]
    assert first["year"] == 2029
    assert first["fill"] == {"target": MFJ_22_TOP, "measure": "ordinary", "reached": MFJ_22_TOP}
    # The tax rule, measuring the finished year the next April, agrees.
    assert _ordinary_taxable(summary, 2029) == MFJ_22_TOP
    for year, event_id in zip(conversions["year"], conversions["event_id"], strict=True):
        if year in summary.index and _ira_balance_after(run, int(event_id)) > 0:
            assert _ordinary_taxable(summary, int(year)) == MFJ_22_TOP, year


def test_taxable_social_security_is_solved_for_not_subtracted(fill_run: Rules) -> None:
    """Alice's benefit starts in 2032: the conversion makes more of it
    taxable, so subtracting the year's income from the target would
    overshoot it; the conversion still lands on the bracket top."""
    summary = _summary(fill_run({"start": "2033-01-31"}))
    without = _summary(fill_run({"start": "2034-01-31"}))
    assert summary.loc[2033, "federal_taxable_ss"] > without.loc[2033, "federal_taxable_ss"]
    assert _ordinary_taxable(summary, 2033) == MFJ_22_TOP


def test_an_amount_target_is_in_todays_dollars_grown_by_its_series(fill_run: Rules) -> None:
    run = fill_run({"rate": "", "amount": "250000.00", "series": "cpi",
                    "base_date": "2026-05-31"})
    conversions, summary = _conversions(run), _summary(run)
    targets = [fill["target"] for fill in conversions["fill"]]
    assert targets[0] > 250_000_00 and targets == sorted(targets)
    first = conversions.iloc[0]
    assert first["fill"]["measure"] == "taxable"
    assert summary.loc[first["year"], "federal_taxable_income"] == first["fill"]["target"]


def test_a_year_already_past_the_target_converts_nothing(fill_run: Rules) -> None:
    """While Bill still works (to 2030), wages alone pass a $1,000 target."""
    assert _conversions(fill_run({"rate": "", "amount": "1000.00", "end": "2030-12-31"})).empty


def test_max_amount_and_the_lots_balance_cap_the_conversion(fill_run: Rules) -> None:
    capped = _conversions(fill_run({"params_json": '{"max_amount": "10000.00"}'}))
    assert not capped.empty and (capped["amount"] == 10_000_00).all()

    run = fill_run({"rate": "35%"})
    whole = _conversions(run)
    assert len(whole) == 1  # the 35% bracket's top is above the whole IRA
    assert _ira_balance_after(run, int(whole.iloc[0]["event_id"])) == 0


def test_max_marginal_rate_stops_where_the_next_100_dollars_costs_more(fill_run: Rules) -> None:
    run = fill_run({"rate": "32%", "params_json": '{"max_marginal_rate": "22%"}'})
    first = _conversions(run).iloc[0]
    reached = first["fill"]["reached"]
    assert MFJ_22_TOP - 100_00 < reached <= MFJ_22_TOP


def test_a_death_moves_the_target_to_the_single_bracket(
    fill_run: Rules, retire_alice_csv: Callable[[str], Path],
) -> None:
    """Bill dies in 2032; from 2033 Alice files Single, whose 22% bracket
    tops out at half the joint one."""
    retire_alice_csv("lifespan.csv").write_text("person,age_at_death\nbill,65\n")
    conversions = _conversions(fill_run({"start": "2033-01-31"}))
    assert conversions.iloc[0]["fill"]["target"] == SINGLE_22_TOP


def test_the_conversion_comes_after_the_years_rmd(fill_run: Rules) -> None:
    """Alice's RMDs start in 2038 (age 73): that December the RMD posts
    first, even though the conversion rule is listed before it."""
    run = fill_run({"start": "2038-01-31", "params_json": '{"max_amount": "5000.00"}'})
    with sqlite3.connect(run) as con:
        rmd = con.execute(
            "SELECT te.id FROM transfer_event te JOIN scheduled_transaction st "
            "ON st.id = te.reason_id WHERE te.reason_kind = 'rule' "
            "AND st.amount_rule_kind = 'rmd' AND te.date LIKE '2038-12%'").fetchone()
    conversion = _conversions(run).iloc[0]
    assert rmd is not None and conversion["year"] == 2038
    assert rmd[0] < conversion["event_id"]


@pytest.mark.parametrize(("row", "message"), [
    ({"from_account": "Asset_Roth_Bill"}, "converts from a traditional"),
    ({"to_account": "Asset_Checking", "to_lot": ""}, "converts to a Roth"),
    ({"amount": "100000.00"}, "needs one of rate"),
    ({"rate": ""}, "needs one of rate"),
    ({"priority": "100"}, "takes no priority"),
    ({"cadence": "monthly", "month_of_year": ""}, "needs cadence annual"),
    ({"tax_character": "none"}, "needs tax_character tax_deferred_out"),
    ({"rate": "23%"}, r"no federal bracket is taxed at 23%"),
    ({"series": "cpi"}, "series is only for amount"),
    ({"tax_year": "2030"}, "takes no tax_year"),
    ({"month_of_year": "6"}, r"before the RMD from the same lot \(month 12\)"),
    ({"rate": "", "amount": "150000.00", "series": "cpi"}, "series cpi needs a base_date"),
    ({"params_json": '{"tax_rule": "tax_federal", "scope": "state"}'},
     "tax_rule 'tax_federal' is a federal rule, not a state one"),
    ({"params_json": '{"cap": 1}'}, "unknown params_json key"),
    ({"params_json": '{"max_amount": "-5.00"}'}, "max_amount"),
    ({"params_json": '{"max_marginal_rate": "30"}'}, "max_marginal_rate"),
    ({"params_json": '{"max_marginal_rate": "-1%"}'}, "max_marginal_rate must be from 0%"),
    ({"params_json": '{"tax_rule": "nope"}'}, "tax_rule 'nope' is not"),
    ({"params_json": '{"scope": "state"}'}, "state tax_accumulator rule, and there is none"),
])
def test_the_loader_checks_a_fill_bracket_row(
    fill_run: Rules, row: dict[str, str], message: str,
) -> None:
    with pytest.raises(LoadError, match=message):
        fill_run(row)


def test_the_loader_wants_a_tax_rule_to_measure_with(fill_run: Rules) -> None:
    with pytest.raises(LoadError, match="federal tax_accumulator rule, and there is none"):
        fill_run({}, tax=False)


def test_scheduled_add_writes_a_fill_bracket_without_a_priority(test_data_dir: Path) -> None:
    path = test_data_dir / "scenarios" / "retire_alice" / "scheduled.csv"
    with path.open("a") as f:  # the tax rule the conversion measures with
        f.write("tax_federal,Asset_Checking,,Expense_Taxes_Federal,,annual,4,tax_accumulator,"
                ",,,,,,,,none,,2027-04-30,,,,,,{},"
                '"{""year"":""previous"",""scope"":""federal""}",\n')

    def add(*args: str):
        return CliRunner().invoke(main, [
            "sim", "scheduled", "add", "2029-01", "Asset_TraditionalIRA_Alice",
            "Asset_Roth_Alice", "--from-lot", "default", "--to-lot", "default",
            "--kind", "fill_bracket", "--name", "convert", *args,
            "--scenario", "retire_alice", "--data-dir", str(test_data_dir)])

    assert "takes one of --rate" in add().output
    assert "takes no --priority" in add("--rate", "22%", "--priority", "5").output
    result = add("--rate", "22%")
    assert result.exit_code == 0, result.output
    scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
    row = scheduled[scheduled["name"] == "convert"].iloc[0]
    assert (row["rate"], row["priority"], row["cadence"], row["month_of_year"],
            row["tax_character"]) == ("22%", "", "annual", "12", "tax_deferred_out")


def test_series_fixed_means_no_inflation_on_either_target(fill_run: Rules) -> None:
    assert not _conversions(fill_run({"series": "fixed"})).empty
    fixed = _conversions(fill_run({"rate": "", "amount": "250000.00", "series": "fixed"}))
    assert {fill["target"] for fill in fixed["fill"]} == {250_000_00}
