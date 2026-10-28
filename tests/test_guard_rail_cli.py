"""Guard rails from the terminal (#305): ``sim report guard-rails``, the
``sim explain`` section, and ``sim guard-rail list|add|edit|delete``."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import cli as sim_cli
from analysis.sim import guard_rails as gr
from analysis.sim.connection import RunConnection
from analysis.sim.registries import GUARD_RAILS_CSV_HEADER


def _report(run_file: Path, data_dir: Path, *args: str) -> Result:
    return CliRunner().invoke(
        main, ["sim", "report", "guard-rails", "--runfile", str(run_file),
               "--data-dir", str(data_dir), *args],
        env={"COLUMNS": "250"},
    )


def _rows(output: str) -> dict[str, list[str]]:
    """A rich table's body rows, by their first cell."""
    rows: dict[str, list[str]] = {}
    for line in output.splitlines():
        cells = [cell.strip() for cell in line.strip("│ ").split("│")]
        if line.startswith("│") and cells:
            rows.setdefault(cells[0], cells[1:])
    return rows


# ---------- sim report guard-rails ----------


def test_report_shows_annual_and_cumulative_margins(retire_alice_run_file: Path, data_dir: Path) -> None:
    result = _report(retire_alice_run_file, data_dir)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("guard rail margins, CPI-constant dollars (retire_alice, run ")
    assert "four_percent  withdrawals above 4% of the portfolio at the rail's start" in result.output
    assert "gk_lower      withdrawals below 4% of the portfolio at each year's start, " \
           "from 2031-01  [positive]" in result.output
    header = next(line for line in lines if "four_percent cum" in line)
    assert [c.strip() for c in header.strip("┃ ").split("┃")][:3] == [
        "year", "four_percent", "four_percent cum"]
    rows = _rows(result.output)
    assert rows["2031"][0].startswith("-$")  # the bridge years overdraw the 4% rule
    assert rows["2040"][1].startswith("-$")  # still overdrawn cumulatively at the end
    # The summary: per rail and form, years past of years covered.
    summary = [line for line in lines if line.startswith("│ four_percent")]
    assert [c.strip() for c in summary[0].strip("│ ").split("│")][:3] == [
        "four_percent", "annual", "6 of 10"]
    assert [c.strip() for c in summary[1].strip("│ ").split("│")][:3] == [
        "four_percent", "cumulative", "10 of 10"]


def test_report_one_rail_shows_its_measure_threshold_and_carried(
    retire_alice_run_file: Path, data_dir: Path,
) -> None:
    result = _report(retire_alice_run_file, data_dir, "--rail", "budget", "--carried",
                     "--dollars", "nominal")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].startswith("guard rail budget, nominal dollars")
    header = next(line for line in result.output.splitlines() if "threshold" in line)
    assert [c.strip() for c in header.strip("┃ ").split("┃")] == [
        "year", "measure", "threshold", "margin", "cumulative", "carried"]


def test_report_names_the_rails_it_has_when_asked_for_another(
    retire_alice_run_file: Path, data_dir: Path,
) -> None:
    result = _report(retire_alice_run_file, data_dir, "--rail", "no_such_rail")

    assert result.exit_code != 0
    assert "no guard rail 'no_such_rail' in 'retire_alice'; it has budget, four_percent" in result.output


def test_past_a_negative_rail_is_red_past_a_positive_one_blue(
    retire_alice_run: RunConnection, scenarios_dir: Path,
) -> None:
    results = gr.evaluate_run(retire_alice_run, gr.run_rails(retire_alice_run, scenarios_dir))
    style = sim_cli._margin_style(results)
    assert style("four_percent cum", -1.0) == "red"
    assert style("gk_lower", -1.0) == "blue"
    assert style("four_percent", 1.0) is None


def test_report_says_so_when_the_scenario_has_no_rails(
    run_test_copy: Callable[[], Path], test_data_dir: Path, write_guard_rails: Callable[..., Path],
) -> None:
    write_guard_rails()
    result = _report(run_test_copy(), test_data_dir)

    assert result.exit_code == 0, result.output
    assert "no guard rails in 'retire_alice'" in result.output


# ---------- sim explain ----------


