"""``sim tax state`` and ``sim tax move`` (#483): choosing the state a
household's state tax reads, without writing JSON, and moving; and
``sim tax list|add|edit|delete`` over the taxes/ files (#507)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import states
from analysis.sim.lineage import TEMPLATE_AUX_DIR
from analysis.sim.loader import LoadResult

_README = Path(__file__).resolve().parents[1] / "README.md"


def _tax(data_dir: Path, *args: str, scenario: str = "retire_alice",
         answers: Sequence[str] = ()) -> Result:
    return CliRunner().invoke(
        main, ["sim", "tax", *args, "--data-dir", str(data_dir), "--scenario", scenario],
        input="".join(a + "\n" for a in answers),
    )


def _params(scenario_dir: Path) -> dict[str, object]:
    """The params of the scenario folder's own state tax (taxes/state.csv)."""
    frame = pd.read_csv(scenario_dir / "taxes" / "state.csv", dtype=str, keep_default_na=False)
    assert len(frame) == 1
    return json.loads(frame.iloc[0]["params_json"] or "{}")


# ---------- the state list ----------


def test_the_readme_tables_list_the_same_states_and_statuses() -> None:
    text = _README.read_text()
    section = text[text.index("## State and territory income tax"):]
    section = section[:section.index("\n## ", 1)]
    rows = re.findall(r"^\| ([^|]+?) \| ([A-Z][a-z]+(?: [a-z]+)?) \|", section, re.MULTILINE)
    listed = [(name, status) for name, status in rows if name not in ("State", "Territory", "Status")]
    assert listed == [(s.name, s.status) for s in states.STATES]


def test_a_shipped_state_is_one_whose_tables_ship() -> None:
    shipped = {p.name.removeprefix("state_brackets__").removesuffix(".csv")
               for p in TEMPLATE_AUX_DIR.glob("state_brackets__*.csv")} - {states.NO_TAX_SERIES}
    assert shipped == {s.code for s in states.STATES if s.status == states.SHIPPED}


def test_a_state_is_found_by_name_or_code_in_any_case() -> None:
    assert states.find("north carolina") == states.find("nc") == states.BY_CODE["NC"]
    assert states.find("Carolina") is None


# ---------- sim tax state ----------


def test_naming_a_state_fixes_a_state_tax_that_names_none(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax(bracket_series=None)

    result = _tax(test_data_dir, "state", "CA", "--yes")

    assert result.exit_code == 0, result.output
    assert "names no state yet" in result.output
    assert _params(retire_alice_csv(""))["bracket_series"] == "CA"
    load_test_copy()


def test_without_a_state_it_lists_the_choices_and_asks(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "state", answers=["Atlantis", "north carolina", "y"])

    assert result.exit_code == 0, result.output
    assert "lives in California (CA)" in result.output
    assert "With tax tables: California (CA), District of Columbia (DC)," in result.output
    assert "No income tax: Alaska (AK), Florida (FL)," in result.output
    assert "Washington (WA)" in result.output.split("No income tax:")[1]
    assert "a state's name or two-letter code" in result.output
    assert _params(retire_alice_csv(""))["bracket_series"] == "NC"


def test_the_state_is_set_where_the_state_tax_is_defined(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], test_child_scenario: Path,
) -> None:
    add_state_tax(bracket_series=None)
    parent = retire_alice_csv("")

    result = _tax(test_data_dir, "state", "KS", "--yes", scenario="child")

    assert result.exit_code == 0, result.output
    assert "writing it in scenario retire_alice" in result.output
    assert _params(parent)["bracket_series"] == "KS"
    assert not (test_child_scenario / "taxes" / "state.csv").exists()

    result = _tax(test_data_dir, "state", "NE", "--here", "--yes", scenario="child")

    assert result.exit_code == 0, result.output
    assert "writing an override in scenario child" in result.output
    assert _params(parent)["bracket_series"] == "KS"
    assert _params(test_child_scenario)["bracket_series"] == "NE"


def test_a_state_without_income_tax_keeps_the_state_tax_paying_nothing(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "state", "Florida", "--yes")

    assert result.exit_code == 0, result.output
    assert _params(retire_alice_csv(""))["bracket_series"] == states.NO_TAX_SERIES
    load_test_copy()


def test_a_state_without_income_tax_needs_no_state_tax(test_data_dir: Path) -> None:
    result = _tax(test_data_dir, "state", "TX")

    assert result.exit_code == 0, result.output
    assert "has no income tax, and this scenario has no state tax" in result.output


def test_a_state_without_tables_says_which_to_write(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "state", "Montana", "--yes")

    assert result.exit_code != 0
    assert "aux/state_brackets__MT.csv and aux/state_standard_deduction__MT.csv" in result.output
    assert _params(retire_alice_csv(""))["bracket_series"] == "CA"


def test_a_scenario_without_a_state_tax_gets_one_like_its_federal_tax(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult], add_tax: Callable[..., Path],
) -> None:
    add_tax(params_json={"nol_limit_percent": 80})

    result = _tax(test_data_dir, "state", "DC", "--yes")

    assert result.exit_code == 0, result.output
    assert "and expense account Expense_Taxes_State" in result.output
    [state] = pd.read_csv(retire_alice_csv("taxes/state.csv"), dtype=str,
                          keep_default_na=False).to_dict("records")
    assert (state["kind"], state["source_chain"], state["destination"], state["start"]) == (
        "state_tables", "Asset_Checking", "Expense_Taxes_State", "2027-04")
    assert json.loads(state["params_json"]) == {"nol_limit_percent": 80, "bracket_series": "DC"}
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    assert "taxes_state" in accounts["subkind"].tolist()
    load_test_copy()


