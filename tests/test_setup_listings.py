"""Setup listings (#185): ``sim calendar list``, ``sim person list``, ``sim
ytd list``, ``sim itemization list|show`` and ``sim scenario show``, each the
scenario's merged rows with where they are defined."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import setup_listings
from analysis.sim.registries import CALENDAR_HEADER


@pytest.fixture
def sim(test_data_dir: Path) -> Callable[..., Result]:
    """Run a ``sim`` command against the private data copy, wide enough that
    rich doesn't wrap the tables."""
    def run(*args: str) -> Result:
        result = CliRunner().invoke(
            main, ["sim", *args, "--data-dir", str(test_data_dir)],
            env={"COLUMNS": "300"},
        )
        assert result.exit_code == 0, result.output
        return result
    return run


def _calendar_csv(**rows: dict[str, str]) -> str:
    frame = pd.DataFrame([{**dict.fromkeys(CALENDAR_HEADER, ""), **row} for row in rows.values()],
                         columns=list(CALENDAR_HEADER))
    return frame.to_csv(index=False)


# ---------- calendar ----------


def test_calendar_list_numbers_rows_as_edit_does_and_marks_overrides(
    derive_scenario: Callable[..., Path],
) -> None:
    # The child redescribes retire_alice's unnamed healthcare set_amount (the
    # row's key columns repeated, so it overrides) and adds a row of its own.
    child = derive_scenario("child", "retire_alice", files={"calendar.csv": _calendar_csv(
        override={"date": "2028-07-31", "kind": "set_amount", "scheduled_name": "healthcare",
                  "amount": "1200.00", "param_date": "2028-07-31", "description": "changed"},
        own={"date": "2032-01-31", "kind": "retire_scheduled", "scheduled_name": "rent",
             "tags": "estimate:rough"},
    )})

    listed = setup_listings.calendar_listing(child)
    by_target = {setup_listings.calendar_target(e.row): e for e in listed if e.row["date"]}

    # Own rows first, in file order, as `sim calendar edit` numbers them.
    assert by_target["healthcare"].number == 1
    assert by_target["healthcare"].defined_in == "overrides retire_alice"
    assert by_target["healthcare"].row["description"] == "changed"
    assert len(listed) == 26  # retire_alice's 25, plus the child's own
    assert by_target["rent"].number == 2
    assert by_target["rent"].defined_in == "this scenario"
    assert by_target["salary_alice"].defined_in == "retire_alice"
    factor = next(e for e in listed if e.row["name"] == "core_inflation")
    assert factor.defined_in == "base"
    # Date order, latent factors (no date) first.
    dates = [e.row["date"] for e in listed]
    assert dates == sorted(dates)


