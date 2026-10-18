"""Automatic pruning of state/ by the data repo's .retention.yaml (#195)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.retention import (
    DEFAULT_RETENTION,
    RETENTION_FILENAME,
    Retention,
    RetentionError,
    parse_retention,
    plan_prune,
    prune_state,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _rules(text: str, base: Path) -> Retention:
    return parse_retention(text, base=base)


def _planned(retention: Retention, intent_dir: Path, runs_dir: Path) -> set[Path]:
    return {item.path for item in plan_prune(retention, intent_dir, runs_dir, now=NOW)}


def test_parses_the_documented_example(tmp_path: Path) -> None:
    retention = _rules(
        "intents:\n  - keep: 10\n    since: 3 days\n"
        "runs:\n  - keep: 5\n    since: 2 days\n  - keep: 1\n"
        "pins:\n  - state/runs/x.sqlite3\n",
        tmp_path,
    )
    assert retention.intents is not None and retention.runs is not None
    assert [(r.keep, r.since and r.since.days) for r in retention.intents] == [(10, 3)]
    assert [(r.keep, r.since and r.since.days) for r in retention.runs] == [(5, 2), (1, None)]
    assert retention.pins == ((tmp_path / "state/runs/x.sqlite3").resolve(),)


@pytest.mark.parametrize(("text", "message"), [
    ("runs:\n  - keep: 5\n  - since 2 days\n", "runs rule 2: expected keep"),
    ("runs:\n  - keep: 0\n", "keep must be a positive whole number"),
    ("runs:\n  - since: a fortnight\n", "since must look like"),
    ("runs:\n  - kept: 5\n", "unknown key(s): kept"),
    ("run:\n  - keep: 5\n", "unknown section(s): run"),
    ("pins: state/runs/x.sqlite3\n", "pins: expected a list"),
    ("runs: [\n", "not valid YAML"),
])
def test_rejects_malformed_rules(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(RetentionError, match=message.replace("(", r"\(").replace(")", r"\)")):
        _rules(text, tmp_path)


def test_keep_counts_each_scenario_separately(
    fake_state: Callable[..., Path], intent_dir: Path, runs_dir: Path, tmp_path: Path,
) -> None:
    alice = [fake_state("run", "alice", f"2026-10-0{d}T120000Z") for d in range(1, 5)]
    bill = fake_state("run", "bill_two", "2026-09-01T120000Z")
    planned = _planned(_rules("runs:\n  - keep: 2\n", tmp_path), intent_dir, runs_dir)
    assert planned == set(alice[:2])
    assert bill.exists()


def test_either_condition_of_a_rule_keeps_an_item(
    fake_state: Callable[..., Path], intent_dir: Path, runs_dir: Path, tmp_path: Path,
) -> None:
    old = fake_state("run", "alice", "2026-10-01T120000Z")
    second = fake_state("run", "alice", "2026-10-02T120000Z")
    recent = fake_state("run", "alice", "2026-10-17T120000Z")
    fake_state("run", "alice", "2026-10-18T110000Z")
    retention = _rules("runs:\n  - keep: 3\n    since: 3 days\n", tmp_path)
    assert _planned(retention, intent_dir, runs_dir) == {old}
    shorter = _rules("runs:\n  - keep: 1\n    since: 3 days\n", tmp_path)
    assert _planned(shorter, intent_dir, runs_dir) == {old, second}
    assert recent.exists()


def test_the_newest_run_and_the_newest_success_are_always_kept(
    fake_state: Callable[..., Path], intent_dir: Path, runs_dir: Path, tmp_path: Path,
) -> None:
    older = fake_state("run", "alice", "2026-10-01T120000Z")
    succeeded = fake_state("run", "alice", "2026-10-02T120000Z")
    fake_state("run", "alice", "2026-10-03T120000Z", status="failed")
    fake_state("run", "alice", "2026-10-04T120000Z", status="failed")
    planned = _planned(_rules("runs:\n  - since: 1 hour\n", tmp_path), intent_dir, runs_dir)
    assert planned == {older, runs_dir / "2026-10-03T120000Z_alice_abc1234.sqlite3"}
    assert succeeded not in planned


def test_batches_are_pruned_whole_and_an_incomplete_batch_is_no_success(
    fake_state: Callable[..., Path], intent_dir: Path, runs_dir: Path, tmp_path: Path,
) -> None:
    stale = fake_state("batch", "alice", "2026-10-01T120000Z")
    complete = fake_state("batch", "alice", "2026-10-02T120000Z")
    incomplete = fake_state("batch", "alice", "2026-10-03T120000Z", complete=False)
    fake_state("batch", "alice", "2026-10-04T120000Z", status="failed")
    planned = _planned(_rules("runs:\n  - keep: 1\n", tmp_path), intent_dir, runs_dir)
    assert planned == {stale, incomplete}
    assert complete not in planned


def test_pinning_a_member_keeps_its_batch_and_its_intents(
    fake_state: Callable[..., Path], intent_dir: Path, runs_dir: Path, tmp_path: Path,
) -> None:
    pinned_run = fake_state("run", "alice", "2026-09-01T120000Z")
    batch = fake_state("batch", "alice", "2026-09-02T120000Z")
    intents = fake_state("intent", "alice", "2026-09-02T120000Z")
    other_intents = fake_state("intent", "alice", "2026-09-03T120000Z")
    for stamp in ("2026-10-01T120000Z", "2026-10-02T120000Z"):
        fake_state("run", "alice", stamp)
        fake_state("batch", "alice", stamp)
        fake_state("intent", "alice", stamp)
    retention = _rules(
        "intents:\n  - keep: 1\nruns:\n  - keep: 1\n"
        f"pins:\n  - {pinned_run.relative_to(tmp_path)}\n"
        f"  - {(batch / 'member_0001.sqlite3').relative_to(tmp_path)}\n",
        tmp_path,
    )
    planned = _planned(retention, intent_dir, runs_dir)
    assert {pinned_run, batch, intents}.isdisjoint(planned)
    assert other_intents in planned


def test_a_missing_or_empty_section_keeps_everything(
    fake_state: Callable[..., Path], intent_dir: Path, runs_dir: Path, tmp_path: Path,
) -> None:
    for d in range(1, 4):
        fake_state("run", "alice", f"2026-10-0{d}T120000Z")
        fake_state("intent", "alice", f"2026-10-0{d}T120000Z")
    assert _planned(_rules("runs:\n", tmp_path), intent_dir, runs_dir) == set()
    assert _planned(_rules("", tmp_path), intent_dir, runs_dir) == set()


def test_prune_state_deletes_and_leaves_unrecognized_files(
    fake_state: Callable[..., Path], test_data_dir: Path, intent_dir: Path, runs_dir: Path,
) -> None:
    (test_data_dir / RETENTION_FILENAME).write_text(
        "intents:\n  - keep: 1\nruns:\n  - keep: 1\n"
    )
    old_run = fake_state("run", "alice", "2026-10-01T120000Z")
    fake_state("run", "alice", "2026-10-02T120000Z")
    old_batch = fake_state("batch", "bill", "2026-10-01T120000Z")
    fake_state("batch", "bill", "2026-10-02T120000Z")
    old_intents = fake_state("intent", "bill", "2026-10-01T120000Z")
    fake_state("intent", "bill", "2026-10-02T120000Z")
    base_case = intent_dir / "bill" / "base_case.sqlite3"
    base_case.write_bytes(b"")
    keepsake = runs_dir / "keepsake.sqlite3"
    keepsake.write_bytes(b"")

    result = prune_state(test_data_dir, intent_dir, runs_dir, now=NOW)

    assert (result.runs, result.batches, result.intents) == ([old_run], [old_batch], [old_intents])
    assert not old_run.exists() and not old_batch.exists() and not old_intents.exists()
    assert base_case.exists() and keepsake.exists()
    assert result.summary() == (
        "pruned 1 run file(s), 1 run batch(es), 1 intent batch(es) (.retention.yaml)"
    )


def test_a_missing_retention_file_is_created_with_the_defaults(
    fake_state: Callable[..., Path], test_data_dir: Path, intent_dir: Path, runs_dir: Path,
) -> None:
    path = test_data_dir / RETENTION_FILENAME
    path.unlink()
    stamps = [f"2026-09-0{d}T120000Z" for d in range(1, 8)]
    runs = [fake_state("run", "alice", stamp) for stamp in stamps]
    result = prune_state(test_data_dir, intent_dir, runs_dir, now=NOW)
    assert result.created == path
    assert path.read_text() == DEFAULT_RETENTION
    assert sorted(result.runs) == runs[:2]  # five newest kept; none within 7 days


def test_sim_run_prunes_after_running(
    fake_state: Callable[..., Path], test_data_dir: Path, runs_dir: Path,
) -> None:
    (test_data_dir / RETENTION_FILENAME).write_text("runs:\n  - keep: 1\n")
    old = fake_state("run", "retire_alice", "2026-01-01T120000Z")
    result = CliRunner().invoke(main, [
        "sim", "run", "retire_alice", "--data-dir", str(test_data_dir), "--allow-dirty-data",
    ])
    assert result.exit_code == 0, result.output
    assert "pruned 1 run file(s) (.retention.yaml)" in result.output
    assert not old.exists()
    assert len(list(runs_dir.glob("*_retire_alice_*.sqlite3"))) == 1


def test_sim_load_warns_of_a_bad_retention_file_and_still_succeeds(
    fake_state: Callable[..., Path], test_data_dir: Path,
) -> None:
    (test_data_dir / RETENTION_FILENAME).write_text("runs:\n  - since 2 days\n")
    old = fake_state("run", "retire_alice", "2026-01-01T120000Z")
    fake_state("run", "retire_alice", "2026-01-02T120000Z")
    result = CliRunner().invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--allow-dirty-data", "--no-run",
    ])
    assert result.exit_code == 0, result.output
    assert "runs rule 1: expected keep" in result.output
    assert "nothing pruned" in result.output
    assert old.exists()


def test_the_shipped_files_are_the_defaults(prototype_data: Path, repo_root: Path) -> None:
    for folder in (prototype_data, repo_root / "templates"):
        assert (folder / RETENTION_FILENAME).read_text() == DEFAULT_RETENTION
    retention = parse_retention(DEFAULT_RETENTION, base=repo_root)
    assert retention.intents == retention.runs
    assert retention.runs is not None
    assert [(r.keep, r.since and r.since.days) for r in retention.runs] == [(5, 7)]
    assert retention.pins == ()
