"""#282: the template scenarios in ``templates/scenarios/`` (``t_`` names),
read from the code repository by any data repo without a copy of its own
(ADR-003 §4.5a), and what ``t_inflation_current`` does with today's rates."""

from __future__ import annotations

import math
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import names
from analysis.sim.dates import place_month
from analysis.sim.ensemble import process_path, resolve_member
from analysis.sim.lineage import TEMPLATE_SCENARIOS_DIR, load_order, scenario_dir
from analysis.sim.loader import list_scenario_names, load_scenario

TEMPLATES = [
    "t_empty", "t_inflation_factors", "t_inflation_series", "t_inflation_ensemble", "t_inflation_current",
    "t_market_factors", "t_market_series", "t_market_ensemble", "t_market_current",
]


def _sim(data_dir: Path, *args: str) -> Result:
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)], terminal_width=200)


def test_the_templates_are_the_ones_the_docs_describe() -> None:
    found = sorted(p.name for p in TEMPLATE_SCENARIOS_DIR.iterdir() if p.name.startswith("t_"))
    assert found == sorted(TEMPLATES)


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_loads_in_a_data_repo_without_a_copy(
    template_data_dir: Path, tmp_path: Path, name: str,
) -> None:
    assert not (template_data_dir / "scenarios" / name).exists()
    load_scenario(template_data_dir, name, intent_dir=tmp_path / "intent", compute_stamps=False)


def test_the_market_templates_build_on_the_inflation_factors(template_data_dir: Path) -> None:
    """One correlation tree: interest rates hang under core inflation."""
    assert load_order(template_data_dir / "scenarios", "t_market_current") == [
        "t_empty", "t_inflation_factors", "t_market_factors", "t_market_series",
        "t_market_ensemble", "t_market_current",
    ]


def test_only_a_template_name_falls_back_to_the_code_repository(test_scenarios_dir: Path) -> None:
    assert scenario_dir(test_scenarios_dir, "t_market_series") == TEMPLATE_SCENARIOS_DIR / "t_market_series"
    assert scenario_dir(test_scenarios_dir, "t_nothing") == test_scenarios_dir / "t_nothing"
    # templates/scenarios/base is the template data repo's own scenario, not a template.
    assert scenario_dir(test_scenarios_dir, "missing") == test_scenarios_dir / "missing"


def test_a_data_repo_folder_replaces_its_template_whole(
    local_templates: Path, test_data_dir: Path, tmp_path: Path,
) -> None:
    (local_templates / "t_market_series" / "series" / "rate__stock_us.csv").write_text(
        "effective_date,annual_rate\n2025-01-31,9%\n")
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=tmp_path / "intent",
                           compute_stamps=False, allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    try:
        payload = con.execute(
            "SELECT payload_json FROM aux_table WHERE kind = 'rate' AND series = 'stock_us'"
        ).fetchone()
    finally:
        con.close()
    assert payload[0] == '{"annual_rate_micros":90000}'


def test_an_edit_to_a_template_is_refused_and_writes_nothing(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "calendar", "add", "set-rate", "2028-01", "food", "5",
                  "--aux-kind", "inflation", "--scenario", "t_inflation_series")
    assert result.exit_code != 0
    assert "t_inflation_series is a template scenario" in result.output
    assert not (test_data_dir / "scenarios" / "t_inflation_series").exists()


def test_templates_complete_but_are_not_listed_or_run_with_the_rest(test_data_dir: Path) -> None:
    assert set(TEMPLATES) <= set(names.scenario_names(test_data_dir))
    assert not any(n.startswith("t_") for n in list_scenario_names(test_data_dir))
    listed = _sim(test_data_dir, "scenario", "list")
    assert "t_inflation" not in listed.output


def _x(rate_micros: int, level_micros: int) -> float:
    return 12 * math.log((1 + rate_micros / 12e6) / (1 + level_micros / 12e6))


def test_member_zero_holds_todays_rate_and_then_returns_to_the_level(
    template_data_dir: Path, tmp_path: Path,
) -> None:
    """CPI-U is 3.4% from the series file until the process row at
    normalization_start (#281), which starts there and decays to the 2.5%
    level at 0.4 a year; medical care, left out of the hold, runs at its
    level from 2027. Member 0's marker is its triangle's median, in October
    2029, after the mode at the fiscal year's start: the range runs longer
    after it."""
    recipe = load_scenario(template_data_dir, "t_inflation_current", intent_dir=tmp_path / "intent",
                           compute_stamps=False).intent_path
    member = resolve_member(recipe, tmp_path / "member_0000.sqlite3", seed=42, member_index=0,
                            member_count=80, recipe_tree_id="deadbeef")
    con = sqlite3.connect(member)
    try:
        def path(series: str) -> list[tuple[str, int]]:
            return [(d, int(a)) for d, a in con.execute(
                "SELECT date, amount FROM calendar_event WHERE kind = 'set_rate' "
                "AND series = ? ORDER BY date", (series,))]
        cpi, medical = path("cpi"), path("medical")
    finally:
        con.close()
    assert cpi[0] == ("2029-10-31", 34_000)
    assert [a for _, a in cpi] == process_path(25_000, 0.4, 14_000, [0.0] * len(cpi), start_micros=34_000)
    assert _x(cpi[1][1], 25_000) == pytest.approx(0.4 * _x(34_000, 25_000), abs=1e-5)
    assert medical[0] == ("2027-01-31", 35_000)
    assert {a for _, a in medical} == {35_000}


