"""Changing chains: `sim chain add|step|rename|delete` and the guided add (issue #129)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import chain_edit


def _sim(data_dir: Path, *args: str, scenario: str = "retire_alice", input: str | None = None):
    return CliRunner().invoke(
        main, ["sim", "chain", *args, "--scenario", scenario, "--data-dir", str(data_dir)],
        input=input, terminal_width=200,
    )


def _steps(scenarios: Path, name: str, scenario: str = "retire_alice") -> list[tuple[str, ...]]:
    path = chain_edit.chain_file(scenarios / scenario, name)
    assert path is not None
    return [tuple(step[c] for c in ("account_name", "lot", "maximum", "percent")) for step in
            chain_edit.read_steps(path)]


@pytest.mark.parametrize(("shares", "percents"), [
    ([100], [""]),
    ([50, 30, 20], ["50%", "60%", ""]),
    ([60, 40], ["60%", ""]),
    ([Decimal("33.3333"), Decimal("33.3333"), Decimal("33.3334")], ["33.3333%", "49.9999%", ""]),
])
def test_shares_of_the_total_become_shares_of_what_is_left(shares, percents) -> None:
    assert chain_edit.shares_to_percents([Decimal(s) for s in shares]) == percents


def test_shares_must_add_up_to_100() -> None:
    with pytest.raises(ValueError, match="add up to 90%"):
        chain_edit.shares_to_percents([Decimal(50), Decimal(40)])


def test_add_then_change_the_steps(test_data_dir: Path, test_scenarios_dir: Path, load_test_copy) -> None:
    assert _sim(test_data_dir, "add", "backup", "Asset_Savings", "--maximum", "500").exit_code == 0
    added = _sim(test_data_dir, "step", "add", "backup", "Asset_Brokerage_Joint", "--lot", "default",
                 "--percent", "50")
    assert added.exit_code == 0, added.output
    assert _sim(test_data_dir, "step", "add", "backup", "Asset_Roth_Alice", "--lot", "default",
                "--position", "1").exit_code == 0
    assert _steps(test_scenarios_dir, "backup") == [
        ("Asset_Roth_Alice", "default", "", ""),
        ("Asset_Savings", "", "500.00", ""),
        ("Asset_Brokerage_Joint", "default", "", "50%"),  # the % is added on the way in
    ]
    assert (test_scenarios_dir / "retire_alice" / "chains" / "asset__backup.csv").exists()
    edited = _sim(test_data_dir, "step", "edit", "backup", "2", "--maximum", "", "--percent", "25%")
    assert edited.exit_code == 0, edited.output
    assert _sim(test_data_dir, "step", "move", "backup", "1", "3").exit_code == 0
    assert _sim(test_data_dir, "step", "rm", "backup", "2").exit_code == 0
    assert _steps(test_scenarios_dir, "backup") == [
        ("Asset_Savings", "", "", "25%"), ("Asset_Roth_Alice", "default", "", "")]
    load_test_copy()


@pytest.mark.parametrize(("args", "message"), [
    (["add", "Asset_Savings", "Asset_Checking"], "already a chain or an account's name"),
    (["add", "x", "Nowhere"], "unknown step account or chain: 'Nowhere'"),
    (["add", "x", "Asset_Savings", "--percent", "150"], "at most 100%"),
    (["add", "x", "Asset_Savings", "--maximum", "12.5"], "invalid dollar value"),
    (["add", "x", "asset_checking_liquidity", "--lot", "a"], "is a chain, so --lot must be blank"),
    (["step", "add", "Asset_Savings", "Asset_Checking"], "already a one-step chain"),
    (["step", "add", "nope", "Asset_Checking"], "no chain 'nope'"),
    (["step", "rm", "asset_checking_liquidity", "3"], "has steps 1 to 2, not 3"),
    (["step", "add", "asset_checking_liquidity", "asset_checking_liquidity"], "a step of itself"),
    (["add", "../escape", "Asset_Savings"], "can't contain ':', '/'"),
    (["add", ".hidden", "Asset_Savings"], "or start with '.'"),
    (["rename", "asset_checking_liquidity", "nested/name"], "can't contain ':', '/'"),
    (["step", "edit", "asset_checking_liquidity", "2", "--lot", ""], "Asset_TraditionalIRA_Alice needs a lot"),
])
def test_refused_changes_write_nothing(test_data_dir: Path, args: list[str], message: str) -> None:
    before = sorted(p.read_bytes() for p in (test_data_dir / "scenarios").rglob("*.csv"))
    result = _sim(test_data_dir, *args)
    assert result.exit_code != 0
    assert message in result.output
    assert sorted(p.read_bytes() for p in (test_data_dir / "scenarios").rglob("*.csv")) == before


def test_a_chain_keeps_one_step(test_data_dir: Path, retire_alice_csv: Callable[[str], Path]) -> None:
    (retire_alice_csv("chains") / "asset__solo.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nAsset_Savings,,,,,\n")
    result = _sim(test_data_dir, "step", "rm", "solo", "1")
    assert result.exit_code != 0 and "only step; `sim chain delete solo`" in result.output


def test_a_change_that_would_not_load_is_refused(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "step", "edit", "asset_checking_liquidity", "1", "--series", "cpi")
    assert result.exit_code != 0
    assert "series requires maximum" in result.output and "nothing was written" in result.output
    assert _steps(test_scenarios_dir, "asset_checking_liquidity")[0] == ("Asset_Savings", "", "", "")


def test_rename_follows_every_reference(guide_chains: Path, test_scenarios_dir: Path, derive_scenario,
                                        load_test_copy) -> None:
    derive_scenario("kid", "retire_alice", files={
        "chains/asset__bridge.csv": "account_name,lot,maximum,percent,series,cadence\nAsset_Savings,,,,,\n"})
    result = _sim(guide_chains, "rename", "bridge", "gap")
    assert result.exit_code == 0, result.output
    assert "retire_alice: chains/asset__bridge.csv -> chains/asset__gap.csv" in result.output
    assert "kid: chains/asset__bridge.csv -> chains/asset__gap.csv" in result.output  # its override too
    assert _steps(test_scenarios_dir, "retirement_draw")[0][0] == "gap"
    renamed = _sim(guide_chains, "rename", "ss_split", "benefit_split")
    assert renamed.exit_code == 0, renamed.output
    scheduled = (test_scenarios_dir / "retire_alice" / "scheduled.csv").read_text()
    assert "ss_alice,Income_SS_Alice,,benefit_split," in scheduled
    liquidity = _sim(guide_chains, "rename", "asset_checking_liquidity", "liquidity")
    assert liquidity.exit_code == 0, liquidity.output
    assert ",liquidity," in (test_scenarios_dir / "retire_alice" / "account_values.csv").read_text()
    load_test_copy()


def test_a_copy_is_renamed_where_the_chain_starts(guide_chains: Path, derive_scenario) -> None:
    derive_scenario("kid", "retire_alice", files={
        "chains/asset__bridge.csv": "account_name,lot,maximum,percent,series,cadence\nAsset_Savings,,,,,\n"})
    result = _sim(guide_chains, "rename", "bridge", "gap", scenario="kid")
    assert result.exit_code != 0 and "bridge comes from retire_alice; rename it there" in result.output


def test_editing_an_inherited_chain_writes_a_copy(test_data_dir: Path, test_scenarios_dir: Path,
                                                   derive_scenario) -> None:
    derive_scenario("kid", "retire_alice")
    result = _sim(test_data_dir, "step", "rm", "asset_checking_liquidity", "2", scenario="kid")
    assert result.exit_code == 0, result.output
    assert "came from retire_alice; wrote kid's own copy" in result.output
    assert _steps(test_scenarios_dir, "asset_checking_liquidity", "kid") == [("Asset_Savings", "", "", "")]
    assert len(_steps(test_scenarios_dir, "asset_checking_liquidity")) == 2  # the parent's is untouched
    deleted = _sim(test_data_dir, "delete", "asset_checking_liquidity", scenario="kid")
    assert deleted.exit_code == 0, deleted.output
    assert "retire_alice's applies again" in deleted.output
    again = _sim(test_data_dir, "delete", "asset_checking_liquidity", scenario="kid")
    assert again.exit_code != 0 and "delete it there (--scenario retire_alice)" in again.output


def test_delete_refuses_a_chain_in_use(guide_chains: Path, test_scenarios_dir: Path) -> None:
    result = _sim(guide_chains, "delete", "bridge")
    assert result.exit_code != 0
    assert "bridge is still used; change these first:\n  a step of retirement_draw (step 1)" in result.output
    assert (test_scenarios_dir / "retire_alice" / "chains" / "asset__bridge.csv").exists()
    assert _sim(guide_chains, "step", "edit", "retirement_draw", "1", "--lot", "").exit_code == 0
    replaced = _sim(guide_chains, "step", "rm", "retirement_draw", "1")
    assert replaced.exit_code == 0, replaced.output
    assert _sim(guide_chains, "delete", "bridge").exit_code == 0
    assert not (test_scenarios_dir / "retire_alice" / "chains" / "asset__bridge.csv").exists()


def test_guided_add_by_shares(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    answers = "\n".join([
        "from", "shares", "triple",
        "Asset_Savings", "50",
        "Asset_Brokerage_Joint", "default", "120", "30",  # a share over what is left is asked again
        "Asset_TraditionalIRA_Alice", "default", "20",
    ]) + "\n"
    result = _sim(test_data_dir, "add", input=answers)
    assert result.exit_code == 0, result.output
    assert "a share is more than 0% and at most the 50% left" in result.output
    assert _steps(test_scenarios_dir, "triple") == [
        ("Asset_Savings", "", "", "50%"), ("Asset_Brokerage_Joint", "default", "", "60%"),
        ("Asset_TraditionalIRA_Alice", "default", "", "")]
    assert "sim chain add triple Asset_Savings --percent 50% --kind asset" in result.output
    assert "sim chain step add triple Asset_Brokerage_Joint --lot default --percent 60%" in result.output
    assert "--refill-chain triple" in result.output  # how to use a chain money comes from


def test_guided_add_by_caps_into_a_destination(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    answers = "\n".join([
        "to", "caps", "Asset_Savings", "spill",  # an account's name is taken, so it is asked again
        "Asset_Savings", "-5", "2000",  # a negative cap is asked again
        "Asset_Brokerage_Joint", "default", "", "",
    ]) + "\n"
    result = _sim(test_data_dir, "add", input=answers)
    assert result.exit_code == 0, result.output
    assert "'Asset_Savings' is already a chain or an account's name" in result.output
    assert "maximum '-5' can't be negative" in result.output
    assert _steps(test_scenarios_dir, "spill") == [
        ("Asset_Savings", "", "2000.00", ""), ("Asset_Brokerage_Joint", "default", "", "")]
    assert "--overflow-chain spill" in result.output


def test_a_chain_file_is_found_by_its_exact_name(
    test_scenarios_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    chains = retire_alice_csv("chains")
    for stem in ("asset__odd[1]", "asset__odd1", "asset__odd_more"):
        (chains / f"{stem}.csv").write_text("account_name,lot,maximum,percent,series,cadence\n")
    scenario = test_scenarios_dir / "retire_alice"
    assert chain_edit.chain_file(scenario, "odd[1]") == chains / "asset__odd[1].csv"
    assert chain_edit.chain_file(scenario, "odd*") is None


def test_guided_step_add_asks_every_column(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    answers = "\n".join([
        "Asset_Brokerage_Joint", "default",
        "150", "40",  # a percent over 100% is asked again
        "1000", "nowhere", "cpi", "annual", "1",
    ]) + "\n"
    result = _sim(test_data_dir, "step", "add", "asset_checking_liquidity", input=answers)
    assert result.exit_code == 0, result.output
    assert "must be more than 0% and at most 100%" in result.output
    assert "no inflation series 'nowhere'" in result.output
    assert "chains: asset_checking_liquidity" not in result.output  # not offered as its own step
    path = chain_edit.chain_file(test_scenarios_dir / "retire_alice", "asset_checking_liquidity")
    assert path is not None
    assert chain_edit.read_steps(path)[0] == {
        "account_name": "Asset_Brokerage_Joint", "lot": "default", "maximum": "1000.00", "percent": "40%",
        "series": "cpi", "cadence": "annual"}
    assert ("sim chain step add asset_checking_liquidity Asset_Brokerage_Joint --lot default --percent 40% "
            "--maximum 1000.00 --series cpi --cadence annual --position 1") in result.output


def test_guided_step_edit_keeps_or_clears_each_column(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    assert _sim(test_data_dir, "step", "edit", "asset_checking_liquidity", "2", "--percent", "25",
                "--maximum", "800", "--series", "cpi").exit_code == 0
    # step 2, its lot kept, percent cleared, maximum kept, series cleared
    result = _sim(test_data_dir, "step", "edit", "asset_checking_liquidity", input="2\n\n-\n\n-\n")
    assert result.exit_code == 0, result.output
    assert "2. Asset_TraditionalIRA_Alice (default), 25% of what is left, at most 800.00, grown by cpi" \
        in result.output
    assert _steps(test_scenarios_dir, "asset_checking_liquidity")[1] == (
        "Asset_TraditionalIRA_Alice", "default", "800.00", "")
    assert "sim chain step edit asset_checking_liquidity 2 --percent '' --series ''" in result.output


def test_step_rm_and_move_ask_for_what_is_left_out(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    moved = _sim(test_data_dir, "step", "move", input="Asset_Savings\nasset_checking_liquidity\n2\n1\n")
    assert moved.exit_code == 0, moved.output
    assert "one of: asset_checking_liquidity" in moved.output  # an account isn't an editable chain
    assert [s[0] for s in _steps(test_scenarios_dir, "asset_checking_liquidity")] == [
        "Asset_TraditionalIRA_Alice", "Asset_Savings"]
    removed = _sim(test_data_dir, "step", "rm", "asset_checking_liquidity", input="3\n1\n")
    assert removed.exit_code == 0, removed.output
    assert "asset_checking_liquidity has steps 1 to 2" in removed.output
    assert [s[0] for s in _steps(test_scenarios_dir, "asset_checking_liquidity")] == ["Asset_Savings"]


def test_guided_rename_and_delete(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    assert _sim(test_data_dir, "add", "backup", "Asset_Savings").exit_code == 0
    renamed = _sim(test_data_dir, "rename", input="backup\nAsset_Savings\nreserve\n")
    assert renamed.exit_code == 0, renamed.output
    assert "'Asset_Savings' is already a chain or an account's name" in renamed.output
    assert "renamed chain backup to reserve" in renamed.output
    kept = _sim(test_data_dir, "delete", input="reserve\nn\n")
    assert kept.exit_code == 0 and "nothing was deleted" in kept.output
    assert chain_edit.chain_file(test_scenarios_dir / "retire_alice", "reserve") is not None
    deleted = _sim(test_data_dir, "delete", input="reserve\ny\n")
    assert deleted.exit_code == 0, deleted.output
    assert chain_edit.chain_file(test_scenarios_dir / "retire_alice", "reserve") is None


def test_guided_step_add_asks_only_what_options_leave_out(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "step", "add", "asset_checking_liquidity", "--maximum", "100",
                  input="Asset_Roth_Alice\ndefault\n25\ncpi\n\n\n")
    assert result.exit_code == 0, result.output
    assert "at most how much" not in result.output
    path = chain_edit.chain_file(test_scenarios_dir / "retire_alice", "asset_checking_liquidity")
    assert path is not None
    assert chain_edit.read_steps(path)[-1] == {
        "account_name": "Asset_Roth_Alice", "lot": "default", "maximum": "100.00", "percent": "25%",
        "series": "cpi", "cadence": ""}


def test_rename_refuses_a_name_a_descendant_already_uses(test_data_dir: Path, test_scenarios_dir: Path,
                                                         derive_scenario) -> None:
    derive_scenario("kid", "retire_alice", files={
        "chains/asset__asset_checking_liquidity.csv":
            "account_name,lot,maximum,percent,series,cadence\nAsset_Savings,,,,,\n",
        "chains/asset__reserve.csv": "account_name,lot,maximum,percent,series,cadence\nAsset_Checking,,,,,\n"})
    result = _sim(test_data_dir, "rename", "asset_checking_liquidity", "reserve")
    assert result.exit_code != 0
    assert "a chain 'reserve' already applies in kid;" in result.output
    assert chain_edit.chain_file(test_scenarios_dir / "kid", "asset_checking_liquidity") is not None
    assert chain_edit.read_steps(test_scenarios_dir / "kid" / "chains" / "asset__reserve.csv")[0][
        "account_name"] == "Asset_Checking"


def test_an_empty_option_is_a_blank_column_not_a_question(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "step", "add", "asset_checking_liquidity", "--percent", "", "--maximum", "100",
                  "--series", "", input="Asset_Roth_Alice\ndefault\n\n")
    assert result.exit_code == 0, result.output
    assert "share of what is still outstanding" not in result.output
    assert "inflation series" not in result.output
    assert _steps(test_scenarios_dir, "asset_checking_liquidity")[-1] == (
        "Asset_Roth_Alice", "default", "100.00", "")


def test_rename_refuses_a_name_a_mix_in_brings(test_data_dir: Path, derive_scenario) -> None:
    derive_scenario("mix", "base", files={
        "chains/asset__reserve.csv": "account_name,lot,maximum,percent,series,cadence\nAsset_Checking,,,,,\n"})
    derive_scenario("kid", "retire_alice:mix")
    result = _sim(test_data_dir, "rename", "asset_checking_liquidity", "reserve")
    assert result.exit_code != 0
    assert "a chain 'reserve' already applies in kid (from mix)" in result.output


def test_two_files_for_one_chain_are_refused(test_data_dir: Path, retire_alice_csv: Callable[[str], Path]) -> None:
    (retire_alice_csv("chains") / "income__asset_checking_liquidity.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nIncome_Interest,,,,,\n")
    result = _sim(test_data_dir, "delete", "asset_checking_liquidity")
    assert result.exit_code != 0
    assert "retire_alice has 2 files for chain 'asset_checking_liquidity'" in result.output
    assert (retire_alice_csv("chains") / "asset__asset_checking_liquidity.csv").exists()


def test_rename_refuses_when_a_mix_in_shadows_the_chain(test_data_dir: Path, test_scenarios_dir: Path,
                                                        derive_scenario) -> None:
    derive_scenario("mix", "base", files={
        "chains/asset__asset_checking_liquidity.csv":
            "account_name,lot,maximum,percent,series,cadence\nAsset_Checking,,,,,\n"})
    derive_scenario("kid", "retire_alice:mix")
    result = _sim(test_data_dir, "rename", "asset_checking_liquidity", "liquidity")
    assert result.exit_code != 0
    assert "kid gets asset_checking_liquidity from mix" in result.output
    assert chain_edit.chain_file(test_scenarios_dir / "retire_alice", "asset_checking_liquidity") is not None


def test_an_underscore_template_is_not_a_chain(test_scenarios_dir: Path,
                                               retire_alice_csv: Callable[[str], Path]) -> None:
    (retire_alice_csv("chains") / "_asset__example.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nAsset_Savings,,,,,\n")
    assert chain_edit.chain_file(test_scenarios_dir / "retire_alice", "example") is None


def test_rename_leaves_underscore_templates_alone(test_data_dir: Path,
                                                  retire_alice_csv: Callable[[str], Path]) -> None:
    template = retire_alice_csv("chains") / "_asset__example.csv"
    text = "account_name,lot,maximum,percent,series,cadence\nasset_checking_liquidity,,,,,\n"
    template.write_text(text)
    result = _sim(test_data_dir, "rename", "asset_checking_liquidity", "liquidity")
    assert result.exit_code == 0, result.output
    assert template.read_text() == text
