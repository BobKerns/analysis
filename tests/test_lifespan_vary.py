"""A person's lifespan as an age, varying across ensemble members (#221)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.dates import final_month_for_age
from analysis.sim.engine import run_scenario
from analysis.sim.ensemble import oat_plan, resolve_member, varying_names
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.sensitivity import parameters

# Alice was born 1965-03-15 and Bill 1967-08-20 (prototype persons.csv);
# retire_alice runs 2026-05 through 2040-12.
_HEADER = (
    "person,age_at_death,vary_distribution,vary_low,vary_high,vary_mode,"
    "vary_correlate_with,vary_correlation\n"
)


@pytest.fixture
def write_lifespans(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Write retire_alice's lifespan.csv from rows of its cells, after the header."""
    def _write(*rows: str) -> None:
        retire_alice_csv("lifespan.csv").write_text(_HEADER + "".join(f"{r}\n" for r in rows))
    return _write


def _lifespans(path: Path) -> dict[str, tuple]:
    con = sqlite3.connect(path)
    try:
        return {row[0]: row[1:] for row in con.execute(
            "SELECT person, age_at_death, final_month, vary_distribution, vary_mode "
            "FROM person_lifespan"
        )}
    finally:
        con.close()


def _draws(path: Path) -> dict[str, tuple]:
    con = sqlite3.connect(path)
    try:
        return {row[0]: row[1:] for row in con.execute(
            "SELECT calendar_event_name, vary_target, drawn_value, quantile, distribution "
            "FROM ensemble_draw WHERE vary_target != 'factor'"
        )}
    finally:
        con.close()


def _member(
    recipe: Path, tmp_path: Path, index: int, *, seed: int | None = 11,
    overrides: dict[str, float] | None = None, mode: str = "monte_carlo",
) -> Path:
    return resolve_member(
        recipe, tmp_path / f"member_{index:04d}.sqlite3", seed=seed, member_index=index,
        member_count=50, recipe_tree_id="deadbeef", overrides=overrides, mode=mode,
    )


# ---------- age to month ----------


@pytest.mark.parametrize(("age", "month"), [
    (84, "2049-03-31"),        # the 84th birthday's month
    (84.5, "2049-09-30"),
    (84.25, "2049-06-30"),
    (61.8333, "2027-01-31"),   # whole months survive four decimals
    (84.04, "2049-03-31"),     # half a month or less rounds back
])
def test_final_month_is_the_birth_month_plus_the_age_in_months(age: float, month: str) -> None:
    assert final_month_for_age("1965-03-15", age) == month


# ---------- loading ----------


