"""``sim tax state`` and ``sim tax move`` (#483, #509): choosing where the
household lives, which picks its state tax's tables, without writing a
file by hand, and moving; and ``sim tax list|add|edit|delete`` over the
taxes/ files (#507)."""

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


def _residence(scenario_dir: Path) -> list[tuple[str, str, str]]:
    """The scenario folder's own residence.csv rows: (from, state, county)."""
    path = scenario_dir / "residence.csv"
    if not path.exists():
        return []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [(r["effective_date"], r["state"], r["county"]) for r in frame.to_dict("records")]


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


def test_naming_a_state_fixes_a_state_tax_with_no_residence(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    """The first row is dated the scenario's start, in the scenario where
    the state tax is defined."""
    add_state_tax(state=None)

    result = _tax(test_data_dir, "state", "CA", "--yes")

    assert result.exit_code == 0, result.output
    assert "doesn't say where the household lives" in result.output
    assert _residence(retire_alice_csv("")) == [("2026-05", "CA", "")]
    load_test_copy()


def test_without_a_state_it_lists_the_choices_and_asks(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "state", answers=["Atlantis", "north carolina", "y"])

    assert result.exit_code == 0, result.output
    assert "lives in California (CA) from 2026-05" in result.output
    assert "With tax tables: California (CA), District of Columbia (DC)," in result.output
    assert "No income tax: Alaska (AK), Florida (FL)," in result.output
    # Washington's tables ship (#489), but until 2028 it taxes only gains.
    assert "Washington (WA)" in result.output.split("No income tax:")[0]
    assert "Washington (WA)" in result.output.split("No income tax:")[1]
    assert ("The federal tax alone: Guam (GU), Northern Mariana Islands (MP), "
            "U.S. Virgin Islands (VI)") in result.output
    assert "a state's name or two-letter code" in result.output
    assert _residence(retire_alice_csv("")) == [("2026-05", "NC", "")]


def test_the_state_is_set_where_the_residence_is_defined(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], test_child_scenario: Path,
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    parent = retire_alice_csv("")

    result = _tax(test_data_dir, "state", "KS", "--yes", scenario="child")

    assert result.exit_code == 0, result.output
    assert "writing residence.csv in scenario retire_alice" in result.output
    assert _residence(parent) == [("2026-05", "KS", "")]
    assert _residence(test_child_scenario) == []

    result = _tax(test_data_dir, "state", "NE", "--here", "--yes", scenario="child")

    assert result.exit_code == 0, result.output
    assert "writing residence.csv in scenario child" in result.output
    assert _residence(parent) == [("2026-05", "KS", "")]
    assert _residence(test_child_scenario) == [("2026-05", "NE", "")]


def test_the_first_row_is_dated_by_the_scenario_it_is_written_in(
    test_data_dir: Path, add_tax: Callable[..., Path],
    retire_alice_csv: Callable[[str], Path], test_child_scenario: Path,
) -> None:
    """Written where the federal tax is, the parent, it starts with the
    parent, not with a child that starts later."""
    add_tax()
    path = test_child_scenario / "scenario.csv"
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.loc[0, "start"] = "2027-01-31"
    frame.to_csv(path, index=False)

    result = _tax(test_data_dir, "state", "CA", "--yes", scenario="child")

    assert result.exit_code == 0, result.output
    assert _residence(retire_alice_csv("")) == [("2026-05", "CA", "")]


def test_a_state_with_tables_of_its_own_is_taxed_by_them(
    test_data_dir: Path, add_tax: Callable[..., Path], write_aux: Callable[[str, str], Path],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    """Washington has no income tax, but tables for it in the data repo (its
    gains tax, say) are what its state tax reads, as in the engine."""
    add_tax()
    write_aux("state_brackets__WA.csv", "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
              + "".join(f"2026-01-31,{s},0,0.00,1%\n" for s in ("Single", "MFJ", "MFS")))

    result = _tax(test_data_dir, "state", "WA", "--yes")

    assert result.exit_code == 0, result.output
    assert "capital gains tax isn't modeled" not in result.output
    assert retire_alice_csv("taxes/state.csv").exists()
    assert _residence(retire_alice_csv("")) == [("2026-05", "WA", "")]
    load_test_copy()


def test_a_new_state_tax_goes_where_it_sees_the_residence(
    test_data_dir: Path, add_tax: Callable[..., Path], add_residence_to: Callable[..., Path],
    retire_alice_csv: Callable[[str], Path], test_child_scenario: Path,
) -> None:
    """The federal tax is the parent's and the residence the child's: the
    state tax goes in the child, beside the residence it reads."""
    add_tax()
    add_residence_to(test_child_scenario, "2026-05", "FL")

    result = _tax(test_data_dir, "state", "CA", "--yes", scenario="child")

    assert result.exit_code == 0, result.output
    assert _residence(test_child_scenario) == [("2026-05", "CA", "")]
    assert (test_child_scenario / "taxes" / "state.csv").exists()
    assert not retire_alice_csv("taxes/state.csv").exists()


def test_a_state_without_income_tax_keeps_the_state_tax_paying_nothing(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "state", "Florida", "--yes")

    assert result.exit_code == 0, result.output
    assert "The state tax stays, paying nothing" in result.output
    assert _residence(retire_alice_csv("")) == [("2026-05", "FL", "")]
    load_test_copy()


def test_a_state_without_income_tax_needs_no_state_tax(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """Where the household lives is written all the same, for a later move
    to a state with an income tax; no state tax is added."""
    result = _tax(test_data_dir, "state", "TX", "--yes")

    assert result.exit_code == 0, result.output
    assert "has no state tax" in result.output
    assert _residence(retire_alice_csv("")) == [("2026-05", "TX", "")]
    assert not retire_alice_csv("taxes/state.csv").exists()
    load_test_copy()


@pytest.mark.parametrize("territory", ["GU", "MP", "VI"])
def test_a_mirror_code_territory_is_taxed_by_the_federal_tax_alone(
    territory: str, test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """#491: the territory's tax is a copy of the federal one, so no state
    tax is added and no tables are asked for."""
    result = _tax(test_data_dir, "state", territory, "--yes")

    assert result.exit_code == 0, result.output
    assert "the federal tax stands in for it: no state tax is needed" in result.output
    assert _residence(retire_alice_csv("")) == [("2026-05", territory, "")]
    assert not retire_alice_csv("taxes/state.csv").exists()
    load_test_copy()


@pytest.mark.parametrize("command", [["state", "PR"], ["move", "2031-07", "PR"]])
def test_puerto_rico_is_refused_naming_its_issue(
    command: list[str], test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, *command, "--yes")

    assert result.exit_code != 0
    assert "Puerto Rico (PR) has a tax code of its own" in result.output
    assert states.OWN_TAX_CODE_ISSUE in result.output
    assert _residence(retire_alice_csv("")) == [("2026-05", "CA", "")]


def test_a_state_without_tables_says_which_to_write(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "state", "Montana", "--yes")

    assert result.exit_code != 0
    assert "aux/state_brackets__MT.csv and aux/state_standard_deduction__MT.csv" in result.output
    assert _residence(retire_alice_csv("")) == [("2026-05", "CA", "")]


def test_a_scenario_without_a_state_tax_gets_one_like_its_federal_tax(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult], add_tax: Callable[..., Path],
    write_aux: Callable[[str, str], Path],
) -> None:
    """Less what is the federal tax's own: its tables' bracket_series."""
    write_aux("federal_brackets__law.csv", (TEMPLATE_AUX_DIR / "federal_brackets.csv").read_text())
    add_tax(params_json={"nol_limit_percent": 80, "bracket_series": "law"})

    result = _tax(test_data_dir, "state", "DC", "--yes")

    assert result.exit_code == 0, result.output
    assert "and expense account Expense_Taxes_State" in result.output
    [state] = pd.read_csv(retire_alice_csv("taxes/state.csv"), dtype=str,
                          keep_default_na=False).to_dict("records")
    assert (state["kind"], state["source_chain"], state["destination"], state["start"]) == (
        "state_tables", "Asset_Checking", "Expense_Taxes_State", "2027-04")
    assert json.loads(state["params_json"]) == {"nol_limit_percent": 80}
    assert _residence(retire_alice_csv("")) == [("2026-05", "DC", "")]
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    assert "taxes_state" in accounts["subkind"].tolist()
    load_test_copy()


def test_a_scenario_without_income_tax_is_told_to_add_the_federal_one(test_data_dir: Path) -> None:
    result = _tax(test_data_dir, "state", "CA", "--yes")

    assert result.exit_code != 0
    assert "add the federal one first" in result.output


def test_a_county_needs_its_local_tax_table(
    test_data_dir: Path, add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
    write_aux: Callable[[str, str], Path], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    add_tax("local")

    missing = _tax(test_data_dir, "state", "CA", "--county", "CA-county", "--yes")
    assert missing.exit_code != 0
    assert "no aux/state_local_tax__CA-county.csv" in missing.output

    write_aux("state_local_tax__CA-county.csv",
              "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
              + "".join(f"2020-01-31,{s},0,0.00,1%\n" for s in ("Single", "MFJ", "MFS")))
    result = _tax(test_data_dir, "state", "CA", "--county", "CA-county", "--yes")
    assert result.exit_code == 0, result.output
    assert _residence(retire_alice_csv("")) == [("2026-05", "CA", "CA-county")]
    load_test_copy()


# ---------- sim tax move ----------


def test_a_move_adds_a_residence_row(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "move", "2031-07", "North Carolina", "--yes")

    assert result.exit_code == 0, result.output
    assert _residence(retire_alice_csv("")) == [
        ("2026-05", "CA", ""), ("2031-07", "NC", "")]
    load_test_copy()

    result = _tax(test_data_dir, "state")
    assert "then moves on 2031-07 to North Carolina (NC)" in result.output


def test_a_move_into_a_state_with_income_tax_adds_a_state_tax(
    test_data_dir: Path, add_tax: Callable[..., Path],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_tax()
    assert _tax(test_data_dir, "state", "TX", "--yes").exit_code == 0
    assert not retire_alice_csv("taxes/state.csv").exists()

    result = _tax(test_data_dir, "move", "2031-07", "NC", "--yes")

    assert result.exit_code == 0, result.output
    assert "It adds a state tax to scenario retire_alice" in result.output
    assert retire_alice_csv("taxes/state.csv").exists()
    assert _residence(retire_alice_csv(""))[-1] == ("2031-07", "NC", "")
    load_test_copy()


def test_a_move_in_a_child_adds_to_what_it_inherits(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], test_child_scenario: Path,
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "move", "2031-07", "NC", "--yes", scenario="child")

    assert result.exit_code == 0, result.output
    assert _residence(retire_alice_csv("")) == [("2026-05", "CA", "")]
    assert _residence(test_child_scenario) == [("2031-07", "NC", "")]


def test_a_move_to_a_state_without_income_tax(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "move", answers=["2030-01", "WY", "y"])

    assert result.exit_code == 0, result.output
    assert _residence(retire_alice_csv(""))[-1] == ("2030-01", "WY", "")
    load_test_copy()


def test_a_move_must_come_after_the_start(
    test_data_dir: Path, add_state_tax: Callable[..., None],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "move", "2026-05", "NC", "--yes")

    assert result.exit_code != 0
    assert "a move must come after 2026-05" in result.output


@pytest.mark.parametrize("state_tax", [True, False])
def test_a_move_needs_the_starting_state_first(
    state_tax: bool, test_data_dir: Path, add_state_tax: Callable[..., None],
) -> None:
    if state_tax:
        add_state_tax(state=None)

    result = _tax(test_data_dir, "move", "2031-07", "NC", "--yes")

    assert result.exit_code != 0
    assert "choose the state you live in first (sim tax state)" in result.output


def test_a_move_to_washington_reads_its_shipped_tables(
    test_data_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    add_state_tax()

    result = _tax(test_data_dir, "move", "2031-07", "WA", "--yes")

    assert result.exit_code == 0, result.output
    assert "isn't modeled" not in result.output
    assert "has no tax tables" not in result.output
    assert _residence(retire_alice_csv(""))[-1] == ("2031-07", "WA", "")


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


def test_add_local_asks_nothing_the_state_sets(
    test_data_dir: Path, add_state_tax: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    """A local tax (#508) is paid with the state's and reads the county's
    (or state's) bands, so `sim tax add local` writes at once; it needs the
    state."""
    alone = _tax(test_data_dir, "add", "local")
    assert alone.exit_code != 0
    assert "builds on the state tax, and retire_alice has none (sim tax add state first)" in alone.output

    add_state_tax()
    result = _tax(test_data_dir, "add", "local")
    assert result.exit_code == 0, result.output
    [row] = pd.read_csv(retire_alice_csv("taxes/local.csv"), dtype=str,
                        keep_default_na=False).to_dict("records")
    assert (row["kind"], row["source_chain"], row["destination"]) == ("local_bands", "", "")
    write_aux("state_local_tax__CA.csv", "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
              + "".join(f"2020-01-31,{s},0,0.00,1%\n" for s in ("Single", "MFJ", "MFS")))
    load_test_copy()

    listed = CliRunner().invoke(main, ["sim", "tax", "list", "retire_alice",
                                       "--data-dir", str(test_data_dir)])
    assert listed.exit_code == 0, listed.output
    assert "state    state_tables on federal: paid from Asset_Checking" in listed.output
    assert ("local    local_bands on state: paid from the state tax's source into the "
            "state tax's destination") in listed.output


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
