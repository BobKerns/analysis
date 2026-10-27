"""A lifespan drawn from a period life table around a median age (#222)."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.dates import final_month_for_age
from analysis.sim.ensemble import life_table_age, oat_plan, resolve_member, varying_names
from analysis.sim.life_table import (
    LifeTable, LifeTableAge, LifeTableError, equivalent_multiplier, life_table_from_rows,
    months_since_birth, solve_set_forward,
)
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.sensitivity import parameters

# Alice (f) was born 1965-03-15 and Bill (m) 1967-08-20 (prototype persons.csv);
# retire_alice starts 2026-05, when Alice is 61 years 2 months old. The
# prototype's aux/life_table.csv is SSA's 2023 period table.
_ALICE_START = months_since_birth("1965-03-15", "2026-05-31")


def _lifespan(path: Path, person: str) -> tuple:
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT age_at_death, final_month, vary_distribution, life_table_set_forward "
            "FROM person_lifespan WHERE person = ?", (person,),
        ).fetchone()
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


def _distribution(path: Path, person: str) -> LifeTableAge:
    con = sqlite3.connect(path)
    try:
        (set_forward,) = con.execute(
            "SELECT life_table_set_forward FROM person_lifespan WHERE person = ?", (person,),
        ).fetchone()
        return life_table_age(con, person, set_forward)
    finally:
        con.close()


# ---------- the arithmetic ----------

# A Gompertz-like table: q doubles every 8 years, capped below 1.
_GOMPERTZ = LifeTable(tuple(min(0.99, 0.0001 * 2 ** (age / 8)) for age in range(110)))


def test_q_interpolates_between_ages_and_is_one_past_the_table() -> None:
    table = LifeTable((0.1, 0.3))
    assert table.at(0.25) == pytest.approx(0.15)
    assert table.at(1) == 0.3
    assert table.at(1.5) == pytest.approx(0.65)  # halfway to the 1 past the end
    assert table.at(2) == 1.0
    assert table.at(-3) == 0.1


@pytest.mark.parametrize("rows", [
    [(0, 0.1), (2, 0.3)],              # a gap
    [(0, 0.1), (0, 0.2), (1, 0.3)],    # an age twice
    [(1, 0.1), (2, 0.3)],              # not from 0
])
def test_a_table_needs_each_age_from_0_once(rows: list[tuple[int, float]]) -> None:
    with pytest.raises(LifeTableError, match="once each, with no gaps"):
        life_table_from_rows(rows)


@pytest.mark.parametrize("median_age", [72, 80, 84.5, 95])
def test_the_solved_set_forward_puts_the_median_at_the_age(median_age: float) -> None:
    start = 70 * 12
    months = round(median_age * 12)

    dist = LifeTableAge(_GOMPERTZ, start, solve_set_forward(_GOMPERTZ, start, months))

    assert dist.central() == months / 12
    assert dist.quantile(0) == 70
    assert dist.quantile(0.1) < dist.quantile(0.5) < dist.quantile(0.9)


def test_a_higher_median_is_a_set_back() -> None:
    start = 70 * 12
    sooner = solve_set_forward(_GOMPERTZ, start, 80 * 12)
    later = solve_set_forward(_GOMPERTZ, start, 90 * 12)
    assert later < sooner


def test_the_equivalent_multiplier_gives_the_same_median() -> None:
    start = 70 * 12
    rating = equivalent_multiplier(_GOMPERTZ, start, 80 * 12)
    assert rating is not None and rating > 1
    rated = LifeTable(tuple(min(1.0, q * rating) for q in _GOMPERTZ.q))
    assert LifeTableAge(rated, start, 0.0).central() == pytest.approx(80, abs=1 / 12)


@pytest.mark.parametrize(("median_months", "message"), [
    (69 * 12, "before the scenario starts"),
    (200 * 12, "too late"),
])
def test_an_unreachable_median_is_an_error(median_months: int, message: str) -> None:
    with pytest.raises(LifeTableError, match=message):
        solve_set_forward(_GOMPERTZ, 70 * 12, median_months)


# ---------- loading ----------


def test_a_life_table_lifespan_loads_with_its_set_forward(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans("alice,80,life_table,,,,,")

    age, final_month, distribution, set_forward = _lifespan(
        load_test_copy().intent_path, "alice")

    assert (age, final_month, distribution) == (80.0, "2045-03-31", "life_table")
    # The 2023 table gives a 61-year-old woman a median near 86: 80 reads it older.
    assert set_forward > 3


@pytest.mark.parametrize(("persons_edit", "row", "message"), [
    (("alice", "f", ""), "alice,80,life_table,,,,,", "needs 'alice's sex"),
    (("alice", "f", "x"), "alice,80,,,,,,", "sex must be one of"),
    (None, "alice,80,life_table,75,90,,,", "vary_low, vary_high must be blank"),
    (None, "alice,61,life_table,,,,,", "before the scenario starts"),
])
def test_a_bad_life_table_lifespan_fails_the_load(
    persons_edit: tuple[str, str, str] | None, row: str, message: str,
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    test_data_dir: Path,
) -> None:
    if persons_edit is not None:
        person, old, new = persons_edit
        persons = test_data_dir / "persons.csv"
        persons.write_text(persons.read_text().replace(
            f"{person},Alice (fake),1965-03-15,{old},", f"{person},Alice (fake),1965-03-15,{new},"))
    write_lifespans(row)

    with pytest.raises(LoadError, match=message):
        load_test_copy()


@pytest.mark.usefixtures("without_template_aux")
def test_a_life_table_lifespan_needs_the_aux_table(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    test_data_dir: Path,
) -> None:
    (test_data_dir / "aux" / "life_table.csv").unlink()
    write_lifespans("alice,80,life_table,,,,,")

    with pytest.raises(LoadError, match="no aux life_table.csv"):
        load_test_copy()


def test_a_life_table_with_a_gap_fails_the_load(
    load_test_copy: Callable[[], LoadResult], test_data_dir: Path,
) -> None:
    path = test_data_dir / "aux" / "life_table.csv"
    path.write_text("".join(
        line for line in path.read_text().splitlines(keepends=True)
        if not line.startswith("2023-12-31,50,")
    ))

    with pytest.raises(LoadError, match="ages must be whole numbers 0, 1, 2"):
        load_test_copy()


def test_a_persons_csv_without_a_sex_column_still_loads(
    load_test_copy: Callable[[], LoadResult], test_data_dir: Path,
) -> None:
    persons = test_data_dir / "persons.csv"
    lines = persons.read_text().splitlines()
    persons.write_text("".join(
        ",".join(cell for i, cell in enumerate(line.split(",", 5)) if i != 3) + "\n"
        for line in lines
    ))
    assert "sex" not in persons.read_text().splitlines()[0]

    load_test_copy()


# ---------- a person's own table (#315) ----------


def test_a_named_life_table_is_read_for_that_person(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans("alice,90,life_table,,,,,", "bill,84,life_table,,,,,")
    us = _distribution(load_test_copy().intent_path, "alice")
    write_lifespans("alice,90,life_table,,,,,,jp", "bill,84,life_table,,,,,")

    recipe = load_test_copy().intent_path
    jp, bill = _distribution(recipe, "alice"), _distribution(recipe, "bill")

    assert sqlite3.connect(recipe).execute(
        "SELECT person, life_table FROM person_lifespan ORDER BY person").fetchall() == [
        ("alice", "jp"), ("bill", None)]
    assert jp.central() == us.central() == 90
    # Japan's own table has a thinner tail than the US one read younger.
    assert jp.quantile(0.95) < us.quantile(0.95) - 1
    assert bill.central() == 84  # Bill keeps the default table


def test_a_named_life_table_must_be_in_the_lineage(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans("bill,84,,,,,,,xx")  # a fixed age too: `sim person show` reads the table

    with pytest.raises(LoadError, match=r"life_table 'xx' needs aux/life_table__xx\.csv"):
        load_test_copy()


def test_a_life_table_name_must_be_a_plain_name(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans("bill,84,life_table,,,,,,../JP")

    with pytest.raises(LoadError, match="life_table must name an aux"):
        load_test_copy()


def test_a_named_life_table_with_a_gap_names_its_file(
    load_test_copy: Callable[[], LoadResult], test_data_dir: Path,
) -> None:
    path = test_data_dir / "aux" / "life_table__jp.csv"
    path.write_text("".join(
        line for line in path.read_text().splitlines(keepends=True)
        if not line.startswith("2023-12-31,50,")
    ))

    with pytest.raises(LoadError, match=r"aux life_table__jp\.csv \(2023-12\): ages must be"):
        load_test_copy()


# ---------- members ----------


def test_member_zero_dies_at_the_median_and_members_spread_around_it(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    write_lifespans("alice,80,life_table,,,,,")
    recipe = load_test_copy().intent_path
    assert varying_names(sqlite3.connect(recipe)) == ["lifespan:alice"]

    assert _lifespan(_member(recipe, tmp_path, 0), "alice")[:3] == (80.0, "2045-03-31", None)
    ages = []
    for index in range(1, 13):
        age, final_month, distribution, _ = _lifespan(_member(recipe, tmp_path, index), "alice")
        assert age >= _ALICE_START / 12
        assert round(age * 12) == pytest.approx(age * 12)
        assert final_month == final_month_for_age("1965-03-15", age)
        assert distribution is None
        ages.append(age)
    assert len(set(ages)) > 6
    assert min(ages) < 80 < max(ages)


def test_the_drawn_distribution_keeps_the_authored_median(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans("alice,80,life_table,,,,,", "bill,84,life_table,,,,,")
    recipe = load_test_copy().intent_path

    alice, bill = _distribution(recipe, "alice"), _distribution(recipe, "bill")

    assert alice.central() == 80
    assert bill.central() == 84
    # Skewed like real mortality: the early tail is the longer one.
    assert 80 - alice.quantile(0.1) > alice.quantile(0.9) - 80


def test_an_oat_member_moves_a_life_table_lifespan_to_its_quantile(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    write_lifespans("alice,80,life_table,,,,,")
    recipe = load_test_copy().intent_path
    assert oat_plan(varying_names(sqlite3.connect(recipe)), (0.1, 0.9))[1:] == [
        {"lifespan:alice": 0.1}, {"lifespan:alice": 0.9}]

    member = _member(recipe, tmp_path, 1, seed=None, overrides={"lifespan:alice": 0.9},
                     mode="sensitivity")

    assert _lifespan(member, "alice")[0] == _distribution(recipe, "alice").quantile(0.9)


def test_fully_correlated_life_table_lifespans_draw_the_same_quantile(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    write_lifespans("alice,80,life_table,,,,,", "bill,84,life_table,,,,lifespan:alice,1")
    recipe = load_test_copy().intent_path
    alice, bill = _distribution(recipe, "alice"), _distribution(recipe, "bill")

    for index in range(1, 5):
        member = _member(recipe, tmp_path, index)
        alice_age, bill_age = _lifespan(member, "alice")[0], _lifespan(member, "bill")[0]
        # The same quantile of each: within a month of where Alice's lands.
        q = next(q / 1000 for q in range(1001) if alice.quantile(q / 1000) >= alice_age)
        assert abs(bill_age - bill.quantile(q)) <= 2 / 12


def test_a_sensitivity_batch_describes_a_life_table_lifespan(
    write_lifespans: Callable[..., None], run_test_batch: Callable[..., Path],
) -> None:
    write_lifespans("alice,80,life_table,,,,,")

    row = parameters(run_test_batch("--sensitivity")).loc["lifespan:alice"]

    assert row["distribution"] == "life_table"
    assert row["center"] == "age 80.0"
    assert row["range"].endswith("(1st to 99th percentile)")
    assert row["shape"].startswith("set-forward +")


# ---------- CLI ----------


def _sim(test_data_dir: Path, *args: str) -> Result:
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(test_data_dir)])


def test_lifespan_set_life_table_shows_what_it_means(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "person", "lifespan", "set", "alice", "80",
                  "--vary", "life_table", "--scenario", "retire_alice")

    assert result.exit_code == 0, result.output
    assert "life table read" in result.output and "years older" in result.output
    assert "of the table's death rates" in result.output
    assert "50th 80.0" in result.output


def test_lifespan_set_life_table_refuses_bounds(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "person", "lifespan", "set", "alice", "80",
                  "--vary", "life_table", "--low", "70", "--scenario", "retire_alice")

    assert result.exit_code != 0
    assert "--low don't apply" in result.output


def test_person_edit_sets_the_sex(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "person", "edit", "joint", "--sex", "M")
    assert result.exit_code == 0, result.output
    assert "joint,Joint household (fake),1970-01-01,m," in (test_data_dir / "persons.csv").read_text()

    refused = _sim(test_data_dir, "person", "edit", "joint", "--sex", "x")
    assert refused.exit_code != 0
    assert "sex must be one of" in refused.output


def test_lifespan_set_takes_a_life_table_per_scenario(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    result = _sim(test_data_dir, "person", "lifespan", "set", "alice", "90",
                  "--vary", "life_table", "--life-table", "jp", "--scenario", "retire_alice")
    assert result.exit_code == 0, result.output
    assert "age 90 (life_table, jp table)" in result.output
    assert "alice,90,life_table,,,,,,jp" in retire_alice_csv("lifespan.csv").read_text()

    cleared = _sim(test_data_dir, "person", "lifespan", "set", "alice", "--life-table", "",
                   "--scenario", "retire_alice")
    assert cleared.exit_code == 0, cleared.output
    assert "alice,90,life_table,,,,,," in retire_alice_csv("lifespan.csv").read_text()


def test_lifespan_set_asks_for_the_life_table_when_guided(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "person", "lifespan", "set", "alice", "--scenario", "retire_alice",
        "--data-dir", str(test_data_dir)], input="90\nlife_table\n\njp\n")

    assert result.exit_code == 0, result.output
    assert "life table (jp; blank for the default)" in result.output
    assert "alice,90,life_table,,,,,,jp" in retire_alice_csv("lifespan.csv").read_text()


# ---------- the extractors ----------


def test_the_extractor_reads_the_ssa_page(repo_root: Path) -> None:
    import runpy

    extract = runpy.run_path(str(repo_root / "scripts" / "extract_ssa_life_table.py"))["extract"]
    page = (
        "<h2>Period Life Table, 2023, as used in the 2026 Trustees Report</h2><table>"
        "<tr><th>Exact age</th><th>Death probability</th></tr>"
        "<tr><td>0</td><td>0.006015</td><td>100,000</td><td>75.79</td>"
        "<td>0.005125</td><td>100,000</td><td>81.06</td></tr>"
        "<tr><td>1</td><td>0.000479</td><td>99,398</td><td>75.25</td>"
        "<td>0.000392</td><td>99,488</td><td>80.47</td></tr></table>"
    )

    assert extract(page) == ("2023", [(0, "0.006015", "0.005125"), (1, "0.000479", "0.000392")])


_HLD_TEXT = """Table A. Abridged Life Tables for Testland, 2023
Male
        x               nq x            lx
       0      (W)      0.00065        100 000
{male}
Female
        x               nq x            lx
{female}
"""


def _hld_rows(q: Callable[[int], float], expectancy: float) -> str:
    rows = [f"       {age}{'      (Y)' if age == 0 else ''}      {q(age):.5f}       99 000"
            for age in range(105)]
    rows.append(f"105 -      1.00000        56        56        59        59      {expectancy}")
    return "\n".join(rows)


def test_the_extractor_reads_an_hld_table_and_extends_its_open_interval(repo_root: Path) -> None:
    import runpy

    script = runpy.run_path(str(repo_root / "scripts" / "extract_hld_life_table.py"))

    # Gompertz: the hazard grows 10% a year, which the extrapolation should keep.
    def gompertz(age: int) -> float:
        return 1 - math.exp(-0.0001 * 1.1 ** (age + 0.5))

    year, tables = script["extract"](_HLD_TEXT.format(
        male=_hld_rows(gompertz, 1.0), female=_hld_rows(lambda a: gompertz(a + 3), 1.3)))

    assert year == "2023"
    assert sorted(tables) == ["female", "male"]
    assert all(sorted(t) == list(range(110)) for t in tables.values())
    assert tables["male"][109] == tables["female"][109] == "1.000000"  # no one reaches 110
    assert tables["male"][104] == f"{gompertz(104):.5f}"
    assert float(tables["male"][108]) == pytest.approx(gompertz(108), abs=0.01)
    female = [float(tables["female"][age]) for age in range(100, 110)]
    assert female == sorted(female)  # the death rate keeps rising


def test_the_blend_averages_hazards_by_weight(repo_root: Path) -> None:
    import runpy

    blend = runpy.run_path(str(repo_root / "scripts" / "blend_life_tables.py"))["blend"]
    a = {0: {"q_female": 0.1, "q_male": 0.2}, 1: {"q_female": 0.5, "q_male": 0.5}}
    b = {0: {"q_female": 0.3, "q_male": 0.2}}

    mixed = blend([(a, 3), (b, 1)])

    hazard = 0.75 * -math.log(0.9) + 0.25 * -math.log(0.7)
    assert mixed[0]["q_female"] == pytest.approx(1 - math.exp(-hazard))
    assert mixed[0]["q_male"] == pytest.approx(0.2)
    assert mixed[1] == {"q_female": 1.0, "q_male": 1.0}  # past b's last age
