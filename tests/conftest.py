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
from pathlib import Path
import shutil
import sqlite3

import pandas as pd
import pytest

from analysis.sim import scenario
from analysis.sim.connection import RunConnection, ScenarioConnection, open_scenario, open_run
from analysis.sim.engine import run_scenario
from analysis.sim.schema import apply_run_schema
from analysis.sim.loader import LoadResult, ScenarioRef, load_scenario, resolve_scenario_name
from analysis.sim.notebook_ui.pickers import MultiScenarioConnections, multi_scenario_picker
from analysis.sim.provenance import CodeStamps
from analysis.sim.registries import ACCOUNT_VALUES_CSV_HEADER, CALENDAR_HEADER

_ANALYSIS_ENV_VARS = (
    "ANALYSIS_DATA_DIR",
    "ANALYSIS_SCENARIO",
)


@pytest.fixture(autouse=True)
def _no_ambient_analysis_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ANALYSIS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


_STABLE_CODE_STAMPS = CodeStamps(version="0" * 40, dirty=False, repo_path="CODE_REPO")


@pytest.fixture(scope="session", autouse=True)
def _stable_code_stamps() -> Generator[None, None, None]:
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
def state_dir(tmp_path: Path) -> Generator[Path, None, None]:
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
                 scenarios_dir: Path) -> Callable[[str], ScenarioRef]:
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
                                   scenario_ref: Callable[[str], ScenarioRef]) -> ScenarioRef:
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
                     retire_alice_run_file: Path) -> Generator[RunConnection, None, None]:
    """Fixture to provide the RunConnection for the retire_alice scenario."""
    with open_run(retire_alice_run_file, retire_alice_scenario,
                  scenarios_dir=scenarios_dir) as r:
        yield r


@pytest.fixture(scope="function")
def variant_a_run(scenarios_dir: Path,
                  variant_a_scenario: ScenarioConnection,
                  variant_a_run_file: Path) -> Generator[RunConnection, None, None]:
    """Fixture to provide the RunConnection for the variant_a scenario."""
    with open_run(variant_a_run_file, variant_a_scenario,
                  scenarios_dir=scenarios_dir) as r:
        yield r


@pytest.fixture(scope="session")
def retire_alice_scenario(data_dir: Path) -> Generator[ScenarioConnection, None, None]:
    """Fixture to provide the ScenarioConnection for the retire_alice scenario."""
    with open_scenario(data_dir, "retire_alice") as conn:
        yield conn


@pytest.fixture(scope="function")
def test_retire_alice_scenario(test_data_dir: Path) -> Generator[ScenarioConnection, None, None]:
    """Fixture to provide the ScenarioConnection for the retire_alice scenario."""
    with open_scenario(test_data_dir, "retire_alice") as conn:
        yield conn


@pytest.fixture(scope="function")
def variant_a_scenario(data_dir: Path,
                       variant_a_scenario_create: Path,
                       ) -> Generator[ScenarioConnection, None, None]:
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


@pytest.fixture(scope="session")
def variant_a_scenario_create(data_dir: Path) -> Path:
    """Fixture to provide the path to the variant_a scenario within the data directory."""
    return scenario.create_scenario(data_dir, "variant_a", "retire_alice")

@pytest.fixture(scope="function")
def two_lot_run(two_lot_run_file: Path, test_data_dir: Path) -> Generator[RunConnection, None, None]:
    """Connection to the ``two_lot_run_file`` run."""
    with open_scenario(test_data_dir, "retire_alice") as scenario_conn:
        with open_run(two_lot_run_file, scenario_conn,
                      scenarios_dir=test_data_dir / "scenarios") as run:
            yield run


