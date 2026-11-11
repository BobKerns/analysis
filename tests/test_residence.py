"""Where the household lives (#509): ``residence.csv``'s dated rows of a
state and a county. A tax year is filed where the household lives on its
December 31, whenever it is settled (#498), and reads that state's tables:
its brackets, standard deduction, Social Security table, and the rest."""

from __future__ import annotations

import json
from contextlib import closing
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult, _firing_years, _residence_states

BRACKETS = "effective_date,filing_status,bracket_index,floor,marginal_rate\n"


def _run(load_test_copy: Callable[[], LoadResult], runs_dir: Path, name: str) -> Path:
    run_path = runs_dir / f"{name}.sqlite3"
    run_scenario(load_test_copy().intent_path, run_path, run_by="test", allow_dirty_data=True)
    return run_path


def _buckets(run: Path, bucket: str) -> dict[int, int]:
    with closing(sqlite3.connect(run)) as con:
        return dict(con.execute(
            "SELECT year, amount FROM tax_year_summary WHERE bucket = ?", (bucket,)).fetchall())


def _oregon(write_aux: Callable[[str, str], Path], rate: str = "0%") -> None:
    """Tables of an Oregon of the tests' own: a flat ``rate``."""
    write_aux("state_brackets__OR.csv", BRACKETS + "".join(
        f"2026-01-31,{status},0,0.00,{rate}\n" for status in ("Single", "MFJ", "MFS")))


