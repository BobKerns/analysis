"""Shell tab completion of the names a scenario defines (#240)."""

from __future__ import annotations

from pathlib import Path

import click
from click.testing import CliRunner

from analysis.sim import names
from analysis.sim.cli import sim_group_command
from analysis.sim.completion import NameType


def _complete(data_dir: Path | None, line: str) -> list[str]:
    """What ``sim`` offers for the last word of ``line`` (complete when the
    line ends in a space), the way a bash TAB asks for it."""
    words = line.split()
    cword = len(words) + (1 if line.endswith(" ") else 0)
    env = {"_SIM_COMPLETE": "bash_complete", "COMP_WORDS": f"sim {line}", "COMP_CWORD": str(cword)}
    if data_dir is not None:
        env["ANALYSIS_DATA_DIR"] = str(data_dir)
    result = CliRunner().invoke(sim_group_command, [], prog_name="sim", env=env)
    assert result.exit_code == 0, result.output
    return [item.split(",", 1)[1] for item in result.output.splitlines() if item]


# ---------- the readers ----------

def test_scenario_names(data_dir: Path) -> None:
    assert {"base", "retire_alice"} <= set(names.scenario_names(data_dir))


def test_account_names_are_the_scenarios_own_and_inherited(test_child_scenario: Path,
                                                           test_data_dir: Path) -> None:
    own = names.account_names(test_data_dir, "retire_alice")
    assert "Asset_Checking" in own
    # child has no accounts.csv rows of its own: everything is inherited.
    assert names.account_names(test_data_dir, "child") == own


def test_lot_names(data_dir: Path) -> None:
    assert names.lot_names(data_dir, "retire_alice", "Asset_Brokerage_Joint") == ["default"]
    # A blank lot in account_lots.csv is the default lot.
    assert names.lot_names(data_dir, "retire_alice", "Asset_Checking") == ["default"]
    assert names.lot_names(data_dir, "retire_alice", "no_such_account") == []


def test_chain_names(data_dir: Path) -> None:
    chains = names.chain_names(data_dir, "retire_alice")
    assert "asset_checking_liquidity" in chains
    assert "Asset_Checking" not in chains
    assert "Asset_Checking" in names.chain_names(data_dir, "retire_alice", accounts=True)


def test_rules_rows_and_persons(data_dir: Path) -> None:
    assert "salary_alice" in names.scheduled_names(data_dir, "retire_alice")
    # base's latent factors, inherited.
    assert "core_inflation" in names.calendar_row_names(data_dir, "retire_alice")
    assert "prior_ordinary_wages" in names.ytd_names(data_dir, "retire_alice")
    assert {"alice", "bill"} <= set(names.person_ids(data_dir))


def test_series_names_by_kind(data_dir: Path) -> None:
    rates = names.series_names(data_dir, "retire_alice", "rate")
    inflation = names.series_names(data_dir, "retire_alice", "inflation")
    assert {"stock", "brokerage_growth"} <= set(rates)  # base's and retire_alice's own
    assert "cpi" in inflation  # the shared aux/ folder's
    assert "cpi" not in rates
    assert set(rates) | set(inflation) <= set(names.series_names(data_dir, "retire_alice"))


def test_symbols_come_from_every_source(symbol_sources_data_dir: Path) -> None:
    assert {"ALLOC", "OVERRIDE", "HELD"} <= set(names.symbols(symbol_sources_data_dir))