@pytest.fixture(scope="function")
def lot_variant_runs(
    two_lot_run_file: Path, test_data_dir: Path, test_scenarios_dir: Path,
    intent_dir: Path, runs_dir: Path,
) -> Generator[tuple[RunConnection, dict[str, RunConnection]], None, None]:
    """Runs of ``retire_alice`` (its brokerage split into lots a and b) and of a
    derived ``variant`` that changes lot b's quantity, balance and basis, adds a
    lot c, and changes one inherited year-to-date row."""
    scenario.create_scenario(test_data_dir, "variant", "retire_alice")
    header = ",".join(ACCOUNT_VALUES_CSV_HEADER)
    variant_dir = test_scenarios_dir / "variant"
    (variant_dir / "account_values.csv").write_text(
        f"{header}\n"
        "Asset_Brokerage_Joint,b,BBB,2026-01-15,1500,,150000.00,100000.00,\n"
        "Asset_Brokerage_Joint,c,CCC,2027-06-30,,,25000.00,20000.00,\n"
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
def ledger_con() -> Generator[sqlite3.Connection, None, None]:
    """In-memory run-schema database for engine unit tests that call
    ledger-posting helpers directly. Lot ids 1..99 exist, each belonging to the
    account with the same id, so tests can use small ints as account/lot ids."""
    con = sqlite3.connect(":memory:")
    apply_run_schema(con)
    con.executemany(
        "INSERT INTO account_model_member(id, model_id, account_id, lot, opening_balance) "
        "VALUES (?, 1, ?, 'default', 0)",
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


@pytest.fixture(scope="function")
def load_test_copy(test_data_dir: Path, test_scenarios_dir: Path,
                   intent_dir: Path) -> Callable[[], LoadResult]:
    """Loader for the private retire_alice copy, after a test has edited it."""
    def _load() -> LoadResult:
        return load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir,
                             scenarios_dir=test_scenarios_dir, allow_dirty_data=True)
    return _load


@pytest.fixture(scope="function")
def write_aux(test_data_dir: Path) -> Callable[[str, str], Path]:
    """Write a file into the private data copy's ``aux/`` directory."""
    def _write(filename: str, text: str) -> Path:
        path = test_data_dir / "aux" / filename
        path.write_text(text)
        return path
    return _write


@pytest.fixture(scope="function")
def append_calendar_row(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Append a row to the private copy's calendar.csv, by column name
    (ADR-006 ``vary_*``/``name`` columns included)."""
    def _add(**fields: str) -> None:
        path = retire_alice_csv("calendar.csv")
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        df = df.reindex(columns=CALENDAR_HEADER, fill_value="")
        unknown = set(fields) - set(CALENDAR_HEADER)
        assert not unknown, f"unknown calendar.csv columns: {sorted(unknown)}"
        df.loc[len(df)] = [fields.get(column, "") for column in CALENDAR_HEADER]
        df.to_csv(path, index=False)
    return _add


@pytest.fixture(scope="function")
def add_account_value(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Append a row to the private copy's account_values.csv, by column name."""
    def _add(account_name: str, **columns: str) -> None:
        path = retire_alice_csv("account_values.csv")
        values = pd.read_csv(path, dtype=str, keep_default_na=False)
        values.loc[len(values)] = {**dict.fromkeys(values.columns, ""),
                                   "account_name": account_name, **columns}
        values.to_csv(path, index=False)
    return _add


@pytest.fixture(scope="function")
def update_account_value(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Set columns on one existing (account, lot) row of the private copy's
    account_values.csv."""
    def _update(account_name: str, lot: str, **columns: str) -> None:
        path = retire_alice_csv("account_values.csv")
        values = pd.read_csv(path, dtype=str, keep_default_na=False)
        row = (values["account_name"] == account_name) & (values["lot"] == lot)
        assert row.any(), (account_name, lot)
        for column, value in columns.items():
            values.loc[row, column] = value
        values.to_csv(path, index=False)
    return _update


@pytest.fixture(scope="function")
def set_brokerage_lots(retire_alice_csv: Callable[[str], Path]) -> Callable[[list[dict[str, str]]], None]:
    """Replace Asset_Brokerage_Joint's account_values row with the given lots
    (dicts of account_values.csv columns; ``account_name`` is filled in)."""
    def _set(lots: list[dict[str, str]]) -> None:
        values = pd.read_csv(retire_alice_csv("account_values.csv"), dtype=str, keep_default_na=False)
        values = values[values["account_name"] != "Asset_Brokerage_Joint"]
        for column in ("symbol", "quantity"):
            values[column] = ""
        rows = pd.DataFrame([{"account_name": "Asset_Brokerage_Joint", **lot} for lot in lots])
        pd.concat([values, rows], ignore_index=True).fillna("").to_csv(
            retire_alice_csv("account_values.csv"), index=False)
    return _set


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
        "effective_date,annual_rate_micros\n2026-01-31,20000\n")
    (retire_alice_csv("chains") / "asset__lotb.csv").write_text(
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
