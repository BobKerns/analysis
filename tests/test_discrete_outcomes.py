"""#165: a `discrete` vary_distribution -- a few weighted outcomes, each a
single value or a range spread evenly -- from the pure distribution through
load and member resolution to a run."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.distributions import (
    Discrete,
    DistributionError,
    Outcome,
    member_rng,
    moments,
    parse_outcomes,
    rewrite_outcomes,
)
from analysis.sim.engine import run_scenario
from analysis.sim.ensemble import resolve_member
from analysis.sim.loader import LoadError, LoadResult

# "Lost", "Small", "Fair", "Good": an exact zero plus three ranges.
_OUTCOMES = Discrete((
    Outcome(0.30, 0, 0), Outcome(0.25, 1_000, 6_000),
    Outcome(0.30, 6_000, 20_000), Outcome(0.15, 20_000, 55_000),
))


def _number(text: str, _role: str) -> float:
    return float(text)


# --- the distribution ------------------------------------------------------------


def test_discrete_quantile_steps_at_a_value_and_spreads_across_a_range() -> None:
    assert _OUTCOMES.quantile(0) == 0
    assert _OUTCOMES.quantile(0.3) == 0  # the whole zero outcome
    assert _OUTCOMES.quantile(0.425) == pytest.approx(3_500)  # halfway through "Small"
    assert _OUTCOMES.quantile(1) == 55_000
    assert _OUTCOMES.central() == pytest.approx(_OUTCOMES.quantile(0.5))


def test_discrete_draws_land_on_each_outcome_at_its_weight() -> None:
    rng = member_rng(seed=3, member_index=0, row_name="exit")
    draws = [_OUTCOMES.draw(rng) for _ in range(20_000)]
    assert sum(d == 0 for d in draws) / len(draws) == pytest.approx(0.30, abs=0.015)
    assert sum(20_000 <= d for d in draws) / len(draws) == pytest.approx(0.15, abs=0.015)


def test_discrete_outcomes_sort_by_value_whatever_the_authored_order() -> None:
    shuffled = Discrete(tuple(reversed(_OUTCOMES.outcomes)))
    assert shuffled == _OUTCOMES


def test_discrete_moments_reflect_the_point_mass() -> None:
    m = moments(Discrete((Outcome(0.5, 0, 0), Outcome(0.5, 10, 10))))
    assert m.mean == pytest.approx(5) and m.sd == pytest.approx(5)


@pytest.mark.parametrize(("outcomes", "message"), [
    ((Outcome(0.5, 0, 0), Outcome(0.4, 1, 1)), "add up to 100%"),
    ((Outcome(0.5, 0, 10), Outcome(0.5, 5, 20)), "overlap"),
    ((Outcome(0.5, 3, 3), Outcome(0.5, 3, 3)), "overlap"),
    ((Outcome(1.0, 10, 0),), "low end must come first"),
    ((Outcome(0.0, 0, 0), Outcome(1.0, 1, 1)), "above 0%"),
    ((), "at least one outcome"),
])
def test_discrete_rejects_bad_outcomes(outcomes: tuple[Outcome, ...], message: str) -> None:
    with pytest.raises(DistributionError, match=message):
        Discrete(outcomes)


def test_discrete_allows_ranges_that_share_an_end() -> None:
    Discrete((Outcome(0.5, 0, 10), Outcome(0.5, 10, 20)))


def test_parse_outcomes_reads_values_ranges_and_weights() -> None:
    parsed = parse_outcomes("30%: 0; 25%: 1000..6000;", _number)
    assert parsed == (Outcome(0.30, 0, 0), Outcome(0.25, 1_000, 6_000))


@pytest.mark.parametrize(("text", "message"), [
    ("30: 0; 70%: 5", "needs a '%'"),
    ("30% 0", "isn't 'weight%: value'"),
    ("lots%: 0", "is not a percent"),
    ("100%: much", "could not convert"),
    ("  ", "is empty"),
])
def test_parse_outcomes_rejects_malformed_text(text: str, message: str) -> None:
    with pytest.raises(DistributionError, match=message):
        parse_outcomes(text, _number)


def test_rewrite_outcomes_converts_values_and_keeps_weights() -> None:
    def roles(text: str, role: str) -> str:
        return f"{text}:{role}"
    assert rewrite_outcomes("30%: a; 70%: b..c", roles) == "30%: a:point; 70%: b:low..c:high"


# --- load ------------------------------------------------------------------------


def _exit_row(append_calendar_row: Callable[..., None], outcomes: str, **extra: str) -> None:
    """A one_off_transfer whose amount is the stock's exit outcome."""
    append_calendar_row(**{
        "date": "2030-06-30", "kind": "one_off_transfer", "name": "stock_exit",
        "account_1": "Income_Inheritance", "account_2": "Asset_Brokerage_Joint",
        "lot_2": "default", "amount": "1.00", "tax_character": "none",
        "vary_target": "amount", "vary_distribution": "discrete", "vary_outcomes": outcomes,
        **extra,
    })


