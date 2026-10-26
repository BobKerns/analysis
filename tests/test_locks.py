"""#300: run-file names and per-scenario build locks."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from analysis.sim import locks
from analysis.sim.loader import latest_ensemble_batch, latest_run_path
from analysis.sim.retention import _run_items

NOW = datetime(2026, 10, 26, 4, 30, 0, tzinfo=UTC)


def test_runs_in_the_same_second_get_numbered_names(runs_dir: Path) -> None:
    paths = [locks.reserve_run_path(runs_dir, "base", "abc1234", now=NOW) for _ in range(3)]
    assert [p.name for p in paths] == [
        "2026-10-26T043000Z_base_abc1234.sqlite3",
        "2026-10-26T043000Z-2_base_abc1234.sqlite3",
        "2026-10-26T043000Z-3_base_abc1234.sqlite3",
    ]
    assert all(p.is_file() and p.stat().st_size == 0 for p in paths)


def _written(path: Path) -> Path:
    path.write_bytes(b"x")  # a run that has begun writing
    return path


def test_latest_run_is_the_last_numbered_one(runs_dir: Path) -> None:
    for _ in range(3):
        last = _written(locks.reserve_run_path(runs_dir, "base", "abc1234", now=NOW))
    # Plain name order would put `…Z-2_` before `…Z_`.
    assert latest_run_path(runs_dir, "base") == last
    assert latest_run_path(runs_dir, "base") != sorted(runs_dir.iterdir())[-1]


def test_tenth_run_in_a_second_is_newer_than_the_ninth(runs_dir: Path) -> None:
    for _ in range(10):
        last = _written(locks.reserve_run_path(runs_dir, "base", "abc1234", now=NOW))
    assert last.name.startswith("2026-10-26T043000Z-10_")
    assert latest_run_path(runs_dir, "base") == last


def test_a_reserved_run_is_not_the_latest_until_written(runs_dir: Path) -> None:
    first = _written(locks.reserve_run_path(runs_dir, "base", "abc1234", now=NOW))
    reserved = locks.reserve_run_path(runs_dir, "base", "abc1234", now=NOW)
    assert latest_run_path(runs_dir, "base") == first
    _written(reserved)
    assert latest_run_path(runs_dir, "base") == reserved


def test_batches_order_by_their_number(runs_dir: Path) -> None:
    for name in ("2026-10-26T043000Z-9", "2026-10-26T043000Z-10"):
        (runs_dir / "base" / name).mkdir(parents=True)
        (runs_dir / "base" / name / "batch_manifest.json").write_text("{}")
    batch = latest_ensemble_batch(runs_dir / "base")
    assert batch is not None and batch.name == "2026-10-26T043000Z-10"


def test_retention_reads_a_numbered_run_name(runs_dir: Path) -> None:
    locks.reserve_run_path(runs_dir, "higher_base", "abc1234", now=NOW)
    locks.reserve_run_path(runs_dir, "higher_base", "abc1234", now=NOW)
    items = sorted(_run_items(runs_dir), key=lambda it: it.order)
    assert [(it.scenario, it.order) for it in items] == [
        ("higher_base", ("2026-10-26T043000Z", 1)),
        ("higher_base", ("2026-10-26T043000Z", 2)),
    ]


def test_build_lock_is_reentrant(intent_dir: Path) -> None:
    seen: list[locks.LockHolder | None] = []
    # `sim load` holds it and invokes `sim run`, which takes it again.
    with locks.build_lock(intent_dir, "base"):
        with locks.build_lock(intent_dir, "base", on_wait=seen.append):
            pass
    assert seen == []
    assert locks.lock_holder(intent_dir, "base") is None


_HOLD = """
import sys, time
from pathlib import Path
from analysis.sim.locks import build_lock
with build_lock(Path(sys.argv[1]), "base"):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


@pytest.fixture
def held_elsewhere(intent_dir: Path):
    """Another process holding ``base``'s build lock for about a second."""
    proc = subprocess.Popen([sys.executable, "-c", _HOLD, str(intent_dir), "1.0"],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    yield proc
    proc.wait(timeout=10)


@pytest.mark.slow
def test_a_second_build_waits_for_the_first(intent_dir: Path, held_elsewhere) -> None:
    holder = locks.lock_holder(intent_dir, "base")
    assert holder is not None and holder.pid == held_elsewhere.pid and holder.by == "manual"
    seen: list[locks.LockHolder | None] = []
    started = time.monotonic()
    with locks.build_lock(intent_dir, "base", on_wait=seen.append):
        waited = time.monotonic() - started
    assert waited > 0.3
    assert len(seen) == 1 and seen[0] is not None and seen[0].pid == held_elsewhere.pid
    # Other scenarios never wait on it.
    with locks.build_lock(intent_dir, "retire_alice", on_wait=seen.append):
        pass
    assert len(seen) == 1


def test_listing_ensembles_never_waits_for_their_builds(
    runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only opening a recipe's batch waits for its background build; the
    notebooks' recipe lists and controls read what is on hand."""
    from analysis.sim import loader
    from analysis.sim.notebook_ui.ensemble_widgets import (
        RecipeSeeds, list_ensemble_recipes, recipe_accounts,
    )
    from analysis.sim.notebook_ui.sensitivity_widgets import list_sensitivity_recipes

    batch = runs_dir / "base" / "2026-10-26T043000Z"
    batch.mkdir(parents=True)
    (batch / "batch_manifest.json").write_text('{"mode": "monte_carlo"}')
    waited: list[str] = []
    monkeypatch.setattr(loader, "_await_fresh", lambda _runs, name: waited.append(name))
    assert list_ensemble_recipes(runs_dir) == ["base"]
    assert list_sensitivity_recipes(runs_dir) == ["base"]
    recipe_accounts(runs_dir, ["base"])
    RecipeSeeds(runs_dir, ["base"])
    assert loader.scenario_run_path(runs_dir, "base", wait=False) is None
    assert waited == []
    loader.latest_ensemble_batch(runs_dir / "base")
    assert waited == ["base"]  # opening one does
