"""Renaming a scenario, or a name scenarios share, everywhere it's used (issue #177)."""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import rename
from analysis.sim.lineage import read_parents
from analysis.sim.loader import growth_rule_name, load_scenario


def _sim(data_dir: Path, *args: str, scenario: str | None = "retire_alice"):
    options = ["--data-dir", str(data_dir)] + (["--scenario", scenario] if scenario else [])
    return CliRunner().invoke(main, ["sim", *args, *options], terminal_width=200)


def _column(path: Path, column: str) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [row[column] for row in csv.DictReader(fh)]


@pytest.fixture
def loads(test_data_dir: Path, intent_dir: Path) -> Callable[[str], None]:
    def _loads(name: str) -> None:
        load_scenario(test_data_dir, name, intent_dir=intent_dir, allow_dirty_data=True)
    return _loads


# ---------- scope ----------

def test_scope_is_the_definition_and_everything_built_on_it(test_scenarios_dir: Path, derive_scenario) -> None:
    derive_scenario("kid", "retire_alice")
    derive_scenario("other", "base")
    defines = rename.account_defined("Asset_Checking")
    # From the child, the rename starts at the parent that defines it.
    assert rename.scope(test_scenarios_dir, "kid", defines) == ["kid", "retire_alice"]
    assert rename.scope(test_scenarios_dir, "other", defines) == []


def test_scope_takes_in_a_mix_in_defining_the_same_name(test_scenarios_dir: Path, derive_scenario) -> None:
    accounts = "name,kind,subkind,owner,tax_treatment\nAsset_Checking,asset,cash,joint,taxable\n"
    derive_scenario("mix", "base", files={"accounts.csv": accounts})
    derive_scenario("kid", "retire_alice:mix")
    derive_scenario("mix_only", "mix")
    derive_scenario("separate", "base", files={"accounts.csv": accounts})
    assert rename.scope(test_scenarios_dir, "retire_alice", rename.account_defined("Asset_Checking")) == [
        "kid", "mix", "mix_only", "retire_alice"]


# ---------- scenarios ----------

def test_relink_markdown_follows_the_folder() -> None:
    scenarios = Path("/data/scenarios")
    old, new = scenarios / "retire_alice", scenarios / "retire_early"
    text = ("See [retire_alice](../retire_alice/README.md#plan) and [chains][c], "
            "`retire_alice`, scenarios/retire_alice/accounts.csv, and [base](../base/README.md).\n\n"
            "[c]: ../retire_alice/chains/\n")
    assert rename.relink_markdown(text, scenarios / "base" / "README.md", old, new) == (
        "See [retire_early](../retire_early/README.md#plan) and [chains][c], "
        "`retire_early`, scenarios/retire_early/accounts.csv, and [base](../base/README.md).\n\n"
        "[c]: ../retire_early/chains/\n")
    # From inside the folder, a link to itself through its parent moves too;
    # the scenario's other links stay as they are.
    inside = "[me](../retire_alice/scheduled.csv) [up](../../aux/README.md) [here](chains/README.md)"
    assert rename.relink_markdown(inside, old / "README.md", old, new) == (
        "[me](../retire_early/scheduled.csv) [up](../../aux/README.md) [here](chains/README.md)")
    # A longer name that only starts with the old one is another scenario.
    assert rename.relink_markdown("`retire_alice_2` scenarios/retire_alice_2", scenarios / "x.md", old, new) == (
        "`retire_alice_2` scenarios/retire_alice_2")


def test_scenario_rename_moves_it_and_repoints_everything(
    test_data_dir: Path, test_scenarios_dir: Path, derive_scenario, loads: Callable[[str], None],
) -> None:
    derive_scenario("kid", "retire_alice", description="Derived from retire_alice")
    derive_scenario("mixed", "base:retire_alice", description="Hand-written, naming retire_alice")
    (test_data_dir / ".current_scenario").write_text("retire_alice\n")
    (test_data_dir / "README.md").write_text("Start with [Alice](scenarios/retire_alice/README.md).\n")

    result = _sim(test_data_dir, "scenario", "rename", "retire_alice", "retire_early", scenario=None)

    assert result.exit_code == 0, result.output
    assert "renamed scenario retire_alice to retire_early" in result.output
    assert not (test_scenarios_dir / "retire_alice").exists()
    moved = test_scenarios_dir / "retire_early"
    assert _column(moved / "scenario.csv", "name") == ["retire_early"]
    assert read_parents(test_scenarios_dir / "kid") == ["retire_early"]
    assert read_parents(test_scenarios_dir / "mixed") == ["base", "retire_early"]
    assert _column(test_scenarios_dir / "kid" / "scenario.csv", "description") == ["Derived from retire_early"]
    assert _column(test_scenarios_dir / "mixed" / "scenario.csv", "description") == [
        "Hand-written, naming retire_alice"]
    assert (test_data_dir / ".current_scenario").read_text() == "retire_early\n"
    assert "Current scenario is now retire_early (was retire_alice)" in result.stderr
    assert (test_data_dir / "README.md").read_text() == (
        "Start with [Alice](scenarios/retire_early/README.md).\n")
    assert "`retire_early` derives from this scenario" in (test_scenarios_dir / "base" / "README.md").read_text()
    for name in ("retire_early", "kid", "mixed"):
        loads(name)


