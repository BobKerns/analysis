"""Scenario edits are validated on a staged copy before anything is written
(issue #44): a rejected or interrupted edit leaves the data repo untouched,
and a scenario that was already broken is reported as such."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import staging


def _snapshot(data_dir: Path) -> dict[Path, bytes]:
    root = data_dir / "scenarios"
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _sim(*args: str, data_dir: Path, input: str | None = None):
    return CliRunner().invoke(
        main, ["sim", *args, "--data-dir", str(data_dir)], input=input,
    )


def _break_retire_alice(append_calendar_row: Callable[..., None]) -> None:
    # Well-formed, but nothing reads the series: the loader rejects it.
    append_calendar_row(date="2028-01-31", kind="set_rate", series="no_such_series",
                        amount="3000000")


def test_good_edit_to_already_broken_scenario_blames_the_scenario(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
) -> None:
    _break_retire_alice(append_calendar_row)
    before = _snapshot(test_data_dir)

    result = _sim("calendar", "add", "set-rate", "2027-03-31", "Asset_Checking", "4",
                  "--scenario", "retire_alice", data_dir=test_data_dir)

    assert result.exit_code != 0
    assert "no_such_series" in result.output
    assert "already fails to load" in result.output
    assert "Traceback" not in result.output
    assert _snapshot(test_data_dir) == before


@pytest.mark.parametrize("args", [
    ("calendar", "add", "set-amount", "2027-03-31", "healthcare", "100"),
    ("scheduled", "add", "2027-03-31", "Asset_Checking", "Asset_Savings", "100",
     "--cadence", "monthly"),
    ("account", "rename", "Asset_Checking", "Asset_Checking_2"),
])
def test_already_broken_scenario_fails_cleanly_before_any_prompt(
    test_data_dir: Path, append_calendar_row: Callable[..., None], args: tuple[str, ...],
) -> None:
    """Commands that look things up before editing used to surface the
    broken scenario as a LoadError traceback."""
    _break_retire_alice(append_calendar_row)
    before = _snapshot(test_data_dir)

    result = _sim(*args, "--scenario", "retire_alice", data_dir=test_data_dir)

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit), result.exception
    assert "already fails to load" in result.output
    assert _snapshot(test_data_dir) == before


def test_loader_crash_while_validating_writes_nothing(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a LoadError used to trigger the revert; any other exception left
    the half-applied row in place."""
    real_load = staging.load_scenario
    calls = []

    def flaky_load(*args, **kwargs):
        calls.append(args)
        if len(calls) > 1:  # the preflight succeeds; validating the edit crashes
            raise RuntimeError("boom")
        return real_load(*args, **kwargs)

    monkeypatch.setattr(staging, "load_scenario", flaky_load)
    before = _snapshot(test_data_dir)

    result = _sim("calendar", "add", "set-rate", "2027-03-31", "Asset_Checking", "4",
                  "--scenario", "retire_alice", data_dir=test_data_dir)

    assert result.exit_code != 0
    assert "loader failed: RuntimeError: boom" in result.output
    assert "nothing was written" in result.output
    assert _snapshot(test_data_dir) == before


def test_interrupted_edit_writes_nothing(test_data_dir: Path) -> None:
    before = _snapshot(test_data_dir)
    real = test_data_dir / "scenarios" / "retire_alice" / "calendar.csv"

    with pytest.raises(KeyboardInterrupt):
        with staging.staged_edit(test_data_dir, "retire_alice") as edit:
            edit.path(real).write_text("garbage\n")
            raise KeyboardInterrupt

    assert _snapshot(test_data_dir) == before


def test_staged_paths_outside_scenarios_are_refused(test_data_dir: Path) -> None:
    with staging.staged_edit(test_data_dir, "retire_alice") as edit:
        with pytest.raises(ValueError, match="not a stageable"):
            edit.path(test_data_dir / "aux" / "anything.csv")


def test_multi_file_edit_commits_every_file(test_data_dir: Path) -> None:
    result = _sim("account", "rename", "Asset_Checking", "Asset_Checking_2",
                  "--scenario", "retire_alice", data_dir=test_data_dir)

    assert result.exit_code == 0, result.output
    scenario_dir = test_data_dir / "scenarios" / "retire_alice"
    for name in ("accounts.csv", "account_values.csv", "scheduled.csv"):
        assert "Asset_Checking_2" in (scenario_dir / name).read_text(), name
    leftovers = [p for p in scenario_dir.rglob("*.tmp")]
    assert not leftovers


def _child_with_transfer(data_dir: Path, **row: str) -> None:
    """Scenario ``child`` of retire_alice, whose calendar.csv adds one transfer."""
    created = _sim("scenario", "create", "child", "retire_alice", data_dir=data_dir)
    assert created.exit_code == 0, created.output
    path = data_dir / "scenarios" / "child" / "calendar.csv"
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.loc[len(df)] = [row.get(column, "") for column in df.columns]
    df.to_csv(path, index=False)


def test_edit_that_breaks_an_inheriting_scenario_is_rejected(test_data_dir: Path) -> None:
    _child_with_transfer(
        test_data_dir, date="2029-03-31", kind="one_off_transfer", account_1="Asset_Savings",
        account_2="Asset_Checking", amount="100000", tax_character="none",
    )
    assert _sim("check", "child", data_dir=test_data_dir).exit_code == 0
    before = _snapshot(test_data_dir)

    # retire_alice itself still loads without Asset_Checking; child doesn't.
    result = _sim("account", "delete", "Asset_Checking", "--scenario", "retire_alice",
                  data_dir=test_data_dir, input="y\n")

    assert result.exit_code != 0
    assert "scenario child:" in result.output
    assert "nothing was written" in result.output
    assert _snapshot(test_data_dir) == before


def test_inheriting_scenario_already_broken_is_reported_not_blamed(
    test_data_dir: Path,
) -> None:
    _child_with_transfer(
        test_data_dir, date="2029-03-31", kind="one_off_transfer", account_1="no_such_account",
        account_2="Asset_Checking", amount="100000", tax_character="none",
    )

    result = _sim("calendar", "add", "set-rate", "2027-03-31", "Asset_Checking", "4",
                  "--scenario", "retire_alice", data_dir=test_data_dir)

    assert result.exit_code == 0, result.output
    assert "WARN: scenario child already failed to load before this edit" in result.output
    calendar = (test_data_dir / "scenarios" / "retire_alice" / "calendar.csv").read_text()
    assert "2027-03-31,set_rate" in calendar


def test_scenario_create_from_broken_parent_creates_nothing(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
) -> None:
    _break_retire_alice(append_calendar_row)

    result = _sim("scenario", "create", "child", "retire_alice", data_dir=test_data_dir)

    assert result.exit_code != 0
    assert "already fails to load" in result.output
    assert not (test_data_dir / "scenarios" / "child").exists()