def test_calendar_list_filters_by_kind_or_command_name_and_tag(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("child", "retire_alice", files={"calendar.csv": _calendar_csv(
        own={"date": "2032-01-31", "kind": "retire_scheduled", "scheduled_name": "rent",
             "tags": "estimate:rough"},
    )})

    by_command = sim("calendar", "list", "--scenario", "child", "--kind", "set-rate").output
    by_kind = sim("calendar", "list", "--scenario", "child", "--kind", "set_rate").output
    assert by_command == by_kind
    assert "Asset_Checking" in by_kind and "salary_alice" not in by_kind

    tagged = sim("calendar", "list", "--scenario", "child", "--tag", "estimate:*").output
    assert "rent" in tagged and "salary_alice" not in tagged
    assert "no calendar rows in child match" in sim(
        "calendar", "list", "--scenario", "child", "--tag", "estimate:other",
    ).output


def test_calendar_list_sums_up_variation(derive_scenario: Callable[..., Path]) -> None:
    child = derive_scenario("child", "retire_alice", files={"calendar.csv": _calendar_csv(
        varying={"date": "2027-01-31", "kind": "set_rate", "series": "Asset_Checking",
                 "amount": "4.5%", "name": "checking_rate", "vary_distribution": "normal",
                 "vary_median": "4.5%", "vary_sd": "1%", "vary_correlate_with": "cash_factor",
                 "vary_correlation": "0.8"},
    )})

    row = next(e.row for e in setup_listings.calendar_listing(child) if e.row["name"] == "checking_rate")

    assert setup_listings.vary_summary(row) == "normal median=4.5% sd=1% ~ cash_factor 0.8"


# ---------- persons ----------


def test_person_list_shows_the_inherited_lifespan(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("parent", "retire_alice",
                    files={"lifespan.csv": "person,final_month\nbill,2055-06-30\n"})
    child = derive_scenario("child", "parent")

    persons = {p.row["id"]: p for p in setup_listings.person_listing(child.parent.parent, child)}

    assert persons["bill"].final_month == "2055-06-30"
    assert persons["bill"].lifespan_defined_in == "parent"
    assert persons["alice"].final_month == ""
    output = sim("person", "list", "--scenario", "child").output
    assert "Bill (fake)" in output and "2055-06-30" in output


# ---------- year to date ----------


def test_ytd_list_has_inherited_rows_and_scheduled_ytd(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path], test_scenarios_dir: Path,
) -> None:
    scheduled = pd.read_csv(test_scenarios_dir / "retire_alice" / "scheduled.csv",
                            dtype=str, keep_default_na=False)
    salary = scheduled[scheduled["name"] == "salary_alice"].copy()
    salary["ytd_amount"] = "32000.00"
    child = derive_scenario("child", "retire_alice", files={
        "scheduled.csv": salary.to_csv(index=False),
        "year_to_date.csv": (
            "name,from_account,from_lot,to_account,to_lot,amount,tax_character,year,description\n"
            "prior_federal_withholding,Asset_Checking,,Expense_Taxes_Federal,,9500.00,none,,more\n"
        ),
    })

    rows = {r.name: r for r in setup_listings.ytd_listing(child)}

    assert rows["prior_ordinary_wages"].defined_in == "retire_alice"
    assert rows["prior_federal_withholding"].amount == "9500.00"
    assert rows["prior_federal_withholding"].defined_in == "overrides retire_alice"
    assert rows["salary_alice"].source == "scheduled"
    assert rows["salary_alice"].amount == "32000.00"
    assert rows["salary_alice"].defined_in == "overrides retire_alice"
    output = sim("ytd", "list", "--scenario", "child").output
    assert "prior_ltcg_loss_carryforward" in output and "32000.00" in output


# ---------- itemizations ----------


def test_itemizations_nearest_file_wins_and_totals_follow_nested_refs(
    sim: Callable[..., Result], test_data_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    (test_data_dir / "itemizations" / "repairs.csv").write_text(
        "Item,Value\nroof,100.00\n", encoding="utf-8")
    (test_data_dir / "itemizations" / "fees.csv").write_text(
        "Item,Value\nclosing,50.00\n", encoding="utf-8")
    child = derive_scenario("child", "retire_alice", files={
        "itemizations/repairs.csv": "Item,Value\nroof,12000.00\nfees,@fees\n",
        # Only the money column is a reference; the loader reads @repairs in
        # a description as text.
        "account_lots.csv": (
            "account_name,lot,opening_cost_basis,description\nAsset_Home,,@repairs,@repairs\n"
        ),
        "chains/source/draw.csv": "account_name,lot,maximum\nAsset_Checking,,@fees\n",
    })

    items = {i.name: i for i in setup_listings.itemizations(test_data_dir, child)}

    assert items["repairs"].defined_in == "this scenario"
    assert items["repairs"].total_cents == 1_205_000
    assert items["fees"].defined_in == "shared"
    refs = setup_listings.itemization_references(test_data_dir, child)
    assert [(r.path.name, r.line, r.column) for r in refs["repairs"]] == [
        ("account_lots.csv", 2, "opening_cost_basis"),
    ]
    assert sorted(r.path.name for r in refs["fees"]) == ["draw.csv", "repairs.csv"]

    shown = sim("itemization", "show", "@repairs", "--scenario", "child").output
    assert "total: $12,050.00" in shown
    assert "scenarios/child/account_lots.csv line 2, opening_cost_basis" in shown
    listed = sim("itemization", "list", "--scenario", "child").output
    assert "$12,050.00" in listed and "$50.00" in listed


def test_itemization_show_refuses_an_unknown_name(test_data_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "itemization", "show", "nope", "--scenario", "retire_alice",
        "--data-dir", str(test_data_dir),
    ])

    assert result.exit_code != 0
    assert "no itemization 'nope'" in result.output


# ---------- scenario ----------


def test_scenario_show_marks_inherited_settings_and_finds_the_latest_run(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path], runs_dir: Path,
) -> None:
    derive_scenario("recipe", "retire_alice",
                    meta_json='{"ensemble_size": 80, "reserve": "250000.00"}')
    child = derive_scenario("child", "recipe:base", seed="7",
                            description="A child of the recipe.")
    for stamp in ("20260101T000000", "20260201T000000"):
        (runs_dir / f"{stamp}_child_abc123.sqlite3").write_bytes(b"")

    shown = setup_listings.scenario_show(child.parent.parent, child, runs_dir)

    assert (shown.parent, shown.mixins) == ("recipe", ["base"])
    assert shown.load_order == ["base", "retire_alice", "recipe", "child"]
    assert shown.reserve == ("$250,000.00", True)
    assert shown.ensemble_size is not None and shown.ensemble_size.inherited
    assert shown.seed is not None and (shown.seed.value, shown.seed.inherited) == (7, False)
    assert shown.latest_run is not None
    assert shown.latest_run[0].name == "20260201T000000_child_abc123.sqlite3"
    output = sim("scenario", "show", "child", "--runs-dir", str(runs_dir)).output
    assert "ensemble size: 80 (inherited)" in output
    assert "reserve: $250,000.00 (inherited)" in output
    assert "seed: 7\n" in output
    assert "A child of the recipe." in output


def test_scenario_show_inherits_the_description_and_skips_a_plain_scenarios_batch(
    derive_scenario: Callable[..., Path], runs_dir: Path,
) -> None:
    derive_scenario("parent", "retire_alice", description="The parent's words.")
    child = derive_scenario("child", "parent")
    # A batch folder left from when the child was an ensemble.
    batch = runs_dir / "child" / "20260101T000000"
    batch.mkdir(parents=True)
    (batch / "member_0000.sqlite3").write_bytes(b"")

    shown = setup_listings.scenario_show(child.parent.parent, child, runs_dir)

    assert shown.description == "The parent's words."
    assert shown.latest_batch is None


def test_scenario_show_takes_a_blank_readme_over_scenario_csv(
    derive_scenario: Callable[..., Path], runs_dir: Path,
) -> None:
    child = derive_scenario("child", "retire_alice", files={"README.md": "\n"},
                            description="Not shown: the README.md wins.")

    shown = setup_listings.scenario_show(child.parent.parent, child, runs_dir)

    assert shown.description == ""