def test_ensemble_rename_is_scenario_rename(test_data_dir: Path, test_scenarios_dir: Path,
                                            derive_scenario) -> None:
    derive_scenario("runs", "retire_alice", meta_json='{"ensemble_size":4}')
    result = _sim(test_data_dir, "ensemble", "rename", "runs", "many_runs", scenario=None)
    assert result.exit_code == 0, result.output
    assert (test_scenarios_dir / "many_runs" / "scenario.csv").is_file()
    assert not (test_scenarios_dir / "runs").exists()


@pytest.mark.parametrize(("new", "message"), [
    ("base", "scenario already exists"),
    ("Retire Early", "invalid scenario name"),
])
def test_scenario_rename_refuses(test_data_dir: Path, test_scenarios_dir: Path, new: str, message: str) -> None:
    result = _sim(test_data_dir, "scenario", "rename", "retire_alice", new, scenario=None)
    assert result.exit_code != 0 and message in result.output
    assert (test_scenarios_dir / "retire_alice" / "scenario.csv").is_file()


def test_rename_scenario_takes_only_a_folder_name(test_data_dir: Path) -> None:
    with pytest.raises(FileNotFoundError, match="scenario not found"):
        rename.rename_scenario(test_data_dir, "../scenarios/retire_alice", "somebody")


@pytest.mark.parametrize("old", ["nobody", "../scenarios/retire_alice", "retire_alice/series"])
def test_scenario_rename_takes_only_a_scenario_folder_name(test_data_dir: Path, test_scenarios_dir: Path,
                                                           old: str) -> None:
    result = _sim(test_data_dir, "scenario", "rename", old, "somebody", scenario=None)
    assert result.exit_code != 0 and "scenario not found" in result.output
    assert not (test_scenarios_dir / "somebody").exists()


# ---------- names shared across scenarios ----------

def test_account_rename_from_a_child_reaches_its_parent(
    test_data_dir: Path, test_scenarios_dir: Path, derive_scenario, loads: Callable[[str], None],
) -> None:
    derive_scenario("kid", "retire_alice", files={
        "account_lots.csv": "account_name,opening_balance\nAsset_Savings,30000.00\n"})
    result = _sim(test_data_dir, "account", "rename", "Asset_Savings", "Asset_Reserve", scenario="kid")
    assert result.exit_code == 0, result.output
    assert "retire_alice: accounts.csv" in result.output and "kid: account_lots.csv" in result.output
    assert "Asset_Reserve" in _column(test_scenarios_dir / "retire_alice" / "accounts.csv", "name")
    assert _column(test_scenarios_dir / "kid" / "account_lots.csv", "account_name") == ["Asset_Reserve"]
    loads("kid")


def test_account_rename_refuses_a_name_a_child_uses(test_data_dir: Path, test_scenarios_dir: Path,
                                                    derive_scenario) -> None:
    derive_scenario("kid", "retire_alice", files={
        "accounts.csv": "name,kind,subkind,owner,tax_treatment\nAsset_Reserve,asset,cash,joint,taxable\n"})
    result = _sim(test_data_dir, "account", "rename", "Asset_Savings", "Asset_Reserve")
    assert result.exit_code != 0
    assert "an account 'Asset_Reserve' already applies in kid" in result.output
    assert "Asset_Savings" in _column(test_scenarios_dir / "retire_alice" / "accounts.csv", "name")


@pytest.mark.parametrize("command", [("account", "rename", "Asset_Savings"),
                                     ("chain", "rename", "asset_checking_liquidity")])
def test_a_rename_onto_a_built_in_account_is_refused(test_data_dir: Path, command: tuple[str, ...]) -> None:
    result = _sim(test_data_dir, *command, "interest_income")
    assert result.exit_code != 0 and "'interest_income' is a built-in account's name" in result.output


