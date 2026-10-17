"""``sim scheduled add --kind``, ``edit`` and ``delete`` (issue #183): every
rule kind with its own columns; an inherited rule is edited by a whole-row
override and deleted by an override ending it before the scenario starts
(#202)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.registries import SCHEDULED_CSV_HEADER


def _sim(test_data_dir: Path, scenario: str, *args: str, answers: Sequence[str] = ()) -> Result:
    return CliRunner().invoke(
        main,
        ["sim", "scheduled", *args, "--data-dir", str(test_data_dir), "--scenario", scenario],
        input="".join(answer + "\n" for answer in answers),
    )


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=list(SCHEDULED_CSV_HEADER))
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return frame.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")


def _rule(frame: pd.DataFrame, name: str) -> dict[str, str]:
    rows = frame[frame["name"] == name]
    assert len(rows) == 1, f"{name!r}: {len(rows)} rows"
    return {str(k): str(v) for k, v in rows.iloc[0].to_dict().items()}


@pytest.fixture
def scheduled(retire_alice_csv: Callable[[str], Path]) -> Callable[[], pd.DataFrame]:
    """The private retire_alice copy's scheduled.csv, every column present."""
    return lambda: _read(retire_alice_csv("scheduled.csv"))


# ---------- add --kind ----------


def test_add_writes_an_ss_benefit_rule_with_its_own_columns(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    result = _sim(
        test_data_dir, "retire_alice", "add", "2027-01", "Income_SS_Alice", "Asset_Checking",
        "2000", "--kind", "ss_benefit", "--person", "alice", "--claim-age", "68",
        "--series", "cpi", "--name", "ss_test",
    )

    assert result.exit_code == 0, result.output
    row = _rule(scheduled(), "ss_test")
    assert (row["amount_rule_kind"], row["person"], row["claim_age"], row["amount"]) == (
        "ss_benefit", "alice", "68", "2000.00")
    # The kind's usual tax character and priority band, and no base date.
    assert (row["tax_character"], row["priority"], row["base_date"]) == ("ss_benefit", "200", "")


def test_add_asks_for_what_the_kind_requires_and_was_not_given(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    result = _sim(
        test_data_dir, "retire_alice", "add", "2027-01", "Asset_TraditionalIRA_Alice",
        "Asset_Checking", "--from-lot", "default", "--kind", "rmd", "--cadence", "annual",
        "--month-of-year", "12", "--name", "rmd_test", answers=["nobody", "alice"],
    )

    assert result.exit_code == 0, result.output
    assert "not one of" in result.output
    row = _rule(scheduled(), "rmd_test")
    assert (row["person"], row["amount"], row["tax_character"], row["priority"]) == (
        "alice", "", "tax_deferred_out", "500")


def test_add_refuses_an_amount_for_a_kind_that_reads_none(test_data_dir: Path) -> None:
    result = _sim(
        test_data_dir, "retire_alice", "add", "2027-01", "Asset_Checking", "Asset_Savings",
        "100", "--kind", "on_death", "--person", "alice",
    )

    assert result.exit_code != 0
    assert "rule kind on_death takes no AMOUNT" in result.output


def test_add_on_death_defaults_to_tax_free_and_refuses_any_other(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    refused = _sim(
        test_data_dir, "retire_alice", "add", "2027-01", "Asset_Savings", "Asset_Checking",
        "--kind", "on_death", "--person", "alice", "--tax-character", "ordinary",
    )
    assert refused.exit_code != 0
    assert "must be tax_free" in refused.output

    result = _sim(
        test_data_dir, "retire_alice", "add", "2027-01", "Asset_Savings", "Asset_Checking",
        "--kind", "on_death", "--person", "alice", "--name", "death_test",
    )

    assert result.exit_code == 0, result.output
    row = _rule(scheduled(), "death_test")
    assert (row["tax_character"], row["priority"], row["series"]) == ("tax_free", "900", "")


def test_add_a_mortgage_writes_its_rate_as_a_percent(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    result = _sim(
        test_data_dir, "retire_alice", "add", "2027-01", "Asset_Checking", "Liability_Mortgage",
        "900", "--kind", "mortgage_payment", "--rate", "4.25",
        "--interest-account", "Expense_Interest_Mortgage", "--name", "heloc",
    )

    assert result.exit_code == 0, result.output
    row = _rule(scheduled(), "heloc")
    assert (row["rate"], row["interest_account"], row["amount"], row["priority"]) == (
        "4.25%", "Expense_Interest_Mortgage", "900.00", "300")


def test_add_takes_a_base_date_and_ytd_columns(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    result = _sim(
        test_data_dir, "retire_alice", "add", "2027-01", "Asset_Checking", "Expense_Living",
        "50", "--series", "cpi", "--base-date", "2026-05", "--inflation-cadence", "annual",
        "--tax-year", "2026", "--ytd-amount", "200", "--name", "dues",
    )

    assert result.exit_code == 0, result.output
    row = _rule(scheduled(), "dues")
    assert (row["base_date"], row["inflation_cadence"], row["tax_year"], row["ytd_amount"]) == (
        "2026-05", "annual", "2026", "200.00")


# ---------- edit ----------


def test_edit_options_change_those_fields_and_keep_the_rest_as_authored(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    before = _rule(scheduled(), "mortgage_pmt")

    result = _sim(test_data_dir, "retire_alice", "edit", "mortgage_pmt", "--rate", "5.5")

    assert result.exit_code == 0, result.output
    assert "updated scheduled rule 'mortgage_pmt'" in result.output
    # base_date is not a mortgage's column, but nothing asked to change it.
    assert _rule(scheduled(), "mortgage_pmt") == {**before, "rate": "5.5%"}


def test_edit_leaves_the_rule_in_its_place(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    before = scheduled()

    result = _sim(test_data_dir, "retire_alice", "edit", "living", "--amount", "4600")

    assert result.exit_code == 0, result.output
    after = scheduled()
    changed = after.ne(before)
    assert changed.sum().sum() == 1 and after.loc[2, "amount"] == "4600.00"


def test_edit_to_another_kind_drops_the_columns_it_does_not_read(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    result = _sim(
        test_data_dir, "retire_alice", "edit", "living", "--kind", "mortgage_payment",
        "--rate", "5", "--interest-account", "Expense_Interest_Mortgage",
    )

    assert result.exit_code == 0, result.output
    row = _rule(scheduled(), "living")
    assert (row["amount_rule_kind"], row["series"], row["base_date"], row["amount"]) == (
        "mortgage_payment", "", "", "4500.00")


def test_edit_refuses_a_kind_without_its_required_columns(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    before = scheduled()

    result = _sim(test_data_dir, "retire_alice", "edit", "living", "--kind", "ss_benefit")

    assert result.exit_code != 0
    assert "rule kind ss_benefit needs --person, --claim-age" in result.output
    pd.testing.assert_frame_equal(scheduled(), before)


def test_guided_edit_changes_a_named_field(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    result = _sim(
        test_data_dir, "retire_alice", "edit", "ss_bill", answers=["claim-age", "69", "write"],
    )

    assert result.exit_code == 0, result.output
    # Only the kind's own fields are shown.
    assert "claim-age" in result.output and "base-date" not in result.output
    assert _rule(scheduled(), "ss_bill")["claim_age"] == "69"


def test_edit_of_an_unknown_rule_says_so(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "edit", "nope", "--amount", "1")

    assert result.exit_code != 0
    assert "no scheduled rule 'nope'" in result.output


def test_editing_an_inherited_rule_writes_a_whole_row_override(
    test_data_dir: Path, test_child_scenario: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    parent = scheduled()

    result = _sim(test_data_dir, "child", "edit", "healthcare", answers=["amount", "700", "write"])

    assert result.exit_code == 0, result.output
    assert "It comes from scenario 'retire_alice'" in result.output
    assert "wrote an override of scheduled rule 'healthcare'" in result.output
    override = _read(test_child_scenario / "scheduled.csv")
    assert len(override) == 1
    assert override.iloc[0].to_dict() == {**_rule(parent, "healthcare"), "amount": "700.00"}
    pd.testing.assert_frame_equal(scheduled(), parent)

    again = _sim(test_data_dir, "child", "edit", "healthcare", "--amount", "750")

    assert again.exit_code == 0, again.output
    assert "updated scheduled rule" in again.output
    override = _read(test_child_scenario / "scheduled.csv")
    assert len(override) == 1 and override.loc[0, "amount"] == "750.00"


def test_writing_an_inherited_rule_unchanged_writes_no_override(
    test_data_dir: Path, test_child_scenario: Path,
) -> None:
    result = _sim(test_data_dir, "child", "edit", "healthcare", answers=["write"])

    assert result.exit_code == 0, result.output
    assert "no change; nothing was written" in result.output
    assert _read(test_child_scenario / "scheduled.csv").empty


# ---------- delete ----------


def test_delete_removes_a_rule_and_the_calendar_rows_naming_it(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    calendar = retire_alice_csv("calendar.csv")
    before = len(pd.read_csv(calendar, dtype=str))

    result = _sim(test_data_dir, "retire_alice", "delete", "salary_alice", answers=["y"])

    assert result.exit_code == 0, result.output
    assert "retire_scheduled  salary_alice" in result.output
    assert "salary_alice" not in set(scheduled()["name"])
    after = pd.read_csv(calendar, dtype=str, keep_default_na=False)
    assert len(after) == before - 1 and "salary_alice" not in set(after["scheduled_name"])


def test_delete_writes_nothing_unless_confirmed(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    before = scheduled()

    result = _sim(test_data_dir, "retire_alice", "delete", "living", answers=["n"])

    assert result.exit_code == 0, result.output
    assert "aborted" in result.output
    pd.testing.assert_frame_equal(scheduled(), before)


def test_deleting_an_inherited_rule_ends_it_before_the_scenario_starts(
    test_data_dir: Path, test_child_scenario: Path, scheduled: Callable[[], pd.DataFrame],
) -> None:
    parent = scheduled()

    result = _sim(test_data_dir, "child", "delete", "living", "--yes")

    assert result.exit_code == 0, result.output
    assert "can't remove what it inherits" in result.output
    override = _read(test_child_scenario / "scheduled.csv")
    # retire_alice starts 2026-05; the override ends the month before.
    assert override.iloc[0].to_dict() == {**_rule(parent, "living"), "end": "2026-04"}
    pd.testing.assert_frame_equal(scheduled(), parent)

    again = _sim(test_data_dir, "child", "delete", "living", "--yes")

    assert again.exit_code == 0, again.output
    assert "inherited from 'retire_alice' again" in again.output
    assert _read(test_child_scenario / "scheduled.csv").empty


def test_deleting_an_inherited_rule_clears_its_year_to_date_catch_up(
    test_data_dir: Path, test_child_scenario: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    path = retire_alice_csv("scheduled.csv")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.loc[frame["name"] == "living", "ytd_preroll"] = "1"
    frame.to_csv(path, index=False)

    result = _sim(test_data_dir, "child", "delete", "living", "--yes")

    assert result.exit_code == 0, result.output
    assert _read(test_child_scenario / "scheduled.csv").loc[0, "ytd_preroll"] == ""


def test_deleting_an_inherited_rule_that_already_ended_writes_nothing(
    test_data_dir: Path, test_child_scenario: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    path = retire_alice_csv("scheduled.csv")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.loc[frame["name"] == "living", "end"] = "2026-01"
    frame.to_csv(path, index=False)

    result = _sim(test_data_dir, "child", "delete", "living", "--yes")

    assert result.exit_code == 0, result.output
    assert "already ends before the scenario starts" in result.output
    assert _read(test_child_scenario / "scheduled.csv").empty


def test_edit_keeps_a_spelled_out_default_lot_it_did_not_ask_about(
    test_data_dir: Path, scheduled: Callable[[], pd.DataFrame],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    path = retire_alice_csv("scheduled.csv")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.loc[frame["name"] == "living", "from_lot"] = "default"
    frame.to_csv(path, index=False)
    before = scheduled()

    result = _sim(test_data_dir, "retire_alice", "edit", "living", answers=["write"])

    assert result.exit_code == 0, result.output
    assert "no change; nothing was written" in result.output
    pd.testing.assert_frame_equal(scheduled(), before)