def test_a_move_before_april_settles_the_year_before_in_the_old_state(
    add_state_tax: Callable[..., None], add_residence: Callable[..., Path],
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    """#498: a February move comes before the April settlement of the year
    before, which is still filed in California, where the household lived
    on its December 31; the year of the move is Oregon's."""
    add_state_tax("CA")
    _oregon(write_aux)
    add_residence("2028-02", "OR", description="Move to Oregon (OR)")
    tax = _buckets(_run(load_test_copy, runs_dir, "move"), "state_tax")
    assert tax[2027] > 0
    assert tax[2028] == 0


def test_the_residence_is_in_the_intent_and_the_run(
    add_state_tax: Callable[..., None], add_residence: Callable[..., Path],
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    add_state_tax("CA")
    _oregon(write_aux)
    add_residence("2030-07", "OR")
    run = _run(load_test_copy, runs_dir, "run")
    with closing(sqlite3.connect(run)) as con:
        assert con.execute(
            "SELECT effective_date, state, county FROM residence ORDER BY effective_date"
        ).fetchall() == [("2026-05-31", "CA", None), ("2030-07-31", "OR", None)]


def test_rows_merge_by_date_over_the_lineage(
    add_state_tax: Callable[..., None], add_residence_to: Callable[..., Path],
    write_aux: Callable[[str, str], Path], test_child_scenario: Path,
    load_test_scenario: Callable[[str], Path],
) -> None:
    """A child adds a move without restating where the household starts,
    and a row of the same month replaces its parent's."""
    add_state_tax("CA")
    _oregon(write_aux)
    add_residence_to(test_child_scenario, "2030-07", "OR")
    add_residence_to(test_child_scenario, "2026-05", "NC")
    with closing(sqlite3.connect(load_test_scenario("child"))) as con:
        assert con.execute(
            "SELECT effective_date, state FROM residence ORDER BY effective_date"
        ).fetchall() == [("2026-05-31", "NC"), ("2030-07-31", "OR")]


@pytest.mark.parametrize(("row", "message"), [
    (("2028-01", "XX"), r"residence.csv line 3: state 'XX' is not a state's or territory's"),
    (("", "OR"), r"residence.csv line 3: effective_date is required"),
    (("2026-05", "OR"), r"residence.csv line 3: duplicate effective_date"),
    (("2028-01", "NY"), r"residence.csv line 3: no aux/state_brackets__NY.csv for New York \(NY\)"),
])
def test_the_loader_checks_each_row(
    row: tuple[str, str], message: str, add_state_tax: Callable[..., None],
    add_residence: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax("CA")
    add_residence(*row)
    with pytest.raises(LoadError, match=message):
        load_test_copy()


@pytest.mark.parametrize("state_tax", [True, False])
def test_a_territory_with_a_tax_code_of_its_own_is_refused(
    state_tax: bool, add_state_tax: Callable[..., None], add_residence: Callable[..., Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """With or without a state tax: the federal tax alone would tax Puerto
    Rico's own income, which federal law leaves to Puerto Rico (#491)."""
    if state_tax:
        add_state_tax("CA")
    else:
        add_residence("2026-05", "CA")
    add_residence("2028-01", "PR")
    with pytest.raises(LoadError, match=r"Puerto Rico \(PR\) has a tax code of its own .*issues/570"):
        load_test_copy()


def test_tables_of_a_mirror_territory_are_refused(
    add_state_tax: Callable[..., None], add_residence: Callable[..., Path],
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    """The federal tax already stands in for the Virgin Islands' (#491)."""
    add_state_tax("CA")
    write_aux("state_brackets__VI.csv", BRACKETS + "".join(
        f"2026-01-31,{status},0,0.00,5%\n" for status in ("Single", "MFJ", "MFS")))
    add_residence("2028-01", "VI")
    with pytest.raises(LoadError, match=r"state_brackets__VI.csv would tax the same income twice"):
        load_test_copy()


@pytest.mark.parametrize("state", ["FL", "GU", "MP", "VI"])
def test_no_tax_states_and_mirror_territories_read_the_none_tables(
    state: str, add_state_tax: Callable[..., None], add_residence: Callable[..., Path],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """Florida has no income tax and no tables: it reads the shipped 0%
    ``none`` ones, and the state tax pays nothing. So does a territory whose
    tax is a copy of the federal one (#491): the federal tax goes on as
    before."""
    add_state_tax("CA")
    add_residence("2028-01", state)
    run = _run(load_test_copy, runs_dir, state)
    tax, federal = _buckets(run, "state_tax"), _buckets(run, "federal_tax")
    assert tax[2027] > 0 and tax[2028] == 0
    assert federal[2028] > 0


def test_a_county_with_a_local_tax_of_its_own(
    add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
    add_residence: Callable[..., Path], write_aux: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """A move to a county with a local tax starts paying it the year of the
    move; the years before, in a county with none, pay none."""
    add_state_tax("CA")
    add_tax("local")
    write_aux("state_local_tax__CA-county.csv", BRACKETS + "".join(
        f"2020-01-31,{status},0,0.00,1%\n" for status in ("Single", "MFJ", "MFS")))
    add_residence("2028-06", "CA", county="CA-county")
    run = _run(load_test_copy, runs_dir, "county")
    local, income = _buckets(run, "local_tax"), _buckets(run, "local_taxable_income")
    assert local[2027] == 0
    assert local[2028] == pytest.approx(income[2028] / 100, abs=1)


def test_a_move_to_another_state_takes_its_standard_deduction(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    add_residence: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    """After a move, the new state's standard deduction (#248), so its
    brackets don't run with the old state's deduction."""
    add_state_tax("CA")
    _oregon(write_aux)
    write_aux("state_standard_deduction__OR.csv", "effective_date,filing_status,amount\n"
              + "".join(f"2026-01-31,{s},1000.00\n" for s in ("Single", "MFJ", "MFS")))
    add_residence("2028-03", "OR")
    assert not any("standard deduction" in w for w in load_test_copy().warnings)
    deduction = _buckets(_run(load_test_copy, runs_dir, "move"), "state_standard_deduction")
    assert deduction[2026] == 11_080_00  # California's, before the move
    assert deduction[2028] == 1_000_00


def test_a_move_to_a_state_without_a_standard_deduction_falls_back_to_federal(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    add_residence: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    add_state_tax("CA")
    _oregon(write_aux)
    add_residence("2028-03", "OR")
    warnings = load_test_copy().warnings
    assert any("federal standard deduction in Oregon (OR)" in w
               and "state_standard_deduction__OR.csv" in w for w in warnings)
    deduction = _buckets(_run(load_test_copy, runs_dir, "move"), "state_standard_deduction")
    assert deduction[2028] == 33_000_00  # the federal figure (indexed, #467), not California's


def test_a_residence_without_a_state_tax_says_nothing_of_the_deduction(
    write_aux: Callable[[str, str], Path], retire_alice_csv: Callable[[str], Path],
    add_residence: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    """With no state tax, nothing reads a state's tables, so a move to a
    state without its own standard deduction isn't worth a warning, and
    a state without tables isn't an error."""
    _oregon(write_aux)
    add_residence("2026-05", "CA")
    add_residence("2028-03", "OR")
    add_residence("2030-03", "NY")
    assert not any("standard deduction" in w for w in load_test_copy().warnings)


@pytest.mark.parametrize("taxed_in", ["CA", "OR"])
def test_a_move_takes_the_states_social_security_table(
    taxed_in: str,
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    add_residence: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    """A move to a state without a Social Security table (#484) taxes none
    of it from then on, and a move to one with one starts taxing it."""
    add_state_tax("CA")
    _oregon(write_aux, "1%")
    write_aux("state_standard_deduction__OR.csv", "effective_date,filing_status,amount\n"
              + "".join(f"2026-01-31,{s},1000.00\n" for s in ("Single", "MFJ", "MFS")))
    write_aux(f"state_social_security__{taxed_in}.csv",
              "effective_date,filing_status,exclusion_percent\n"
              + "".join(f"2026-01-31,{s},0%\n" for s in ("Single", "MFJ", "MFS")))
    add_residence("2026-07", "OR")
    taxable = _buckets(_run(load_test_copy, runs_dir, "move"), "state_ss_taxable")
    assert any(v > 0 for v in taxable.values()) == (taxed_in == "OR")


def test_a_state_fill_bracket_rate_must_be_one_of_each_states_it_converts_in(
    add_state_tax: Callable[..., None], add_residence: Callable[..., Path],
    write_aux: Callable[[str, str], Path], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """A conversion to the top of California's 9.3% bracket has nothing to
    fill after a move to a state without one, so the loader says so; ending
    the rule before the move's year is fine."""
    from analysis.sim.registries import SCHEDULED_CSV_HEADER

    add_state_tax("CA")
    _oregon(write_aux, "1%")
    add_residence("2031-03", "OR")
    path = retire_alice_csv("scheduled.csv")
    scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
    scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
    scheduled.loc[len(scheduled)] = {
        **dict.fromkeys(SCHEDULED_CSV_HEADER, ""), "name": "convert",
        "from_account": "Asset_TraditionalIRA_Alice", "from_lot": "default",
        "to_account": "Asset_Roth_Alice", "to_lot": "default",
        "cadence": "annual", "month_of_year": "12", "amount_rule_kind": "fill_bracket",
        "rate": "9.3%", "tax_character": "tax_deferred_out", "start": "2029-01",
        "meta_json": "{}", "params_json": json.dumps({"scope": "state"}),
    }
    scheduled.to_csv(path, index=False)
    with pytest.raises(LoadError, match=r"no state bracket in Oregon \(OR\) is taxed at 9.3%"):
        load_test_copy()
    scheduled.loc[len(scheduled) - 1, "end"] = "2030-12"
    scheduled.to_csv(path, index=False)
    load_test_copy()
    # Ending in the year of the move, before its December, is fine too.
    scheduled.loc[len(scheduled) - 1, "end"] = "2031-06"
    scheduled.to_csv(path, index=False)
    load_test_copy()


def test_the_states_a_rule_may_file_in() -> None:
    rows = [{"effective_date": "2026-05-31", "state": "CA"},
            {"effective_date": "2028-02-29", "state": "OR"},
            {"effective_date": "2031-12-31", "state": "NY"}]
    assert _residence_states(rows, range(2029, 2040)) == ["OR", "NY"]
    assert _residence_states(rows, [2026, 2027]) == ["CA"]
    # A December move files that year in the new state.
    assert _residence_states(rows, [2030, 2031]) == ["OR", "NY"]
    assert _residence_states(rows, [2030]) == ["OR"]
    # The first row covers the years before it.
    assert _residence_states(rows, [2020, 2025]) == ["CA"]


def test_a_rule_posts_in_the_years_its_month_falls_within_both_spans() -> None:
    rule = {"month_of_year": "12", "start": "2029-01-31", "end": "2031-06-30"}
    assert _firing_years(rule, "2026-05-31", "2040-12-31") == [2029, 2030]
    assert _firing_years({**rule, "end": ""}, "2026-05-31", "2032-11-30") == [2029, 2030, 2031]
    assert _firing_years({"month_of_year": "3"}, "2026-05-31", "2028-12-31") == [2027, 2028]
    assert _firing_years({"month_of_year": "Dec"}, "2026-05-31", "2028-12-31") == []


def _migration():
    import importlib.util
    import sys

    path = Path(__file__).parents[1] / "scripts" / "migrate_509.py"
    spec = importlib.util.spec_from_file_location("migrate_509", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


CALENDAR = "date,kind,series,description,params_json,vary_distribution\n"


def _swap(date: str, kind: str, source: str, description: str = "", series: str = "") -> str:
    """A calendar.csv row swapping in ``kind``'s ``source`` table."""
    params = json.dumps({"aux_kind": kind, "source_series": source}).replace('"', '""')
    return f'{date},swap_aux_table,{series},{description},"{params}",\n'


def _tax(params: dict[str, str]) -> str:
    """A taxes/<jurisdiction>.csv with these params, as before #509."""
    text = json.dumps({"filing_status": "MFJ", **params}).replace('"', '""')
    return f'name,kind,params_json\nx,state_tables,"{text}"\n'


def _scenario(name: str, parent: str = "") -> str:
    """A scenario.csv: a root starts 2026-05; a child inherits its start."""
    return f"name,start,parent_scenario\n{name},{'' if parent else '2026-05-31'},{parent}\n"


def test_the_migration_moves_series_and_swaps_to_residence(
    new_scenario_file: Callable[[str, str, str], Path], test_data_dir: Path,
) -> None:
    new_scenario_file("base", "scenario.csv", _scenario("base"))
    new_scenario_file("child", "scenario.csv", _scenario("child", "base"))
    new_scenario_file("base", "taxes/state.csv", _tax({"bracket_series": "CA"}))
    new_scenario_file("base", "taxes/local.csv", _tax({"bracket_series": "CA-county"}))
    new_scenario_file("child", "taxes/state.csv", _tax({"bracket_series": "NC"}))
    calendar = new_scenario_file("base", "calendar.csv", (
        CALENDAR
        + _swap("2027-01", "federal_brackets", "law2027")
        + _swap("2030-07", "state_brackets", "OR", "Move to Oregon (OR)")
        + _swap("2030-07", "state_standard_deduction", "OR")
        + _swap("2033-01", "state_brackets", "none", "Move to Florida (FL)")))
    base, child = calendar.parent, calendar.parent.parent / "child"
    migration = _migration()

    planned = migration.plan(test_data_dir)
    assert planned.problems == []
    assert not (base / "residence.csv").exists()  # a dry run writes nothing
    migration.apply(planned)

    assert (base / "residence.csv").read_text().splitlines() == [
        "effective_date,state,county,description",
        "2026-05,CA,CA-county,",
        "2030-07,OR,,Move to Oregon (OR)",
        "2033-01,FL,,Move to Florida (FL)",
    ]
    # The child's own state replaces its parent's first row, on its date.
    assert (child / "residence.csv").read_text().splitlines()[1:] == ["2026-05,NC,,"]
    text = calendar.read_text()
    assert "law2027" in text and "state_brackets" not in text
    assert "state_standard_deduction" not in text
    assert "bracket_series" not in (base / "taxes" / "state.csv").read_text()
    assert "bracket_series" not in (base / "taxes" / "local.csv").read_text()
    assert migration.plan(test_data_dir).problems == [
        f"{test_data_dir}: no bracket_series or state table swaps; already migrated?"]


def test_a_rows_left_out_county_is_blank_not_its_parents(
    add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
    write_aux: Callable[[str, str], Path], test_child_scenario: Path,
    load_test_scenario: Callable[[str], Path],
) -> None:
    """A child's row of the same month replaces its parent's whole, even
    from a file without a county column."""
    add_tax("local")
    write_aux("state_local_tax__CA-county.csv", BRACKETS + "".join(
        f"2020-01-31,{status},0,0.00,1%\n" for status in ("Single", "MFJ", "MFS")))
    add_state_tax("CA", county="CA-county")
    (test_child_scenario / "residence.csv").write_text("effective_date,state\n2026-05,NC\n")
    with closing(sqlite3.connect(load_test_scenario("child"))) as con:
        assert con.execute("SELECT state, county FROM residence").fetchall() == [("NC", None)]


def test_the_migration_reads_the_parents_move_of_the_same_month(
    new_scenario_file: Callable[[str, str, str], Path], test_data_dir: Path,
) -> None:
    """A child's county move in the month its parent moves to another state
    is in the state moved to."""
    new_scenario_file("base", "scenario.csv", _scenario("base"))
    new_scenario_file("child", "scenario.csv", _scenario("child", "base"))
    new_scenario_file("base", "taxes/state.csv", _tax({"bracket_series": "CA"}))
    new_scenario_file("base", "calendar.csv",
                      CALENDAR + _swap("2030-07", "state_brackets", "NC", series="CA"))
    child = new_scenario_file("child", "calendar.csv",
                              CALENDAR + _swap("2030-07", "state_local_tax", "NC-county")).parent
    migration = _migration()
    planned = migration.plan(test_data_dir)
    assert planned.problems == []
    migration.apply(planned)
    assert (child / "residence.csv").read_text().splitlines()[1:] == ["2030-07,NC,NC-county,"]


@pytest.mark.parametrize(("swap", "problem"), [
    (_swap("2030-07", "state_brackets", "none", "Retire to the beach"),
     "the description doesn't name the state"),
    (_swap("2030-07", "state_surcharge", "CA"), "swaps state_surcharge without a move"),
    (_swap("+5y", "state_brackets", "OR"), "isn't a month (a relative date?)"),
    (_swap("2030-07", "state_brackets", "OR") + _swap("2030-07", "state_standard_deduction", "special"),
     "swaps state_standard_deduction into a table other than OR's"),
])
def test_the_migration_reports_what_it_cannot_move(
    swap: str, problem: str, new_scenario_file: Callable[[str, str, str], Path],
    test_data_dir: Path,
) -> None:
    new_scenario_file("base", "scenario.csv", _scenario("base"))
    new_scenario_file("base", "taxes/state.csv", _tax({"bracket_series": "CA"}))
    new_scenario_file("base", "calendar.csv", CALENDAR + swap)
    problems = _migration().plan(test_data_dir).problems
    assert len(problems) == 1 and problem in problems[0]


def test_the_migration_checks_which_state_a_swap_replaced(
    new_scenario_file: Callable[[str, str, str], Path], test_data_dir: Path,
) -> None:
    """A swap replaced the table its series named, the state the household
    started in: every move after the first names it too. A child starting
    elsewhere never moved, so the migration can't make it."""
    new_scenario_file("base", "scenario.csv", _scenario("base"))
    new_scenario_file("base", "taxes/state.csv", _tax({"bracket_series": "CA"}))
    new_scenario_file("base", "calendar.csv", CALENDAR
                      + _swap("2030-07", "state_brackets", "OR", series="CA")
                      + _swap("2033-01", "state_brackets", "NC", series="CA"))
    migration = _migration()
    assert migration.plan(test_data_dir).problems == []

    new_scenario_file("child", "scenario.csv", _scenario("child", "base"))
    new_scenario_file("child", "taxes/state.csv", _tax({"bracket_series": "NY"}))
    problems = migration.plan(test_data_dir).problems
    assert len(problems) == 2
    assert all("replaces CA's tables, but scenario child starts in NY" in p for p in problems)