def test_account_rename_follows_its_generated_growth_rule(
    test_data_dir: Path, test_scenarios_dir: Path, append_calendar_row, loads: Callable[[str], None],
) -> None:
    rule = growth_rule_name("Asset_Checking", "default")
    append_calendar_row(date="2030-01-31", kind="retire_scheduled", scheduled_name=rule)
    result = _sim(test_data_dir, "account", "rename", "Asset_Checking", "Asset_Spending")
    assert result.exit_code == 0, result.output
    scheduled = _column(test_scenarios_dir / "retire_alice" / "calendar.csv", "scheduled_name")
    assert rule not in scheduled and growth_rule_name("Asset_Spending", "default") in scheduled
    loads("retire_alice")


def test_account_rename_leaves_an_authored_rule_with_a_growth_rule_name(
    test_data_dir: Path, test_scenarios_dir: Path, retire_alice_csv: Callable[[str], Path],
    append_calendar_row, loads: Callable[[str], None],
) -> None:
    # An authored rule that takes the generated name pushes the growth rule to a
    # wider one; the calendar row names the authored rule, so it must not move.
    rule = growth_rule_name("Asset_Checking", "default")
    scheduled = retire_alice_csv("scheduled.csv")
    scheduled.write_text(scheduled.read_text() + f"{rule},Income_Salary_Alice,,Asset_Checking,,"
                         "monthly,,fixed_amount,,,cpi,,,10.00,2026-05-31,,ordinary,,2026-05-31,,"
                         "200,,,authored,{},,\n")
    append_calendar_row(date="2030-01-31", kind="retire_scheduled", scheduled_name=rule)
    result = _sim(test_data_dir, "account", "rename", "Asset_Checking", "Asset_Spending")
    assert result.exit_code == 0, result.output
    assert rule in _column(test_scenarios_dir / "retire_alice" / "calendar.csv", "scheduled_name")
    assert rule in _column(test_scenarios_dir / "retire_alice" / "scheduled.csv", "name")
    loads("retire_alice")


def test_account_rename_follows_an_rmd_subject_account(
    test_data_dir: Path, test_scenarios_dir: Path, retire_alice_csv: Callable[[str], Path],
    loads: Callable[[str], None],
) -> None:
    scheduled = retire_alice_csv("scheduled.csv")
    scheduled.write_text(scheduled.read_text().replace(
        "Alice RMD from IRA,{},,",
        'Alice RMD from IRA,{},"{""subject_account"": ""Asset_TraditionalIRA_Alice""}",'))
    result = _sim(test_data_dir, "account", "rename", "Asset_TraditionalIRA_Alice", "Asset_IRA_Alice")
    assert result.exit_code == 0, result.output
    params = _column(test_scenarios_dir / "retire_alice" / "scheduled.csv", "params_json")
    assert '{"subject_account": "Asset_IRA_Alice"}' in params
    loads("retire_alice")


def test_account_edit_strips_a_new_name(test_data_dir: Path, test_scenarios_dir: Path,
                                        loads: Callable[[str], None]) -> None:
    result = _sim(test_data_dir, "account", "edit", "Asset_Checking", "--new-name", " Asset_Spending ")
    assert result.exit_code == 0, result.output
    assert "Asset_Spending" in _column(test_scenarios_dir / "retire_alice" / "accounts.csv", "name")
    loads("retire_alice")


def test_scheduled_rename_follows_calendar_rows(test_data_dir: Path, test_scenarios_dir: Path,
                                                loads: Callable[[str], None]) -> None:
    result = _sim(test_data_dir, "scheduled", "rename", "salary_alice", "wages_alice")
    assert result.exit_code == 0, result.output
    folder = test_scenarios_dir / "retire_alice"
    assert "wages_alice" in _column(folder / "scheduled.csv", "name")
    assert "wages_alice" in _column(folder / "calendar.csv", "scheduled_name")
    assert "salary_alice" not in _column(folder / "calendar.csv", "scheduled_name")
    loads("retire_alice")


def test_calendar_rename_follows_correlation_parents(test_data_dir: Path, local_templates: Path,
                                                     loads: Callable[[str], None]) -> None:
    result = _sim(test_data_dir, "calendar", "rename", "core_inflation", "inflation_core")
    assert result.exit_code == 0, result.output
    assert "t_inflation_factors: calendar.csv" in result.output
    calendar = local_templates / "t_inflation_factors" / "calendar.csv"
    assert "inflation_core" in _column(calendar, "name")
    assert "core_inflation" not in _column(calendar, "vary_correlate_with")
    assert "inflation_core" in _column(calendar, "vary_correlate_with")
    loads("retire_alice")