def _issues(load: Callable[[], LoadResult]) -> list[str]:
    with pytest.raises(LoadError) as ei:
        load()
    return ei.value.issues


def test_discrete_amount_loads_in_cents_with_a_zero_outcome(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _exit_row(append_calendar_row, "30%: 0; 70%: 1000..6000.50")
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        stored = con.execute(
            "SELECT vary_outcomes FROM calendar_event WHERE name = 'stock_exit'"
        ).fetchone()[0]
    finally:
        con.close()
    assert stored == "30%: 0; 70%: 100000..600050"


def test_discrete_date_outcomes_take_bare_months_by_role(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(
        date="2030-12-31", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill", vary_target="date", vary_distribution="discrete",
        vary_outcomes="50%: 2030-06; 50%: 2031-01..2031-03",
    )
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        stored = con.execute(
            "SELECT vary_outcomes FROM calendar_event WHERE name = 'retire_bill_vary'"
        ).fetchone()[0]
    finally:
        con.close()
    assert stored == "50%: 2030-06-15; 50%: 2031-01-01..2031-03-31"


@pytest.mark.parametrize(("outcomes", "extra", "message"), [
    ("30%: 0; 60%: 1000", {}, "add up to 100%"),
    ("50%: 0..2000; 50%: 1000..3000", {}, "overlap"),
    ("50%: -5; 50%: 1000", {}, "requires no negative outcome"),
    ("100%: lots", {}, "vary_outcomes"),
    ("", {}, "discrete requires vary_outcomes"),
    ("100%: 5", {"vary_low": "1.00"}, "vary_low is not used by discrete"),
])
def test_discrete_rejects_bad_rows(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    outcomes: str, extra: dict[str, str], message: str,
) -> None:
    _exit_row(append_calendar_row, outcomes, **extra)
    issues = _issues(load_test_copy)
    assert sum(message in issue for issue in issues) == 1, issues


def test_vary_outcomes_rejected_on_another_distribution(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _exit_row(append_calendar_row, "100%: 5", vary_distribution="uniform",
              vary_low="1.00", vary_high="2.00")
    assert any("vary_outcomes is not used by uniform" in i for i in _issues(load_test_copy))


# --- members and a run -----------------------------------------------------------


def test_members_draw_outcomes_and_run_through_a_zero(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """Member 0 sits at the median; drawn members land on an outcome; a zero
    outcome runs as a transfer of nothing."""
    _exit_row(append_calendar_row, "50%: 0; 50%: 1000..6000")
    recipe = load_test_copy().intent_path
    amounts = []
    for i in range(12):
        member = resolve_member(
            recipe, tmp_path / f"member_{i:04d}.sqlite3",
            seed=5, member_index=i, member_count=12, recipe_tree_id="deadbeef",
        )
        con = sqlite3.connect(member)
        try:
            amounts.append(con.execute(
                "SELECT amount FROM calendar_event WHERE name = 'stock_exit'"
            ).fetchone()[0])
        finally:
            con.close()
        if amounts[-1] == 0:
            run_scenario(member, tmp_path / f"run_{i:04d}.sqlite3", run_by="test",
                         allow_dirty_data=True, allow_dirty_code=True)
    assert amounts[0] == 0  # the median of a 50% zero is its top edge
    assert all(a == 0 or 100_000 <= a <= 600_000 for a in amounts)
    assert 0 < sum(a == 0 for a in amounts) < len(amounts)


# --- the command line --------------------------------------------------------------


def _add_exit(test_data_dir: Path, outcomes: str) -> Result:
    return CliRunner().invoke(main, [
        "sim", "calendar", "add", "transfer",
        "2030-06-30", "Income_Inheritance", "Asset_Brokerage_Joint", "1.00",
        "--to-lot", "default", "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
        "--name", "stock_exit", "--vary-target", "amount",
        "--vary-distribution", "discrete", "--vary-outcomes", outcomes,
    ])


def test_calendar_add_writes_outcomes_in_canonical_dollars_and_runs(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
    runs_dir: Path,
) -> None:
    result = _add_exit(test_data_dir, "40%: 0;60%: 1000..6000.5")
    assert result.exit_code == 0, result.output
    df = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    assert df.iloc[-1]["vary_outcomes"] == "40%: 0.00; 60%: 1000.00..6000.50"

    runner = CliRunner()
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--allow-dirty-data", "--no-run",
    ])
    assert load.exit_code == 0, load.output
    run = runner.invoke(main, [
        "sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--ensemble-size", "4", "--allow-dirty-data",
    ])
    assert run.exit_code == 0, run.output


@pytest.mark.parametrize(("outcomes", "message"), [
    ("40%: 0; 50%: 1000", "add up to 100%"),
    ("40%: 0; 60%: lots", "lots"),
])
def test_calendar_add_rejects_bad_outcomes(test_data_dir: Path, outcomes: str, message: str) -> None:
    result = _add_exit(test_data_dir, outcomes)
    assert result.exit_code != 0 and message in result.output, result.output