def test_readers_tolerate_a_missing_data_repo(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"
    assert names.scenario_names(missing) == []
    assert names.account_names(missing, "base") == []
    assert names.person_ids(missing) == []


# ---------- completing a command line ----------

def test_scenario_argument_completes(data_dir: Path) -> None:
    assert _complete(data_dir, "run ") == names.scenario_names(data_dir)
    assert _complete(data_dir, "run re") == ["retire_alice"]


def test_account_completes_in_the_named_scenario(data_dir: Path) -> None:
    assert _complete(data_dir, "account lot list --scenario retire_alice Asset_R") == [
        "Asset_Roth_Alice", "Asset_Roth_Bill",
    ]
    # Matching ignores case.
    assert _complete(data_dir, "account edit --scenario retire_alice asset_ch") == ["Asset_Checking"]


def test_a_year_to_date_row_completes(data_dir: Path) -> None:
    assert _complete(data_dir, "ytd edit --scenario retire_alice prior_f") == ["prior_federal_withholding"]
    assert _complete(data_dir, "ytd add --scenario retire_alice x Income_Salary_Alice Asset_Ch") == [
        "Asset_Checking",
    ]


def test_account_completes_in_the_current_scenario(test_data_dir: Path) -> None:
    (test_data_dir / ".current_scenario").write_text("retire_alice\n", encoding="utf-8")
    assert "Asset_Checking" in _complete(test_data_dir, "account edit ")


def test_data_dir_option_overrides_the_environment(data_dir: Path) -> None:
    line = f"account edit --data-dir {data_dir} --scenario retire_alice Asset_Ch"
    assert _complete(None, line) == ["Asset_Checking"]


def test_lot_option_follows_its_account_argument(data_dir: Path) -> None:
    # Click hasn't assigned the arguments while an option waits for its
    # value; the completion still finds TO_ACCOUNT.
    line = "transfer add --scenario retire_alice 2027-01 Asset_Checking Asset_Brokerage_Joint 100 --to-lot "
    assert _complete(data_dir, line) == ["default"]
    assert _complete(data_dir, "account lot edit --scenario retire_alice Asset_Roth_Alice ") == ["default"]


def test_series_follows_the_aux_kind(data_dir: Path) -> None:
    rates = _complete(data_dir, "calendar add set-rate --scenario retire_alice 2027-01 ")
    inflation = _complete(
        data_dir, "calendar add set-rate --scenario retire_alice --aux-kind inflation 2027-01 ",
    )
    assert "stock" in rates and "cpi" not in rates
    assert "cpi" in inflation and "stock" not in inflation


def test_ensemble_commands_complete_only_ensembles(test_data_dir: Path,
                                                   test_ensemble_scenario: str) -> None:
    # The template ensembles (#282) are offered too: they run like any other.
    assert _complete(test_data_dir, "ensemble summary ") == sorted([
        test_ensemble_scenario, "t_inflation_current", "t_inflation_ensemble",
        "t_market_current", "t_market_ensemble",
    ])
    assert test_ensemble_scenario in _complete(test_data_dir, "run ")


def test_tags_complete(test_data_dir: Path) -> None:
    result = CliRunner().invoke(sim_group_command, [
        "account", "tag", "add", "Asset_Checking", "bucket:cash",
        "--scenario", "retire_alice", "--data-dir", str(test_data_dir),
    ])
    assert result.exit_code == 0, result.output
    # A filter takes a whole role too.
    assert _complete(test_data_dir, "tags items --scenario retire_alice b") == ["bucket:cash", "bucket:*"]


def test_no_data_repo_completes_nothing() -> None:
    assert _complete(None, "run ") == []


def test_unknown_scenario_completes_nothing(data_dir: Path) -> None:
    assert _complete(data_dir, "account edit --scenario no_such ") == []


# ---------- every name completes ----------

# Params that name something the scenario defines, so must complete.
_NAMING_PARAMS = {
    "scenario", "scenario_name", "parent", "mixins", "baseline", "variant",
    "account", "from_account", "to_account", "interest_account", "basis_of", "source",
    "lot", "from_lot", "to_lot", "basis_of_lot", "source_lot", "refill_lot", "overflow_lot",
    "refill_chain", "overflow_chain", "scheduled_name", "row", "person", "owner",
    "series", "source_series", "symbol", "symbols", "tag_token", "tag_tokens",
    "vary_correlate_with", "correlate_with",
}
# ... except where the command makes a new one (by the command's function,
# as `sim accounts` repeats `sim account`).
_NEW_NAMES = {("lot_add", "lot"), ("invest_command", "lot"), ("symbol_add_command", "symbol")}


def _params(command: click.Command, path: str) -> list[tuple[str, click.Command, click.Parameter]]:
    found = [(path, command, param) for param in command.params]
    if isinstance(command, click.Group):
        for name, sub in command.commands.items():
            found += _params(sub, f"{path} {name}")
    return found


def test_every_naming_param_completes() -> None:
    missing = [
        f"{path} {param.name}" for path, command, param in _params(sim_group_command, "sim")
        if param.name in _NAMING_PARAMS
        and (getattr(command.callback, "__name__", ""), param.name) not in _NEW_NAMES
        and not isinstance(param.type, NameType)
    ]
    assert missing == []
