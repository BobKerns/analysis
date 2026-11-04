"""`sim series list|show|add|edit|delete|revert` (#471)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import names
from analysis.sim.loader import LoadError, load_scenario

_HEADER = "effective_date,annual_rate\n"


@pytest.fixture
def sim(test_data_dir: Path) -> Callable[..., Result]:
    """Run ``sim`` on the private data copy's retire_alice (or ``scenario``);
    ``input`` answers its prompts."""
    def run(*args: str, input: str | None = None, scenario: str = "retire_alice") -> Result:  # noqa: A002
        return CliRunner().invoke(
            main, ["sim", *args, "--data-dir", str(test_data_dir), "--scenario", scenario],
            input=input, terminal_width=200,
        )
    return run


@pytest.fixture
def series_dir(test_scenarios_dir: Path) -> Path:
    return test_scenarios_dir / "retire_alice" / "series"


def _loads(data_dir: Path, intent_dir: Path, scenario: str) -> list[str]:
    """The scenario's load issues; none when it loads."""
    try:
        load_scenario(data_dir, scenario, intent_dir=intent_dir, allow_dirty_data=True)
    except LoadError as exc:
        return list(exc.issues)
    return []


# ---------- list and show ----------

def test_list_says_where_each_series_comes_from(sim: Callable[..., Result]) -> None:
    result = sim("series", "list")
    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if " brokerage_growth " in line)
    assert "rate" in line and "6%" in line and "retire_alice" in line
    food = next(line for line in result.output.splitlines() if " food " in line)
    assert "inflation" in food and "t_inflation_series" in food
    assert "aux/inflation__cpi.csv" in result.output
    rates = sim("series", "list", "--kind", "rate")
    assert " food " not in rates.output and " brokerage_growth " in rates.output


def test_show_lists_the_rows_and_what_names_the_series(sim: Callable[..., Result]) -> None:
    result = sim("series", "show", "brokerage_growth")
    assert result.exit_code == 0, result.output
    assert "series/rate__brokerage_growth.csv (retire_alice)" in result.output
    assert "2026-01" in result.output and "6%" in result.output
    assert "retire_alice: accounts.csv:" in result.output


# ---------- add ----------

def test_add_writes_a_series_from_options(sim: Callable[..., Result], series_dir: Path,
                                          test_data_dir: Path, intent_dir: Path) -> None:
    result = sim("series", "add", "bonds_long", "--kind", "rate", "--rate", "4%", "--set", "2030-01=3.5")
    assert result.exit_code == 0, result.output
    # --rate starts at the scenario's start; a rate's % may be left off.
    assert (series_dir / "rate__bonds_long.csv").read_text() == (
        _HEADER + "2026-05-31,4%\n2030-01-31,3.5%\n")
    assert _loads(test_data_dir, intent_dir, "retire_alice") == []


def test_add_asks_for_what_is_left_out(sim: Callable[..., Result], series_dir: Path) -> None:
    result = sim("series", "add", input="tuition\nrate\n2027-09\n5%\n2030-09\n6%\n\n")
    assert result.exit_code == 0, result.output
    assert (series_dir / "rate__tuition.csv").read_text() == (
        _HEADER + "2027-09-30,5%\n2030-09-30,6%\n")


@pytest.mark.parametrize(("args", "message"), [
    (("brokerage_growth", "--kind", "rate", "--rate", "5%"), "`sim series edit brokerage_growth`"),
    (("two words", "--kind", "rate", "--rate", "5%"), "one word, without spaces"),
    (("new_one", "--kind", "rate", "--rate", "five"), "invalid percent value"),
    (("new_one", "--kind", "rate", "--set", "2030-01"), "--set takes YYYY-MM=RATE"),
])
def test_add_refuses(sim: Callable[..., Result], args: tuple[str, ...], message: str) -> None:
    result = sim("series", "add", *args)
    assert result.exit_code != 0 and message in result.output, result.output


# ---------- edit ----------

