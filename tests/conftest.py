"""Test-wide safety net: never let a forgotten override touch real user data.

Without this, a test that omits ``--data-dir`` (or a matching ``monkeypatch``)
silently falls back to whatever ``$ANALYSIS_DATA_DIR`` happens to be set to in
the *ambient* shell — which, on a dev machine with the data repo activated,
is the developer's real, personal data directory. That happened: a test run
overwrote the real data repo's ``.current_scenario`` with a prototype
scenario name. Clearing these here makes any such test fail loudly with
"ANALYSIS_DATA_DIR not set" instead of quietly mutating real data.

Fixture pattern for filesystem test data
-----------------------------------------
Any test data that lives on disk (a data repo, a scenario, an intent/run
directory, a DB connection opened against one of those) is provided by a
fixture, never by a helper function a test calls directly. All setup that
touches the filesystem — ``mkdir``, ``shutil.copytree``, ``scenario.create_scenario``,
opening a connection — happens inside the fixture body, so pytest owns the
setup/teardown and the dependency graph, and other test files can reuse the
same fixture instead of rolling their own helper.

Scope follows mutability, not convenience:

- If a fixture is read-only and depends only on session-scoped fixtures, make
  it ``session``-scoped so its (possibly expensive) setup runs once. E.g.
  ``data_dir`` copies ``prototype_data`` into a ``tmp_path_factory`` dir once
  per session; ``scenarios_dir``, ``retire_alice_scenario`` and
  ``variant_a_scenario_create`` build on it read-only.
- If a test needs to mutate the data (e.g. add a tag to an account before
  running), it must not share the session copy with every other test. Use a
  function-scoped fixture built on the function-scoped ``tmp_path`` instead.
  This is why two parallel families exist: ``data_dir``/``scenarios_dir``/
  ``retire_alice_scenario`` (session, shared, read-only) vs. ``test_data_dir``/
  ``test_scenarios_dir``/``test_retire_alice_scenario``/
  ``test_retire_alice_scenario_ref`` (function, private copy, mutation-safe).
  Anything downstream of a function-scoped fixture (``state_dir``,
  ``intent_dir``, ``runs_dir``, and everything built from them) is
  necessarily function-scoped too — pytest forbids a session fixture
  depending on a narrower one.

Fixtures can't take arguments, so a fixture parameterized by name (e.g. "give
me a ``ScenarioRef`` for scenario X") is written as a fixture that returns a
*callable* closing over its own dependencies — see ``scenario_ref`` and
``run_path``. Named per-scenario fixtures (``retire_alice_scenario_ref``,
``variant_a_scenario_ref``, ...) then wrap that callable so tests can inject
"the retire_alice ref" by name and so further fixtures can depend on it
directly.

Setup is layered one step at a time — ref -> intent file -> run file ->
connection (``retire_alice_scenario_ref`` -> ``retire_alice_intent_file`` ->
``retire_alice_run_file`` -> ``retire_alice_run``) — so a test pulls in only
as much setup as it needs by picking the right fixture name, and pytest
caches each layer per test. ``load_from_ref``/``run_from_ref`` are plain
functions, but they exist only to be called from inside a fixture body, not
from a test directly — if you find a test importing a helper to set up its
own data, that setup belongs in a fixture instead.
"""

from __future__ import annotations
from collections.abc import Callable, Generator
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import warnings

import git
import pandas as pd
import pytest

from analysis.sim import scenario
from analysis.sim.connection import RunConnection, ScenarioConnection, open_scenario, open_run
from analysis.sim.engine import run_scenario
from analysis.sim.ensemble import resolve_member
from analysis.sim.schema import apply_run_schema
from analysis.sim.loader import LoadResult, ScenarioRef, load_scenario, resolve_scenario_name
from analysis.sim.notebook_ui.pickers import MultiScenarioConnections, multi_scenario_picker
from analysis.sim.provenance import CodeStamps
from analysis.sim.registries import (
    ACCOUNT_LOTS_CSV_HEADER, CALENDAR_HEADER, GUARD_RAILS_CSV_HEADER, SCENARIO_HEADER,
    SCHEDULED_CSV_HEADER,
)

_ANALYSIS_ENV_VARS = (
    "ANALYSIS_DATA_DIR",
    "ANALYSIS_SCENARIO",
)


@pytest.fixture(autouse=True)
def _no_ambient_analysis_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    for name in _ANALYSIS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Without $ANALYSIS_DATA_DIR the notebook widgets fall back to the
    # checkout's data/shared/sim: on a dev machine, the real data repo,
    # whose disabled scenarios once hid a test's recipe (#401).
    monkeypatch.setattr(
        "analysis.sim.notebook_ui.pickers._checkout_data_dir",
        lambda: tmp_path_factory.getbasetemp() / "no-checkout-data" / "sim")
    # No test starts or waits on a background builder (#300) unless it
    # turns the builder back on itself.
    monkeypatch.setenv("ANALYSIS_BUILDER", "off")
    monkeypatch.delenv("ANALYSIS_BUILDER_CHILD", raising=False)


_STABLE_CODE_STAMPS = CodeStamps(version="0" * 40, dirty=False, repo_path="CODE_REPO")