@pytest.mark.parametrize("member_index", [1, 2, 3])
def test_every_held_series_returns_at_the_drawn_normalization_start(
    template_data_dir: Path, tmp_path: Path, member_index: int,
) -> None:
    """#281: one marker moves every held series together, within the fiscal
    calendar's narrow range (Bob: 3 months before October 2029 to 6 after)."""
    recipe = load_scenario(template_data_dir, "t_inflation_current", intent_dir=tmp_path / "intent",
                           compute_stamps=False).intent_path
    member = resolve_member(recipe, tmp_path / f"member_{member_index:04d}.sqlite3", seed=42,
                            member_index=member_index, member_count=80, recipe_tree_id="deadbeef")
    con = sqlite3.connect(member)
    try:
        drawn = con.execute("SELECT drawn_value FROM ensemble_draw "
                            "WHERE calendar_event_name = 'normalization_start'").fetchone()[0]
        held = ("cpi_path", "cpi_w_path", "cpi_e_path", "chained_cpi_path", "housing_path",
                "education_path", "services_path")
        starts = dict(con.execute(
            f"SELECT name, date FROM calendar_event WHERE name IN ({','.join('?' * len(held))})",
            held))
    finally:
        con.close()
    assert "2029-06-30" <= drawn <= "2030-03-31"
    assert len(starts) == 7
    assert set(starts.values()) == {place_month(date.fromisoformat(drawn), 0).isoformat()}


def _content(intent_path: Path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(intent_path)
    try:
        return {table: sorted(con.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
                for table in ("account", "scheduled_transaction", "calendar_event", "aux_table")}
    finally:
        con.close()


def test_t_empty_adds_nothing_to_a_lineage(
    test_data_dir: Path, tmp_path: Path, derive_scenario, without_prototype_defaults: None,
) -> None:
    """Bob: t_empty may be the root, but whether it is in a lineage or not
    changes nothing a scenario loads, even mixed in after a scenario that
    doesn't build on the templates: its dates and seed are fallbacks."""
    derive_scenario("plain", "retire_alice")
    derive_scenario("with_empty", "retire_alice:t_empty")
    loaded = [load_scenario(test_data_dir, name, intent_dir=tmp_path / name, compute_stamps=False,
                            allow_dirty_data=True).intent_path for name in ("plain", "with_empty")]
    assert _content(loaded[0]) == _content(loaded[1])
    assert _span(loaded[0]) == _span(loaded[1]) == ("2026-05-31", "2040-12-31", 42)


def _span(intent_path: Path) -> tuple[str, str, int]:
    con = sqlite3.connect(intent_path)
    try:
        return con.execute('SELECT start, "end", seed FROM scenario').fetchone()
    finally:
        con.close()


def test_a_mixed_in_template_keeps_the_scenarios_span_and_seed(
    test_data_dir: Path, tmp_path: Path, derive_scenario, without_prototype_defaults: None,
) -> None:
    """Bob: an ensemble template mixed in after a scenario mustn't override
    its seed (nor, from t_empty, its dates)."""
    folder = test_data_dir / "scenarios" / "retire_alice" / "scenario.csv"
    folder.write_text(folder.read_text().replace(",42,", ",7,"))
    derive_scenario("mixed", "retire_alice:t_market_ensemble")
    intent = load_scenario(test_data_dir, "mixed", intent_dir=tmp_path / "intent",
                           compute_stamps=False, allow_dirty_data=True).intent_path
    assert _span(intent) == ("2026-05-31", "2040-12-31", 7)


def test_list_all_includes_the_templates(test_data_dir: Path) -> None:
    plain = _sim(test_data_dir, "scenario", "list").output.split()
    every = _sim(test_data_dir, "scenario", "list", "--all").output.split()
    assert every == sorted([*plain, *TEMPLATES])
    ensembles = _sim(test_data_dir, "ensemble", "list", "-a").output.split()
    assert {"t_inflation_ensemble", "t_market_current"} <= set(ensembles)
    table = _sim(test_data_dir, "scenario", "list", "-a", "-v")
    assert table.exit_code == 0 and "t_market_series" in table.output, table.output


def test_the_ensemble_templates_set_no_seed_of_their_own() -> None:
    """Bob: an ensemble template as a mix-in after a scenario must not
    override that scenario's seed; t_empty's 42 is only a fallback."""
    import csv
    seeds = {name: next(csv.DictReader((TEMPLATE_SCENARIOS_DIR / name / "scenario.csv").open()))["seed"]
             for name in TEMPLATES}
    assert {name for name, seed in seeds.items() if seed} == {"t_empty"}


def test_a_scenario_or_ensemble_can_be_created_from_a_template(test_data_dir: Path) -> None:
    """What the read-only refusal tells you to do must work: building on a
    template writes only the new scenario's folder."""
    made = _sim(test_data_dir, "scenario", "create", "kid", "t_inflation_series")
    assert made.exit_code == 0, made.output
    ensemble = _sim(test_data_dir, "ensemble", "create", "ens", "retire_alice", "t_market_current")
    assert ensemble.exit_code == 0, ensemble.output
    assert {"kid", "ens"} <= set(list_scenario_names(test_data_dir))
    assert not any((test_data_dir / "scenarios").glob("t_*"))