def test_a_scenario_without_income_tax_is_told_to_add_the_federal_one(test_data_dir: Path) -> None:
    result = _tax(test_data_dir, "state", "CA", "--yes")

    assert result.exit_code != 0
    assert "add the federal one first" in result.output


# ---------- sim tax move ----------


def test_a_move_swaps_in_the_new_states_tables(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "move", "2031-07", "North Carolina", "--yes")

    assert result.exit_code == 0, result.output
    calendar = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    move = calendar[calendar["kind"] == "swap_aux_table"].iloc[0]
    assert (move["date"], move["series"]) == ("2031-07", "CA")
    assert json.loads(move["params_json"]) == {"aux_kind": "state_brackets", "source_series": "NC"}
    load_test_copy()

    result = _tax(test_data_dir, "state")
    assert "then moves on 2031-07 to North Carolina (NC)" in result.output


def test_a_move_to_a_state_without_income_tax_reads_the_zero_table(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "move", answers=["2030-01", "WY", "y"])

    assert result.exit_code == 0, result.output
    calendar = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    move = calendar[calendar["kind"] == "swap_aux_table"].iloc[0]
    assert json.loads(move["params_json"])["source_series"] == states.NO_TAX_SERIES
    load_test_copy()


@pytest.mark.parametrize("series", [None, "absent"])
def test_a_move_needs_the_starting_state_first(
    series: str | None, test_data_dir: Path, add_state_tax: Callable[..., None],
) -> None:
    if series is None:
        add_state_tax(bracket_series=None)

    result = _tax(test_data_dir, "move", "2031-07", "NC", "--yes")

    assert result.exit_code != 0
    assert "choose the state you live in first (sim tax state)" in result.output


def test_washington_has_no_income_tax_to_model(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "move", "2031-07", "WA", "--yes")

    assert result.exit_code == 0, result.output
    assert "its capital gains tax isn't modeled" in result.output
    calendar = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    move = calendar[calendar["kind"] == "swap_aux_table"].iloc[0]
    assert json.loads(move["params_json"])["source_series"] == states.NO_TAX_SERIES


# ---------- sim tax list|add|edit|delete (#507) ----------


def test_add_writes_the_federal_tax_file(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    result = _tax(test_data_dir, "add", "federal", "--source-chain", "Asset_Checking",
                  "--destination", "Expense_Taxes_Federal", "--start", "2027-04")

    assert result.exit_code == 0, result.output
    [row] = pd.read_csv(retire_alice_csv("taxes/federal.csv"), dtype=str,
                        keep_default_na=False).to_dict("records")
    assert (row["kind"], row["source_chain"], row["destination"], row["start"]) == (
        "federal", "Asset_Checking", "Expense_Taxes_Federal", "2027-04")
    load_test_copy()

    again = _tax(test_data_dir, "add", "federal", "--destination", "Expense_Taxes_Federal")
    assert again.exit_code != 0
    assert "already has a federal tax" in again.output


def test_add_asks_for_what_is_left_out(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    # Nothing to default the source from; Enter takes the rest: the
    # taxes_federal account, blanks, the suggested description; then write.
    result = _tax(test_data_dir, "add",
                  answers=["federal", "Asset_Checking", "", "", "", "", "", "write"])

    assert result.exit_code == 0, result.output
    [row] = pd.read_csv(retire_alice_csv("taxes/federal.csv"), dtype=str,
                        keep_default_na=False).to_dict("records")
    assert (row["source_chain"], row["destination"]) == ("Asset_Checking", "Expense_Taxes_Federal")
    assert row["description"] == "federal income tax for the year before"


def test_list_edit_and_delete(
    test_data_dir: Path, add_tax: Callable[..., Path], retire_alice_csv: Callable[[str], Path],
    test_child_scenario: Path,
) -> None:
    add_tax()

    listed = CliRunner().invoke(main, ["sim", "tax", "list", "child", "--data-dir", str(test_data_dir)])
    assert listed.exit_code == 0, listed.output
    assert "federal  federal: paid from Asset_Checking into Expense_Taxes_Federal" in listed.output
    assert "[from retire_alice]" in listed.output

    edited = _tax(test_data_dir, "edit", "federal", "--params-json", '{"nol_limit_percent": 50}',
                  scenario="child")
    assert edited.exit_code == 0, edited.output
    assert "overriding retire_alice's" in edited.output
    assert (test_child_scenario / "taxes" / "federal.csv").exists()

    deleted = _tax(test_data_dir, "delete", "federal", "--yes")
    assert deleted.exit_code == 0, deleted.output
    assert not retire_alice_csv("taxes/federal.csv").exists()
    # The child's override still stands.
    still = _tax(test_data_dir, "delete", "federal", "--yes", scenario="child")
    assert still.exit_code == 0, still.output


def test_delete_refuses_an_inherited_tax(
    test_data_dir: Path, add_tax: Callable[..., Path], test_child_scenario: Path,
) -> None:
    add_tax()

    result = _tax(test_data_dir, "delete", "federal", "--yes", scenario="child")

    assert result.exit_code != 0
    assert "inherited from retire_alice: delete it there" in result.output


def test_a_guided_edit_shows_the_fields_and_changes_the_one_named(
    test_data_dir: Path, add_tax: Callable[..., Path], retire_alice_csv: Callable[[str], Path],
) -> None:
    add_tax()

    result = _tax(test_data_dir, "edit", "federal", answers=["end", "2035-04", "write"])

    assert result.exit_code == 0, result.output
    assert "source-chain" in result.output and "write this row" in result.output
    [row] = pd.read_csv(retire_alice_csv("taxes/federal.csv"), dtype=str,
                        keep_default_na=False).to_dict("records")
    assert (row["end"], row["source_chain"]) == ("2035-04", "Asset_Checking")
