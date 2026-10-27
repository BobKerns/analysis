"""Each person's age-at-death distribution, as numbers and charts (#312)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.connection import ScenarioConnection
from analysis.sim.lifespan_curves import PersonLifespan, household, lifespan_figure, person_lifespans
from analysis.sim.loader import LoadResult

# Alice (f, born 1965-03) and Bill (m, born 1967-08) have a sex in the
# prototype's persons.csv; joint doesn't. retire_alice starts 2026-05.


@pytest.fixture
def lifespans(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> Callable[..., list[PersonLifespan]]:
    def _read(*rows: str) -> list[PersonLifespan]:
        write_lifespans(*rows)
        con = sqlite3.connect(load_test_copy().intent_path)
        try:
            persons, notes = person_lifespans(con)
        finally:
            con.close()
        assert notes == []
        return persons
    return _read


def test_only_persons_with_a_sex_are_drawn(lifespans: Callable[..., list[PersonLifespan]]) -> None:
    assert [p.person for p in lifespans()] == ["alice", "bill"]


def test_a_life_table_lifespan_is_drawn_with_its_median_at_the_authored_age(
    lifespans: Callable[..., list[PersonLifespan]],
) -> None:
    alice, bill = lifespans("alice,88,life_table,,,,,", "bill,84,,,,,,")

    assert alice.adjusted and alice.assumed_age is None
    assert alice.quantile(0.5) == pytest.approx(88.0, abs=1 / 12)
    # Bill's fixed age doesn't move the table; it is marked beside it.
    assert not bill.adjusted and bill.set_forward == 0.0 and bill.assumed_age == 84.0
    assert bill.quantile(0.5) != pytest.approx(84.0, abs=0.5)


def test_the_yearly_chances_start_now_and_add_up(lifespans: Callable[..., list[PersonLifespan]]) -> None:
    alice, _bill = lifespans()
    years = alice.yearly()

    assert years[0][0] == 61 and alice.age_now == pytest.approx(61 + 2 / 12)
    assert sum(chance for _age, chance, _by in years) == pytest.approx(1.0)
    by_end = [by for _age, _chance, by in years]
    assert by_end == sorted(by_end) and by_end[-1] == pytest.approx(1.0)


def test_the_household_outlives_each_person(lifespans: Callable[..., list[PersonLifespan]]) -> None:
    persons = lifespans()
    h = household(persons)

    for k in (0, 120, 300):
        assert h.any_alive[k] >= max(a[k] for a in h.alive) >= h.all_alive[k]
        assert h.any_alive[k] == pytest.approx(1 - (1 - h.alive[0][k]) * (1 - h.alive[1][k]))
    # The last of the two dies later than either one's own median.
    last = h.last_death_quantile(0.5)
    for p in persons:
        own = p.birthdate.year + p.quantile(0.5)
        assert last.year > own
    assert h.last_death_quantile(0.05) < last < h.last_death_quantile(0.95) <= date(2097, 1, 1)


def test_the_figure_has_two_panels_a_person_and_the_household(
    lifespans: Callable[..., list[PersonLifespan]],
) -> None:
    persons = lifespans("alice,88,life_table,,,,,")

    assert len(lifespan_figure(persons).axes) == 5
    assert len(lifespan_figure(persons, show_household=False).axes) == 4
    assert len(lifespan_figure(persons[:1]).axes) == 2


def test_the_charts_span_the_scenario_and_share_the_pdf_scale(
    lifespans: Callable[..., list[PersonLifespan]],
) -> None:
    alice, bill = persons = lifespans("alice,88,life_table,,,,,")
    axes = lifespan_figure(persons).axes

    # retire_alice runs 2026-05 through 2040-12.
    assert axes[0].get_xlim() == pytest.approx((alice.age_now, alice.age_now + 176 / 12))
    assert axes[3].get_xlim() == pytest.approx((bill.age_now, bill.age_at_end))
    assert axes[0].get_ylim() == axes[2].get_ylim()
    peak = max(chance for p in persons for _age, chance, _by in p.yearly())
    assert axes[0].get_ylim()[1] == pytest.approx(peak * 1.05)
    assert axes[4].get_xlim() == pytest.approx((2026 + 4 / 12, 2041))


def test_the_scenario_connection_reads_the_lifespans(test_data_dir: Path) -> None:
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        persons, notes = scenario.lifespans()

    assert [p.person for p in persons] == ["alice", "bill"] and notes == []


# ---------- sim person show ----------


def _sim(test_data_dir: Path, *args: str) -> Result:
    return CliRunner().invoke(main, ["sim", "person", "show", *args,
                                     "--scenario", "retire_alice", "--data-dir", str(test_data_dir)])


def test_person_show_prints_percentiles_and_the_household(
    test_data_dir: Path, write_lifespans: Callable[..., None],
) -> None:
    write_lifespans("alice,88,life_table,,,,,", "bill,84,,,,,,")

    result = _sim(test_data_dir)

    assert result.exit_code == 0, result.output
    assert "life_table lifespan, median 88.0" in result.output
    assert "the plain m table; the scenario assumes 84" in result.output
    assert "Died by then" in result.output and "p95" in result.output
    assert "household: the year the last of alice, bill dies" in result.output


def test_person_show_one_person_leaves_out_the_household(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "bill")

    assert result.exit_code == 0, result.output
    assert "Bill (fake)" in result.output and "Alice (fake)" not in result.output
    assert "household" not in result.output
    assert "the scenario sets no lifespan" in result.output


def test_person_show_refuses_a_person_without_a_sex(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "joint")

    assert result.exit_code != 0
    assert "has no sex" in result.output


@pytest.mark.slow
def test_person_show_writes_the_graph_file(test_data_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "lifespans.png"

    result = _sim(test_data_dir, "--graph-file", str(out))

    assert result.exit_code == 0, result.output
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
