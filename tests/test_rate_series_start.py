"""The loader refuses a growth-rate series with no rate by the month a rule
first reads it (#475), instead of the run failing that month with "no aux
row"."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from analysis.sim import loader
from analysis.sim.loader import ORIGIN, LoadError, LoadResult

SCENARIO = {"start": "2026-05-31", "end": "2040-12-31"}


def _rate(series: str, *effective: str) -> list[tuple[str, str | None, dict[str, Any], str]]:
    return [("rate", series, {"annual_rate_micros": 50_000}, e) for e in effective]


def _rule(**fields: str) -> dict[str, str]:
    """A hand-written percent_of_balance rule, as read from scheduled.csv."""
    return {
        ORIGIN: "scenarios/s/scheduled.csv line 2", "name": "grow",
        "amount_rule_kind": "percent_of_balance", "cadence": "monthly",
        "start": "2026-05-31", **fields,
    }


def _growth(series: str = "", blend: list[Any] | None = None) -> dict[str, str]:
    """A lot's growth rule as ``_expand_account_growth`` makes it: no ORIGIN."""
    return {
        "name": "growth_acct", "to_account": "Acct", "to_lot": "default",
        "amount_rule_kind": "percent_of_balance", "cadence": "monthly",
        "series": series, "start": SCENARIO["start"],
        "params_json": json.dumps({"blend": blend}) if blend else "",
    }


def _issues(scheduled: list[dict[str, str]], aux: list, calendar: list | None = None) -> list[str]:
    issues: list[str] = []
    loader._validate_rate_series_start(scheduled, calendar or [], aux, SCENARIO, issues)
    return issues


def test_a_lots_growth_reads_its_series_from_the_scenarios_start() -> None:
    [issue] = _issues([_growth("stocks")], _rate("stocks", "2027-01-31"))
    assert issue == (
        "rate__stocks.csv: its first rate is from 2027-01, but the growth of Acct reads it "
        "from 2026-05; add a row on or before 2026-05, or a set_rate row with a fixed date "
        "by then"
    )


def test_a_lots_payouts_are_named_as_such() -> None:
    """A taxable lot's dividend or interest rule (#468) reads its yield series
    from the start too."""
    payout = {**_growth("yield"), "params_json": '{"payout": true}'}
    [issue] = _issues([payout], _rate("yield", "2027-01-31"))
    assert "but the payout rule for Acct reads it from 2026-05" in issue


def test_a_growth_rule_reads_the_payout_rates_it_takes_out() -> None:
    """Even with its payout rule retired, a taxable lot's growth rule reads
    the yield it subtracts from the total return (#468)."""
    growth = {**_growth("stocks"),
              "params_json": json.dumps({"less": [{"series": "yield"}]})}
    aux = _rate("stocks", "2026-01-31") + _rate("yield", "2027-01-31")
    [issue] = _issues([growth], aux)
    assert "but the growth of Acct (less its payout) reads it from 2026-05" in issue
    blended = {**growth, "params_json": json.dumps(
        {"less": [{"blend": [["2026-05-31", [["yield", 1_000_000]]]]}]})}
    assert len(_issues([blended], aux)) == 1


def test_a_series_with_a_rate_by_the_start_loads() -> None:
    assert _issues([_growth("stocks")], _rate("stocks", "2026-05-31", "2030-01-31")) == []


def test_a_hand_written_rule_reads_from_its_own_start() -> None:
    aux = _rate("bonus", "2028-01-31")
    assert _issues([_rule(series="bonus", start="2028-01-31")], aux) == []
    [issue] = _issues([_rule(series="bonus", start="2027-06-30")], aux)
    assert issue.startswith("rate__bonus.csv: its first rate is from 2028-01, but "
                            "scenarios/s/scheduled.csv line 2 (grow) reads it from 2027-06")


def test_a_rule_reads_from_its_first_firing() -> None:
    """An annual rule starting in January first posts in its month_of_year."""
    aux = _rate("bonus", "2027-06-30")
    annual = _rule(series="bonus", start="2027-01-31", cadence="annual", month_of_year="6")
    assert _issues([annual], aux) == []
    annual["month_of_year"] = "3"
    assert len(_issues([annual], aux)) == 1


def test_a_rule_that_never_fires_in_the_run_never_reads_it() -> None:
    aux = _rate("bonus", "2045-01-31")
    assert _issues([_rule(series="bonus", start="2041-01-31")], aux) == []
    one_off_before_the_start = _rule(series="bonus", start="2025-01-31", cadence="one_off")
    assert _issues([one_off_before_the_start], aux) == []


def test_a_rule_retired_before_it_first_fires_never_reads() -> None:
    """retire_scheduled ends a rule after that month: a December rule retired
    in June 2026 never posts, but one retired in December still does."""
    aux = _rate("bonus", "2027-01-31")
    annual = _rule(series="bonus", cadence="annual", month_of_year="12")
    retire = {"kind": "retire_scheduled", "scheduled_name": "grow", "date": "2026-06-30"}
    assert _issues([annual], aux, [retire]) == []
    assert len(_issues([annual], aux, [{**retire, "date": "2026-12-31"}])) == 1
    assert len(_issues([annual], aux, [{**retire, "vary_target": "date"}])) == 1


def test_a_rule_retired_mid_month_misses_that_month() -> None:
    """A retirement keeps its day and is the rule's inclusive end; the rule
    posts at month-end, so a June rule retired June 15 never posts, even when
    the retirement falls in the start month's first days."""
    aux = _rate("bonus", "2027-01-31")
    june = _rule(series="bonus", cadence="annual", month_of_year="6", start="2026-06-30")
    retire = {"kind": "retire_scheduled", "scheduled_name": "grow", "date": "2026-06-15"}
    assert _issues([june], aux, [retire]) == []
    december = _rule(series="bonus", cadence="annual", month_of_year="12")
    assert _issues([december], aux, [{**retire, "date": "2026-05-15"}]) == []
    biweekly = _rule(series="bonus", cadence="biweekly", start="2026-06-05")
    assert len(_issues([biweekly], aux, [{**retire, "date": "2026-06-15"}])) == 1