def test_series_rename_moves_its_file_and_references(test_data_dir: Path, local_templates: Path,
                                                     loads: Callable[[str], None]) -> None:
    result = _sim(test_data_dir, "series", "rename", "food", "groceries")
    assert result.exit_code == 0, result.output
    assert "t_inflation_series: series/inflation__food.csv -> series/inflation__groceries.csv" in result.output
    assert not (local_templates / "t_inflation_series" / "series" / "inflation__food.csv").exists()
    assert (local_templates / "t_inflation_series" / "series" / "inflation__groceries.csv").is_file()
    loads("retire_alice")


@pytest.mark.parametrize(("what", "old", "new"), [
    ("series", "food", "groceries"),
    ("calendar", "core_inflation", "inflation_core"),
])
def test_a_rename_of_what_a_template_defines_is_refused(
    test_data_dir: Path, what: str, old: str, new: str,
) -> None:
    """The code repository's templates aren't the data repo's to change."""
    result = _sim(test_data_dir, what, "rename", old, new)
    assert result.exit_code != 0
    assert "is defined in the template scenario" in result.output, result.output


def test_series_rename_leaves_a_same_named_account_alone(test_data_dir: Path, test_scenarios_dir: Path,
                                                         loads: Callable[[str], None]) -> None:
    result = _sim(test_data_dir, "series", "rename", "Asset_Checking", "checking_yield")
    assert result.exit_code == 0, result.output
    folder = test_scenarios_dir / "retire_alice"
    assert "Asset_Checking" in _column(folder / "accounts.csv", "name")
    assert "checking_yield" in _column(folder / "accounts.csv", "series")
    assert "checking_yield" in _column(folder / "calendar.csv", "series")
    loads("retire_alice")


@pytest.mark.parametrize(("old", "new", "message"), [
    ("cpi", "prices", "shared aux/inflation__cpi.csv"),
    ("stock", "equities", "is an asset class"),
    ("brokerage_growth", "Asset_Checking", "a series 'Asset_Checking' already applies in"),
    ("food", "two words", "one word, without spaces"),
])
def test_series_rename_refuses(test_data_dir: Path, old: str, new: str, message: str) -> None:
    result = _sim(test_data_dir, "series", "rename", old, new)
    assert result.exit_code != 0 and message in result.output, result.output


@pytest.mark.parametrize(("name", "message"), [
    ("VTI", "'VTI' is a symbol"),
    ("VTI.stock_us", "<symbol>.<asset class> series"),
])
def test_series_rename_refuses_a_series_read_by_name(test_data_dir: Path, write_series, name: str,
                                                      message: str) -> None:
    (test_data_dir / "securities.csv").write_text("symbol,name\nVTI,Total market fund\n")
    write_series(name, "2026-01-31,7%")
    for old, new in ((name, "renamed"), ("food", name)):
        result = _sim(test_data_dir, "series", "rename", old, new)
        assert result.exit_code != 0 and message in result.output, result.output


def test_series_rename_leaves_other_aux_kinds_alone(local_templates: Path) -> None:
    # A symbol's prices or a state's brackets end in __<name> too, but aren't a series.
    folder = local_templates / "t_inflation_series"
    (folder / "aux").mkdir(exist_ok=True)
    prices = folder / "aux" / "security_price__food.csv"
    prices.write_text("date,price\n")
    changed = rename.rename_series(folder, "food", "groceries")
    assert "series/inflation__food.csv -> series/inflation__groceries.csv" in changed
    assert prices.exists()


def test_lot_rename_reaches_every_scenario(test_data_dir: Path, test_scenarios_dir: Path, derive_scenario,
                                           loads: Callable[[str], None]) -> None:
    derive_scenario("kid", "retire_alice")
    result = _sim(test_data_dir, "account", "lot", "rename", "Asset_Brokerage_Joint", "default", "core",
                  scenario="kid")
    assert result.exit_code == 0, result.output
    assert "renamed lot of Asset_Brokerage_Joint default to core" in result.output
    values = test_scenarios_dir / "retire_alice" / "account_lots.csv"
    lots = dict(zip(_column(values, "account_name"), _column(values, "lot"), strict=True))
    assert lots["Asset_Brokerage_Joint"] == "core"
    loads("kid")


def test_lot_rename_of_an_unknown_lot(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "account", "lot", "rename", "Asset_Brokerage_Joint", "nope", "core")
    assert result.exit_code != 0 and "'Asset_Brokerage_Joint' has no lot 'nope'" in result.output