def test_a_fixed_age_loads_as_its_final_month(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans("alice,68.25,,,,,,")

    assert _lifespans(load_test_copy().intent_path) == {
        "alice": (68.25, "2033-06-30", None, None),
    }


def test_a_triangular_mode_left_blank_is_the_age(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans("alice,84,triangular,75,95,,,", "bill,86,uniform,78,97,,,")

    assert _lifespans(load_test_copy().intent_path) == {
        "alice": (84.0, "2049-03-31", "triangular", 84.0),
        "bill": (86.0, "2053-08-31", "uniform", None),
    }


def test_the_old_final_month_column_is_rejected(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    retire_alice_csv("lifespan.csv").write_text("person,final_month\nalice,2033-06\n")

    with pytest.raises(LoadError, match="final_month was replaced by age_at_death") as raised:
        load_test_copy()
    assert len(raised.value.issues) == 1  # the file, not each row too

    retire_alice_csv("lifespan.csv").write_text("person,final_month\n")
    with pytest.raises(LoadError, match="final_month was replaced by age_at_death"):
        load_test_copy()


@pytest.mark.parametrize(("row", "message"), [
    ("alice,,,,,,,", "person and age_at_death are required"),
    ("alice,old,,,,,,", "'old' is not an age"),
    ("alice,140,,,,,,", "at most 130"),
    ("alice,84,,75,95,,,", "vary_low, vary_high need vary_distribution"),
    ("alice,84,normal,75,95,,,", "must be one of"),
    ("alice,84,uniform,75,,,,", "needs vary_low and vary_high"),
    ("alice,84,uniform,95,75,,,", "vary_low must be below vary_high"),
    ("alice,70,uniform,75,95,,,", "age_at_death 70 must lie within"),
    ("alice,84,triangular,75,95,96,,", "vary_mode 96 must lie within"),
    ("alice,84,uniform,75,95,80,,", "vary_mode is only used by triangular"),
    ("alice,84,uniform,55,95,,,", "before the scenario starts"),
    ("alice,84,uniform,75,95,,lifespan:bill,", "go together"),
    ("alice,84,uniform,75,95,,lifespan:bill,1.5", "between -1 and 1"),
    ("alice,84,uniform,75,95,,lifespan:carol,0.5", "names no varying calendar row"),
    ("alice,84,uniform,75,95,,lifespan:alice,0.5", "can't correlate with itself"),
])
def test_a_bad_lifespan_row_fails_the_load(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    row: str, message: str,
) -> None:
    write_lifespans(row)

    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_a_lifespan_needs_the_persons_birthdate(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    test_data_dir: Path,
) -> None:
    persons = test_data_dir / "persons.csv"
    persons.write_text(persons.read_text() + "carol,Carol (fake),,,{}\n")
    write_lifespans("carol,84,,,,,,")

    with pytest.raises(LoadError, match="needs 'carol's birthdate"):
        load_test_copy()


def test_two_lifespans_correlating_in_a_cycle_fail_the_load(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans("alice,84,uniform,75,95,,lifespan:bill,0.5",
                    "bill,86,uniform,78,97,,lifespan:alice,0.5")

    with pytest.raises(LoadError, match="correlation cycle"):
        load_test_copy()


def test_a_calendar_row_can_correlate_with_a_lifespan_but_not_take_its_name(
    write_lifespans: Callable[..., None], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans("alice,84,uniform,75,95,,,")
    append_calendar_row(
        date="2027-06-30", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill", vary_target="date", vary_distribution="uniform",
        vary_low="2029-01-01", vary_high="2029-12-31",
        vary_correlate_with="lifespan:alice", vary_correlation="0.5",
    )
    load_test_copy()

    append_calendar_row(
        date="2027-07-31", kind="retire_scheduled", name="lifespan:bill",
        scheduled_name="salary_bill", vary_target="date", vary_distribution="uniform",
        vary_low="2030-01-01", vary_high="2030-12-31",
    )
    with pytest.raises(LoadError, match="can't start with 'lifespan:'"):
        load_test_copy()


# ---------- members ----------


def test_member_zero_dies_at_the_authored_age_not_the_median(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    # Triangular 75..95 peaking at 84: its median is about 84.6.
    write_lifespans("alice,84,triangular,75,95,,,")
    recipe = load_test_copy().intent_path

    member = _member(recipe, tmp_path, 0)

    assert _lifespans(member) == {"alice": (84.0, "2049-03-31", None, 84.0)}
    assert _draws(member) == {
        "lifespan:alice": ("age_at_death", "84.0000", None, "triangular"),
    }


def test_members_draw_whole_month_ages_within_the_bounds(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    write_lifespans("alice,84,uniform,75,95,,,")
    recipe = load_test_copy().intent_path
    assert varying_names(sqlite3.connect(recipe)) == ["lifespan:alice"]

    ages = []
    for index in range(1, 21):
        age, final_month, distribution, _mode = _lifespans(_member(recipe, tmp_path, index))["alice"]
        assert 75 <= age <= 95
        assert round(age * 12) == pytest.approx(age * 12)
        assert final_month == final_month_for_age("1965-03-15", age)
        assert distribution is None
        ages.append(age)
    assert len(set(ages)) > 10


def test_fully_correlated_lifespans_draw_the_same_quantile(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    write_lifespans("alice,84,uniform,74,94,,,",
                    "bill,86,uniform,80,90,,lifespan:alice,1")
    recipe = load_test_copy().intent_path

    for index in range(1, 6):
        lifespans = _lifespans(_member(recipe, tmp_path, index))
        alice_q = (lifespans["alice"][0] - 74) / 20
        bill_q = (lifespans["bill"][0] - 80) / 10
        assert bill_q == pytest.approx(alice_q, abs=1 / 12 / 10 + 1 / 12 / 20)


def test_an_oat_member_moves_a_lifespan_to_its_quantile(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    write_lifespans("alice,84,uniform,75,95,,,", "bill,86,uniform,78,97,,,")
    recipe = load_test_copy().intent_path
    plan = oat_plan(varying_names(sqlite3.connect(recipe)), (0.1, 0.9))
    assert plan[1:] == [{"lifespan:alice": 0.1}, {"lifespan:alice": 0.9},
                        {"lifespan:bill": 0.1}, {"lifespan:bill": 0.9}]

    member = _member(recipe, tmp_path, 1, seed=None, overrides={"lifespan:alice": 0.1},
                     mode="sensitivity")

    lifespans = _lifespans(member)
    assert lifespans["alice"][0] == 77.0
    assert lifespans["bill"][0] == 86.0  # unmoved: its base age, not its median
    assert _draws(member)["lifespan:alice"][2] == 0.1


def test_a_sensitivity_batch_describes_a_varying_lifespan(
    write_lifespans: Callable[..., None], run_test_batch: Callable[..., Path],
) -> None:
    write_lifespans("alice,84,triangular,75,95,88,,")

    table = parameters(run_test_batch("--sensitivity"))

    row = table.loc["lifespan:alice"]
    assert (row["kind"], row["affects"], row["target"], row["distribution"]) == (
        "lifespan", "alice", "age_at_death", "triangular")
    assert row["range"] == "age 75 to 95"
    assert row["center"] == "age 84.0"
    assert row["shape"] == "mode age 88"
    assert row["value@0.1"].startswith("age ")


def _salary_alice_months(run_path: Path) -> list[str]:
    con = sqlite3.connect(run_path)
    try:
        return [d for (d,) in con.execute(
            "SELECT DISTINCT substr(e.date, 1, 7) FROM transfer_event e "
            "JOIN scheduled_transaction s ON s.id = e.reason_id "
            "WHERE s.name = 'salary_alice' ORDER BY 1"
        )]
    finally:
        con.close()


def test_each_member_stops_the_persons_rules_at_its_own_drawn_month(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    fund_asset_savings: Callable[..., None], tmp_path: Path,
) -> None:
    fund_asset_savings("500000.00")  # her salary stops years early
    # Alice's salary runs until she dies: a draw between ages 61.5 and 62.5
    # lands between 2026-09 and 2027-09, before she retires.
    write_lifespans("alice,62,uniform,61.5,62.5,,,")
    recipe = load_test_copy().intent_path

    last_months = {}
    for index in (0, 1, 2):
        member = _member(recipe, tmp_path, index)
        final_month = _lifespans(member)["alice"][1][:7]
        run = tmp_path / f"run_{index}.sqlite3"
        run_scenario(member, run, run_by="test", allow_dirty_data=True)
        months = _salary_alice_months(run)
        assert months and months[-1] == final_month, (index, final_month, months[-3:])
        last_months[index] = final_month
    assert last_months[0] == "2027-03"  # member 0 at the authored 62
    assert len(set(last_months.values())) > 1


def test_guided_set_offers_only_the_parents_the_loader_accepts(
    write_lifespans: Callable[..., None], append_calendar_row: Callable[..., None],
    test_data_dir: Path,
) -> None:
    from analysis.sim.cli import _lifespan_parents

    write_lifespans("alice,84,uniform,75,95,,,", "bill,86,,,,,,")
    append_calendar_row(
        date="2027-06-30", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill", vary_target="date", vary_distribution="uniform",
        vary_low="2029-01-01", vary_high="2029-12-31",
    )

    for_bill = _lifespan_parents(test_data_dir, "retire_alice", "bill")
    for_alice = _lifespan_parents(test_data_dir, "retire_alice", "alice")

    assert {"lifespan:alice", "retire_bill_vary"} <= set(for_bill)
    assert "lifespan:bill" not in for_bill  # bill's age is fixed
    assert "lifespan:alice" not in for_alice  # not itself