def test_a_swap_supplies_the_series_from_its_date() -> None:
    swap = {"kind": "swap_aux_table", "series": "stocks", "date": "2026-05-31",
            "params_json": '{"aux_kind": "rate", "source_series": "old"}'}
    aux = _rate("stocks", "2027-01-31") + _rate("old", "2026-01-31")
    assert _issues([_growth("stocks")], aux, [swap]) == []
    assert len(_issues([_growth("stocks")], aux, [{**swap, "date": "2026-07-31"}])) == 1


def test_rate_kind_names_the_series_kind() -> None:
    aux = [("inflation", "medical", {"annual_rate_micros": 50_000}, "2027-01-31")]
    rule = _rule(series="medical", params_json='{"rate_kind": "inflation"}')
    [issue] = _issues([rule], aux)
    assert issue.startswith("inflation__medical.csv: its first rate is from 2027-01")


def test_a_set_rate_with_a_fixed_date_supplies_a_rate_in_time() -> None:
    aux = _rate("stocks", "2027-01-31")
    set_rate = {"kind": "set_rate", "series": "stocks", "date": "2026-05-31", "params_json": ""}
    assert _issues([_growth("stocks")], aux, [set_rate]) == []
    late = {**set_rate, "date": "2026-06-30"}
    assert len(_issues([_growth("stocks")], aux, [late])) == 1


def test_a_set_rate_before_the_first_month_does_not_count() -> None:
    """The run never applies an event dated before its first month."""
    set_rate = {"kind": "set_rate", "series": "stocks", "date": "2026-04-30"}
    assert len(_issues([_growth("stocks")], _rate("stocks", "2027-01-31"), [set_rate])) == 1


def test_an_unknown_cadence_is_left_to_the_scheduled_check() -> None:
    assert _issues([_rule(series="bonus", cadence="weekly")], _rate("bonus", "2030-01-31")) == []


@pytest.mark.parametrize("extra", [
    {"vary_target": "date"},
    {loader.DATE_REFS: '{"date":["retire",0]}'},
], ids=["date varies", "relative to a marker"])
def test_a_set_rate_whose_date_can_move_does_not_count(extra: dict[str, str]) -> None:
    set_rate = {"kind": "set_rate", "series": "stocks", "date": "2026-05-31", **extra}
    assert len(_issues([_growth("stocks")], _rate("stocks", "2027-01-31"), [set_rate])) == 1


def test_a_set_rate_for_another_aux_kind_does_not_count() -> None:
    set_rate = {"kind": "set_rate", "series": "stocks", "date": "2026-05-31",
                "params_json": '{"aux_kind": "inflation"}'}
    assert len(_issues([_growth("stocks")], _rate("stocks", "2027-01-31"), [set_rate])) == 1


def test_each_blend_slice_is_read_from_when_its_set_applies() -> None:
    """A glide path: stocks from the start, bonds once the 2030 set applies."""
    blend = [
        ["2026-05-31", [["stock_us", 1_000_000]]],
        ["2030-12-31", [["stock_us", 500_000], ["bond", 500_000]]],
    ]
    stocks = _rate("stock_us", "2026-01-31")
    assert _issues([_growth(blend=blend)], stocks + _rate("bond", "2030-12-31")) == []
    [issue] = _issues([_growth(blend=blend)], stocks + _rate("bond", "2031-01-31"))
    assert issue.startswith(
        "rate__bond.csv: its first rate is from 2031-01, but the growth of Acct (bond in its "
        "blend) reads it from 2030-12")


def test_a_blend_set_replaced_before_the_start_is_never_read() -> None:
    blend = [
        ["2020-01-31", [["bond", 1_000_000]]],
        ["2025-01-31", [["stock_us", 1_000_000]]],
    ]
    aux = _rate("stock_us", "2026-01-31") + _rate("bond", "2035-01-31")
    assert _issues([_growth(blend=blend)], aux) == []


def test_a_blend_set_that_applies_after_the_run_ends_is_never_read() -> None:
    blend = [
        ["2026-05-31", [["stock_us", 1_000_000]]],
        ["2045-01-31", [["bond", 1_000_000]]],
    ]
    aux = _rate("stock_us", "2026-01-31") + _rate("bond", "2046-01-31")
    assert _issues([_growth(blend=blend)], aux) == []


def test_a_series_nothing_supplies_is_left_to_the_reference_check() -> None:
    assert _issues([_growth("nowhere")], []) == []


def test_the_loader_refuses_a_lot_growth_series_that_starts_late(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    retire_alice_csv("series/rate__brokerage_growth.csv").write_text(
        "effective_date,annual_rate\n2027-01-31,6%\n")
    with pytest.raises(LoadError, match=(
            r"rate__brokerage_growth.csv: its first rate is from 2027-01, but the growth of "
            r"Asset_Brokerage_Joint reads it from 2026-05")):
        load_test_copy()


def test_the_loader_takes_a_set_rate_at_the_start(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
    append_calendar_row: Callable[..., None],
) -> None:
    retire_alice_csv("series/rate__brokerage_growth.csv").write_text(
        "effective_date,annual_rate\n2027-01-31,6%\n")
    append_calendar_row(date="2026-05-31", kind="set_rate", series="brokerage_growth",
                        amount="5%")
    load_test_copy()