def test_explain_lists_each_stretch_past_a_rail(retire_alice_run_file: Path, data_dir: Path) -> None:
    result = CliRunner().invoke(main, ["sim", "explain", "--runfile", str(retire_alice_run_file),
                                       "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    start = lines.index("guard rails past, CPI-constant dollars:")
    past = {line.split()[0]: line for line in lines[start + 1:]}
    assert past["four_percent"].startswith(
        "  four_percent (negative): past 2031-2036 (worst 2031, $")
    assert "; cumulative past 2031-2040 (worst 2036, $" in past["four_percent"]
    assert past["gk_lower"].startswith("  gk_lower (positive): past 2038-2040 (worst 2038, $")
    assert past["gk_lower"].endswith(" under)")


def test_explain_without_a_data_repo_leaves_the_rails_out(retire_alice_run_file: Path) -> None:
    result = CliRunner().invoke(main, ["sim", "explain", "--runfile", str(retire_alice_run_file)],
                                env={"ANALYSIS_DATA_DIR": None})

    assert result.exit_code == 0, result.output
    assert "guard rails" not in result.output


# ---------- sim guard-rail list|add|edit|delete ----------


def _sim(data_dir: Path, scenario: str, *args: str, answers: Sequence[str] = ()) -> Result:
    return CliRunner().invoke(
        main,
        ["sim", "guard-rail", args[0], "--data-dir", str(data_dir), "--scenario", scenario, *args[1:]],
        input="".join(answer + "\n" for answer in answers), env={"COLUMNS": "250"},
    )


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=list(GUARD_RAILS_CSV_HEADER))
    return pd.read_csv(path, dtype=str, keep_default_na=False).reindex(
        columns=GUARD_RAILS_CSV_HEADER, fill_value="")


def _row(path: Path, name: str) -> dict[str, str]:
    frame = _read(path)
    rows = frame[frame["name"] == name]
    assert len(rows) == 1, f"{name!r}: {len(rows)} rows"
    return {str(k): str(v) for k, v in rows.iloc[0].to_dict().items()}


@pytest.fixture
def rails_csv(retire_alice_csv: Callable[[str], Path]) -> Path:
    return retire_alice_csv("guard_rails.csv")


def test_list_shows_each_rail_and_where_it_is_defined(
    test_child_scenario: Path, test_data_dir: Path,
) -> None:
    result = _sim(test_data_dir, "child", "list")

    assert result.exit_code == 0, result.output
    rows = _rows(result.output)
    assert set(rows) >= {"four_percent", "gk_upper", "gk_lower", "budget"}
    assert rows["budget"][:4] == [
        "spending above $85,000 a year in CPI-constant dollars", "negative", "2031-01", ""]
    assert rows["budget"][-1] == "retire_alice"


def test_add_with_options_writes_the_row(test_data_dir: Path, rails_csv: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "add", "floor", "--measure", "spending",
                  "--side", "below", "--basis", "cpi_constant", "--threshold", "60000",
                  "--sense", "positive", "--start", "2031-01")

    assert result.exit_code == 0, result.output
    assert "appended guard rail 'floor' (spending below $60,000 a year in CPI-constant dollars)" \
        in result.output
    row = _row(rails_csv, "floor")
    assert (row["measure"], row["side"], row["threshold"], row["basis"], row["sense"],
            row["start"]) == ("spending", "below", "60000.00", "cpi_constant", "positive", "2031-01")


def test_add_asks_only_for_what_the_options_leave_out(test_data_dir: Path, rails_csv: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "add", "five", "--measure", "withdrawals",
                  "--basis", "portfolio", answers=["sideways", "above", "5", "5%"])

    assert result.exit_code == 0, result.output
    assert "not one of: above, below" in result.output
    assert "needs its % sign" in result.output
    row = _row(rails_csv, "five")
    assert (row["side"], row["threshold"]) == ("above", "5%")


def test_add_with_no_options_asks_everything_then_writes(test_data_dir: Path, rails_csv: Path) -> None:
    # withdrawals: no select; a percent basis: the portfolio is asked.
    result = _sim(test_data_dir, "retire_alice", "add", "bengen", answers=[
        "withdrawals", "above", "initial_portfolio", "4%", "", "", "2031-01", "", "the 4% rule",
        "write",
    ])

    assert result.exit_code == 0, result.output
    assert "Equivalent command:" in result.output
    row = _row(rails_csv, "bengen")
    assert (row["measure"], row["select"], row["threshold"], row["basis"], row["start"],
            row["description"]) == ("withdrawals", "", "4%", "initial_portfolio", "2031-01",
                                    "the 4% rule")


def test_add_refuses_a_taken_name_and_a_threshold_its_basis_cant_take(
    test_data_dir: Path, rails_csv: Path,
) -> None:
    before = rails_csv.read_text()
    taken = _sim(test_data_dir, "retire_alice", "add", "budget", "--measure", "spending")
    assert taken.exit_code != 0
    assert "guard rail 'budget' already exists" in taken.output

    dollars = _sim(test_data_dir, "retire_alice", "add", "r", "--measure", "withdrawals",
                   "--side", "above", "--basis", "nominal", "--threshold", "4%")
    assert dollars.exit_code != 0
    assert "threshold '4%' is a percent: only basis portfolio or initial_portfolio takes one" \
        in dollars.output
    assert rails_csv.read_text() == before


def test_add_refuses_a_tag_nothing_carries(test_data_dir: Path, rails_csv: Path) -> None:
    before = rails_csv.read_text()
    result = _sim(test_data_dir, "retire_alice", "add", "core", "--measure", "withdrawals",
                  "--side", "above", "--basis", "portfolio", "--threshold", "4%",
                  "--portfolio", "-exclude:emergency")

    assert result.exit_code != 0
    assert "exclude:emergency" in result.output
    assert rails_csv.read_text() == before


def test_a_portfolio_may_leave_out_tagged_accounts(
    test_data_dir: Path, rails_csv: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    accounts.loc[accounts["name"] == "Asset_Savings", "tags"] = "exclude:emergency"
    accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)

    # The option's value starts with "-", and is still read as its value.
    result = _sim(test_data_dir, "retire_alice", "add", "core", "--measure", "withdrawals",
                  "--side", "above", "--basis", "portfolio", "--threshold", "4%",
                  "--portfolio", "-exclude:emergency")

    assert result.exit_code == 0, result.output
    assert _row(rails_csv, "core")["portfolio"] == "-exclude:emergency"


def test_edit_with_options_changes_those_fields(test_data_dir: Path, rails_csv: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "edit", "gk_upper", "--threshold", "5.5%",
                  "--description", "")

    assert result.exit_code == 0, result.output
    assert "updated guard rail 'gk_upper'" in result.output
    row = _row(rails_csv, "gk_upper")
    assert (row["threshold"], row["description"], row["basis"]) == ("5.5%", "", "portfolio")


def test_edit_blanks_a_field_that_no_longer_applies(test_data_dir: Path, rails_csv: Path,
                                                    retire_alice_csv: Callable[[str], Path]) -> None:
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    accounts.loc[accounts["name"] == "Expense_Living", "tags"] = "budget:core"
    accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)
    assert _sim(test_data_dir, "retire_alice", "edit", "budget", "--select", "budget:core").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "edit", "budget", "--measure", "withdrawals")

    assert result.exit_code == 0, result.output
    assert _row(rails_csv, "budget")["select"] == ""