def test_edit_with_options_changes_the_scenarios_own_file(sim: Callable[..., Result], series_dir: Path) -> None:
    result = sim("series", "edit", "brokerage_growth", "--set", "2030-01=5%", "--set", "2026-01=6.5%")
    assert result.exit_code == 0, result.output
    assert "updated rate series 'brokerage_growth'" in result.output
    path = series_dir / "rate__brokerage_growth.csv"
    assert path.read_text() == _HEADER + "2026-01-31,6.5%\n2030-01-31,5%\n"
    assert sim("series", "edit", "brokerage_growth", "--remove", "2030-01").exit_code == 0
    assert path.read_text() == _HEADER + "2026-01-31,6.5%\n"


def test_editing_an_inherited_series_writes_an_override(
    sim: Callable[..., Result], series_dir: Path, test_data_dir: Path,
) -> None:
    result = sim("series", "edit", "food", "--set", "2030-01=3%")
    assert result.exit_code == 0, result.output
    assert "wrote an override of inflation series 'food' (from t_inflation_series)" in result.output
    assert (series_dir / "inflation__food.csv").read_text() == _HEADER + "2025-01-31,2.8%\n2030-01-31,3%\n"
    listed = next(line for line in sim("series", "list").output.splitlines() if " food " in line)
    assert listed.rstrip(" │").endswith("t_inflation_series")  # Overrides


def test_editing_cpi_overrides_the_shared_file_in_the_scenarios_aux(
    sim: Callable[..., Result], test_scenarios_dir: Path,
) -> None:
    result = sim("series", "edit", "cpi", "--set", "2027-01=3%")
    assert result.exit_code == 0, result.output
    assert (test_scenarios_dir / "retire_alice" / "aux" / "inflation__cpi.csv").is_file()


def test_guided_edit_changes_adds_and_removes_rows(sim: Callable[..., Result], series_dir: Path) -> None:
    answers = [
        "1", "2026-01", "6.5%",      # change row 1's rate
        "+", "2031-06", "5",         # add a row
        "+", "2040-01", "4%",        # and another
        "-2",                        # remove the 2031 one
        "", ]                        # write
    result = sim("series", "edit", "brokerage_growth", input="\n".join(answers) + "\n")
    assert result.exit_code == 0, result.output
    assert (series_dir / "rate__brokerage_growth.csv").read_text() == (
        _HEADER + "2026-01-31,6.5%\n2040-01-31,4%\n")


def test_guided_edit_quit_writes_nothing(sim: Callable[..., Result], series_dir: Path) -> None:
    path = series_dir / "rate__brokerage_growth.csv"
    before = path.read_text()
    result = sim("series", "edit", "brokerage_growth", input="-1\nq\n")
    assert result.exit_code == 0 and "nothing was written" in result.output, result.output
    assert path.read_text() == before


def test_edit_refuses_to_leave_no_rows(sim: Callable[..., Result]) -> None:
    result = sim("series", "edit", "brokerage_growth", "--remove", "2026-01")
    assert result.exit_code != 0 and "`sim series delete brokerage_growth`" in result.output, result.output


# ---------- delete and revert ----------

def test_delete_removes_a_series_only_this_scenario_has(sim: Callable[..., Result], series_dir: Path) -> None:
    assert sim("series", "add", "spare", "--kind", "rate", "--rate", "1%").exit_code == 0
    result = sim("series", "delete", "spare", "--yes")
    assert result.exit_code == 0, result.output
    assert not (series_dir / "rate__spare.csv").exists()
    assert "revert" not in result.output


def test_delete_empties_an_inherited_series_and_suggests_revert(
    sim: Callable[..., Result], series_dir: Path, test_data_dir: Path,
) -> None:
    result = sim("series", "delete", "food", "--yes")
    assert result.exit_code == 0, result.output
    assert (series_dir / "inflation__food.csv").read_text() == _HEADER
    assert "`sim series revert food` inherits t_inflation_series's again" in result.output
    # An empty series is no series: it isn't offered, listed or shown.
    assert "food" not in names.series_names(test_data_dir, "retire_alice")
    assert " food " not in sim("series", "list").output
    assert " empty " in next(line for line in sim("series", "list", "--all").output.splitlines()
                             if " food " in line)
    shown = sim("series", "show", "food")
    assert shown.exit_code != 0 and "is empty in retire_alice" in shown.output, shown.output


