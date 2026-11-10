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
from analysis.sim import engine
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.registries import SCHEDULED_CSV_HEADER

def _top_22(run: Path, year: int, status: str = "MFJ") -> int:
    """The top of the 22% bracket for ``status`` in tax ``year``, as the run
    indexed the prototype's aux/federal_brackets.csv (#467): the floor of
    the 24% bracket above it."""
    with sqlite3.connect(run) as con:
        aux = engine._load_aux(con)
    rows = engine._indexed_tax_rows(aux, "federal_brackets", None, year, {})
    return next(
        r["floor"] for r in rows
        if r["filing_status"] == status and r["marginal_rate_micros"] == 240_000
    )

Rules = Callable[..., Path]


@pytest.fixture
def fill_run(
    retire_alice_csv: Callable[[str], Path], fund_asset_savings: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
    add_tax: Callable[..., Path],
) -> Rules:
    """Run retire_alice with the federal tax and the given fill_bracket
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
            add_tax()
        else:
            (path.parent / "taxes" / "federal.csv").unlink(missing_ok=True)
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
    return int(taxable - min(summary.loc[year, "federal_ltcg_taxed"], taxable))


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
    top = _top_22(run, 2029)
    assert first["fill"] == {"target": top, "measure": "ordinary", "reached": top}
    # The tax rule, measuring the finished year the next April, agrees.
    assert _ordinary_taxable(summary, 2029) == top
    for year, event_id in zip(conversions["year"], conversions["event_id"], strict=True):
        if year in summary.index and _ira_balance_after(run, int(event_id)) > 0:
            assert _ordinary_taxable(summary, int(year)) == _top_22(run, int(year)), year
    # The bracket rises with inflation, and the target with it (#467).
    assert _top_22(run, 2030) > top


def test_taxable_social_security_is_solved_for_not_subtracted(fill_run: Rules) -> None:
    """Alice's benefit starts in 2032: the conversion makes more of it
    taxable, so subtracting the year's income from the target would
    overshoot it; the conversion still lands on the bracket top."""
    run = fill_run({"start": "2033-01-31"})
    summary = _summary(run)
    without = _summary(fill_run({"start": "2034-01-31"}))
    assert summary.loc[2033, "federal_ss_taxable"] > without.loc[2033, "federal_ss_taxable"]
    assert _ordinary_taxable(summary, 2033) == _top_22(run, 2033)


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
    reached, top = first["fill"]["reached"], _top_22(run, int(first["year"]))
    assert top - 100_00 < reached <= top


def test_a_death_moves_the_target_to_the_single_bracket(
    fill_run: Rules, retire_alice_csv: Callable[[str], Path],
) -> None:
    """Bill dies in 2032; from 2033 Alice files Single, whose 22% bracket
    tops out at half the joint one."""
    retire_alice_csv("lifespan.csv").write_text("person,age_at_death\nbill,65\n")
    run = fill_run({"start": "2033-01-31"})
    conversions = _conversions(run)
    assert conversions.iloc[0]["fill"]["target"] == _top_22(run, 2033, "Single")


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
    ({"params_json": '{"tax_rule": "tax_federal"}'}, "fill_bracket takes no tax_rule"),
    ({"params_json": '{"cap": 1}'}, "unknown params_json key"),
    ({"params_json": '{"max_amount": "-5.00"}'}, "max_amount"),
    ({"params_json": '{"max_marginal_rate": "30"}'}, "max_marginal_rate"),
    ({"params_json": '{"max_marginal_rate": "-1%"}'}, "max_marginal_rate must be from 0%"),
    ({"params_json": '{"scope": "state"}'}, r"measures with the state tax, and there is none \(taxes/state.csv\)"),
])
def test_the_loader_checks_a_fill_bracket_row(
    fill_run: Rules, row: dict[str, str], message: str,
) -> None:
    with pytest.raises(LoadError, match=message):
        fill_run(row)


def test_the_loader_wants_the_federal_tax_to_measure_with(fill_run: Rules) -> None:
    with pytest.raises(LoadError, match="measures with the federal tax, and there is none"):
        fill_run({}, tax=False)


def test_scheduled_add_writes_a_fill_bracket_without_a_priority(
    test_data_dir: Path, write_tax: Callable[..., Path],
) -> None:
    path = test_data_dir / "scenarios" / "retire_alice" / "scheduled.csv"
    write_tax(path.parent)  # the tax the conversion measures with

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


def test_a_states_max_marginal_rate_counts_the_local_tax_paid_with_it(
    fill_run: Rules, add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
    write_aux: Callable[[str, str], Path],
) -> None:
    """A local tax is charged on the state's income (#508), so the next
    dollar's cost in the state's scope includes it: at 3% local, the 8%
    bracket costs 11% and a 10% limit stops below it."""
    add_state_tax()
    rule = {"rate": "9.3%", "params_json": '{"scope": "state", "max_marginal_rate": "10%"}'}
    without = _conversions(fill_run(rule)).iloc[0]["fill"]["reached"]
    write_aux("state_local_tax__CA.csv", "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
              + "".join(f"2020-01-31,{s},0,0.00,3%\n" for s in ("Single", "MFJ", "MFS")))
    add_tax("local")
    with_local = _conversions(fill_run(rule)).iloc[0]["fill"]["reached"]
    # Far less: the local tax's payments alone move it only a little.
    assert with_local < without // 2, (with_local, without)


# -- max_marginal_rate's gallop ----------------------------------------------

def _step_scan(
    tax_with: Callable[[int, int], int], cliff: Callable[[int], int] | None,
    amount: int, limit: int,
) -> int:
    """``max_marginal_rate`` as it was first written: one $100 step at a
    time. The gallop must find the same amount."""
    step_size = engine.FILL_BRACKET_STEP

    def cost(x: int) -> int:
        return tax_with(x, 0) + (cliff(x) if cliff else 0)

    def ok(x: int, step: int) -> bool:
        return (tax_with(x + step, 0) - tax_with(x, 0)) * 1_000_000 <= limit * step

    taken = 0
    while taken < amount:
        step = min(step_size, amount - taken)
        if not ok(taken, step):
            break
        if cliff is None or cliff(taken + step) == cliff(taken):
            taken += step
            continue
        below = engine._largest_within(cliff, cliff(taken), taken + step)
        end, crossing = below, True
        while end < amount:
            next_step = min(step_size, amount - end)
            if not ok(end, next_step) or (not crossing and cliff(end + next_step) != cliff(end)):
                break
            end, crossing = end + next_step, False
        if (cost(end) - cost(below)) * 1_000_000 <= limit * (end - below):
            taken = end
        else:
            return below
    return taken


def _torpedo_tax(base: int, ss: int) -> Callable[[int, int], int]:
    """Federal-style tax on ``base`` ordinary income plus a conversion, with
    ``ss`` of Social Security taxed by the tiers: its rate climbs through
    the torpedo and falls back once 85% of the benefit is taxed."""
    from analysis.sim.taxes import common
    brackets = [(0, 100_000), (23_850_00, 120_000), (96_950_00, 220_000),
                (206_700_00, 240_000), (394_600_00, 320_000)]

    def tax(extra: int, _ltcg: int) -> int:
        ordinary = base + extra
        taxable_ss = common.taxable_social_security(
            ordinary_income=ordinary, ltcg_income=0, ss_income=ss,
            tier1=32_000_00, tier2=44_000_00)
        return common.progressive_tax(max(0, ordinary + taxable_ss - 30_000_00), brackets)
    return tax


@pytest.mark.parametrize("limit", [100_000, 120_000, 220_000, 230_000, 250_000, 410_000])
@pytest.mark.parametrize("cliff_floor", [None, 41_234_56, 150_000_00])
@pytest.mark.parametrize(("base", "ss"), [(0, 0), (10_000_00, 40_000_00), (30_000_00, 60_000_00)])
def test_the_gallop_finds_what_stepping_does(
    limit: int, cliff_floor: int | None, base: int, ss: int,
) -> None:
    tax = _torpedo_tax(base, ss)
    cliff = None if cliff_floor is None else (lambda x, f=cliff_floor: 3_000_00 if base + x > f else 0)
    for amount in (12_345_67, 180_000_00, 400_000_01):
        assert engine._within_marginal_rate(tax, cliff, amount, limit) \
            == _step_scan(tax, cliff, amount, limit)


@pytest.mark.parametrize("jump", [1, 50_00, 2_000_00])
@pytest.mark.parametrize("at", [9_999_99, 263_456_78])
@pytest.mark.parametrize("limit", [220_000, 240_000, 300_000])
def test_the_gallop_stops_at_a_jump_in_the_tax(jump: int, at: int, limit: int) -> None:
    # A state exclusion lost all at once at an income limit: the tax jumps
    # in one step, far into a long span.
    torpedo = _torpedo_tax(10_000_00, 40_000_00)

    def tax(extra: int, ltcg: int) -> int:
        return torpedo(extra, ltcg) + (jump if extra > at else 0)
    for amount in (180_000_00, 400_000_01):
        assert engine._within_marginal_rate(tax, None, amount, limit) \
            == _step_scan(tax, None, amount, limit)


@pytest.mark.parametrize("limit", [93_700, 93_800, 120_000])
def test_the_gallop_matches_stepping_where_a_rate_rounds_unevenly(limit: int) -> None:
    # 9.37% of $100 isn't a whole number of cents: steps cost 9.37 or 9.38.
    def tax(extra: int, _ltcg: int) -> int:
        return extra * 93_740 // 1_000_000 + max(0, extra - 150_000_00) * 30_000 // 1_000_000
    assert engine._within_marginal_rate(tax, None, 300_000_00, limit) \
        == _step_scan(tax, None, 300_000_00, limit)


def test_the_gallop_takes_far_fewer_tax_computations() -> None:
    tax = _torpedo_tax(10_000_00, 40_000_00)
    calls = []

    def counted(extra: int, ltcg: int) -> int:
        calls.append(extra)
        return tax(extra, ltcg)
    assert engine._within_marginal_rate(counted, None, 300_000_00, 220_000) == \
        _step_scan(tax, None, 300_000_00, 220_000)
    # Stepping computes the tax twice for each of some 2,000 steps.
    assert len(set(calls)) < 300