def test_guided_edit_changes_a_field_then_writes(test_data_dir: Path, rails_csv: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "edit", "four_percent",
                  answers=["threshold", "3.5%", "write"])

    assert result.exit_code == 0, result.output
    assert _row(rails_csv, "four_percent")["threshold"] == "3.5%"


def test_editing_an_inherited_rail_writes_an_override(
    test_child_scenario: Path, test_data_dir: Path, rails_csv: Path,
) -> None:
    result = _sim(test_data_dir, "child", "edit", "budget", "--threshold", "90000")

    assert result.exit_code == 0, result.output
    assert "wrote an override of guard rail 'budget' (inherited from 'retire_alice')" in result.output
    assert _row(test_child_scenario / "guard_rails.csv", "budget")["threshold"] == "90000.00"
    assert _row(rails_csv, "budget")["threshold"] == "85000.00"


def test_delete_removes_an_own_rail(test_data_dir: Path, rails_csv: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "delete", "budget", "--yes")

    assert result.exit_code == 0, result.output
    assert "budget" not in set(_read(rails_csv)["name"])


def test_deleting_an_inherited_rail_ends_it_before_the_scenario_starts(
    test_child_scenario: Path, test_data_dir: Path,
) -> None:
    result = _sim(test_data_dir, "child", "delete", "budget", "--yes")

    assert result.exit_code == 0, result.output
    override = _row(test_child_scenario / "guard_rails.csv", "budget")
    assert override["end"] == "2026-04"  # the prototype starts in 2026-05
    rails = gr.scenario_rails(test_data_dir / "scenarios", "child")
    assert next(r for r in rails if r.name == "budget").end == "2026-04-30"

    restored = _sim(test_data_dir, "child", "delete", "budget", "--yes")
    assert restored.exit_code == 0, restored.output
    assert "it is inherited from 'retire_alice' again" in restored.output
    assert "budget" not in set(_read(test_child_scenario / "guard_rails.csv")["name"])