def test_delete_empties_the_scenarios_own_override_too(sim: Callable[..., Result], series_dir: Path) -> None:
    assert sim("series", "edit", "food", "--set", "2030-01=3%").exit_code == 0
    result = sim("series", "delete", "food", "--yes")
    assert result.exit_code == 0, result.output
    assert (series_dir / "inflation__food.csv").read_text() == _HEADER


def test_an_emptied_series_must_be_supplied_by_a_scenario_reading_it(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path],
    test_data_dir: Path, intent_dir: Path,
) -> None:
    """Emptying hides the inherited series: a child that reads it has to
    supply its own, so the parent's is never used by mistake."""
    assert sim("series", "delete", "food", "--yes").exit_code == 0
    scheduled = (
        "name,amount_rule_kind,from_account,to_account,amount,series,start,cadence,tax_character\n"
        "groceries,fixed_amount,Asset_Checking,Expense_Living,100.00,food,2026-05,monthly,none\n"
    )
    kid = derive_scenario("kid", "retire_alice", files={"scheduled.csv": scheduled})
    issues = _loads(test_data_dir, intent_dir, "kid")
    assert any("references series 'food'" in issue for issue in issues), issues
    (kid / "series").mkdir()
    (kid / "series" / "inflation__food.csv").write_text(_HEADER + "2026-01-31,3%\n")
    assert _loads(test_data_dir, intent_dir, "kid") == []


def test_delete_refuses_a_series_the_scenario_reads(sim: Callable[..., Result], series_dir: Path) -> None:
    result = sim("series", "delete", "brokerage_growth", "--yes")
    assert result.exit_code != 0, result.output
    assert "references series 'brokerage_growth'" in result.output
    assert (series_dir / "rate__brokerage_growth.csv").read_text() == _HEADER + "2026-01-31,6%\n"


def test_delete_refuses_cpi(sim: Callable[..., Result]) -> None:
    result = sim("series", "delete", "cpi", "--yes")
    assert result.exit_code != 0 and "cpi can't be deleted" in result.output, result.output


def test_delete_asks_first(sim: Callable[..., Result], series_dir: Path) -> None:
    result = sim("series", "delete", "food", input="n\n")
    assert result.exit_code == 0 and "aborted" in result.output, result.output
    assert not (series_dir / "inflation__food.csv").exists()


def test_add_gives_an_emptied_series_rates_again(sim: Callable[..., Result], series_dir: Path) -> None:
    assert sim("series", "delete", "food", "--yes").exit_code == 0
    result = sim("series", "add", "food", "--kind", "inflation", "--rate", "2%")
    assert result.exit_code == 0, result.output
    assert (series_dir / "inflation__food.csv").read_text() == _HEADER + "2026-05-31,2%\n"


@pytest.mark.parametrize("change", [("delete", "food", "--yes"), ("edit", "food", "--set", "2030-01=3%")])
def test_revert_inherits_the_parents_series_again(
    sim: Callable[..., Result], series_dir: Path, change: tuple[str, ...],
) -> None:
    assert sim("series", *change).exit_code == 0
    result = sim("series", "revert", "food")
    assert result.exit_code == 0, result.output
    assert "inherits series/inflation__food.csv in t_inflation_series again" in result.output
    assert not (series_dir / "inflation__food.csv").exists()


def test_revert_refuses_a_series_only_this_scenario_has(sim: Callable[..., Result]) -> None:
    result = sim("series", "revert", "brokerage_growth")
    assert result.exit_code != 0 and "nothing to revert to" in result.output, result.output


def test_revert_offers_only_the_scenarios_own_files_over_inherited_ones(sim: Callable[..., Result]) -> None:
    assert sim("series", "delete", "food", "--yes").exit_code == 0
    result = sim("series", "revert", input="food\n")
    assert result.exit_code == 0, result.output
    offered = next(line for line in result.output.splitlines() if line.startswith("series: "))
    assert offered == "series: food"


# ---------- a name with both kinds ----------

def test_a_name_with_both_kinds_needs_kind(sim: Callable[..., Result], series_dir: Path) -> None:
    (series_dir / "rate__food.csv").write_text(_HEADER + "2026-01-31,1%\n")
    result = sim("series", "show", "food")
    assert result.exit_code != 0 and "say which with --kind" in result.output, result.output
    shown = sim("series", "show", "food", "--kind", "rate")
    assert shown.exit_code == 0 and "rate series 'food'" in shown.output, shown.output