@pytest.fixture(scope="session", autouse=True)
def _stable_code_stamps() -> Generator[None]:
    """Stamp runs with a fixed code version instead of asking git.

    ``provenance.code_stamps`` runs ``git status`` on this repo for every load
    and run, so a test's result and its speed would depend on the state of the
    working tree (a large uncommitted notebook made the suite ~10x slower).
    Patched once per session, since nothing a test does can change the stub.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name in ("analysis.sim.runs.code_stamps", "analysis.sim.loader.code_stamps"):
            patch.setattr(name, lambda: _STABLE_CODE_STAMPS)
        yield


@pytest.fixture(scope="session", autouse=True)
def _no_sqlite_fsync() -> Generator[None]:
    """Open every SQLite database without fsync or an on-disk rollback journal.

    Each commit otherwise waits for an fsync and creates and deletes a
    journal file, and the suite commits thousands of times into throwaway
    temp dirs: on a disk-backed ``/tmp`` (CI, cloud containers) that was
    about 45% of the suite's wall time. A test database never needs to
    survive a crash, and rollback still works with the journal in memory.
    """
    connect = sqlite3.connect

    def _connect(*args, **kwargs) -> sqlite3.Connection:
        con = connect(*args, **kwargs)
        con.execute("PRAGMA synchronous = OFF")
        con.execute("PRAGMA journal_mode = MEMORY")
        return con

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, "connect", _connect)
        yield


@pytest.fixture(autouse=True)
def _no_current_scenario_writes(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make the ``sim`` CLI's ``write_current_scenario`` a no-op by retire_alice.

    It writes into whatever ``--data-dir`` points at — real data repo,
    activated data repo, or the tracked ``prototype_data/`` fixture — none of
    which a test should ever mutate. Tests that specifically verify the
    recording behavior opt back in with
    ``@pytest.mark.uses_current_scenario_write``.
    """
    if request.node.get_closest_marker("uses_current_scenario_write"):
        return
    monkeypatch.setattr("analysis.sim.cli.write_current_scenario", lambda *args, **kwargs: None)
    # And its notice, which would otherwise announce a switch that never happens.
    monkeypatch.setattr("analysis.sim.cli._make_current", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _no_working_set_writes(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make writing the working set (#322) a no-op, for the reason above:
    any command naming a scenario, any edit and any notebook pick writes it
    into the ``state/`` beside the data dir. Tests of the working set opt back in with
    ``@pytest.mark.uses_working_set_write``."""
    if request.node.get_closest_marker("uses_working_set_write"):
        return
    monkeypatch.setattr("analysis.sim.working_set._write", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _one_cpu_for_default_jobs(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve ``sim run``'s default ``--jobs`` to 1, so batches run in-process.

    A pool worker starts a fresh interpreter and re-imports the package
    (~1 s each); a test batch's members take a fraction of that, so the
    pool made the suite slower, not faster. Parallel execution is covered
    by the tests that pass ``--jobs 2`` explicitly, and the default's own
    resolution by a test marked ``@pytest.mark.uses_real_cpu_count``.
    """
    if request.node.get_closest_marker("uses_real_cpu_count"):
        return
    monkeypatch.setattr("analysis.sim.cli._available_cpus", lambda: 1)


@pytest.fixture(autouse=True)
def _negative_balance_is_an_error(request: pytest.FixtureRequest) -> Generator[None]:
    """Promote engine.py's "account went negative" RuntimeWarning to a hard
    error for every test.

    An account going negative after exhausting its refill chain almost
    always means a test's fixture data is under-funded for whatever it's
    exercising, not a deliberate outcome -- better to fail loudly at the
    test that caused it than let it slip into the warnings summary at the
    end of the run, one of a pile easy to stop reading closely. A test that
    genuinely wants the old warn-and-continue behavior (verifying the
    warning itself, or deliberately modeling a shortfall) opts back in with
    ``@pytest.mark.allow_negative_balance``. This is a test-suite-only
    policy -- ``engine.py`` itself is untouched, so a real ``sim run``
    still only warns.
    """
    if request.node.get_closest_marker("allow_negative_balance"):
        yield
        return
    with warnings.catch_warnings():
        warnings.filterwarnings("error", category=RuntimeWarning, message=".*went negative.*")
        yield


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory, prototype_data: Path) -> Path:
    """
    Create and populate a temporary data directory for tests.

    This includes the retire_alice scenario data used in tests.
    """
    data_dir = tmp_path_factory.mktemp("data")
    shutil.copytree(prototype_data, data_dir, dirs_exist_ok=True)
    return data_dir


@pytest.fixture(scope="function")
def test_data_dir(tmp_path, prototype_data: Path) -> Path:
    """
    Create and populate a temporary data directory for tests, per test.

    This includes the retire_alice scenario data used in tests.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(prototype_data, data_dir, dirs_exist_ok=True)
    return data_dir

@pytest.fixture(scope="session")
def scenarios_dir(data_dir: Path) -> Path:
    """Create a temporary scenario directory within the temporary data directory for tests."""
    scenarios_dir = data_dir / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    return scenarios_dir


@pytest.fixture(scope="function")
def test_scenarios_dir(test_data_dir: Path) -> Path:
    """Create a temporary scenario directory within the temporary data directory for tests."""
    scenarios_dir = test_data_dir / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    return scenarios_dir


@pytest.fixture(scope="function")
def template_data_dir(tmp_path: Path, repo_root: Path) -> Path:
    """A private data repo built as ``scripts/link-data.sh --template``
    builds one: ``templates/`` copied whole, less its ``t_`` template
    scenarios, which the loader reads from the code repository instead."""
    from analysis.sim.lineage import is_template_name

    data_dir = tmp_path / "sim"
    shutil.copytree(repo_root / "templates", data_dir,
                    ignore=lambda folder, names: [n for n in names if is_template_name(n)]
                    if Path(folder).name == "scenarios" else [])
    return data_dir


@pytest.fixture(scope="function")
def local_templates(test_scenarios_dir: Path) -> Path:
    """The private data copy with the code repository's template scenarios
    copied into its own ``scenarios/``, so they're ordinary, editable
    scenarios there (a data repo's folder replaces a template whole)."""
    from analysis.sim.lineage import TEMPLATE_SCENARIOS_DIR, is_template_name

    for folder in TEMPLATE_SCENARIOS_DIR.iterdir():
        if folder.is_dir() and is_template_name(folder.name):
            shutil.copytree(folder, test_scenarios_dir / folder.name)
    return test_scenarios_dir


@pytest.fixture(scope="function")
def empty_scenarios_dir(test_scenarios_dir: Path) -> Path:
    """The private data copy's ``scenarios/``, emptied: a data repo with no
    scenarios yet. Remove the folder itself for one with no ``scenarios/``."""
    shutil.rmtree(test_scenarios_dir)
    test_scenarios_dir.mkdir()
    return test_scenarios_dir

@pytest.fixture(scope="function")
def state_dir(tmp_path: Path) -> Generator[Path]:
    """Create a temporary state directory within the temporary data directory for tests."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield state_dir
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


@pytest.fixture(scope="function")
def intent_dir(state_dir: Path) -> Path:
    """Create a temporary intent directory within the temporary data directory for tests."""
    intent_dir = state_dir / "intent"
    intent_dir.mkdir(parents=True, exist_ok=True)
    return intent_dir


@pytest.fixture(scope="function")
def runs_dir(state_dir: Path) -> Path:
    """Create a temporary runs directory within the temporary state directory for tests."""
    runs_dir = state_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir


@pytest.fixture(scope="function")
def scenario_ref(data_dir: Path,
                 intent_dir: Path,
                 runs_dir: Path,
                 scenarios_dir: Path) -> Callable[..., ScenarioRef]:
    """Fixture to provide a callable that returns a ScenarioRef for a given scenario name."""   
    def _scenario_ref(scenario: str,
                      data_dir: Path = data_dir,
                      scenarios_dir: Path = scenarios_dir) -> ScenarioRef:
        return resolve_scenario_name(data_dir, scenario,
                                     intent_dir=intent_dir,
                                     scenarios_dir=scenarios_dir,
                                     runs_dir=runs_dir)
    return _scenario_ref

@pytest.fixture(scope="function")
def run_path(runs_dir: Path) -> Callable[[str], Path]:
    """
    Fixture to provide a callable that returns the run path for a given scenario.
    
    This closes over the appropriate runs directory for the test.
    """
    def _run_path(scenario: str) -> Path:
        return runs_dir / f"test_{scenario}_run.sqlite3"
    return _run_path


def load_from_ref(scenario_ref: ScenarioRef) -> Path:
    """Utility function to load and get the intent path from a ScenarioRef."""
    result: LoadResult = load_scenario(scenario_ref.data_dir, scenario_ref.name,
                                       intent_path=scenario_ref.intent_path,
                                       allow_dirty_data=True)       
    return result.intent_path


def run_from_ref(scenario_ref: ScenarioRef, *,
                 intent_file: Path | None = None,
                 run_path: Path | None = None) -> Path:
    """
    Utility function to run a scenario and get the run path from a ScenarioRef.
    
    The scenario is expected to have been loaded, and this function
    returns the path to the resulting run file.

    The intent_file parameter will ordinarily be the same as scenario_ref.intent_path.
    Specifying it specifically lets you get it from the fixture that causes it to be
    loaded by the corresponding load fixture, allowing for a more clear pattern
    of dependency between loading and running the scenario.
    """
    intent_file = intent_file or scenario_ref.intent_path
    run_path = (
        run_path
        or scenario_ref.run_path
        or scenario_ref.runs_dir / f"test_{scenario_ref.name}_run.sqlite3"
    )
    return run_scenario(intent_file, run_path,
                        allow_dirty_data=True,
                        allow_dirty_code=True,
                        run_by="TEST",
                        )


@pytest.fixture(scope="function")
def retire_alice_intent_file(retire_alice_scenario_ref: ScenarioRef) -> Path:
    """Fixture to load and get the intent path for the retire_alice scenario."""
    return load_from_ref(retire_alice_scenario_ref)


@pytest.fixture(scope="function")
def variant_a_intent_file(variant_a_scenario_ref: ScenarioRef) -> Path:
    """Fixture to load and get the intent path for the variant_a scenario."""
    return load_from_ref(variant_a_scenario_ref)


@pytest.fixture(scope="function")
def retire_alice_run_file(retire_alice_scenario_ref: ScenarioRef,
                          retire_alice_intent_file: Path) -> Path:
    """Fixture to load and run the retire_alice scenario."""
    return run_from_ref(retire_alice_scenario_ref,
                        intent_file=retire_alice_intent_file)


@pytest.fixture(scope="function")
def variant_a_run_file(variant_a_scenario_ref: ScenarioRef,
                       variant_a_intent_file: Path) -> Path:
    """Fixture to load and run the variant_a scenario."""
    return run_from_ref(variant_a_scenario_ref,
                        intent_file=variant_a_intent_file)


@pytest.fixture(scope="function")
def variant_a_scenario_ref(scenario_ref: Callable[[str], ScenarioRef],
                           variant_a_scenario_create: Path) -> ScenarioRef:
    """Fixture to provide the ScenarioRef for the variant_a scenario."""
    return scenario_ref("variant_a")


@pytest.fixture(scope="function")
def retire_alice_scenario_ref(scenario_ref: Callable[[str], ScenarioRef]) -> ScenarioRef:
    """Fixture to provide the ScenarioRef for the retire_alice scenario."""
    return scenario_ref("retire_alice")


@pytest.fixture(scope="function")
def test_retire_alice_scenario_ref(test_data_dir: Path,
                                   test_scenarios_dir: Path,
                                   scenario_ref: Callable[..., ScenarioRef]) -> ScenarioRef:
    """Function fixture to provide the ScenarioRef for the retire_alice scenario."""
    return scenario_ref("retire_alice",
                        data_dir=test_data_dir,
                        scenarios_dir=test_scenarios_dir)


@pytest.fixture(scope="function")
def retire_alice_run_path(run_path: Callable[[str], Path]) -> Path:
    """Fixture to provide the run path for the retire_alice scenario."""
    return run_path("retire_alice")


@pytest.fixture(scope="function")
def variant_a_run_path(run_path: Callable[[str], Path]) -> Path:
    """Fixture to provide the run path for the variant_a scenario."""
    return run_path("variant_a")


@pytest.fixture(scope="function")
def retire_alice_run(scenarios_dir: Path,
                     retire_alice_scenario: ScenarioConnection,
                     retire_alice_run_file: Path) -> Generator[RunConnection]:
    """Fixture to provide the RunConnection for the retire_alice scenario."""
    with open_run(retire_alice_run_file, retire_alice_scenario,
                  scenarios_dir=scenarios_dir) as r:
        yield r


@pytest.fixture(scope="function")
def variant_a_run(scenarios_dir: Path,
                  variant_a_scenario: ScenarioConnection,
                  variant_a_run_file: Path) -> Generator[RunConnection]:
    """Fixture to provide the RunConnection for the variant_a scenario."""
    with open_run(variant_a_run_file, variant_a_scenario,
                  scenarios_dir=scenarios_dir) as r:
        yield r


@pytest.fixture(scope="session")
def retire_alice_scenario(data_dir: Path) -> Generator[ScenarioConnection]:
    """Fixture to provide the ScenarioConnection for the retire_alice scenario."""
    with open_scenario(data_dir, "retire_alice") as conn:
        yield conn


@pytest.fixture(scope="function")
def test_retire_alice_scenario(test_data_dir: Path) -> Generator[ScenarioConnection]:
    """Fixture to provide the ScenarioConnection for the retire_alice scenario."""
    with open_scenario(test_data_dir, "retire_alice") as conn:
        yield conn


@pytest.fixture(scope="function")
def variant_a_scenario(data_dir: Path,
                       variant_a_scenario_create: Path,
                       ) -> Generator[ScenarioConnection]:
    """Fixture to provide the ScenarioConnection for the variant_a scenario."""
    with open_scenario(data_dir, "variant_a") as conn:
        yield conn


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Fixture to provide the root path of the repository."""
    return Path(__file__).parent.parent

@pytest.fixture(scope="session")
def prototype_data(repo_root: Path) -> Path:
    """Fixture to provide the path to the prototype directory within the repository."""
    return repo_root / "prototype_data"


@pytest.fixture(scope="function")
def videos_root(tmp_path: Path, repo_root: Path) -> Path:
    """A private docs/videos: the repo's theme, assets and YouTube settings, no videos yet.

    Tests write a video folder (``videos_root / "01-x" / "script.md"``) into it.
    """
    root = tmp_path / "videos"
    for part in ("theme", "assets"):
        shutil.copytree(repo_root / "docs" / "videos" / part, root / part)
    shutil.copy(repo_root / "docs" / "videos" / "youtube.toml", root / "youtube.toml")
    return root


@pytest.fixture(scope="session")
def variant_a_scenario_create(data_dir: Path) -> Path:
    """Fixture to provide the path to the variant_a scenario within the data directory."""
    return scenario.create_scenario(data_dir, "variant_a", "retire_alice")

@pytest.fixture(scope="function")
def test_child_scenario(test_data_dir: Path) -> Path:
    """A ``child`` scenario deriving from the private retire_alice copy, with
    empty overlays: everything it has, it inherits."""
    return scenario.create_scenario(test_data_dir, "child", "retire_alice")


@pytest.fixture(scope="function")
def test_grandchild_scenario(test_child_scenario: Path, test_data_dir: Path) -> Path:
    """A ``grandchild`` scenario deriving from ``test_child_scenario``."""
    return scenario.create_scenario(test_data_dir, "grandchild", "child")


@pytest.fixture(scope="function")
def two_lot_run(two_lot_run_file: Path, test_data_dir: Path) -> Generator[RunConnection]:
    """Connection to the ``two_lot_run_file`` run."""
    with open_scenario(test_data_dir, "retire_alice") as scenario_conn:
        with open_run(two_lot_run_file, scenario_conn,
                      scenarios_dir=test_data_dir / "scenarios") as run:
            yield run


@pytest.fixture(scope="function")
def report_run(report_run_file: Path, test_data_dir: Path) -> Generator[RunConnection]:
    """Connection to the ``report_run_file`` run."""
    with open_scenario(test_data_dir, "retire_alice") as scenario_conn:
        with open_run(report_run_file, scenario_conn,
                      scenarios_dir=test_data_dir / "scenarios") as run:
            yield run


@pytest.fixture(scope="function")
def lot_variant_runs(
    two_lot_run_file: Path, test_data_dir: Path, test_scenarios_dir: Path,
    intent_dir: Path, runs_dir: Path,
) -> Generator[tuple[RunConnection, dict[str, RunConnection]]]:
    """Runs of ``retire_alice`` (its brokerage split into lots a and b) and of a
    derived ``variant`` that changes lot b's quantity, balance and basis, adds a
    lot c, and changes one inherited year-to-date row."""
    scenario.create_scenario(test_data_dir, "variant", "retire_alice")
    header = ",".join(ACCOUNT_LOTS_CSV_HEADER)
    variant_dir = test_scenarios_dir / "variant"
    (variant_dir / "account_lots.csv").write_text(
        f"{header}\n"
        "Asset_Brokerage_Joint,b,,BBB,2026-01-15,1500,,150000.00,100000.00,\n"
        "Asset_Brokerage_Joint,c,,CCC,2027-06-30,,,25000.00,20000.00,\n"
    )
    (variant_dir / "year_to_date.csv").write_text(
        "name,from_account,from_lot,to_account,to_lot,amount,tax_character,year,description\n"
        "prior_ordinary_wages,Income_Salary_Alice,,Asset_Checking,,70000.00,ordinary,,\n"
    )
    loaded = load_scenario(test_data_dir, "variant", intent_dir=intent_dir,
                           scenarios_dir=test_scenarios_dir, allow_dirty_data=True)
    variant_run = run_scenario(loaded.intent_path, runs_dir / "variant.sqlite3", run_by="TEST",
                               allow_dirty_data=True, allow_dirty_code=True)
    scenarios_dir = test_data_dir / "scenarios"
    with open_run(two_lot_run_file, "retire_alice", data_dir=test_data_dir,
                  scenarios_dir=scenarios_dir) as base:
        with open_run(variant_run, "variant", data_dir=test_data_dir,
                      scenarios_dir=scenarios_dir) as variant:
            yield base, {"variant": variant}


@pytest.fixture(scope="function")
def empty_scenario_runs(
    test_data_dir: Path, test_scenarios_dir: Path, intent_dir: Path, runs_dir: Path,
) -> Generator[tuple[RunConnection, RunConnection]]:
    """Runs of ``retire_alice`` and of ``empty``, a child of ``base`` with no
    accounts or transactions: no balance or net-worth rows at all (#174)."""
    scenario.create_scenario(test_data_dir, "empty", "base")
    runs = {}
    for name in ("retire_alice", "empty"):
        loaded = load_scenario(test_data_dir, name, intent_dir=intent_dir,
                               scenarios_dir=test_scenarios_dir, allow_dirty_data=True)
        runs[name] = run_scenario(loaded.intent_path, runs_dir / f"{name}.sqlite3",
                                  run_by="TEST", allow_dirty_data=True, allow_dirty_code=True)
    with open_run(runs["retire_alice"], "retire_alice", data_dir=test_data_dir,
                  scenarios_dir=test_scenarios_dir) as full:
        with open_run(runs["empty"], "empty", data_dir=test_data_dir,
                      scenarios_dir=test_scenarios_dir) as empty:
            yield full, empty


@pytest.fixture(scope="function")
def multi_connections(data_dir: Path,
                      retire_alice_run: RunConnection,
                      variant_a_run: RunConnection) -> MultiScenarioConnections:
    """Fixture to provide multiple run connections for different scenarios."""
    _, container = multi_scenario_picker(data_dir,
                                         retire_alice_run.name,
                                         [variant_a_run.name])
    return container


@pytest.fixture(scope="session")
def test_data(repo_root: Path) -> Path:
    """Fixture to provide the path to the test data directory within the repository."""
    return repo_root / "tests" / "data"

@pytest.fixture(scope="session")
def bad_subkind_data(test_data: Path) -> Path:
    """Fixture to provide the path to the bad_subkind data within the test data directory."""
    return test_data / "sim/bad_subkind"


@pytest.fixture(scope="function")
def ledger_con() -> Generator[sqlite3.Connection]:
    """In-memory run-schema database for engine unit tests that call
    ledger-posting helpers directly. Lot ids 1..99 exist, each belonging to the
    account with the same id, so tests can use small ints as account/lot ids."""
    con = sqlite3.connect(":memory:")
    apply_run_schema(con)
    con.executemany(
        "INSERT INTO account_lot(id, account_id, lot, opening_balance) "
        "VALUES (?, ?, 'default', 0)",
        [(i, i) for i in range(1, 100)],
    )
    try:
        yield con
    finally:
        con.close()


@pytest.fixture(scope="function")
def retire_alice_csv(test_scenarios_dir: Path) -> Callable[[str], Path]:
    """Path of one of the private retire_alice copy's scenario files, for tests
    that edit inputs before loading."""
    return lambda name: test_scenarios_dir / "retire_alice" / name


_LIFESPAN_HEADER = (
    "person,age_at_death,vary_distribution,vary_low,vary_high,vary_mode,"
    "vary_correlate_with,vary_correlation\n"
)


@pytest.fixture(scope="function")
def write_lifespans(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Write the private retire_alice copy's lifespan.csv (#221) from rows of
    its cells, after the header: ``write_lifespans("alice,80,life_table,,,,,")``,
    or with a ninth cell naming a life table, ``"alice,90,life_table,,,,,,jp"``."""
    def _write(*rows: str) -> None:
        # A row with a ninth cell gives the optional life_table (#315).
        header = _LIFESPAN_HEADER
        if any(r.count(",") == 8 for r in rows):
            header = header.replace("\n", ",life_table\n")
        retire_alice_csv("lifespan.csv").write_text(header + "".join(f"{r}\n" for r in rows))
    return _write


@pytest.fixture(scope="function")
def fund_asset_savings(test_scenarios_dir: Path) -> Callable[[str], None]:
    """Add headroom to the private retire_alice copy's Asset_Savings opening
    balance.

    retire_alice is funded to run its own base scenario through 2040
    without Asset_Checking going negative, but several tests add an extra
    expense it wasn't sized for (a federal tax payment via tax_accumulator,
    an early death that cuts household income in half while shared
    expenses continue) -- this keeps Asset_Checking's refill chain from
    running dry before it reaches the (amply funded) IRA behind it, rather
    than each such test having to size its own top-up.
    """
    import pandas as pd

    def _fund(extra: str = "100000.00") -> None:
        values_path = test_scenarios_dir / "retire_alice" / "account_lots.csv"
        df = pd.read_csv(values_path, dtype=str, keep_default_na=False)
        mask = df["account_name"] == "Asset_Savings"
        current = float(df.loc[mask, "opening_balance"].iloc[0])
        df.loc[mask, "opening_balance"] = f"{current + float(extra):.2f}"
        df.to_csv(values_path, index=False)

    return _fund


@pytest.fixture(scope="function")
def add_alice_401k(
    retire_alice_csv: Callable[[str], Path], add_account_value: Callable[..., None],
) -> Callable[[str], None]:
    """Give the private retire_alice copy a traditional 401(k) for Alice, lot
    ``default``, with the opening balance given: a balance to roll over
    (#134). It and her IRA, the rollover's destination, both grow at
    ``brokerage_growth``."""
    def _add(opening_balance: str = "100000.00") -> None:
        path = retire_alice_csv("accounts.csv")
        accounts = pd.read_csv(path, dtype=str, keep_default_na=False)
        accounts.loc[len(accounts)] = {
            **dict.fromkeys(accounts.columns, ""),
            "name": "Asset_401k_Alice", "kind": "asset", "subkind": "retirement",
            "owner": "alice", "tax_treatment": "",
            "series": "brokerage_growth", "tax_character": "deferred", "meta_json": "{}",
        }
        ira = accounts["name"] == "Asset_TraditionalIRA_Alice"
        accounts.loc[ira, ["series", "tax_character"]] = ["brokerage_growth", "deferred"]
        accounts.to_csv(path, index=False)
        add_account_value("Asset_401k_Alice", lot="default", subkind="traditional_401k",
                          opening_balance=opening_balance)
    return _add


_CHAIN_HEADER = "account_name,lot,maximum,percent,series,cadence\n"


@pytest.fixture(scope="function")
def guide_chains(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], update_account_value,
) -> Path:
    """docs/guides/chains.md's examples on the private copy (issue #129): a
    chain inside a chain drawn by a rule, a benefit swept into a split, and
    checking's refill and overflow. Returns the data dir."""
    sources, destinations = retire_alice_csv("chains/source"), retire_alice_csv("chains/destination")
    (sources / "bridge.csv").write_text(
        _CHAIN_HEADER + "Asset_Savings,,10000.00,,cpi,annual\nAsset_Brokerage_Joint,default,,,,\n")
    (sources / "retirement_draw.csv").write_text(
        _CHAIN_HEADER + "bridge,,,50%,,\nAsset_TraditionalIRA_Alice,default,,,,\n")
    (destinations / "ss_split.csv").write_text(
        _CHAIN_HEADER + "Asset_Checking,,,50%,,\nAsset_Savings,,,,,\n")
    scheduled = retire_alice_csv("scheduled.csv")
    text = scheduled.read_text().replace(
        "ss_alice,Income_SS_Alice,,Asset_Checking,", "ss_alice,Income_SS_Alice,,ss_split,")
    text += ("monthly_draw,retirement_draw,,Asset_Checking,,monthly,,fixed_amount,,,,,,1000.00,,,"
             "none,,2030-01-31,,200,,,,{},,\n")
    scheduled.write_text(text)
    update_account_value("Asset_Checking", "", maximum_balance="30000.00", refill_target="3000.00",
                         overflow_chain="Asset_Savings")
    return test_data_dir


def _deriver(scenarios_dir: Path) -> Callable[..., Path]:
    def _derive(
        name: str, parents: str, files: dict[str, str] | None = None,
        **fields: str,
    ) -> Path:
        unknown = set(fields) - set(SCENARIO_HEADER)
        assert not unknown, f"unknown scenario.csv columns: {sorted(unknown)}"
        folder = scenarios_dir / name
        folder.mkdir()
        row = {**dict.fromkeys(SCENARIO_HEADER, ""), **fields,
               "name": name, "parent_scenario": parents}
        pd.DataFrame([row], columns=list(SCENARIO_HEADER)).to_csv(
            folder / "scenario.csv", index=False,
        )
        for relative, text in (files or {}).items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return folder
    return _derive


@pytest.fixture(scope="function")
def derive_scenario(test_scenarios_dir: Path) -> Callable[..., Path]:
    """Create a scenario in the private data copy deriving from ``parents``
    (a ``parent_scenario`` value: one name, or colon-separated mix-ins after
    it). It overrides nothing beyond ``files``, a {relative path: text} map
    written into its folder, and ``fields``, its own scenario.csv values."""
    return _deriver(test_scenarios_dir)


@pytest.fixture(scope="function")
def derive_template_scenario(template_data_dir: Path) -> Callable[..., Path]:
    """``derive_scenario`` for ``template_data_dir``: a scenario on the
    template ``base``, with template mix-ins."""
    return _deriver(template_data_dir / "scenarios")


@pytest.fixture(scope="session")
def scheduled_override(prototype_data: Path) -> Callable[..., str]:
    """scheduled.csv text repeating the prototype retire_alice rows named as
    keywords with only their ``amount`` changed -- an override replaces a row
    whole, so it has to repeat every other column."""
    def _override(**amounts: str) -> str:
        rows = pd.read_csv(prototype_data / "scenarios" / "retire_alice" / "scheduled.csv",
                           dtype=str, keep_default_na=False)
        rows = rows[rows["name"].isin(amounts)].copy()
        rows["amount"] = rows["name"].map(amounts)
        return rows.to_csv(index=False)
    return _override


@pytest.fixture(scope="function")
def depreciating_car(test_data_dir: Path, retire_alice_csv: Callable[[str], Path]) -> str:
    """Add ``Asset_Car`` to the private retire_alice copy: a $30,000 vehicle
    whose ``jalopy`` rate series is -12% a year, so its value drains into the
    built-in ``depreciation`` expense account (ADR-003 §2.2). Returns its name."""
    (test_data_dir / "aux" / "rate__jalopy.csv").write_text(
        "effective_date,annual_rate\n2020-01-31,-12%\n", encoding="utf-8")
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    accounts.loc[len(accounts)] = {
        **dict.fromkeys(accounts.columns, ""),
        "name": "Asset_Car", "kind": "asset", "subkind": "vehicle", "owner": "joint",
        "tax_treatment": "none", "series": "jalopy", "tax_character": "depreciation",
        "meta_json": "{}",
    }
    accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)
    lots = pd.read_csv(retire_alice_csv("account_lots.csv"), dtype=str, keep_default_na=False)
    lots.loc[len(lots)] = {**dict.fromkeys(lots.columns, ""),
                           "account_name": "Asset_Car", "opening_balance": "30000.00"}
    lots.to_csv(retire_alice_csv("account_lots.csv"), index=False)
    return "Asset_Car"


@pytest.fixture(scope="function")
def business_scenarios(test_scenarios_dir: Path, test_data: Path) -> Path:
    """Alice's software business (#231) in the private data copy:
    ``alice_business`` builds on retire_alice, and ``alice_business_hobby`` is
    the same business ruled a hobby. Kept out of ``prototype_data`` so the
    prototype's scenario list stays the one other tests expect."""
    for folder in (test_data / "sim" / "business").iterdir():
        shutil.copytree(folder, test_scenarios_dir / folder.name)
    return test_scenarios_dir


@pytest.fixture(scope="function")
def strategy_scenarios(test_data_dir: Path, test_scenarios_dir: Path, test_data: Path) -> Path:
    """Alice and Bill's rebalancing scenarios (#326) in the private data copy:
    ``alice_rebalance`` builds on retire_alice with a 60/40 target, a
    ``portfolio:invest`` tag and the built-in ``rebalance`` strategy owning
    checking's refill chain; ``alice_last_leaf`` swaps in the data-repo kind
    ``last_leaf`` from ``strategies/``; ``alice_rebalance_off`` switches the
    strategy off (an ``enabled`` false override), for comparing the two
    (#359); ``alice_spending`` adds travel and gifts rules tagged
    ``flex:optional``, owned by a ``guardrail`` spending strategy (#387).
    Brings its own allocations.csv (USSTK, BONDX). Returns the data
    directory."""
    source = test_data / "sim" / "strategies"
    for folder in (source / "scenarios").iterdir():
        shutil.copytree(folder, test_scenarios_dir / folder.name)
    shutil.copytree(source / "strategies", test_data_dir / "strategies")
    shutil.copyfile(source / "allocations.csv", test_data_dir / "allocations.csv")
    return test_data_dir


@pytest.fixture(scope="function")
def strategy_csv(strategy_scenarios: Path) -> Callable[[str, str], Path]:
    """Path of one of a strategy scenario's files, ``(scenario, filename)``,
    for tests that edit it before loading."""
    return lambda scenario, name: strategy_scenarios / "scenarios" / scenario / name


@pytest.fixture(scope="function")
def strategy_module(strategy_scenarios: Path) -> Callable[[str, str], Path]:
    """Write a module into the private copy's ``strategies/`` folder."""
    def _write(name: str, text: str) -> Path:
        path = strategy_scenarios / "strategies" / f"{name}.py"
        path.write_text(text)
        return path
    return _write


@pytest.fixture(scope="function")
def run_strategy_scenario(
    strategy_scenarios: Path, intent_dir: Path, runs_dir: Path,
) -> Callable[[str], Path]:
    """Load and run a strategy scenario (after a test's edits); its run file."""
    def _run(name: str) -> Path:
        loaded = load_scenario(strategy_scenarios, name, intent_dir=intent_dir / name,
                               allow_dirty_data=True, compute_stamps=False)
        return run_scenario(loaded.intent_path, runs_dir / f"{name}.sqlite3", run_by="TEST",
                            allow_dirty_data=True, allow_dirty_code=True)
    return _run


@pytest.fixture(scope="function")
def strategy_ensemble(strategy_scenarios: Path, intent_dir: Path, runs_dir: Path) -> Callable[[str], Path]:
    """Run a 3-member Monte Carlo batch of a strategy scenario (after a
    test's edits): retire_alice's varied rows, seed 5, under
    ``runs_dir/<name>/``. Returns the batch dir."""
    def _run(name: str) -> Path:
        recipe = load_scenario(strategy_scenarios, name, intent_dir=intent_dir / "recipe",
                               allow_dirty_data=True, compute_stamps=False).intent_path
        batch_dir = runs_dir / name / "2026-01-01T000000Z"
        for i in range(3):
            member_intent = resolve_member(
                recipe, intent_dir / f"{name}_member_{i}.sqlite3",
                seed=5, member_index=i, member_count=3, recipe_tree_id="deadbeef",
            )
            run_scenario(member_intent, batch_dir / f"member_{i:04d}.sqlite3", run_by="test",
                         allow_dirty_data=True, allow_dirty_code=True)
        return batch_dir
    return _run


@pytest.fixture(scope="function")
def strategy_ensemble_batch(strategy_ensemble: Callable[[str], Path]) -> Path:
    """``strategy_ensemble`` of ``alice_rebalance``, its rebalancing strategy
    steering every member (#359). Returns the batch dir."""
    return strategy_ensemble("alice_rebalance")


@pytest.fixture(scope="function")
def load_test_scenario(test_data_dir: Path, intent_dir: Path) -> Callable[[str], Path]:
    """Load any scenario of the private data copy (after a test has added or
    edited it); returns its intent file."""
    def _load(name: str) -> Path:
        return load_scenario(test_data_dir, name, intent_dir=intent_dir / name,
                             allow_dirty_data=True).intent_path
    return _load


@pytest.fixture(scope="function")
def commit_test_data(test_data_dir: Path) -> Callable[[], Path]:
    """Make the private data copy a git repo with everything committed, for
    provenance tests; call it once the test's scenarios are in place."""
    def _commit() -> Path:
        repo = git.Repo.init(test_data_dir, b="main")
        repo.git.add(A=True)
        with repo.config_writer() as cw:
            cw.set_value("commit", "gpgsign", "false")
        actor = git.Actor("t", "t@t")
        repo.index.commit("init", author=actor, committer=actor)
        return test_data_dir
    return _commit


@pytest.fixture(scope="function")
def load_test_copy(test_data_dir: Path, test_scenarios_dir: Path,
                   intent_dir: Path) -> Callable[[], LoadResult]:
    """Loader for the private retire_alice copy, after a test has edited it."""
    def _load() -> LoadResult:
        return load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir,
                             scenarios_dir=test_scenarios_dir, allow_dirty_data=True)
    return _load


@pytest.fixture(scope="function")
def run_test_copy(load_test_copy: Callable[[], LoadResult], runs_dir: Path) -> Callable[[], Path]:
    """Load and run the private retire_alice copy, after a test has edited it;
    returns the run file."""
    def _run() -> Path:
        intent = load_test_copy().intent_path
        return run_scenario(intent, runs_dir / "test_copy.sqlite3", run_by="TEST",
                            allow_dirty_data=True, allow_dirty_code=True)
    return _run


@pytest.fixture(scope="function")
def write_guard_rails(retire_alice_csv: Callable[[str], Path]) -> Callable[..., Path]:
    """Write the private retire_alice copy's guard_rails.csv (#305), replacing
    the prototype's, from rows given as {column: cell} (others blank)."""
    def _write(*rows: dict[str, str]) -> Path:
        unknown = set().union(*rows) - set(GUARD_RAILS_CSV_HEADER) if rows else set()
        assert not unknown, f"unknown guard_rails.csv columns: {sorted(unknown)}"
        path = retire_alice_csv("guard_rails.csv")
        pd.DataFrame([{c: row.get(c, "") for c in GUARD_RAILS_CSV_HEADER} for row in rows],
                     columns=list(GUARD_RAILS_CSV_HEADER)).to_csv(path, index=False)
        return path
    return _write


@pytest.fixture(scope="function")
def write_aux(test_data_dir: Path) -> Callable[[str, str], Path]:
    """Write a file into the private data copy's ``aux/`` directory."""
    def _write(filename: str, text: str) -> Path:
        path = test_data_dir / "aux" / filename
        path.write_text(text)
        return path
    return _write


@pytest.fixture(scope="function")
def without_prototype_defaults(test_data_dir: Path) -> None:
    """Cut the private copy's ``base`` scenario, which retire_alice inherits
    from (#91), loose from the template scenarios it builds on (#282): their
    default factor structure (#80) and their class rate and inflation series.
    For a test whose subject is a tree or series it builds itself, and which
    asserts on the whole set (no warnings, every factor, a fund with no
    class series)."""
    path = test_data_dir / "scenarios" / "base" / "scenario.csv"
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["parent_scenario"] = ""
    df.to_csv(path, index=False)


@pytest.fixture(scope="function")
def write_series(retire_alice_csv: Callable[[str], Path]) -> Callable[[str, str], Path]:
    """Write a rate series into the private retire_alice copy's ``series/``, as
    ``(series name, "effective_date,annual_rate" rows)``, rates in percent."""
    def _write(series: str, rows: str) -> Path:
        path = retire_alice_csv("series") / f"rate__{series}.csv"
        path.write_text(f"effective_date,annual_rate\n{rows}\n")
        return path
    return _write


@pytest.fixture(scope="function")
def blended_fund(test_data_dir: Path, set_brokerage_lots: Callable[[list[dict[str, str]]], None],
                 write_series: Callable[[str, str], Path]) -> None:
    """Make the private retire_alice copy's brokerage one lot ``fund`` of symbol
    FND, $300,000, allocated 60% ``stock_us`` / 40% ``bond``, with class rate
    series at 8% and 4% (#78): it grows at their blend, 6.4%."""
    set_brokerage_lots([
        {"lot": "fund", "symbol": "FND", "opening_balance": "300000.00",
         "opening_cost_basis": "200000.00"},
    ])
    (test_data_dir / "allocations.csv").write_text(
        "symbol,as_of,asset_class,weight\nFND,,stock_us,60%\nFND,,bond,40%\n")
    calendar = test_data_dir / "scenarios" / "retire_alice" / "calendar.csv"
    calendar.write_text(calendar.read_text().replace(
        ",Asset_Brokerage_Joint,default,", ",Asset_Brokerage_Joint,fund,"))
    write_series("stock_us", "2026-01-31,8%")
    write_series("bond", "2026-01-31,4%")


@pytest.fixture(scope="function")
def append_calendar_row(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Append a row to the private copy's calendar.csv, by column name
    (ADR-006 ``vary_*``/``name`` columns included)."""
    def _add(**fields: str) -> None:
        _append_calendar_row_to(retire_alice_csv("calendar.csv"), **fields)
    return _add


def _append_calendar_row_to(path: Path, **fields: str) -> None:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = df.reindex(columns=CALENDAR_HEADER, fill_value="")
    unknown = set(fields) - set(CALENDAR_HEADER)
    assert not unknown, f"unknown calendar.csv columns: {sorted(unknown)}"
    df.loc[len(df)] = [fields.get(column, "") for column in CALENDAR_HEADER]
    df.to_csv(path, index=False)


@pytest.fixture(scope="function")
def write_itemization(test_data_dir: Path) -> Callable[..., None]:
    """Write ``itemizations/<name>.csv`` in the private data copy, shared by
    every scenario, with one row per value: ``write_itemization("fees", "100.00")``
    is what ``@fees`` sums (ADR-003 §4.1c)."""
    def _write(name: str, *values: str) -> None:
        folder = test_data_dir / "itemizations"
        folder.mkdir(exist_ok=True)
        rows = "".join(f"item{n},{value}\n" for n, value in enumerate(values, 1))
        (folder / f"{name}.csv").write_text(f"Item,Value\n{rows}")
    return _write


@pytest.fixture(scope="function")
def add_account_value(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Append a row to the private copy's account_lots.csv, by column name.
    A new lot of an account whose lots have a subkind (a retirement account)
    takes the first one's unless ``subkind`` is given."""
    def _add(account_name: str, **columns: str) -> None:
        path = retire_alice_csv("account_lots.csv")
        values = pd.read_csv(path, dtype=str, keep_default_na=False)
        siblings = values.loc[values["account_name"] == account_name, "subkind"]
        if "subkind" not in columns and len(siblings):
            columns["subkind"] = siblings.iloc[0]
        values.loc[len(values)] = {**dict.fromkeys(values.columns, ""),
                                   "account_name": account_name, **columns}
        values.to_csv(path, index=False)
    return _add


@pytest.fixture(scope="function")
def update_account_value(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Set columns on one existing (account, lot) row of the private copy's
    account_lots.csv."""
    def _update(account_name: str, lot: str, **columns: str) -> None:
        path = retire_alice_csv("account_lots.csv")
        values = pd.read_csv(path, dtype=str, keep_default_na=False)
        row = (values["account_name"] == account_name) & (values["lot"] == lot)
        assert row.any(), (account_name, lot)
        for column, value in columns.items():
            values.loc[row, column] = value
        values.to_csv(path, index=False)
    return _update


@pytest.fixture(scope="function")
def update_account(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Set columns on one existing account row of the private copy's
    accounts.csv: ``update_account("Asset_Brokerage_Joint", owner="alice")``."""
    def _update(name: str, **columns: str) -> None:
        path = retire_alice_csv("accounts.csv")
        accounts = pd.read_csv(path, dtype=str, keep_default_na=False)
        row = accounts["name"] == name
        assert row.any(), name
        for column, value in columns.items():
            accounts.loc[row, column] = value
        accounts.to_csv(path, index=False)
    return _update


@pytest.fixture(scope="function")
def update_scheduled(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Set columns on one existing rule of the private copy's scheduled.csv:
    ``update_scheduled("living", person="joint")``."""
    def _update(name: str, **columns: str) -> None:
        path = retire_alice_csv("scheduled.csv")
        rules = pd.read_csv(path, dtype=str, keep_default_na=False)
        row = rules["name"] == name
        assert row.any(), name
        for column, value in columns.items():
            rules.loc[row, column] = value
        rules.to_csv(path, index=False)
    return _update


@pytest.fixture(scope="function")
def set_scenario_meta(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Set keys of the private retire_alice copy's scenario.csv ``meta_json``,
    keeping the others: ``set_scenario_meta(community_property=True)``."""
    def _set(**keys: object) -> None:
        path = retire_alice_csv("scenario.csv")
        scenario = pd.read_csv(path, dtype=str, keep_default_na=False)
        meta = json.loads(scenario.loc[0, "meta_json"] or "{}")
        scenario.loc[0, "meta_json"] = json.dumps({**meta, **keys})
        scenario.to_csv(path, index=False)
    return _set


@pytest.fixture(scope="function")
def set_brokerage_lots(retire_alice_csv: Callable[[str], Path]) -> Callable[[list[dict[str, str]]], None]:
    """Replace Asset_Brokerage_Joint's account_lots row with the given lots
    (dicts of account_lots.csv columns; ``account_name`` is filled in)."""
    def _set(lots: list[dict[str, str]]) -> None:
        values = pd.read_csv(retire_alice_csv("account_lots.csv"), dtype=str, keep_default_na=False)
        values = values[values["account_name"] != "Asset_Brokerage_Joint"]
        for column in ("symbol", "quantity"):
            values[column] = ""
        rows = pd.DataFrame([{"account_name": "Asset_Brokerage_Joint", **lot} for lot in lots])
        pd.concat([values, rows], ignore_index=True).fillna("").to_csv(
            retire_alice_csv("account_lots.csv"), index=False)
    return _set


@pytest.fixture(scope="function")
def savings_lots(retire_alice_csv: Callable[[str], Path]) -> tuple[str, str]:
    """Split the private retire_alice copy's Asset_Savings, an ordinary (not
    investment) account, into lots ``emergency`` and ``travel``, neither
    named ``default``: a transfer must name one of them. The checking
    liquidity chain draws on ``emergency``. Returns the lots."""
    path = retire_alice_csv("account_lots.csv")
    values = pd.read_csv(path, dtype=str, keep_default_na=False)
    savings = values[values["account_name"] == "Asset_Savings"].iloc[0]
    lots = ("emergency", "travel")
    rows = pd.DataFrame([{**savings, "lot": lot, "opening_balance": "10000.00"} for lot in lots])
    pd.concat([values[values["account_name"] != "Asset_Savings"], rows], ignore_index=True).to_csv(
        path, index=False)
    chain_path = retire_alice_csv("chains/source/asset_checking_liquidity.csv")
    chain = pd.read_csv(chain_path, dtype=str, keep_default_na=False)
    chain.loc[chain["account_name"] == "Asset_Savings", "lot"] = lots[0]
    chain.to_csv(chain_path, index=False)
    return lots


@pytest.fixture(scope="function")
def two_lot_run_file(test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
                     set_brokerage_lots: Callable[[list[dict[str, str]]], None],
                     load_test_copy: Callable[[], LoadResult], runs_dir: Path) -> Path:
    """Run of a retire_alice copy whose brokerage is split into lots ``a`` (symbol
    AAA, 1000 shares, with its own growth series) and ``b`` (BBB, 2000.5 shares,
    acquired 2026-01-15, so sales from it are short-term for a year),
    with a monthly draw from lot b."""
    set_brokerage_lots([
        {"lot": "a", "symbol": "AAA", "quantity": "1000", "opening_balance": "100000.00",
         "opening_cost_basis": "60000.00"},
        {"lot": "b", "symbol": "BBB", "quantity": "2,000.5", "acquired": "2026-01-15",
         "opening_balance": "200000.00", "opening_cost_basis": "140000.00"},
    ])

    (test_data_dir / "securities.csv").write_text(
        "symbol,name\nAAA,Alpha Growth Fund\nBBB,Beta Income Fund\n")

    calendar = retire_alice_csv("calendar.csv")
    calendar.write_text(calendar.read_text().replace(
        ",Asset_Brokerage_Joint,default,", ",Asset_Brokerage_Joint,a,"))
    (test_data_dir / "aux" / "rate__AAA.csv").write_text(
        "effective_date,annual_rate\n2026-01-31,2%\n")
    (retire_alice_csv("chains/source") / "lotb.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nAsset_Brokerage_Joint,b,,,,\n")

    scheduled = pd.read_csv(retire_alice_csv("scheduled.csv"), dtype=str, keep_default_na=False)
    scheduled.loc[len(scheduled)] = {
        **dict.fromkeys(scheduled.columns, ""),
        "name": "lot_b_draw", "from_account": "lotb", "to_account": "Asset_Checking",
        "cadence": "monthly", "amount_rule_kind": "fixed_amount", "amount": "1000.00",
        "tax_character": "none", "start": "2026-05-31", "priority": "500",
        "meta_json": "{}",
    }
    scheduled.to_csv(retire_alice_csv("scheduled.csv"), index=False)

    intent = load_test_copy().intent_path
    return run_scenario(intent, runs_dir / "two_lot.sqlite3", run_by="TEST",
                        allow_dirty_data=True, allow_dirty_code=True)


@pytest.fixture(scope="function")
def add_state_tax(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Give the private retire_alice copy a state income tax: an
    Expense_Taxes_State account and a ``tax_accumulator`` rule (scope state)
    paying the previous year's state tax each April. ``bracket_series`` names
    the state when more than one ``state_brackets`` table exists."""
    def _add(bracket_series: str | None = None) -> None:
        params = {"year": "previous", "scope": "state"}
        if bracket_series:
            params["bracket_series"] = bracket_series
        accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str,
                               keep_default_na=False)
        accounts.loc[len(accounts)] = {
            **dict.fromkeys(accounts.columns, ""),
            "name": "Expense_Taxes_State", "kind": "expense", "subkind": "taxes_state",
            "owner": "joint", "tax_treatment": "none", "opened": "2020-01-31",
            "meta_json": "{}",
        }
        accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)
        scheduled = pd.read_csv(retire_alice_csv("scheduled.csv"), dtype=str,
                                keep_default_na=False)
        scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
        scheduled.loc[len(scheduled)] = {
            **dict.fromkeys(SCHEDULED_CSV_HEADER, ""),
            "name": "tax_state_prev_year", "from_account": "Asset_Checking",
            "to_account": "Expense_Taxes_State", "cadence": "annual", "month_of_year": "4",
            "amount_rule_kind": "tax_accumulator", "tax_character": "none",
            "start": "2027-04-30", "priority": "400", "meta_json": "{}",
            "params_json": json.dumps(params),
        }
        scheduled.to_csv(retire_alice_csv("scheduled.csv"), index=False)
    return _add


@pytest.fixture(scope="function")
def report_run_file(retire_alice_csv: Callable[[str], Path],
                    fund_asset_savings: Callable[..., None],
                    load_test_copy: Callable[[], LoadResult], runs_dir: Path) -> Path:
    """Run of a retire_alice copy that computes federal tax (a ``tax_accumulator``
    rule paying the previous year's tax each April) and tags its spending in
    role ``budget``: Expense_Living and Expense_Healthcare are ``budget:core``,
    Expense_Housing is ``budget:home``, and the ``living`` rule's own
    ``budget:fun`` overrides its account's tag. Income_Salary_Alice is
    ``source:work``, the only income tagged. For the result reports (#187)."""
    fund_asset_savings()
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    for name, tag in [("Expense_Living", "budget:core"), ("Expense_Healthcare", "budget:core"),
                      ("Expense_Housing", "budget:home"),
                      ("Income_Salary_Alice", "source:work")]:
        accounts.loc[accounts["name"] == name, "tags"] = tag
    accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)

    scheduled = pd.read_csv(retire_alice_csv("scheduled.csv"), dtype=str, keep_default_na=False)
    scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
    scheduled.loc[scheduled["name"] == "living", "tags"] = "budget:fun"
    scheduled.loc[len(scheduled)] = {
        **dict.fromkeys(SCHEDULED_CSV_HEADER, ""),
        "name": "tax_federal_prev_year", "from_account": "Asset_Checking",
        "to_account": "Expense_Taxes_Federal", "cadence": "annual", "month_of_year": "4",
        "amount_rule_kind": "tax_accumulator", "tax_character": "none",
        "start": "2027-04-30", "priority": "400", "meta_json": "{}",
        "params_json": '{"year":"previous","scope":"federal"}',
    }
    scheduled.to_csv(retire_alice_csv("scheduled.csv"), index=False)

    intent = load_test_copy().intent_path
    return run_scenario(intent, runs_dir / "report.sqlite3", run_by="TEST",
                        allow_dirty_data=True, allow_dirty_code=True)


@pytest.fixture(scope="module", autouse=True)
def close_plots():
    import matplotlib.pyplot as plt
    yield
    plt.close("all")


@pytest.fixture(scope="function", autouse=True)
def no_FigureCanvasAgg_warning():
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore",
                                category=UserWarning,
                                message=".*FigureCanvasAgg.*"
                                )
        yield


# --- ADR-007 sensitivity batches -------------------------------------------


def _add_varying_gift_and_retirement(append_calendar_row: Callable[..., None]) -> None:
    """A gift whose size is uncertain, and a retirement date uncertain by
    six months either side of the prototype's own 2030-12-31."""
    append_calendar_row(
        date="2029-06-30", kind="one_off_transfer", name="gift",
        account_1="Income_Inheritance", account_2="Asset_Brokerage_Joint", lot_2="default",
        amount="20000.00", tax_character="none",
        vary_target="amount", vary_distribution="uniform",
        vary_low="10000.00", vary_high="30000.00",
    )
    append_calendar_row(
        date="2030-12-31", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="uniform",
        vary_low="2030-06-30", vary_high="2031-06-30",
    )


def _sim(*args: str) -> None:
    from click.testing import CliRunner

    from analysis.cli import main

    result = CliRunner().invoke(main, ["sim", *args])
    assert result.exit_code == 0, result.output


@pytest.fixture(scope="function")
def retire_alice_test_run(test_data_dir: Path, intent_dir: Path, runs_dir: Path) -> Path:
    """retire_alice from the private data copy, loaded and run into the
    function's state dir, beside ``test_data_dir``: where a picker opened on
    ``test_data_dir`` looks. Returns its runs folder."""
    _sim("load", "retire_alice", "--data-dir", str(test_data_dir),
         "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
         "--allow-dirty-data")
    return runs_dir / "retire_alice"


@pytest.fixture(scope="function")
def symbol_sources_data_dir(test_data_dir: Path) -> Path:
    """The private data copy with a symbol named only in each of the places
    other than securities.csv: ``ALLOC`` in allocations.csv, ``OVERRIDE`` in
    retire_alice's own prices/, ``HELD`` in a retire_alice lot."""
    (test_data_dir / "allocations.csv").write_text(
        "symbol,as_of,asset_class,weight\nALLOC,,stock_us,100%\n", encoding="utf-8")
    scenario_dir = test_data_dir / "scenarios" / "retire_alice"
    (scenario_dir / "prices").mkdir()
    (scenario_dir / "prices" / "security_price__OVERRIDE.csv").write_text(
        "effective_date,price\n", encoding="utf-8")
    lots = scenario_dir / "account_lots.csv"
    text = lots.read_text(encoding="utf-8")
    row = {"account_name": "Asset_Brokerage_Joint", "lot": "held", "symbol": "HELD"}
    lots.write_text(
        text + ",".join(row.get(c, "") for c in text.splitlines()[0].split(",")) + "\n",
        encoding="utf-8")
    return test_data_dir


@pytest.fixture(scope="function")
def test_ensemble_scenario(test_data_dir: Path) -> str:
    """``recipe_0``, a three-member ensemble over retire_alice in the private
    data copy, created but not run. Returns its name."""
    name = "recipe_0"
    _sim("ensemble", "create", name, "retire_alice", "--size", "3",
         "--data-dir", str(test_data_dir))
    return name


@pytest.fixture(scope="function")
def ensemble_recipe(test_data_dir: Path, intent_dir: Path, runs_dir: Path) -> str:
    """``recipe_1``, a three-member ensemble recipe over retire_alice in the
    private data copy (one varied rate), run into the function's state dir.
    Returns its name."""
    name = "recipe_1"
    _sim("ensemble", "create", name, "retire_alice", "--size", "3",
         "--data-dir", str(test_data_dir))
    _sim("calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
         "--scenario", name, "--data-dir", str(test_data_dir),
         "--name", "checking_rate_vary", "--vary-target", "amount",
         "--vary-distribution", "uniform", "--vary-low", "4.0", "--vary-high", "5.0")
    _sim("run", name, "--data-dir", str(test_data_dir),
         "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
         "--allow-dirty-data")
    return name


@pytest.fixture(scope="function")
def code_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> git.Repo:
    """A stand-in code repo on main that run freshness (``freshness.code_tree``)
    compares runs against, so a test depends neither on this checkout's
    branch nor on its uncommitted changes."""
    repo = git.Repo.init(tmp_path / "code", b="main")
    with repo.config_writer() as cw:
        cw.set_value("commit", "gpgsign", "false")
    root = Path(str(repo.working_tree_dir))
    for name, text in {"src/engine.py": "v1\n", "templates/t.csv": "", "pyproject.toml": "",
                       "uv.lock": "", "docs/guide.md": "one\n"}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    repo.git.add(A=True)
    actor = git.Actor("t", "t@t")
    repo.index.commit("init", author=actor, committer=actor)
    from analysis.sim import freshness

    monkeypatch.setattr(freshness, "code_tree", lambda: freshness.code_tree_of(repo))
    return repo


@pytest.fixture(scope="function")
def stamp_runs(code_repo: git.Repo) -> Callable[[Path], None]:
    """A function recording ``code_repo``'s HEAD, clean, as the code every
    run under a runs dir ran, and dating the plain runs earlier, in order,
    so a rerun is always the newest: runs that are fresh until the code or
    data moves."""
    def _stamp(runs_dir: Path) -> None:
        for n, run_file in enumerate(sorted(runs_dir.glob("*_*.sqlite3"))):
            run_file.rename(run_file.with_name(
                f"2000-01-01T{n:06d}Z_{run_file.name.split('_', 1)[1]}"))
        for run_file in runs_dir.rglob("*.sqlite3"):
            with sqlite3.connect(run_file) as con:
                if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'simulation_run'").fetchone():
                    con.execute("UPDATE simulation_run SET code_version = ?, code_dirty = 0",
                                (code_repo.head.commit.hexsha,))
    return _stamp


@pytest.fixture(scope="function")
def pool_scenarios(test_data_dir: Path) -> str:
    """Pool ``mix`` (ADR-009) in the private data copy, created but not run:
    ``e_a`` 25% (a four-member ensemble over retire_alice, seed 7, with a
    varied checking rate) and ``e_b`` 75% (a child of ``e_a`` adding a
    varied gift, so it shares the seed and pairs with ``e_a``). Returns the
    pool's name."""
    data = ("--data-dir", str(test_data_dir))
    _sim("ensemble", "create", "e_a", "retire_alice", "--size", "4", "--seed", "7", *data)
    _sim("calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
         "--scenario", "e_a", *data, "--name", "checking_rate_vary", "--vary-target", "amount",
         "--vary-distribution", "uniform", "--vary-low", "0", "--vary-high", "40")
    _sim("ensemble", "create", "e_b", "e_a", *data)
    calendar = test_data_dir / "scenarios" / "e_b" / "calendar.csv"
    if not calendar.is_file():
        calendar.write_text(",".join(CALENDAR_HEADER) + "\n", encoding="utf-8")
    _append_calendar_row_to(
        calendar,
        date="2029-06-30", kind="one_off_transfer", name="gift",
        account_1="Income_Inheritance", account_2="Asset_Brokerage_Joint", lot_2="default",
        amount="20000.00", tax_character="none",
        vary_target="amount", vary_distribution="uniform",
        vary_low="10000.00", vary_high="30000.00",
    )
    _sim("ensemble", "pool", "create", "mix", "e_a=25%", "e_b=75%", *data)
    return "mix"


@pytest.fixture
def write_scenario_meta(test_data_dir: Path) -> Callable[..., None]:
    """Rewrite scenario ``name``'s scenario.csv in the private data copy:
    its ``meta_json`` to ``meta`` (a dict), and any other cells given."""
    import csv

    def write(name: str, meta: dict, **row: str) -> None:
        path = test_data_dir / "scenarios" / name / "scenario.csv"
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            header, rows = list(reader.fieldnames or []), list(reader)
        rows[0].update(meta_json=json.dumps(meta), **row)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)
    return write


@pytest.fixture
def pool_batch(test_data_dir: Path, pool_scenarios: str, state_dir: Path) -> Path:
    """``pool_scenarios``'s ``mix`` run (both components run first), returning
    its batch dir."""
    from click.testing import CliRunner

    from analysis.cli import main
    from analysis.sim.loader import latest_ensemble_batch

    result = CliRunner().invoke(
        main, ["sim", "run", pool_scenarios, "--data-dir", str(test_data_dir), "--allow-dirty-data"])
    assert result.exit_code == 0, result.output
    assert "component e_a: running" in result.output
    assert "pool of 2, seed 7: e_a 1 (25%), e_b 3 (75%)" in result.output, result.output
    batch = latest_ensemble_batch(state_dir / "runs" / pool_scenarios)
    assert batch is not None
    return batch


@pytest.fixture
def fake_batch(tmp_path: Path) -> Callable[..., Path]:
    """Make ``runs/<scenario>/b`` under ``tmp_path``: ``members`` empty member
    files and a batch manifest (``member_count`` defaults to ``members``;
    ``failures`` maps index to error; ``pool`` is a pool record), for code
    that reads only a batch's names and manifest. Returns the folder."""
    from analysis.sim.ensemble import write_batch_manifest

    def make(scenario: str, *, members: int = 0, member_count: int | None = None,
             failures: dict[int, str] | None = None, pool: dict | None = None) -> Path:
        batch = tmp_path / "runs" / scenario / "b"
        batch.mkdir(parents=True)
        for j in range(members):
            (batch / f"member_{j:04d}.sqlite3").write_bytes(b"")
        write_batch_manifest(
            batch, member_count=members if member_count is None else member_count,
            failures=[(i, batch / f"member_{i:04d}.sqlite3", e) for i, e in (failures or {}).items()],
            pool=pool)
        return batch
    return make


@pytest.fixture(scope="function")
def two_lot_ensemble_runs_dir(
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], intent_dir: Path, runs_dir: Path,
) -> Path:
    """``runs_dir`` holding a 2-member Monte Carlo batch of a retire_alice copy
    whose brokerage is split into lots ``a`` and ``b``, with a varied
    checking rate: an ensemble with an account to drill into (#170)."""
    set_brokerage_lots([
        {"lot": "a", "opening_balance": "100000.00", "opening_cost_basis": "60000.00"},
        {"lot": "b", "opening_balance": "200000.00", "opening_cost_basis": "140000.00"},
    ])
    calendar = retire_alice_csv("calendar.csv")
    calendar.write_text(calendar.read_text().replace(
        ",Asset_Brokerage_Joint,default,", ",Asset_Brokerage_Joint,a,"))
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    recipe = load_test_copy().intent_path
    batch_dir = runs_dir / "two_lot" / "2026-01-01T000000Z"
    for i in range(2):
        member_intent = resolve_member(
            recipe, intent_dir / f"two_lot_member_{i}.sqlite3",
            seed=1, member_index=i, member_count=2, recipe_tree_id="deadbeef",
        )
        run_scenario(member_intent, batch_dir / f"member_{i:04d}.sqlite3",
                     run_by="test", allow_dirty_data=True)
    return runs_dir


@pytest.fixture(scope="function")
def seeded_ensemble_batch(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    intent_dir: Path, runs_dir: Path,
) -> Callable[[str, int], Path]:
    """A function building a 3-member Monte Carlo batch of retire_alice with
    a widely varied checking rate under ``runs_dir/<name>/``, drawn with
    ``seed``, and returning the batch dir: two names with one seed stand for
    a recipe and an unchanged child (their members pair, #163); with two
    seeds, for unrelated recipes."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0%", vary_high="40%",
    )
    recipe = load_test_copy().intent_path

    def _batch(name: str, seed: int) -> Path:
        batch_dir = runs_dir / name / "2026-01-01T000000Z"
        for i in range(3):
            member_intent = resolve_member(
                recipe, intent_dir / f"{name}_member_{i}.sqlite3",
                seed=seed, member_index=i, member_count=3, recipe_tree_id="deadbeef",
            )
            run_scenario(
                member_intent, batch_dir / f"member_{i:04d}.sqlite3",
                run_by="test", allow_dirty_data=True,
            )
        return batch_dir

    return _batch


def _load_and_run_batch(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path, *run_args: str,
) -> str:
    from click.testing import CliRunner

    from analysis.cli import main

    runner = CliRunner()
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--allow-dirty-data", "--no-run",
    ])
    assert load.exit_code == 0, load.output
    if "--sensitivity" not in run_args:
        con = sqlite3.connect(intent_dir / "retire_alice.sqlite3")
        con.execute("UPDATE scenario SET seed = 7")
        con.commit()
        con.close()
    run = runner.invoke(main, [
        "sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--allow-dirty-data", *run_args,
    ])
    assert run.exit_code == 0, run.output
    return run.output


def _add_varying_cpi(append_calendar_row: Callable[..., None]) -> None:
    """The CPI inflation rate itself, uncertain between 1.5% and 3.5%."""
    append_calendar_row(
        date="2027-01-31", kind="set_rate", name="cpi_vary", series="cpi",
        amount="2.5%", params_json='{"aux_kind": "inflation"}',
        vary_target="amount", vary_distribution="uniform",
        vary_low="1.5%", vary_high="3.5%",
    )


# name -> (calendar edits, `sim run` arguments, member count or None, batch mode)
_SHARED_BATCHES: dict[str, tuple[Callable[[Callable[..., None]], None], tuple[str, ...], int | None, str]] = {
    "oat": (_add_varying_gift_and_retirement, ("--sensitivity",), 5, "sensitivity"),
    "ladder": (_add_varying_gift_and_retirement,
               ("--sensitivity", "--quantiles", "0.1,0.25,0.75,0.9"), 9, "sensitivity"),
    "mc": (_add_varying_gift_and_retirement, ("--ensemble-size", "8"), None, "monte_carlo"),
    "inflation": (_add_varying_cpi, ("--sensitivity",), 3, "sensitivity"),
}


@pytest.fixture(scope="session")
def _built_batches(
    tmp_path_factory: pytest.TempPathFactory, prototype_data: Path,
) -> Callable[[str], Path]:
    """Build each of ``_SHARED_BATCHES`` at most once per session (once per
    xdist worker), returning the state dir (``intent/``, ``runs/``) it was
    built into. Never hand this dir to a test: ``_copy_batch`` gives each
    test a private copy, since some tests edit or delete a batch's files.

    A session fixture is set up before the function-scoped autouse fixtures
    apply, so the build repeats their policies itself: in-process members,
    no ``.current_scenario`` writes, and a negative balance is an error.
    """
    built: dict[str, Path] = {}

    def _build(name: str) -> Path:
        if name in built:
            return built[name]
        edit, run_args, members, _mode = _SHARED_BATCHES[name]
        root = tmp_path_factory.mktemp(f"batch_{name}")
        data = root / "data"
        shutil.copytree(prototype_data, data)
        calendar = data / "scenarios" / "retire_alice" / "calendar.csv"
        edit(lambda **fields: _append_calendar_row_to(calendar, **fields))
        state = root / "state"
        intent, runs = state / "intent", state / "runs"
        intent.mkdir(parents=True)
        runs.mkdir()
        with pytest.MonkeyPatch.context() as patch, warnings.catch_warnings():
            patch.setattr("analysis.sim.cli._available_cpus", lambda: 1)
            patch.setattr("analysis.sim.cli.write_current_scenario", lambda *args, **kwargs: None)
            warnings.filterwarnings("error", category=RuntimeWarning, message=".*went negative.*")
            output = _load_and_run_batch(data, intent, runs, *run_args)
        if members is not None:
            assert f"{members}/{members} member(s) succeeded" in output, output
        built[name] = state
        return state

    return _build


def _copy_batch(built_batches: Callable[[str], Path], name: str, state_dir: Path) -> Path:
    """Copy a shared batch's ``intent/`` and ``runs/`` into the test's own
    ``state_dir`` (where ``intent_dir`` and ``runs_dir`` point), returning the
    copied batch dir."""
    from analysis.sim.loader import latest_ensemble_batch

    shutil.copytree(built_batches(name), state_dir, dirs_exist_ok=True)
    batch = latest_ensemble_batch(state_dir / "runs" / "retire_alice", mode=_SHARED_BATCHES[name][3])
    assert batch is not None
    return batch


@pytest.fixture(scope="function")
def run_test_batch(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path,
) -> Callable[..., Path]:
    """Load and run the private retire_alice copy as a batch, after a test
    has edited it: ``run_test_batch("--sensitivity")`` or
    ``run_test_batch("--ensemble-size", "8")`` (seed 7). Returns the batch dir."""
    from analysis.sim.loader import latest_ensemble_batch

    def _run(*run_args: str) -> Path:
        output = _load_and_run_batch(test_data_dir, intent_dir, runs_dir, *run_args)
        mode = "sensitivity" if "--sensitivity" in run_args else "monte_carlo"
        batch = latest_ensemble_batch(runs_dir / "retire_alice", mode=mode)
        assert batch is not None, output
        return batch
    return _run


@pytest.fixture(scope="function")
def oat_batch(_built_batches: Callable[[str], Path], state_dir: Path) -> Path:
    """An OAT sensitivity batch (default p10/p90) over a varying gift and a
    varying retirement date on a copy of retire_alice: 5 members, in this
    test's ``runs_dir``."""
    return _copy_batch(_built_batches, "oat", state_dir)


@pytest.fixture(scope="function")
def ladder_batch(_built_batches: Callable[[str], Path], state_dir: Path) -> Path:
    """Like ``oat_batch``, but a 4-rung quantile ladder: 9 members."""
    return _copy_batch(_built_batches, "ladder", state_dir)


@pytest.fixture(scope="function")
def mc_batch(_built_batches: Callable[[str], Path], state_dir: Path) -> Path:
    """An 8-member Monte Carlo batch over the same two varying rows as
    ``oat_batch`` (seed 7)."""
    return _copy_batch(_built_batches, "mc", state_dir)


@pytest.fixture(scope="function")
def inflation_batch(_built_batches: Callable[[str], Path], state_dir: Path) -> Path:
    """An OAT batch (p10/p90) perturbing the CPI inflation rate itself: 3 members."""
    return _copy_batch(_built_batches, "inflation", state_dir)


@pytest.fixture(scope="function")
def calibration_cache_dir(tmp_path: Path) -> Path:
    """A private, initially empty download cache for
    ``analysis historical``."""
    return tmp_path / "calibration_cache"


@pytest.fixture(scope="function")
def calibration_rate_csv(tmp_path: Path) -> Path:
    """A ``date,rate`` CSV of three annual rates (3%, 5%, 4% for 2020-2022),
    ``analysis historical``'s input format for a series already in percent."""
    path = tmp_path / "premiums.csv"
    path.write_text("date,rate\n2020-01-01,3.0\n2021-01-01,5.0\n2022-01-01,4.0\n")
    return path


@pytest.fixture(scope="function")
def fake_state(intent_dir: Path, runs_dir: Path) -> Callable[..., Path]:
    """Callable that fabricates what ``sim run`` leaves under ``state/``, for
    retention tests (#195) that need many old runs without running them:
    ``fake_state("run", "alice", "2026-10-01T120000Z", status=...)`` writes a
    single run file; ``fake_state("batch", ...)`` a run batch of ``members``
    member files with a batch manifest (``complete=False`` leaves the last
    member out); ``fake_state("intent", ...)`` a member-intent batch. Each
    run file holds just a ``simulation_run`` row with the status. Every file
    and folder written gets the stamp as its mtime, so nothing reads as still
    being written (retention.ACTIVE_WINDOW)."""
    import os
    from datetime import UTC, datetime

    from analysis.sim.ensemble import write_batch_manifest

    def _run_file(path: Path, status: str) -> None:
        con = sqlite3.connect(path)
        try:
            con.execute("CREATE TABLE simulation_run (id INTEGER PRIMARY KEY, status TEXT)")
            con.execute("INSERT INTO simulation_run VALUES (1, ?)", (status,))
            con.commit()
        finally:
            con.close()

    def _fake(kind: str, scenario_name: str, stamp: str, *, status: str = "succeeded",
              members: int = 2, complete: bool = True) -> Path:
        made = datetime.strptime(stamp, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=UTC).timestamp()
        if kind == "run":
            path = runs_dir / f"{stamp}_{scenario_name}_abc1234.sqlite3"
            _run_file(path, status)
            os.utime(path, (made, made))
            return path
        path = (runs_dir if kind == "batch" else intent_dir) / scenario_name / stamp
        path.mkdir(parents=True)
        written = members if complete else members - 1
        for i in range(written):
            _run_file(path / f"member_{i:04d}.sqlite3", status)
        if kind == "batch":
            write_batch_manifest(path, member_count=members, failures=[])
        for child in [*path.iterdir(), path]:
            os.utime(child, (made, made))
        return path

    return _fake


@pytest.fixture(scope="function")
def without_template_aux(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the code repository's templates/aux/ (#317) from the loader, so a
    test that deletes one of the private copy's aux tables sees it missing
    instead of the template's copy filling in."""
    monkeypatch.setattr("analysis.sim.loader.TEMPLATE_AUX_DIR", tmp_path_factory.mktemp("no_template_aux"))


@pytest.fixture(scope="session")
def research_bundles(repo_root: Path) -> Path:
    """Research bundles as the finance-research plugin writes them (#344),
    read-only: ``abcdx/`` (every tab, an allocation export, a ``fund-work/``
    folder), ``unclassified/`` (JSON only: part of the fund unclassified),
    and two researched by name (#372): ``acme-robotics/``, a private
    company's tabs under ``ACME-ROBOTICS``, and ``balanced-by-name/``, a
    fund's tear sheet and weights under ``A-BALANCED-INDEX-FUND``."""
    return repo_root / "tests" / "data" / "research"


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, research_bundles: Path) -> Path:
    """A stand-in ``claude`` first on PATH for `sim symbol research`: it copies
    the bundle named by ``$FAKE_CLAUDE_BUNDLE`` (default ``abcdx``) into its
    working folder and prints ``stream-json`` events. ``$FAKE_CLAUDE_FAIL``
    makes it report an error; ``$FAKE_CLAUDE_SLEEP`` delays it;
    ``$FAKE_CLAUDE_CHILD`` leaves a child holding its stdout. Each call's
    arguments go to the returned file, one per line."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    calls = tmp_path / "claude-args.txt"
    script = bin_dir / "claude"
    script.write_text(f"""#!{sys.executable}
import json, os, shutil, sys, time
from pathlib import Path
Path({str(calls)!r}).write_text("\\n".join(sys.argv[1:]) + "\\n", encoding="utf-8")
if os.environ.get("FAKE_CLAUDE_CHILD"):
    import subprocess
    subprocess.Popen(["sleep", "30"])  # inherits stdout, so it holds the pipe open
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
name = os.environ.get("FAKE_CLAUDE_BUNDLE", "abcdx")
if name:
    shutil.copytree(Path({str(research_bundles)!r}) / name, Path.cwd(), dirs_exist_ok=True)
def emit(event):
    print(json.dumps(event), flush=True)
emit({{"type": "system", "subtype": "init"}})
emit({{"type": "assistant", "message": {{"content": [
    {{"type": "tool_use", "name": "WebSearch", "input": {{"query": "ABCDX fact sheet"}}}},
    {{"type": "text", "text": "Found it."}}]}}}})
fail = os.environ.get("FAKE_CLAUDE_FAIL")
emit({{"type": "result", "subtype": "error" if fail else "success", "is_error": bool(fail),
       "result": fail or "Wrote the tear sheet.", "total_cost_usd": 1.25}})
""", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return calls
