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
from analysis.sim import registries, staging
from analysis.sim.cli import _append_csv_row


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


def test_preflight_loader_crash_is_a_clean_error(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(staging, "load_scenario", crash)
    before = _snapshot(test_data_dir)

    result = _sim("calendar", "add", "set-rate", "2027-03-31", "Asset_Checking", "4",
                  "--scenario", "retire_alice", data_dir=test_data_dir)

    assert isinstance(result.exception, SystemExit), result.exception
    assert "loader failed: RuntimeError: boom" in result.output
    assert "already fails to load" in result.output
    assert _snapshot(test_data_dir) == before


def _add_rate_row(calendar: Path) -> None:
    _append_csv_row(calendar, registries.CALENDAR_HEADER,
                    {"date": "2027-03-31", "kind": "set_rate", "series": "Asset_Checking",
                     "amount": "40000"})


def test_concurrent_change_to_a_file_being_written_refuses_the_edit(test_data_dir: Path) -> None:
    calendar = test_data_dir / "scenarios" / "retire_alice" / "calendar.csv"

    with pytest.raises(staging.ConcurrentEdit, match="calendar.csv"):
        with staging.staged_edit(test_data_dir, "retire_alice") as edit:
            _add_rate_row(edit.path(calendar))
            calendar.write_text(calendar.read_text() + "\n")  # another edit lands first

    assert "2027-03-31,set_rate" not in calendar.read_text()


def test_concurrent_change_to_another_file_is_kept(test_data_dir: Path) -> None:
    """Only files this edit wrote go back; the stale staged copy of a file
    someone else changed meanwhile must not overwrite theirs."""
    scenario_dir = test_data_dir / "scenarios" / "retire_alice"
    result_text = "# concurrently changed\n"

    with staging.staged_edit(test_data_dir, "retire_alice") as edit:
        _add_rate_row(edit.path(scenario_dir / "calendar.csv"))
        readme = scenario_dir / "README.md"
        readme.write_text(result_text)

    assert readme.read_text() == result_text
    assert "2027-03-31,set_rate" in (scenario_dir / "calendar.csv").read_text()


def test_failed_commit_rolls_back_files_already_swapped(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(test_data_dir)
    real_replace = staging.os.replace
    swaps = []

    def failing_replace(src, dst):
        if str(src).endswith(".tmp"):
            swaps.append(dst)
            if len(swaps) == 2:
                raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(staging.os, "replace", failing_replace)
    result = _sim("account", "rename", "Asset_Checking", "Asset_Checking_2",
                  "--scenario", "retire_alice", data_dir=test_data_dir)

    assert result.exit_code != 0
    assert len(swaps) == 2
    assert _snapshot(test_data_dir) == before
    scenario_dir = test_data_dir / "scenarios" / "retire_alice"
    assert not [p for p in scenario_dir.rglob(".*") if p.suffix in {".tmp", ".bak"}]


def test_delete_investment_account_drops_its_growth_rate_rows(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
) -> None:
    """An investment account's leaf chain is '<account>:<lot>', so its own
    growth rule must be recognized by account, not by chain name."""
    append_calendar_row(date="2027-01-31", kind="set_rate", series="brokerage_growth",
                        amount="60000")

    result = _sim("account", "delete", "Asset_Brokerage_Joint", "--scenario", "retire_alice",
                  data_dir=test_data_dir, input="y\n")

    assert result.exit_code == 0, result.output
    assert "series (its growth rate)" in result.output
    calendar = pd.read_csv(test_data_dir / "scenarios" / "retire_alice" / "calendar.csv",
                           dtype=str, keep_default_na=False)
    assert "brokerage_growth" not in set(calendar["series"])