# ---------- review follow-ups ----------

def test_an_inflation_series_must_have_a_rate_at_the_start(sim: Callable[..., Result], series_dir: Path) -> None:
    # The run grows every inflation index from the first month, read or not.
    result = sim("series", "add", "late", "--kind", "inflation", "--set", "2030-01=3%")
    assert result.exit_code != 0, result.output
    assert "its first rate is from 2030-01, after the scenario's start (2026-05)" in result.output
    assert not (series_dir / "inflation__late.csv").exists()


def test_the_rate_at_the_start_follows_dates_not_file_order(sim: Callable[..., Result], series_dir: Path) -> None:
    (series_dir / "rate__brokerage_growth.csv").write_text(_HEADER + "2026-01-31,6%\n2025-01-31,3%\n")
    line = next(line for line in sim("series", "list").output.splitlines() if " brokerage_growth " in line)
    assert " 6% " in line, line


def test_delete_empties_a_series_in_both_folders(sim: Callable[..., Result], test_scenarios_dir: Path,
                                                 series_dir: Path) -> None:
    """The loader reads a name's series/ and aux/ files both, so emptying
    only one would leave rates the run still uses."""
    aux = test_scenarios_dir / "retire_alice" / "aux"
    aux.mkdir()
    (aux / "inflation__food.csv").write_text(_HEADER + "2025-01-31,9%\n")
    result = sim("series", "delete", "food", "--yes")
    assert result.exit_code == 0, result.output
    assert (series_dir / "inflation__food.csv").read_text() == _HEADER
    assert not (aux / "inflation__food.csv").exists()  # only this scenario had it there
    reverted = sim("series", "revert", "food")
    assert reverted.exit_code == 0, reverted.output
    assert not (series_dir / "inflation__food.csv").exists()


def test_the_rate_at_the_start_counts_a_same_named_aux_file(
    sim: Callable[..., Result], test_scenarios_dir: Path, series_dir: Path,
) -> None:
    # The loader reads both files; the later dated row is the one in force.
    aux = test_scenarios_dir / "retire_alice" / "aux"
    aux.mkdir()
    (aux / "rate__brokerage_growth.csv").write_text(_HEADER + "2026-03-31,2%\n")
    line = next(line for line in sim("series", "list").output.splitlines() if " brokerage_growth " in line)
    assert " 2% " in line, line


def test_revert_finds_an_override_in_the_scenarios_aux(
    sim: Callable[..., Result], test_scenarios_dir: Path,
) -> None:
    """series/ is inherited, but the child's aux/ has its own file of the name."""
    aux = test_scenarios_dir / "retire_alice" / "aux"
    aux.mkdir()
    (aux / "inflation__food.csv").write_text(_HEADER + "2025-01-31,9%\n")
    # Inherited from a parent's aux/ as well, so there is something to revert to.
    base_aux = test_scenarios_dir / "base" / "aux"
    base_aux.mkdir(exist_ok=True)
    (base_aux / "inflation__food.csv").write_text(_HEADER + "2025-01-31,1%\n")
    result = sim("series", "revert", input="food\n")
    assert result.exit_code == 0, result.output
    assert not (aux / "inflation__food.csv").exists()
    assert (base_aux / "inflation__food.csv").exists()


def test_delete_refuses_when_the_file_changed_while_it_asked(
    sim: Callable[..., Result], series_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim import cli as sim_cli

    path = series_dir / "rate__spare.csv"
    path.write_text(_HEADER + "2026-01-31,1%\n")

    def confirm_after_a_change(*_args: object, **_kwargs: object) -> bool:
        path.write_text(_HEADER + "2026-01-31,2%\n")
        return True

    monkeypatch.setattr(sim_cli.click, "confirm", confirm_after_a_change)
    result = sim("series", "delete", "spare")
    assert result.exit_code != 0 and "changed on disk" in result.output, result.output
    assert path.read_text() == _HEADER + "2026-01-31,2%\n"
