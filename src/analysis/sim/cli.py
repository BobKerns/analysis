"""``analysis sim`` subcommand group."""

from __future__ import annotations

import csv as _csv
import functools
import json
import math
import secrets
import shlex
import shutil
import sys
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence, Set
from contextlib import ExitStack, contextmanager
import os
import sqlite3
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, NoReturn

import click
import pandas as pd

from analysis.amazon.notebook import load_monthly_summaries
from analysis.expenses.baseline import (
    build_expense_baseline,
    build_scheduled_baseline_rows,
    load_category_map,
    load_category_splits,
    load_excluded_payees,
    load_expense_account_map,
    replace_generated_scheduled_rows,
)
from analysis.sim import (
    allocation, calendar_guide, chain_edit, chain_map, completion, guard_rail_guide, guard_rails,
    lot_guide, names, prices, readme, registries, rename, reports, scenario_guide, scheduled_guide,
    sensitivity, setup_listings, symbols, units, working_set, ytd_guide,
)
from analysis.sim.accounts import load_accounts_view, render_accounts_table
from analysis.sim.connection import (
    open_run, open_scenario, year_end_values, Connection, RunConnection,
)
from analysis.sim.holdings import (
    account_holdings,
    holding_accounts,
    holding_symbols,
    render_holders_table,
    render_holdings_table,
    symbol_holders,
)
from analysis.sim.distributions import (
    Discrete,
    DistributionError,
    OutcomeRole,
    TruncatedNormal,
    parse_outcomes,
    parse_quantiles,
    resolve_truncated_normal_bounds,
    rewrite_outcomes,
)
from analysis.sim.engine import describe_negative_balance_event, run_scenario
from analysis.sim.series_db import (
    EnsembleSeries,
    MemberSeries,
    member_series,
    open_series,
    pair_members,
    write_series_db,
)
from analysis.sim.locks import BUILDER_CHILD_ENV, LockHolder, build_lock, building, reserve_run_path
from analysis.sim.ensemble import (
    EnsembleError,
    factor_reach,
    latest_ensemble_batch,
    member_run_statuses,
    comparison_deltas,
    oat_plan,
    life_table_age,
    varying_names,
    resolve_member,
    risk_summary,
    summary_with_percentiles,
    write_batch_manifest,
)
from analysis.sim.loader import (
    DEFAULT_INTENT_DIR,
    DEFAULT_RUNS_DIR,
    DEFAULT_STATE_DIR,
    DataDirtyRefused,
    LoadError,
    LoadResult,
    ScenarioRef,
    batch_mode,
    DISABLE_FILE,
    calendar_row_key,
    current_scenario_path,
    is_ensemble_batch,
    list_scenario_names,
    template_aux_copies,
    load_scenario,
    open_intent_db,
    find_state_dir,
    latest_run_path,
    read_current_scenario,
    read_disabled_scenarios,
    scenario_run_path,
    set_scenario_enabled,
    write_current_scenario,
)
from analysis.sim.quicken import (
    format_dollars,
    is_investment_asset,
    load_account_map,
    parse_net_worth_export,
)
import analysis.sim.scenario as sc
from analysis.sim.staging import (
    ConcurrentEdit,
    EditRejected,
    ScenarioAlreadyBroken,
    StagedEdit,
    staged_edit,
)
from analysis.sim.life_table import equivalent_multiplier
from analysis.sim.dates import (
    MONTH_FORMAT_HINT, is_full_date, is_marker_name, is_month_or_date, month_end_iso, parse_day,
    parse_relative_date, parse_vary_date,
)
from analysis.sim.lineage import (
    MIXIN_BASE,
    default_description,
    is_mixin_only,
    load_order,
    parse_parents,
    read_parents,
)
from analysis.sim.lineage import is_template as _is_template
from analysis.sim.lineage import scenario_dir as _folder_of
from analysis.sim.overlay import (
    CalendarRow as _CalendarRow,
    Overlaid as _Overlaid,
    overlay_rows as _overlay_rows,
    scenario_calendar_rows as _scenario_calendar_rows,
)
from analysis.frames import index_years, named_rows
from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS, DollarMode, ItemType
from analysis.sim.retention import RetentionError, prune_state
from analysis.sim.schema import connect_sim_file

if TYPE_CHECKING:
    from rich.console import RenderableType


def _resolve_state_dir(
    directory: Path | None, default: Path, data_dir: Path | None = None,
) -> Path:
    """``directory`` if given, else ``default`` (``intent`` or ``runs``) under the
    state dir beside the data repo (``data_dir``, else ``$ANALYSIS_DATA_DIR``)."""
    if directory is not None:
        return directory
    return find_state_dir(data_dir, DEFAULT_STATE_DIR) / default


def _builds(fn):
    """A command that builds runs: it never waits on the background builder
    (#300, ``locks.building``), even while resolving its scenario."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with building():
            return fn(*args, **kwargs)
    return wrapper


@click.group(name="sim")
def sim_group_command() -> None:
    """Scenario-based finance simulator."""


@sim_group_command.command()
@click.argument("scenario_name", type=completion.SCENARIO, required=False)
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Data repo (default: $ANALYSIS_DATA_DIR)",
)
@click.option(
    "--intent-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Default: data-repo state/intent when activated, else state/intent.",
)
@click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Default: data-repo state/runs when activated, else state/runs. Used only when running.",
)
@click.option(
    "--allow-dirty-data",
    is_flag=True,
    default=False,
    help="Allow loading (and running) when the data repo has uncommitted changes in the scenario's scoped paths.",
)
@click.option(
    "--allow-dirty-code", is_flag=True, default=False, help="Used only when running.",
)
@click.option(
    "--run-by",
    default=None,
    help="Identifier recorded in simulation_run.run_by (default: $USER). Used only when running.",
)
@click.option(
    "--run/--no-run",
    "run_after_load",
    default=True,
    help="Also run the scenario after loading (default). Use --no-run to only build the intent file.",
)
@click.pass_context
@_builds
def load(
    ctx: click.Context,
    scenario_name: str | None,
    data_dir: Path | None,
    intent_dir: Path | None,
    runs_dir: Path | None,
    allow_dirty_data: bool,
    allow_dirty_code: bool,
    run_by: str | None,
    run_after_load: bool,
) -> None:
    """Build state/intent/<scenario>.sqlite3 from the data repo, then run it.

    Naming a scenario also records it as the data repo's current scenario;
    omitting it reuses that recorded scenario. Pass --no-run to only load.
    Either way, old runs and batches are then pruned by the data repo's
    .retention.yaml, created with default rules if missing.
    """
    resolved = _resolve_data_dir(data_dir)
    resolved_intent_dir = _resolve_state_dir(intent_dir, DEFAULT_INTENT_DIR, resolved)
    ref = _resolve_scenario(resolved, scenario_name, intent_dir=resolved_intent_dir)
    with _build_lock(resolved_intent_dir, ref.name):
        _load_locked(
            ctx, ref.name, resolved, resolved_intent_dir, data_dir=data_dir,
            intent_dir=intent_dir, runs_dir=runs_dir, allow_dirty_data=allow_dirty_data,
            allow_dirty_code=allow_dirty_code, run_by=run_by, run_after_load=run_after_load,
        )


def _build_lock(intent_dir: Path, scenario: str):
    """``scenario``'s build lock (#300), saying so on stderr when another
    build of it is in progress and this one has to wait."""
    def on_wait(holder: LockHolder | None) -> None:
        by = f" by {holder.describe()}" if holder is not None else ""
        click.echo(f"waiting for {scenario}: being built{by}", err=True)

    return build_lock(intent_dir, scenario, on_wait=on_wait)


def _load_locked(
    ctx: click.Context, name: str, resolved: Path, resolved_intent_dir: Path, *,
    data_dir: Path | None, intent_dir: Path | None, runs_dir: Path | None,
    allow_dirty_data: bool, allow_dirty_code: bool, run_by: str | None,
    run_after_load: bool,
) -> None:
    """``sim load``'s work, under the scenario's build lock."""
    try:
        result = load_scenario(
            resolved, name, intent_dir=resolved_intent_dir,
            allow_dirty_data=allow_dirty_data,
        )
    except LoadError as exc:
        for issue in exc.issues:
            click.echo(f"  error: {issue}", err=True)
        raise click.ClickException(f"{len(exc.issues)} issue(s)") from None
    except DataDirtyRefused as exc:
        click.echo(str(exc), err=True)
        raise click.ClickException("data dirty; refusing to load") from None
    if not run_after_load:
        _echo_load_warnings(result)  # otherwise `run` reloads and prints them, once
    click.echo(f"loaded {result.intent_path}")
    click.echo(f"  current scenario: {name}")
    for table, n in result.counts.items():
        click.echo(f"  {table}: {n}")
    if not run_after_load:
        _prune_state(resolved, resolved_intent_dir,
                     _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR, resolved))

    if run_after_load:
        ctx.invoke(
            run,
            scenario_name=name,
            data_dir=data_dir,
            intent_dir=intent_dir,
            runs_dir=runs_dir,
            run_by=run_by,
            allow_dirty_data=allow_dirty_data,
            allow_dirty_code=allow_dirty_code,
        )


@sim_group_command.command()
@click.argument("scenario_name", type=completion.SCENARIO, required=False)
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
)
@click.option(
    "--all", "check_all", is_flag=True, default=False,
    help="Check every scenario in the data repo, continuing past failures.",
)
@click.option(
    "-v", "--verbose", is_flag=True, default=False,
    help="With --all, also warn of things defined but not read yet (unused "
    "latent factors, set_rate rows nothing reads), as a single check does. "
    "Also list the data repo's aux/ files that only copy templates/aux/.",
)
def check(
    scenario_name: str | None, data_dir: Path | None, check_all: bool, verbose: bool,
) -> None:
    """Validate the scenario's CSVs without producing a persistent intent file.

    A single scenario's check also warns of things defined but not read yet;
    --all leaves those out unless --verbose is given (#204), since base and
    mix-in scenarios define many on purpose for the scenarios built on them.
    --verbose also names the shared aux/ files that only copy templates/aux/
    (#317), which can be deleted.
    """
    if check_all and scenario_name is not None:
        raise click.UsageError("give a scenario name or --all, not both")
    resolved = _resolve_data_dir(data_dir)
    if verbose:
        for name in template_aux_copies(resolved):
            click.echo(
                f"note: aux/{name} is the same as templates/aux/{name}, which is read "
                "anyway; deleting it lets later template updates through", err=True)
    if not check_all:
        ref = _resolve_scenario(resolved, scenario_name)
        _check_scenario(resolved, ref.name, unused=True)
        return
    try:
        names = list_scenario_names(resolved)
    except LoadError as exc:
        raise click.ClickException("; ".join(exc.issues)) from None
    if not names:
        raise click.ClickException(f"no scenarios found under {resolved / 'scenarios'}")
    failed: list[str] = []
    for name in names:
        click.echo(f"Checking {name}")
        try:
            _check_scenario(resolved, name, unused=verbose)
        except click.ClickException:
            failed.append(name)  # its issues are already printed
    click.echo()
    if failed:
        # Non-zero exit so the gate loop this replaces still fails a push.
        raise click.ClickException(
            f"{len(failed)} of {len(names)} scenario(s) failed: {', '.join(failed)}"
        )
    click.echo(f"all {len(names)} scenario(s) ok")


def _check_scenario(data_dir: Path, name: str, *, unused: bool) -> None:
    """Load one scenario into a throwaway intent file, printing its issues and
    warnings (with ``unused``, the unused ones too); a failed load raises
    ClickException."""
    with tempfile.TemporaryDirectory() as td:
        try:
            result = load_scenario(
                data_dir, name, intent_dir=Path(td), compute_stamps=False,
            )
        except LoadError as exc:
            for issue in exc.issues:
                click.echo(f"  error: {issue}", err=True)
            raise click.ClickException(f"{len(exc.issues)} issue(s)") from None
    _echo_load_warnings(result, unused=unused)
    click.echo("ok")


def _echo_load_warnings(result: LoadResult, *, unused: bool = False) -> None:
    """Print a load's non-fatal warnings to stderr, the way errors are
    printed, without failing. Warnings about things defined but not read yet
    (an unused latent factor or set_rate series) are often deliberate, so
    only `sim check` asks for them (``unused=True``, #204)."""
    for warning in [*result.warnings, *(result.unused if unused else ())]:
        click.echo(f"WARN: {warning}", err=True)  # ADR-004 §6's format


_SCENARIO_ANNOUNCED = "analysis.sim.scenario_announced"


def _resolve_scenario(
    data_dir: Path, scenario: str | None, *, intent_dir: Path | None = None,
    runs_dir: Path | None = None, remember: bool = True,
) -> ScenarioRef:
    """The scenario a command works on (#214): the one named, which becomes
    the data repo's current scenario, or else the current scenario, announced
    on stderr so a stale default is never a surprise. With no current
    scenario, the command asks for one by name rather than guessing.

    ``remember=False`` is for a name that is only a reference, such as a new
    scenario's parent.
    """
    defaulted = scenario is None
    if scenario is None:
        scenario = read_current_scenario(data_dir)
        if scenario is None:
            names = ", ".join(list_scenario_names(data_dir)) or "(none)"
            raise click.UsageError(
                f"no current scenario; name one (scenarios: {names})"
            )
    try:
        ref = sc.resolve_scenario_name(
            data_dir, scenario, intent_dir=intent_dir, runs_dir=runs_dir,
        )
    except LoadError as exc:
        if defaulted:
            raise click.ClickException(
                f"{current_scenario_path(data_dir)} names a missing scenario: {scenario!r}; "
                "name one"
            ) from None
        raise click.ClickException("; ".join(exc.issues)) from None
    if defaulted:
        # Once per command, though several steps of it may resolve the default.
        ctx = click.get_current_context(silent=True)
        if ctx is None or not ctx.meta.get(_SCENARIO_ANNOUNCED):
            click.echo(f"Using scenario {ref.name}", err=True)
            if ctx is not None:
                ctx.meta[_SCENARIO_ANNOUNCED] = True
        working_set.use(data_dir, ref.name)  # working on it is use too (#322)
    elif remember:
        _make_current(data_dir, ref.name)
    return ref


def _make_current(data_dir: Path, name: str) -> None:
    """Record ``name`` as the current scenario, and say so on stderr when that
    changes it (#233): later commands only say ``Using scenario X``, which is
    easy to miss when X is not the one expected."""
    if os.environ.get(BUILDER_CHILD_ENV):
        return  # a background build (#300) never moves the user's current scenario
    working_set.use(data_dir, name)  # first in the builder's order (#322)
    previous = read_current_scenario(data_dir)
    if previous == name:
        return
    write_current_scenario(data_dir, name)
    _announce_current(name, previous)


def _announce_current(name: str | None, previous: str | None) -> None:
    """The notice of a change to the current scenario (#233), also for one
    renamed or deleted along with its scenario."""
    was = f" (was {previous})" if previous else ""
    now = f"Current scenario is now {name}" if name else "No scenario is current now"
    click.secho(f"{now}{was}", err=True, bold=True)


def _note_use(data_dir: Path | None, *names: str | None) -> None:
    """Record use of ``names`` (#322) by a command that reads only runs, so
    may have no data repo (``--runs-dir``): then there is nothing to record."""
    try:
        data = _resolve_data_dir(data_dir)
    except click.ClickException:
        return
    working_set.use(data, *names)


def _resolve_data_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir.resolve()
    env = os.environ.get("ANALYSIS_DATA_DIR")
    if not env:
        raise click.ClickException(
            "ANALYSIS_DATA_DIR not set; pass --data-dir or source the data repo's activate"
        )
    return Path(env).resolve()


@sim_group_command.command()
@click.argument("scenario_name", type=completion.SCENARIO, required=False)
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Data repo (default: $ANALYSIS_DATA_DIR)",
)
@click.option(
    "--intent-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Default: data-repo state/intent when activated, else state/intent.",
)
@click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Default: data-repo state/runs when activated, else state/runs.",
)
@click.option(
    "--run-by",
    default=None,
    help="Identifier recorded in simulation_run.run_by (default: $USER)",
)
@click.option("--allow-dirty-data", is_flag=True, default=False)
@click.option("--allow-dirty-code", is_flag=True, default=False)
@click.option(
    "--ensemble-size",
    type=int,
    default=None,
    help="Member count for an ensemble recipe, overriding scenario.meta_json.ensemble_size "
         "(ADR-006). Ignored for an ordinary (non-recipe) scenario.",
)
@click.option(
    "--sensitivity", is_flag=True, default=False,
    help="Run a one-at-a-time sensitivity batch over the recipe's varying rows "
         "instead of a Monte Carlo ensemble (ADR-007).",
)
@click.option(
    "--only", "only", default=None, metavar="NAME[,NAME...]",
    help="With --sensitivity: perturb only these varying rows or latent factors; the rest "
         "stay central. By default every varying row is perturbed, and every latent factor "
         "that a varying row depends on, moving its whole family (ADR-007 §2).",
)
@click.option(
    "--jobs", type=int, default=0,
    help="Ensemble/sensitivity members to run in parallel (default 0: one per CPU "
         "available to this process; 1 runs them in-process, one at a time; ADR-007 §8).",
)
@click.option(
    "--quantiles", "quantiles_text", default=None, metavar="Q[,Q...]",
    help="With --sensitivity: quantiles each row is perturbed to "
         "(default 0.1,0.9; e.g. 0.1,0.25,0.4,0.6,0.75,0.9 for a response curve).",
)
@_builds
def run(
    scenario_name: str | None,
    data_dir: Path | None,
    intent_dir: Path | None,
    runs_dir: Path | None,
    run_by: str | None,
    allow_dirty_data: bool,
    allow_dirty_code: bool,
    ensemble_size: int | None,
    sensitivity: bool,
    only: str | None,
    quantiles_text: str | None,
    jobs: int,
) -> None:
    """Load <scenario> fresh from the data repo, then run it and write a new run file.

    Naming a scenario also records it as the data repo's current scenario,
    same as ``sim load <scenario>``. When a data repo is resolvable
    (--data-dir or $ANALYSIS_DATA_DIR), this reloads the intent file first --
    loading is cheap, and it means a CSV edit since the last explicit
    ``sim load`` is never silently missed. Without a resolvable data repo,
    this only needs the already-built intent file, unchanged.

    If the loaded intent still has any unresolved ``vary_distribution`` row
    (ADR-006) and is an ensemble (a positive ``ensemble_size``, or
    ``--ensemble-size``), this generates and runs an ensemble instead of a
    single run; with ``--sensitivity``, a one-at-a-time sensitivity batch
    (ADR-007). A scenario with varying rows that is not an ensemble runs its
    base case, each varying row at its central value.

    Afterwards, old run files and batches are pruned by the data repo's
    ``.retention.yaml``, created with default rules if missing (ADR-004 §10).
    """
    quantiles, only_names, jobs = _batch_options(
        sensitivity=sensitivity, ensemble_size=ensemble_size, only=only,
        quantiles_text=quantiles_text, jobs=jobs,
    )
    data_dir_available = data_dir is not None or bool(os.environ.get("ANALYSIS_DATA_DIR"))
    resolved_data_dir = _resolve_data_dir(data_dir) if data_dir_available else None
    if resolved_data_dir is not None or scenario_name is None:
        resolved_data_dir = resolved_data_dir or _resolve_data_dir(data_dir)
        scenario_name = _resolve_scenario(resolved_data_dir, scenario_name).name
        if is_mixin_only(resolved_data_dir / "scenarios", scenario_name):
            _refuse_mixin_only_run(scenario_name, sc.dependents(resolved_data_dir / "scenarios", scenario_name))
    resolved_intent_dir = _resolve_state_dir(intent_dir, DEFAULT_INTENT_DIR, resolved_data_dir)
    resolved_runs_dir = _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR, resolved_data_dir)
    intent_path = resolved_intent_dir / f"{scenario_name}.sqlite3"
    try:
        with _build_lock(resolved_intent_dir, scenario_name):
            if resolved_data_dir is not None:
                try:
                    _echo_load_warnings(load_scenario(
                        resolved_data_dir, scenario_name, intent_dir=resolved_intent_dir,
                        allow_dirty_data=allow_dirty_data,
                    ))
                except LoadError as exc:
                    for issue in exc.issues:
                        click.echo(f"  error: {issue}", err=True)
                    raise click.ClickException(f"{len(exc.issues)} issue(s)") from None
                except DataDirtyRefused as exc:
                    click.echo(str(exc), err=True)
                    raise click.ClickException("data dirty; refusing to load") from None
            elif not intent_path.exists():
                raise click.ClickException(
                    f"intent file not found: {intent_path}\n"
                    f"  run: analysis sim load {scenario_name}"
                )
            elif _intent_is_mixin_only(intent_path):
                # Without the data repo, the intent a `sim check` left says so.
                _refuse_mixin_only_run(scenario_name, [])
            resolved_by = run_by or os.environ.get("USER") or "unknown"
            _run_loaded(
                scenario_name, intent_path, resolved_intent_dir, resolved_runs_dir,
                run_by=resolved_by, allow_dirty_data=allow_dirty_data,
                allow_dirty_code=allow_dirty_code, ensemble_size=ensemble_size,
                sensitivity=sensitivity, quantiles=quantiles, only=only_names, jobs=jobs,
            )
    finally:
        # A failed run leaves its run file too, so it still gets housekeeping;
        # as its scenario's newest attempt, it is never pruned itself.
        if resolved_data_dir is not None:
            _prune_state(resolved_data_dir, resolved_intent_dir, resolved_runs_dir)


def _refuse_mixin_only_run(name: str, users: Sequence[str]) -> NoReturn:
    raise click.ClickException(
        f"{name} is a mix-in only scenario (no start or end anywhere in its lineage), so it doesn't "
        f"run by itself; run {', '.join(users) if users else 'a scenario that mixes it in'}, "
        "or give it --start and --end (`sim scenario edit`)")


def _intent_is_mixin_only(intent_path: Path) -> bool:
    """Whether the intent was loaded from a mix-in only scenario, as the
    loader recorded (``scenario.mixin_only``). An intent from before schema
    50 has no such column; there only a root mix-in was one, loaded with no
    parents on ``MIXIN_BASE``."""
    con = connect_sim_file(intent_path)
    try:
        try:
            row = con.execute("SELECT mixin_only FROM scenario").fetchone()
            return bool(row and row[0])
        except sqlite3.OperationalError:
            row = con.execute("SELECT parents_json, load_order_json FROM scenario").fetchone()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    if row is None:
        return False
    try:
        parents, order = json.loads(row[0] or "[]"), json.loads(row[1] or "[]")
    except json.JSONDecodeError:
        return False
    return not parents and len(order) > 1 and order[0] == MIXIN_BASE


def _prune_state(data_dir: Path, intent_dir: Path, runs_dir: Path) -> None:
    """Apply the data repo's ``.retention.yaml`` after a load or run
    (ADR-004 §10). Housekeeping never fails the command: a bad file or a
    failed delete is a warning."""
    try:
        result = prune_state(data_dir, intent_dir, runs_dir)
    except RetentionError as exc:
        click.echo(f"  warning: {data_dir / '.retention.yaml'}: {exc}; nothing pruned", err=True)
        return
    except OSError as exc:
        click.echo(f"  warning: pruning state stopped: {exc}", err=True)
        return
    if result.created is not None:
        click.echo(f"created {result.created} with the default retention rules")
    if result:
        click.echo(result.summary())


def _run_loaded(
    scenario_name: str, intent_path: Path, intent_dir: Path, runs_dir: Path,
    *, run_by: str, allow_dirty_data: bool, allow_dirty_code: bool,
    ensemble_size: int | None, sensitivity: bool, quantiles: tuple[float, ...],
    only: list[str] | None, jobs: int,
) -> None:
    """``sim run``'s work once the intent is loaded: a sensitivity batch, an
    ensemble, or a single run."""
    if sensitivity:
        if not _unresolved_vary_names(intent_path):
            raise click.ClickException(
                f"{scenario_name}: nothing to perturb -- no calendar row has "
                "vary_distribution set (ADR-007)"
            )
        _run_sensitivity(
            scenario_name, intent_path,
            intent_dir, runs_dir,
            quantiles=quantiles, only=only, run_by=run_by,
            allow_dirty_data=allow_dirty_data, allow_dirty_code=allow_dirty_code, jobs=jobs,
        )
        return

    if _has_unresolved_vary(intent_path):
        if ensemble_size is not None or _recorded_ensemble_size(intent_path):
            _run_ensemble(
                scenario_name, intent_path,
                intent_dir, runs_dir,
                ensemble_size=ensemble_size, run_by=run_by,
                allow_dirty_data=allow_dirty_data, allow_dirty_code=allow_dirty_code,
                jobs=jobs,
            )
            return
        click.echo(
            f"{scenario_name} is not an ensemble: running its base case, each varying "
            "row at its central value (--ensemble-size N runs it as an ensemble)"
        )
        intent_path = _resolve_base_case(scenario_name, intent_path, intent_dir)

    data_version = _read_data_version(intent_path)
    short = data_version[:7] if data_version else "nodata"
    run_path = reserve_run_path(runs_dir, scenario_name, short)

    try:
        out = run_scenario(
            intent_path, run_path,
            run_by=run_by,
            allow_dirty_data=allow_dirty_data,
            allow_dirty_code=allow_dirty_code,
            reserved=True,
        )
    except DataDirtyRefused as exc:
        click.echo(str(exc), err=True)
        raise click.ClickException("data dirty; refusing to run") from None
    click.echo(f"run {out}")


def _batch_options(
    *, sensitivity: bool, ensemble_size: int | None, only: str | None,
    quantiles_text: str | None, jobs: int,
) -> tuple[tuple[float, ...], list[str] | None, int]:
    """Validate ``sim run``'s ensemble/sensitivity options: (quantiles,
    ``--only`` names, worker count)."""
    if not sensitivity and (only is not None or quantiles_text is not None):
        raise click.UsageError("--only and --quantiles require --sensitivity")
    if sensitivity and ensemble_size is not None:
        raise click.UsageError(
            "--sensitivity and --ensemble-size are exclusive: a sensitivity batch's "
            "size is fixed by its rows and quantiles"
        )
    try:
        quantiles = (
            parse_quantiles(quantiles_text) if quantiles_text is not None
            else registries.DEFAULT_SENSITIVITY_QUANTILES
        )
    except DistributionError as exc:
        raise click.UsageError(str(exc)) from None
    only_names = [n.strip() for n in only.split(",") if n.strip()] if only else None
    if jobs < 0:
        raise click.UsageError(f"--jobs must be 0 (one per CPU) or positive (got {jobs})")
    return quantiles, only_names, jobs or _available_cpus()


def _available_cpus() -> int:
    """CPUs this process may use (honoring affinity), for ``--jobs 0`` (#42)."""
    return os.process_cpu_count() or 1


def _has_unresolved_vary(intent_path: Path) -> bool:
    """ADR-006: whether ``intent_path`` still has any calendar_event row or
    lifespan (#221) with `vary_distribution` set, or a `vary_resume` row
    (#291), i.e. is a recipe that hasn't been resolved."""
    if _unresolved_vary_names(intent_path):
        return True
    con = connect_sim_file(intent_path)
    try:
        return con.execute(
            "SELECT 1 FROM calendar_event WHERE vary_resume IS NOT NULL LIMIT 1"
        ).fetchone() is not None
    finally:
        con.close()


def _recorded_ensemble_size(intent_path: Path) -> int:
    """The intent's merged ``scenario.meta_json.ensemble_size``, which the
    loader checked and stored as an int; absent or 0 means not an ensemble
    (ADR-006 §7)."""
    _seed, meta_json, _tree_id = _recipe_identity(intent_path)
    return json.loads(meta_json or "{}").get("ensemble_size", 0)


def _resolve_base_case(scenario_name: str, intent_path: Path, intent_dir: Path) -> Path:
    """ADR-006 §7: a scenario that has varying rows but is not an ensemble
    runs as its member 0, each row at its central value. The resolved intent
    goes beside the scenario's ensemble batches, so the intent the load
    built keeps its varying rows."""
    seed, _meta_json, recipe_tree_id = _recipe_identity(intent_path)
    return resolve_member(
        intent_path, intent_dir / scenario_name / "base_case.sqlite3",
        seed=seed, member_index=0, member_count=1, recipe_tree_id=recipe_tree_id,
    )


def _run_ensemble(
    scenario_name: str,
    recipe_intent_path: Path,
    intent_dir: Path,
    runs_dir: Path,
    *,
    ensemble_size: int | None,
    run_by: str,
    allow_dirty_data: bool,
    allow_dirty_code: bool,
    jobs: int = 1,
) -> None:
    """ADR-006 §7: generate and run every member of a recipe scenario.

    A member that fails is reported, not retried -- its resolved intent file
    (kept, not discarded) is the specific input that failed, one open-file
    away.
    """
    seed, meta_json, recipe_tree_id = _recipe_identity(recipe_intent_path)
    if seed is None:
        raise click.ClickException(
            f"{scenario_name}: ensemble recipe requires scenario.seed to be set"
        )
    if ensemble_size is not None:
        member_count = ensemble_size
        source = "--ensemble-size"
    else:
        # `run` only gets here with a recorded size, which the loader made
        # a positive int.
        member_count = json.loads(meta_json or "{}")["ensemble_size"]
        source = "scenario.meta_json.ensemble_size"
    if member_count <= 0:
        raise click.ClickException(
            f"{scenario_name}: {source} must be a positive integer (got {member_count})"
        )

    batch, member_intent_dir, member_runs_dir = _reserve_batch(scenario_name, intent_dir, runs_dir)
    # Provisional, so an interrupted batch is still recognizable; rewritten
    # with the real failures below.
    write_batch_manifest(member_runs_dir, member_count=member_count, failures=[])
    series: list[MemberSeries] = []
    failures = _run_members(
        recipe_intent_path, member_intent_dir, member_runs_dir,
        [(i, None) for i in range(member_count)],
        seed=seed, recipe_tree_id=recipe_tree_id, mode="monte_carlo",
        run_by=run_by, allow_dirty_data=allow_dirty_data, allow_dirty_code=allow_dirty_code,
        jobs=jobs, series=series,
    )
    # A member whose resolve_member call itself raised never got a run file
    # at all -- member_run_statuses can only see files that exist, so this
    # batch-level record is the only place a failure like that (or every
    # member failing, leaving no member_*.sqlite3 at all) is still visible
    # to a later `sim ensemble summary`/notebook widget.
    write_batch_manifest(member_runs_dir, member_count=member_count, failures=failures)
    _build_draws_db(member_runs_dir, member_intent_dir)
    _write_series_db(member_runs_dir, series, member_count - len(failures))
    _report_batch(f"ensemble {scenario_name} ({batch})", member_count, failures)


def _run_sensitivity(
    scenario_name: str,
    recipe_intent_path: Path,
    intent_dir: Path,
    runs_dir: Path,
    *,
    quantiles: tuple[float, ...],
    only: list[str] | None,
    run_by: str,
    allow_dirty_data: bool,
    allow_dirty_code: bool,
    jobs: int = 1,
) -> None:
    """ADR-007 §2: generate and run an OAT sensitivity batch -- a base member
    (every varying row central) plus one member per (row or latent factor,
    quantile). Fully deterministic, so ``scenario.seed`` isn't required."""
    _seed, _meta_json, recipe_tree_id = _recipe_identity(recipe_intent_path)
    rows = _unresolved_vary_names(recipe_intent_path)
    try:
        plan = oat_plan(rows, quantiles, only, factor_reach(recipe_intent_path))
    except EnsembleError as exc:
        raise click.ClickException(f"{scenario_name}: {exc}") from None
    perturbed = sorted({name for overrides in plan for name in overrides})

    batch, member_intent_dir, member_runs_dir = _reserve_batch(scenario_name, intent_dir, runs_dir)
    # Provisional (ADR-007 §5): if this run is interrupted, the partial batch
    # must still read as a sensitivity batch, never as Monte Carlo draws.
    write_batch_manifest(
        member_runs_dir, member_count=len(plan), failures=[],
        mode="sensitivity", quantiles=quantiles, rows=perturbed,
    )
    failures = _run_members(
        recipe_intent_path, member_intent_dir, member_runs_dir,
        list(enumerate(plan)),
        seed=None, recipe_tree_id=recipe_tree_id, mode="sensitivity",
        run_by=run_by, allow_dirty_data=allow_dirty_data, allow_dirty_code=allow_dirty_code,
        jobs=jobs,
    )
    write_batch_manifest(
        member_runs_dir, member_count=len(plan), failures=failures,
        mode="sensitivity", quantiles=quantiles, rows=perturbed,
    )
    _build_draws_db(member_runs_dir, member_intent_dir)
    _report_batch(f"sensitivity {scenario_name} ({batch})", len(plan), failures)


def _write_series_db(member_runs_dir: Path, series: list[MemberSeries], succeeded: int) -> None:
    """#298: write the batch's series store from its members' rows. Without
    every succeeded member's rows, or if writing fails, there's no store
    yet, and the batch's first reader builds one: a warning, not an error.
    A batch where no member succeeded gets none: an empty store would be
    trusted, and read as an ensemble of no futures."""
    if succeeded == 0:
        return
    if len(series) != succeeded:
        click.echo(f"warning: series store not written ({succeeded - len(series)} member(s) "
                   "unread); the first notebook or 'sim ensemble' read builds it", err=True)
        return
    try:
        write_series_db(member_runs_dir, series)
    except (sqlite3.Error, OSError) as exc:
        click.echo(f"warning: series store not written ({exc}); "
                   "the first notebook or 'sim ensemble' read builds it", err=True)


def _build_draws_db(member_runs_dir: Path, member_intent_dir: Path) -> None:
    """#261: gather the batch's draws into its draws.sqlite3. A batch whose
    members ran is worth keeping even if this fails, so a failure is a
    warning naming the command that retries it, not an error."""
    from analysis.sim.draws_db import build_draws_db

    try:
        build_draws_db(member_runs_dir, member_intent_dir)
    except (EnsembleError, sqlite3.Error, OSError) as exc:
        click.echo(f"warning: draws database not written ({exc}); "
                   "'sim ensemble draws --rebuild' retries it", err=True)


def _recipe_identity(recipe_intent_path: Path) -> tuple[int | None, str | None, str]:
    """(scenario.seed, scenario.meta_json, intent_manifest.scenario_tree_id)."""
    con = connect_sim_file(recipe_intent_path)
    try:
        return con.execute(
            "SELECT scenario.seed, scenario.meta_json, intent_manifest.scenario_tree_id "
            "FROM scenario, intent_manifest"
        ).fetchone()
    finally:
        con.close()


def _unresolved_vary_names(intent_path: Path) -> list[str]:
    con = connect_sim_file(intent_path)
    try:
        return varying_names(con)
    finally:
        con.close()


def _reserve_batch(scenario_name: str, intent_dir: Path, runs_dir: Path) -> tuple[str, Path, Path]:
    """Reserve a fresh timestamped batch: (batch name, member intent dir,
    member runs dir).

    One timestamped batch subfolder per invocation -- like an ordinary run
    file, a batch is never overwritten, so rerunning a recipe (more members,
    a tweaked distribution, ...) keeps every previous batch's history. A
    disambiguating suffix covers two runs within the same wall-clock second.
    Reserved by exclusive mkdir (no separate existence check first), so two
    concurrent `sim run` processes can never both win the same batch name
    and race on the same member files.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    suffix = 1
    while True:
        batch = stamp if suffix == 1 else f"{stamp}-{suffix}"
        member_intent_dir = intent_dir / scenario_name / batch
        member_runs_dir = runs_dir / scenario_name / batch
        try:
            member_intent_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            suffix += 1
            continue
        try:
            member_runs_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            suffix += 1
            continue
        return batch, member_intent_dir, member_runs_dir


def _run_members(
    recipe_intent_path: Path,
    member_intent_dir: Path,
    member_runs_dir: Path,
    specs: list[tuple[int, dict[str, float] | None]],
    *,
    seed: int | None,
    recipe_tree_id: str,
    mode: str,
    run_by: str,
    allow_dirty_data: bool,
    allow_dirty_code: bool,
    jobs: int = 1,
    series: list[MemberSeries] | None = None,
) -> list[tuple[int, Path, str]]:
    """Resolve and run each ``(member index, overrides)`` spec; return the
    failures as ``(index, member intent path, error)``, sorted by index. A
    failure is reported and skipped, never retried (ADR-006 §7). Given a
    ``series`` list, each succeeded member's series store rows (#298) are
    read by the process that ran it and appended there.

    With ``jobs > 1`` members run in a process pool (ADR-007 §8): they share
    no files, so the only ordering that changes is the progress lines, which
    are printed as members finish.
    """
    tasks = [
        (
            recipe_intent_path, member_intent_dir / f"member_{i:04d}.sqlite3",
            member_runs_dir / f"member_{i:04d}.sqlite3", i, overrides,
            seed, len(specs), recipe_tree_id, mode, run_by, allow_dirty_data, allow_dirty_code,
            series is not None,
        )
        for i, overrides in specs
    ]
    failures: list[tuple[int, Path, str]] = []

    def _report(i: int, overrides: dict[str, float] | None, intent_path: Path,
                run_path: Path, result: tuple[str | None, MemberSeries | None]) -> None:
        error, rows = result
        if rows is not None and series is not None:
            series.append(rows)
        if error is not None:
            failures.append((i, intent_path, error))
            click.echo(f"  member {i}: FAILED ({error}) -- intent: {intent_path}", err=True)
        else:
            label = "" if not overrides else " " + ", ".join(f"{k}@{q:g}" for k, q in overrides.items())
            click.echo(f"  member {i}:{label} {run_path}")

    if jobs <= 1 or len(tasks) <= 1:
        for task in tasks:
            _report(task[3], task[4], task[1], task[2], _run_one_member(*task))
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        with ProcessPoolExecutor(max_workers=min(jobs, len(tasks))) as pool:
            futures = {pool.submit(_run_one_member, *task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # the worker process itself died
                    result = (f"worker failed: {exc}", None)
                _report(task[3], task[4], task[1], task[2], result)
    return sorted(failures)


def _run_one_member(
    recipe_intent_path: Path, member_intent_path: Path, member_run_path: Path,
    member_index: int, overrides: dict[str, float] | None, seed: int | None, member_count: int,
    recipe_tree_id: str, mode: str, run_by: str, allow_dirty_data: bool, allow_dirty_code: bool,
    with_series: bool = False,
) -> tuple[str | None, MemberSeries | None]:
    """Resolve and run one member: the error text on failure, else ``None``,
    and with ``with_series`` its series store rows (#298), read here so
    they're read in parallel, or ``None`` if reading them failed (the store
    is then built when the batch is first opened). Module-level (not a
    closure) so a process pool can pickle it."""
    try:
        resolve_member(
            recipe_intent_path, member_intent_path,
            seed=seed, member_index=member_index, member_count=member_count,
            recipe_tree_id=recipe_tree_id, overrides=overrides, mode=mode,
        )
        run_scenario(
            member_intent_path, member_run_path,
            run_by=run_by,
            allow_dirty_data=allow_dirty_data, allow_dirty_code=allow_dirty_code,
        )
    except Exception as exc:
        return str(exc), None
    if not with_series:
        return None, None
    try:
        return None, member_series(member_run_path)
    except Exception:
        return None, None


def _report_batch(label: str, member_count: int, failures: list[tuple[int, Path, str]]) -> None:
    succeeded = member_count - len(failures)
    click.echo(f"{label}: {succeeded}/{member_count} member(s) succeeded")
    if failures:
        click.echo(f"  {len(failures)} failure(s):")
        for i, path, _error in failures:
            click.echo(f"    member {i}: {path}")


def _suggest_readme(target: Path) -> None:
    click.echo(f"  next: edit {target / 'README.md'} to say what the scenario is for; "
               "its title and first paragraph are what listings and notebooks show")


@sim_group_command.group(name="ensemble")
def ensemble_group_command() -> None:
    """Inspect or manage an ensemble recipe's generated members (ADR-006)."""


@ensemble_group_command.command("create")
@click.argument("name", required=False)
@click.argument("parent", required=False, type=completion.SCENARIO)
@click.argument("mixins", type=completion.SCENARIO, nargs=-1)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default=None,
              help="Default: 'Derived from <parent> + <mixin>...'.")
@click.option(
    "--seed", type=int, default=None,
    help="Ensemble master seed. Default: inherited when a parent is an ensemble, "
         "so the two pair member by member; otherwise a random 31-bit int, "
         "printed on creation.",
)
@click.option(
    "--new-seed", is_flag=True, default=False,
    help="Draw a fresh random seed even when a parent is an ensemble.",
)
@click.option("--no-parent", is_flag=True, default=False,
              help="Build on nothing: a mix-in only recipe, for mixing into others.")
@click.option(
    "--size", "ensemble_size", type=int, default=None,
    help="Default member count (scenario.meta_json.ensemble_size), so "
         "'sim run'/'sim all' can generate and run this recipe without "
         "--ensemble-size on every invocation; 'sim run --ensemble-size' "
         "can still override it per invocation. Default: inherited when a "
         f"parent is an ensemble, otherwise {registries.DEFAULT_ENSEMBLE_SIZE}. "
         "See docs/guides/ensembles.md for what size is actually appropriate "
         "for this recipe.",
)
def ensemble_create(
    name: str | None,
    parent: str | None,
    mixins: tuple[str, ...],
    data_dir: Path | None,
    description: str | None,
    seed: int | None,
    new_seed: bool,
    no_parent: bool,
    ensemble_size: int | None,
) -> None:
    """Create ensemble recipe NAME deriving from PARENT, and make it current (ADR-006).

    Each MIXIN is applied on top of PARENT, as for ``sim scenario create``.
    When the parents already make an ensemble, NAME inherits its seed and
    member count, so member N of each draws the same future for every row
    they share; --seed, --new-seed and --size override that.

    A recipe is an ordinary scenario until a calendar.csv row is marked
    varying (``sim calendar add <kind> ... --vary-distribution ...``);
    ``sim run NAME`` then generates and runs every member instead of a
    single run, using the recorded member count unless overridden by
    ``sim run NAME --ensemble-size N``.

    With --no-parent, NAME builds on nothing, as for ``sim scenario create``:
    a mix-in only recipe, with its own seed and member count.

    Without NAME or PARENT, they are asked for; the parents one at a time,
    and a blank answer, for no parent, must be confirmed.
    """
    if ensemble_size is not None and ensemble_size <= 0:
        raise click.ClickException(f"--size must be a positive integer (got {ensemble_size})")
    if seed is not None and new_seed:
        raise click.UsageError("--seed and --new-seed are exclusive")
    data_dir_p = _resolve_data_dir(data_dir)
    name, parents = _ask_new_scenario(data_dir_p, "ensemble", name, parent, mixins, no_parent=no_parent)
    parent, mixins = (parents[0], tuple(parents[1:])) if parents else (None, ())
    # Preflights PARENT, then creates and validates the recipe in a staged copy.
    with _scenario_edit(data_dir_p, parent or MIXIN_BASE, remember=False, template_parent=True) as edit:
        try:
            staged = sc.create_scenario(
                edit.data_dir, name, parent, description=description, mixins=mixins,
            )
        except Exception as exc:
            raise click.ClickException(f"failed to create scenario {name}: {exc}") from None
        # With nothing of its own yet, the new scenario's merged values are
        # exactly what it would inherit.
        inherited_seed, inherited_size = sc.ensemble_settings(edit.data_dir, name)
        from_ensemble = inherited_size is not None
        if seed is None and (new_seed or not from_ensemble or inherited_seed is None):
            seed = secrets.randbelow(2**31)
        if ensemble_size is None and not from_ensemble:
            ensemble_size = registries.DEFAULT_ENSEMBLE_SIZE

        scenario_csv = staged / "scenario.csv"
        df = pd.read_csv(scenario_csv, dtype=str, keep_default_na=False)
        if seed is not None:
            df.loc[0, "seed"] = str(seed)
        if ensemble_size is not None:
            meta = json.loads(df.loc[0, "meta_json"] or "{}")
            meta["ensemble_size"] = ensemble_size
            df.loc[0, "meta_json"] = json.dumps(meta, separators=(",", ":"))
        df.to_csv(scenario_csv, index=False)
    target = data_dir_p / "scenarios" / name

    _make_current(data_dir_p, name)
    click.echo(f"created ensemble recipe {target}")
    click.echo(f"  inherits: {_inherits_line(data_dir_p, parents)}")
    for label, own, inherited in (
        ("seed", seed, inherited_seed), ("ensemble_size", ensemble_size, inherited_size),
    ):
        if own is not None:
            click.echo(f"  {label}: {own}")
        elif inherited is not None:
            click.echo(f"  {label}: {inherited.value} (inherited)")
    _suggest_readme(target)
    click.echo(
        "  next: sim calendar add <kind> ... --vary-distribution ... to mark rows as "
        f"varying, then sim run {name}"
    )


@ensemble_group_command.command("list")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show a table with each ensemble's parents, dates, seed, "
                   "ensemble size and description.")
@click.option("-a", "--all", "all_", is_flag=True, default=False,
              help="Include the template ensembles (t_ names) read from the code repository.")
def ensemble_list(data_dir: Path | None, verbose: bool, all_: bool) -> None:
    """List the data repo's ensembles: ``sim scenario list``, keeping only
    the scenarios whose merged ensemble_size is positive."""
    _list_scenarios(data_dir, verbose, ensembles_only=True, templates=all_)


def _ensemble_batch_dir(runs_dir: Path, scenario_name: str, batch: str | None) -> Path:
    """The Monte Carlo batch ``sim ensemble summary``/``compare`` report on."""
    recipe_runs_dir = runs_dir / scenario_name
    batch_dir = (
        recipe_runs_dir / batch if batch
        else latest_ensemble_batch(recipe_runs_dir, mode="monte_carlo")
    )
    if batch_dir is None or not batch_dir.is_dir():
        raise click.ClickException(f"no ensemble runs found under {recipe_runs_dir}")
    if batch_mode(batch_dir) != "monte_carlo":
        # ADR-007 §5: a fan-chart summary over an OAT batch is meaningless.
        raise click.ClickException(
            f"{batch_dir.name} is a sensitivity batch; use "
            f"'sim ensemble sensitivity {scenario_name} --batch {batch_dir.name}'"
        )
    return batch_dir


def _echo_year_table(table: pd.DataFrame) -> None:
    """One header row over right-aligned columns, the same columns the
    notebook's table shows: with nine statistics a row, key=value fields
    made it hard to tell this was the same data (#155). Shares (a ``P(``
    or ``_reserve`` column) print as percents, the rest as dollars."""

    def cell(column: str, value: float) -> str:
        if column.startswith("P(") or column.endswith("_reserve"):
            return f"{value:.0%}"
        return f"{value:,.0f}"

    header = ["year", *table.columns]
    rows = [
        [str(year), *(cell(c, v) for c, v in zip(table.columns, row, strict=True))]
        for year, row in table.iterrows()
    ]
    widths = [max(len(cell) for cell in column) for column in zip(header, *rows, strict=True)]
    for line in (header, *rows):
        click.echo("  " + "  ".join(cell.rjust(width) for cell, width in zip(line, widths, strict=True)))


def _close_all(connections: dict[str, RunConnection]) -> None:
    for con in connections.values():
        con.close()


_ENSEMBLE_RUNS_DIR_OPTION = click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Default: data-repo state/runs when activated, else state/runs.",
)
_ENSEMBLE_DATA_DIR_OPTION = click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Data repo (default: $ANALYSIS_DATA_DIR)",
)
_ENSEMBLE_ACCOUNT_OPTION = click.option(
    "--account", default=None, type=completion.ACCOUNT,
    help="Report one account's balance instead of net worth.",
)
_ENSEMBLE_LOT_OPTION = click.option(
    "--lot", default=None, type=completion.LotName("account"),
    help="With --account: report one lot of the account instead of its total.",
)
_ENSEMBLE_RESERVE_OPTION = click.option(
    "--reserve", default=None,
    help="Reserve in CPI-constant dollars (e.g. 250000) for what's reported, "
         "overriding the recipe's scenario.meta_json reserve (which applies to "
         "net worth only).",
)
_PRE_TAX_OPTION = click.option(
    "--pre-tax", is_flag=True,
    help="Net worth before tax. By default it is after tax (#251): less the tax a "
         "drawdown of traditional balances and taxable gains would owe, and any tax "
         "due but not yet paid. An account's balance is always before tax.",
)
_ENSEMBLE_GRAPH_OPTION = click.option(
    "--graph", "as_graph", is_flag=True,
    help="After the tables, draw them as the notebook's chart in the terminal (kitty or "
         "iTerm2 image protocol), or write a PNG file when the terminal can't show images.",
)
_ENSEMBLE_GRAPH_FILE_OPTION = click.option(
    "--graph-file", type=click.Path(dir_okay=False, path_type=Path), default=None,
    help="After the tables, write the chart to this .png or .svg file.",
)


def _check_graph_options(as_graph: bool, graph_file: Path | None) -> None:
    if as_graph and graph_file is not None:
        raise click.UsageError("use one of --graph and --graph-file")
    _check_graph_file(graph_file)


def _ask_ensemble_run(runs_dir: Path, mode: str | None, prompt: str = "ensemble",
                      batch: str | None = None, exclude: str | None = None) -> str:
    """An ensemble with a run batch of ``mode`` (any, when None) under
    ``runs_dir`` asked for: the batch named ``batch`` when given; never
    ``exclude``."""
    def has_runs(recipe: Path) -> bool:
        if batch:
            return is_ensemble_batch(recipe / batch, mode)
        return latest_ensemble_batch(recipe, mode=mode, wait=False) is not None

    recipes = sorted(
        p.name for p in (runs_dir.iterdir() if runs_dir.is_dir() else ())
        if p.is_dir() and p.name != exclude and has_runs(p)
    )
    plural = f"ensembles with a batch {batch}" if batch else "ensembles with runs"
    return _ask_name("ensemble", recipes, prompt=prompt, plural=plural)


def _resolve_runs_dir(runs_dir: Path | None, data_dir: Path | None) -> Path:
    data_dir_available = data_dir is not None or bool(os.environ.get("ANALYSIS_DATA_DIR"))
    resolved_data_dir = _resolve_data_dir(data_dir) if data_dir_available else None
    return _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR, resolved_data_dir)


def _reserve_dollars(
    reserve: str | None, store: EnsembleSeries, account: str | None,
) -> float | None:
    """``--reserve``, applied to whatever is reported; without it, the
    recipe's own reserve, which is a net-worth floor and so applies to net
    worth only."""
    if reserve is None:
        return store.reserve() if account is None else None
    try:
        return units.dollars_to_cents(reserve) / 100
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--reserve") from exc


def _member_frame(
    store: EnsembleSeries, account: str | None, mode: DollarMode,
    lot: str | None = None, *, after_tax: bool = False,
) -> pd.DataFrame:
    try:
        return store.frame(account, mode, lot, after_tax=after_tax)
    except EnsembleError as exc:
        raise click.ClickException(str(exc)) from exc


def _open_series(batch_dir: Path) -> EnsembleSeries:
    """The batch's series store (#298), saying so when it must be built
    first: a batch run before the store existed, or read by other code."""
    from analysis.sim.series_db import series_db_current

    if not series_db_current(batch_dir):
        click.echo(f"building the series store for {batch_dir.parent.name} "
                   f"({batch_dir.name}); later reads are quick", err=True)
    try:
        return open_series(batch_dir)
    except EnsembleError as exc:
        raise click.ClickException(str(exc)) from exc


def _check_lot_has_account(account: str | None, lot: str | None) -> None:
    if lot is not None and account is None:
        raise click.UsageError("--lot needs --account")


def _subject(account: str | None, lot: str | None, after_tax: bool = False) -> str:
    """What an ensemble report is about, for its heading."""
    if account is None:
        return "after-tax net worth" if after_tax else "net worth"
    return f"account {account}" + (f" lot {lot}" if lot is not None else "")


@ensemble_group_command.command("summary")
@click.argument("scenario_name", required=False, type=completion.ENSEMBLE)
@_ENSEMBLE_RUNS_DIR_OPTION
@_ENSEMBLE_DATA_DIR_OPTION
@click.option(
    "--batch", default=None,
    help="Which generated batch (a 'sim run' timestamp) to summarize; default: the latest.",
)
@_ENSEMBLE_ACCOUNT_OPTION
@_ENSEMBLE_LOT_OPTION
@_ENSEMBLE_RESERVE_OPTION
@_PRE_TAX_OPTION
@_ENSEMBLE_GRAPH_OPTION
@_ENSEMBLE_GRAPH_FILE_OPTION
def ensemble_summary(
    scenario_name: str | None, runs_dir: Path | None, data_dir: Path | None, batch: str | None,
    account: str | None, lot: str | None, reserve: str | None, pre_tax: bool,
    as_graph: bool, graph_file: Path | None,
) -> None:
    """Mean/median/SD, 5th/20th/.../95th percentiles and the mean of the
    worst 5% and 20% (CVaR) of year-end net worth (or --account's balance,
    or one --lot of it) across SCENARIO_NAME's members, and the share below
    the reserve when one is set. --graph draws the members' fan chart.

    Without SCENARIO_NAME, an ensemble that has been run is asked for."""
    _check_lot_has_account(account, lot)
    _check_graph_options(as_graph, graph_file)
    resolved_runs_dir = _resolve_runs_dir(runs_dir, data_dir)
    if scenario_name is None:
        scenario_name = _ask_ensemble_run(resolved_runs_dir, "monte_carlo", batch=batch)
        known = _ask_again_once_known(scenario_name=scenario_name)
        account, lot = known["account"], known["lot"]
    after_tax = not pre_tax and account is None
    _note_use(data_dir, scenario_name)
    batch_dir = _ensemble_batch_dir(resolved_runs_dir, scenario_name, batch)
    statuses = member_run_statuses(batch_dir)
    failed = sorted(stem for stem, status in statuses.items() if status != "succeeded")
    store = _open_series(batch_dir)
    frame = _member_frame(store, account, DollarMode.NOMINAL, lot, after_tax=after_tax)
    reserve_dollars = _reserve_dollars(reserve, store, account)
    cpi_frame = (
        None if reserve_dollars is None
        else _member_frame(store, account, DollarMode.CPI_CONSTANT, lot,
                           after_tax=after_tax)
    )
    monthly_summary = summary_with_percentiles(frame)
    table = year_end_values(pd.concat(
        [monthly_summary, risk_summary(frame, reserve_dollars, cpi_frame)], axis=1,
    ))
    click.echo(
        f"ensemble {scenario_name} ({batch_dir.name}): {len(store)} succeeded "
        f"member(s), {len(failed)} failed"
        + (f"; {_subject(account, lot, after_tax)}" if account or after_tax else "")
        + ("" if reserve_dollars is None
           else f"; reserve ${reserve_dollars:,.0f} (CPI-constant)")
    )
    _echo_year_table(table)
    if failed:
        click.echo(f"  {len(failed)} failed member(s), excluded above:")
        for stem in failed:
            click.echo(f"    {stem}: {statuses[stem]}")
    if as_graph or graph_file is not None:
        from analysis.sim.ensemble_plots import ensemble_fan_figure

        # Nominal, as the table is, so no reserve line: the reserve is
        # in CPI-constant dollars, and the notebook draws it only there.
        _draw_figure(ensemble_fan_figure(
            monthly_summary,
            f"{_subject(account, lot, after_tax)} fan chart (nominal) ({scenario_name})",
            deaths=store.base_case_deaths(),
        ), graph_file, "ensemble-")


@ensemble_group_command.command("compare")
@click.argument("baseline", required=False, type=completion.ENSEMBLE)
@click.argument("variant", required=False, type=completion.ENSEMBLE)
@_ENSEMBLE_RUNS_DIR_OPTION
@_ENSEMBLE_DATA_DIR_OPTION
@_ENSEMBLE_ACCOUNT_OPTION
@_ENSEMBLE_LOT_OPTION
@_ENSEMBLE_RESERVE_OPTION
@click.option(
    "--dollars", type=click.Choice([m.value for m in DollarMode]),
    default=DollarMode.CPI_CONSTANT.value, show_default=True,
    help="Dollars the percentiles and tail means are reported in.",
)
@_PRE_TAX_OPTION
@_ENSEMBLE_GRAPH_OPTION
@_ENSEMBLE_GRAPH_FILE_OPTION
def ensemble_compare(
    baseline: str | None, variant: str | None, runs_dir: Path | None, data_dir: Path | None,
    account: str | None, lot: str | None, reserve: str | None, dollars: str,
    pre_tax: bool, as_graph: bool, graph_file: Path | None,
) -> None:
    """Compare VARIANT's ensemble with BASELINE's on risk (#163), each from
    its latest batch: by year end, each percentile breakpoint's delta, the
    delta in the mean of the worst 5% and 20% of members (CVaR), and, when
    the two recipes share a seed, the share of paired futures where VARIANT
    comes out ahead. Then each recipe's tail means and, when a reserve is
    set, the share of members below it.

    --graph draws the notebook's comparison panels: each percentile's delta,
    the paired futures' deltas when the recipes pair, the CVaR deltas and,
    with a reserve, the change in the shares below it. When only one recipe
    has the account, it draws that recipe's fan chart instead.

    Either ensemble left out is asked for, from those that have been run."""
    _check_lot_has_account(account, lot)
    _check_graph_options(as_graph, graph_file)
    resolved_runs_dir = _resolve_runs_dir(runs_dir, data_dir)
    if baseline is None or variant is None:
        if baseline is None:
            baseline = _ask_ensemble_run(resolved_runs_dir, "monte_carlo", "baseline", exclude=variant)
        if variant is None:
            variant = _ask_ensemble_run(resolved_runs_dir, "monte_carlo", "variant", exclude=baseline)
        known = _ask_again_once_known(baseline=baseline, variant=variant)
        account, lot = known["account"], known["lot"]
    after_tax = not pre_tax and account is None
    _note_use(data_dir, baseline, variant)
    mode = DollarMode(dollars)
    sides: dict[str, EnsembleSeries] = {}
    for name in (baseline, variant):
        sides[name] = _open_series(_ensemble_batch_dir(resolved_runs_dir, name, None))
    base_conns, variant_conns = sides[baseline], sides[variant]
    # An account only one recipe has gets that recipe's own tail table
    # below, with no deltas: there is nothing to subtract it from.
    frames: dict[str, pd.DataFrame] = {}
    for name, store in sides.items():
        try:
            frames[name] = store.frame(account, mode, lot, after_tax=after_tax)
        except EnsembleError:
            pass
    subject = _subject(account, lot, after_tax)
    if not frames:
        raise click.ClickException(f"no {subject} in {baseline} or {variant}")
    reserve_dollars = _reserve_dollars(reserve, base_conns, account)
    cpi_frames = {
        name: (
            frames[name] if mode == DollarMode.CPI_CONSTANT
            else sides[name].frame(account, DollarMode.CPI_CONSTANT, lot, after_tax=after_tax)
        )
        for name in frames
    } if reserve_dollars is not None else {}
    mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
    click.echo(
        f"{variant} vs {baseline}: {subject}, {mode_label} dollars; "
        f"{len(base_conns)} and {len(variant_conns)} member(s)"
    )
    pairs: list[tuple[str, str]] = []
    if len(frames) == 2:
        pairs, unpaired = pair_members(base_conns.member_ids(), variant_conns.member_ids())
        click.echo(
            f"  paired: {len(pairs)} future(s)" if pairs else f"  not paired: {unpaired}"
        )
        click.echo(f"{variant} − {baseline}, year end:")
        _echo_year_table(year_end_values(comparison_deltas(
            frames[baseline], frames[variant], pairs, reserve_dollars,
            (cpi_frames[baseline], cpi_frames[variant]) if cpi_frames else None,
        )))
    else:
        missing = variant if baseline in frames else baseline
        click.echo(f"  only one side: {missing} has no {subject}")

    reserve_note = (
        "" if reserve_dollars is None
        else f"; reserve ${reserve_dollars:,.0f} (CPI-constant)"
    )
    for name, frame in frames.items():
        click.echo(f"{name} tail, year end{reserve_note}:")
        _echo_year_table(year_end_values(
            risk_summary(frame, reserve_dollars, cpi_frames.get(name))
        ))
    _echo_ensemble_rail_deltas(data_dir, baseline, variant, sides, mode)
    if as_graph or graph_file is not None:
        from analysis.sim.ensemble_plots import ensemble_compare_figure, ensemble_fan_figure

        if len(frames) == 2:
            figure = ensemble_compare_figure(
                baseline, variant, frames[baseline], frames[variant], pairs, mode_label,
                reserve_dollars,
                (cpi_frames[baseline], cpi_frames[variant]) if cpi_frames else None,
                deaths=base_conns.base_case_deaths(),
            )
        else:
            (name, frame), = frames.items()
            figure = ensemble_fan_figure(
                summary_with_percentiles(frame), f"{subject} fan chart ({mode_label}) ({name})",
                reserve=reserve_dollars if mode == DollarMode.CPI_CONSTANT else None,
                deaths=sides[name].base_case_deaths(),
            )
        _draw_figure(figure, graph_file, "ensemble-")


def _ensemble_guard_rails(
    data_dir: Path | None, recipe: str, store: EnsembleSeries, mode: DollarMode,
    rail_names: Sequence[str] = (), *, required: bool = True,
) -> list[guard_rails.EnsembleRailResult] | None:
    """``recipe``'s rails (#305) on the store's members, in ``mode`` dollars.
    Rails are read from the data repo; without one, ``required`` fails and
    otherwise there are none to show (None)."""
    try:
        scenarios_dir = _resolve_data_dir(data_dir) / "scenarios"
    except click.ClickException:
        if required:
            raise
        return None
    try:
        rails = guard_rails.ensemble_rails(scenarios_dir, recipe)
    except guard_rails.GuardRailError as exc:
        for issue in exc.issues:
            click.echo(f"  error: {issue}", err=True)
        raise click.ClickException(
            f"the guard rails of {recipe!r} don't load ({len(exc.issues)} issue(s))") from None
    except LoadError:
        if required:
            raise click.ClickException(f"no scenario {recipe!r} in the data repo") from None
        return None
    if rail_names:
        known = {rail.name: rail for rail in rails}
        if missing := [n for n in rail_names if n not in known]:
            raise click.ClickException(
                f"no guard rail {', '.join(map(repr, missing))} in {recipe!r}"
                + (f"; it has {', '.join(sorted(known))}" if known else ""))
        rails = [known[n] for n in dict.fromkeys(rail_names)]
    if rails and mode == DollarMode.CPI_CONSTANT and store.frame(None, mode).isna().all().any():
        raise click.ClickException(
            f"a member of {recipe!r} has no 'cpi' inflation index, so no CPI-constant "
            "guard rail figures; pass --dollars nominal")
    return guard_rails.ensemble_results(store, rails, mode) if rails else []


def _echo_rail_share_table(frame: pd.DataFrame) -> None:
    """Shares (``past``, ``ever_past``, their ``Δ``) as percents, the rest
    as dollars, under one header row, as ``_echo_year_table`` prints."""
    def cell(column: str, value: float) -> str:
        if pd.isna(value):
            return ""
        if column.lstrip("Δ").startswith(("past", "ever_past")):
            return f"{value:+.0%}" if column.startswith("Δ") else f"{value:.0%}"
        return f"{value:+,.0f}" if column.startswith("Δ") else f"{value:,.0f}"

    header = ["year", *frame.columns]
    rows = [[str(year), *(cell(c, v) for c, v in zip(frame.columns, row, strict=True))]
            for year, row in frame.iterrows()]
    widths = [max(len(c) for c in column) for column in zip(header, *rows, strict=True)]
    for line in (header, *rows):
        click.echo("  " + "  ".join(c.rjust(w) for c, w in zip(line, widths, strict=True)))


_ENSEMBLE_RAIL_LEGEND = (
    "past: the share of members past the rail that year; ever_past: past it in some year so far;",
    "rms_over: the root mean square of how far past, members inside counting as zero;",
    "cum: the same for the cumulative margin",
)


@ensemble_group_command.command("guard-rails")
@click.argument("scenario_name", required=False, type=completion.ENSEMBLE)
@_ENSEMBLE_RUNS_DIR_OPTION
@_ENSEMBLE_DATA_DIR_OPTION
@click.option(
    "--batch", default=None,
    help="Which generated batch (a 'sim run' timestamp) to report on; default: the latest.",
)
@click.option("--rail", "rail_names", type=completion.GUARD_RAIL, multiple=True,
              help="Only this rail; repeat for more.")
@click.option(
    "--dollars", type=click.Choice([m.value for m in DollarMode]),
    default=DollarMode.CPI_CONSTANT.value, show_default=True,
    help="Dollars the RMS overshoot is reported in.",
)
@_ENSEMBLE_GRAPH_OPTION
@_ENSEMBLE_GRAPH_FILE_OPTION
def ensemble_guard_rails(
    scenario_name: str | None, runs_dir: Path | None, data_dir: Path | None, batch: str | None,
    rail_names: tuple[str, ...], dollars: str, as_graph: bool, graph_file: Path | None,
) -> None:
    """How an ensemble's members stand against its guard rails (#305).

    Per rail and year, annual and cumulative (`cum`): the share of members
    past it that year, the share past it in some year so far, and the RMS
    overshoot, the root mean square of how far past the members are, those
    inside counting as zero (so it mixes how often with how far: read it
    beside the share). Then per rail and form, over the whole run: the share
    of member-years past, the share of members ever past, the share past in
    their last year (for the cumulative form, the members who end the plan
    overdrawn) and the RMS over every member-year.

    The rails are the recipe's guard_rails.csv as it is now, read from the
    data repo: editing one reruns nothing. --graph draws the shares and RMS.

    Without SCENARIO_NAME, an ensemble that has been run is asked for."""
    _check_graph_options(as_graph, graph_file)
    resolved_runs_dir = _resolve_runs_dir(runs_dir, data_dir)
    if scenario_name is None:
        scenario_name = _ask_ensemble_run(resolved_runs_dir, "monte_carlo", batch=batch)
    _note_use(data_dir, scenario_name)
    mode = DollarMode(dollars)
    batch_dir = _ensemble_batch_dir(resolved_runs_dir, scenario_name, batch)
    store = _open_series(batch_dir)
    results = _ensemble_guard_rails(data_dir, scenario_name, store, mode, rail_names)
    if not results:
        click.echo(f"no guard rails in {scenario_name!r}; `sim guard-rail add` adds one")
        return
    click.echo(f"ensemble {scenario_name} ({batch_dir.name}): {len(store)} member(s); "
               f"guard rails, {_DOLLAR_LABELS[mode]}")
    for line in _ENSEMBLE_RAIL_LEGEND:
        click.echo(f"  {line}")
    shown = [r for r in results if r.applies]
    for r in results:
        if not r.applies:
            click.echo(f"  {r.rail.name} covers none of these members' years")
    for r in shown:
        click.echo(f"\n{r.rail.name} ({r.rail.sense}): {r.rail.describe()}")
        _echo_rail_share_table(guard_rails.ensemble_rail_frame(r))
    if not shown:
        return
    summary = guard_rails.ensemble_summary_frame(shown)
    click.echo("\nOver the whole run:")
    width = max(len(str(v)) for v in summary["rail"])
    click.echo(f"  {'rail':<{width}}  {'form':<10}  {'members':>7}  {'member-years past':>17}"
               f"  {'ever past':>9}  {'past at end':>11}  {'RMS past':>10}")
    for row in named_rows(summary):
        click.echo(f"  {row.rail:<{width}}  {row.form:<10}  {row.members:>7}  "
                   f"{float(row.past):>17.0%}  {float(row.ever_past):>9.0%}  "
                   f"{float(row.past_at_end):>11.0%}  {float(row.rms_over):>10,.0f}")
    if as_graph or graph_file is not None:
        from analysis.sim.guard_rail_charts import ensemble_guard_rail_figure

        unit = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
        _draw_figure(ensemble_guard_rail_figure(
            shown, f"Guard rails ({scenario_name})", unit=unit,
            deaths=store.base_case_deaths()), graph_file, "ensemble-")


def _echo_ensemble_rail_deltas(
    data_dir: Path | None, baseline: str, variant: str,
    sides: Mapping[str, EnsembleSeries], mode: DollarMode,
) -> None:
    """``sim ensemble compare``'s guard rails (#305): per rail both recipes
    have, matched by name, the variant's change in each share and RMS.
    Nothing without a data repo, or when neither recipe has rails."""
    found = {name: _ensemble_guard_rails(data_dir, name, sides[name], mode, required=False)
             for name in (baseline, variant)}
    if not any(found.values()):
        return
    frames = guard_rails.ensemble_comparison_frames(found[baseline] or [], found[variant] or [])
    if not frames:
        click.echo("guard rails: none in both recipes")
        return
    click.echo(f"guard rails, {variant} − {baseline} ({_DOLLAR_LABELS[mode]}): Δ of each share "
               "of members past and of the RMS overshoot")
    for rail, frame in frames.items():
        click.echo(f"{rail}:")
        _echo_rail_share_table(frame)


@ensemble_group_command.command("sensitivity")
@click.argument("scenario_name", required=False, type=completion.ENSEMBLE)
@click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Default: data-repo state/runs when activated, else state/runs.",
)
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Data repo (default: $ANALYSIS_DATA_DIR)",
)
@click.option(
    "--batch", default=None,
    help="Which generated batch (a 'sim run' timestamp) to report on; default: the latest "
         "batch of --mode.",
)
@click.option(
    "--mode", "mode", type=click.Choice(registries.ENSEMBLE_MODES), default=None,
    help="Which kind of batch to pick when --batch isn't given; default: the latest of either.",
)
@click.option(
    "--dollars", type=click.Choice([m.value for m in DollarMode]),
    default=DollarMode.CPI_CONSTANT.value, show_default=True,
)
@click.option("--relative", is_flag=True, default=False, help="Report deltas as % of the base.")
@click.option(
    "--measure", type=click.Choice(sensitivity.MEASURES), default="net_worth", show_default=True,
    help="net_worth at each year-end, or the lowest net worth up to it.",
)
@click.option(
    "--years", default=None, metavar="YYYY[,YYYY...]",
    help="Reporting years to print; default: first full year, every fifth year, final year.",
)
@click.option(
    "--negligible", type=click.FloatRange(0, 1, min_open=True, max_open=True),
    default=sensitivity.DEFAULT_NEGLIGIBLE, show_default=True,
    help="Share below which a row is a pin candidate (in every year).",
)
@click.option(
    "--attribute", "attribute", default=None, metavar="NAME",
    help="Sensitivity batches: break NAME's net-worth deltas down along --by, exactly.",
)
@click.option(
    "--by", "by", type=click.Choice(sensitivity.ATTRIBUTION_AXES), default="category",
    show_default=True, help="With --attribute: income/expense category, flow account, or "
                            "holding account.",
)
def ensemble_sensitivity(
    scenario_name: str | None, runs_dir: Path | None, data_dir: Path | None, batch: str | None,
    mode: str | None, dollars: str, relative: bool, measure: str, years: str | None,
    negligible: float, attribute: str | None, by: str,
) -> None:
    """Which of SCENARIO_NAME's varying rows matter, and when (ADR-007).

    On a sensitivity batch ('sim run --sensitivity'): per-year one-at-a-time
    deltas, swings, impacts and shares, plus a rank-by-year grid. On a Monte Carlo
    batch: per-year Spearman correlations and rank-regression coefficients with
    95% intervals, partial rank correlations and R^2, and which rows could be
    pinned to a literal (judged by the regression coefficient when rows are
    correlated). A correlated batch also shows how closely its draws reproduce
    the specified correlations.

    Without SCENARIO_NAME, an ensemble that has been run is asked for.
    """
    data_dir_available = data_dir is not None or bool(os.environ.get("ANALYSIS_DATA_DIR"))
    resolved_data_dir = _resolve_data_dir(data_dir) if data_dir_available else None
    resolved_runs_dir = _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR, resolved_data_dir)
    if scenario_name is None:
        scenario_name = _ask_ensemble_run(resolved_runs_dir, mode, batch=batch)
    recipe_runs_dir = resolved_runs_dir / scenario_name
    batch_dir = recipe_runs_dir / batch if batch else latest_ensemble_batch(recipe_runs_dir, mode=mode)
    if batch_dir is None or not batch_dir.is_dir():
        raise click.ClickException(f"no ensemble runs found under {recipe_runs_dir}")
    dollar_mode = DollarMode(dollars)
    try:
        requested_years = (
            [int(y) for y in years.split(",") if y.strip()] if years else None
        )
    except ValueError:
        raise click.UsageError(f"--years: not a comma-separated list of years: {years!r}") from None

    if attribute is not None:
        if batch_mode(batch_dir) != "sensitivity":
            raise click.UsageError("--attribute needs a sensitivity batch")
        if measure != "net_worth":
            raise click.UsageError(
                "--attribute decomposes net worth itself; --measure min_net_worth "
                "has no flow decomposition"
            )
        _report_attribution(
            scenario_name, batch_dir, attribute, by=by, dollar_mode=dollar_mode,
            years=requested_years, relative=relative,
        )
        return
    if batch_mode(batch_dir) == "sensitivity":
        _report_oat(
            scenario_name, batch_dir, dollar_mode, relative=relative, measure=measure,
            years=requested_years, negligible=negligible,
        )
    else:
        _report_correlation(
            scenario_name, batch_dir, dollar_mode, measure=measure,
            years=requested_years, negligible=negligible,
        )



@ensemble_group_command.command("draws")
@click.argument("scenario_name", required=False, type=completion.ENSEMBLE)
@_ENSEMBLE_RUNS_DIR_OPTION
@click.option(
    "--intent-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Default: data-repo state/intent when activated, else state/intent.",
)
@_ENSEMBLE_DATA_DIR_OPTION
@click.option(
    "--batch", default=None,
    help="Which generated batch (a 'sim run' timestamp); default: the latest batch of --mode.",
)
@click.option(
    "--mode", "mode", type=click.Choice(registries.ENSEMBLE_MODES), default=None,
    help="Which kind of batch to pick when --batch isn't given; default: the latest of either.",
)
@click.option(
    "--node", default=None, type=completion.DRAW_NODE,
    help="List one node's draws (a varying row, lifespan:<person> or a latent factor).",
)
@click.option(
    "--csv", "csv_file", type=click.Path(dir_okay=False, path_type=Path), default=None,
    help="Write the drawn members' values (member x node, display units) to this CSV.",
)
@click.option("--rebuild", is_flag=True, default=False,
              help="Gather the batch's draws database again from its member files.")
def ensemble_draws(
    scenario_name: str | None, runs_dir: Path | None, intent_dir: Path | None,
    data_dir: Path | None, batch: str | None, mode: str | None, node: str | None,
    csv_file: Path | None, rebuild: bool,
) -> None:
    """Every draw of an ensemble batch, and what it depends on (#261).

    Prints the path of the batch's draws database (draws.sqlite3, beside its
    member run files) and, per node, the distribution's own median and SD
    beside the drawn members' (member 0, the base, left out), with a
    Kolmogorov-Smirnov p-value of the draws' probabilities against uniform;
    then each correlated pair's implied and achieved rho. A batch run before
    the database existed gets one gathered from what its member files hold.

    Without SCENARIO_NAME, an ensemble that has been run is asked for."""
    from analysis.sim.draws_db import build_draws_db, draws_db_path, open_draws_db

    data_dir_available = data_dir is not None or bool(os.environ.get("ANALYSIS_DATA_DIR"))
    resolved_data_dir = _resolve_data_dir(data_dir) if data_dir_available else None
    resolved_runs_dir = _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR, resolved_data_dir)
    if scenario_name is None:
        scenario_name = _ask_ensemble_run(resolved_runs_dir, mode, batch=batch)
    recipe_runs_dir = resolved_runs_dir / scenario_name
    batch_dir = recipe_runs_dir / batch if batch else latest_ensemble_batch(recipe_runs_dir, mode=mode)
    if batch_dir is None or not batch_dir.is_dir():
        raise click.ClickException(f"no ensemble runs found under {recipe_runs_dir}")
    path = draws_db_path(batch_dir)
    if rebuild or not path.is_file():
        resolved_intent_dir = _resolve_state_dir(intent_dir, DEFAULT_INTENT_DIR, resolved_data_dir)
        try:
            build_draws_db(batch_dir, resolved_intent_dir / scenario_name / batch_dir.name)
        except EnsembleError as exc:
            raise click.ClickException(str(exc)) from None
    with open_draws_db(batch_dir) as con:
        con.row_factory = sqlite3.Row
        if node is not None:
            _echo_node_draws(con, node)
        else:
            _echo_draws_summary(con, path)
        if csv_file is not None:
            frame = pd.read_sql_query("SELECT * FROM draws_wide_display", con)
            frame.to_csv(csv_file, index=False)
            click.echo(f"wrote {len(frame)} member(s) x {len(frame.columns) - 1} node(s) to {csv_file}")


def _draw_cell(unit: str, value: Any, *, spread: bool = False) -> str:
    """A display-unit value (``node_display``, ``draw_display``) as text."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "—"
    if unit == "date":
        return f"{value:,.0f} days" if spread else str(value)
    if unit == "dollars":
        return f"${value:,.0f}"
    if unit == "percent":
        return f"{value:.2f}%"
    if unit == "years":
        return f"{value:.1f}"
    return f"{value:+.2f}" if not spread else f"{value:.2f}"


def _echo_draws_summary(con: sqlite3.Connection, path: Path) -> None:
    batch = con.execute("SELECT * FROM batch").fetchone()
    members = con.execute(
        "SELECT count(*), sum(status = 'succeeded') FROM member").fetchone()
    seed = "" if batch["master_seed"] is None else f", seed {batch['master_seed']}"
    click.echo(f"{batch['ensemble_name']} ({batch['batch']}): {batch['mode']}, "
               f"{members[0]} member(s), {members[1] or 0} succeeded{seed}")
    click.echo(f"  draws database: {path}")
    checks = {r["node"]: r for r in con.execute("SELECT * FROM draw_check_display")}
    nodes = con.execute("SELECT * FROM node_display ORDER BY node_kind = 'factor', name").fetchall()
    if not nodes:
        click.echo("  no varying rows")
        return
    click.echo("")
    if checks:
        rows = []
        for n in nodes:
            c = checks.get(n["name"])
            unit = n["unit"]
            rows.append([
                n["name"], n["distribution"] or n["node_kind"], "—" if c is None else str(c["n"]),
                _draw_cell(unit, None if c is None else c["expected_p50"]),
                _draw_cell(unit, None if c is None else c["drawn_p50"]),
                _draw_cell(unit, None if c is None else c["expected_sd"], spread=True),
                _draw_cell(unit, None if c is None else c["drawn_sd"], spread=True),
                "—" if c is None or c["ks_u_p"] is None else f"{c['ks_u_p']:.2f}",
            ])
        _echo_table(["node", "distribution", "n", "median", "drawn", "sd", "drawn",
                     "KS p(u)"], rows, left=2)
    else:
        # A sensitivity batch draws nothing; its members sit at set quantiles.
        _echo_table(["node", "distribution", "median"], [
            [n["name"], n["distribution"] or n["node_kind"], _draw_cell(n["unit"], n["median"])]
            for n in nodes
        ], left=2)
    # Every pair in a tree is in node_pair; a parent and child's own rho
    # is what was authored, so those show, and any other pair whose
    # implied rho falls outside what the batch achieved.
    pairs = con.execute(
        "SELECT * FROM node_pair WHERE kind = 'link' OR implied_rho < achieved_low "
        "OR implied_rho > achieved_high ORDER BY kind = 'implied', a, b").fetchall()
    implied = con.execute("SELECT count(*) FROM node_pair WHERE kind = 'implied'").fetchone()[0]
    if pairs:
        click.echo("\n  correlated pairs (rho; achieved from the scores' Kendall tau):")
        _echo_table(["a", "b", "kind", "implied", "achieved", "95% interval"], [
            [p["a"], p["b"], p["kind"], f"{p['implied_rho']:+.2f}",
             "—" if p["achieved_rho"] is None else f"{p['achieved_rho']:+.2f}",
             "—" if p["achieved_low"] is None
             else f"{p['achieved_low']:+.2f} to {p['achieved_high']:+.2f}"]
            for p in pairs
        ], left=3)
    if implied:
        click.echo(f"  ({implied} implied pair(s) in all, in node_pair; shown only when outside "
                   "their interval)")
    scales = con.execute(
        "SELECT tree_root, count(*), min(scale), avg(scale), max(scale) FROM tail_scale "
        "WHERE year IS NULL GROUP BY tree_root ORDER BY tree_root").fetchall()
    if scales:
        click.echo("\n  t-copula scales S per member:")
        _echo_table(["tree", "members", "min", "mean", "max"], [
            [root, str(count), f"{low:.2f}", f"{mean:.2f}", f"{high:.2f}"]
            for root, count, low, mean, high in scales
        ])


def _echo_node_draws(con: sqlite3.Connection, node: str) -> None:
    found = con.execute("SELECT * FROM node_display WHERE name = ?", (node,)).fetchone()
    if found is None:
        known = [name for (name,) in con.execute("SELECT name FROM node ORDER BY name")]
        raise click.UsageError(f"--node: {node!r} isn't drawn in this batch; it draws {', '.join(known)}")
    unit = found["unit"]
    click.echo(f"{node}: {found['distribution'] or found['node_kind']}"
               + (f", correlated with {found['correlate_with']} at rho {found['rho']:+g}"
                  if found["correlate_with"] else ""))
    _echo_table(["member", "value", "u", "z", "noise", "quantile"], [
        [f"{r['member_index']}{' (base)' if r['is_base'] else ''}", _draw_cell(unit, r["value"]),
         *("—" if r[c] is None else f"{r[c]:.4f}" for c in ("u", "z", "noise", "quantile"))]
        for r in con.execute(
            "SELECT * FROM draw_display WHERE node = ? ORDER BY member_index", (node,))
    ])


def _report_years(batch_dir: Path, available: list[int], requested: list[int] | None) -> list[int]:
    """``--years`` checked against the batch's reporting years, or the
    default set (first full year, every fifth year, final year)."""
    available = sorted(available)
    if requested is None:
        return sensitivity.default_report_years(available, sensitivity.months_per_year(batch_dir))
    missing = sorted(set(requested) - set(available))
    if missing:
        raise click.UsageError(
            f"--years: no reporting year(s) {missing}; available {available[0]}-{available[-1]}"
        )
    return requested


def _report_attribution(
    scenario_name: str, batch_dir: Path, row: str, *, by: str, dollar_mode: DollarMode,
    years: list[int] | None, relative: bool = False,
) -> None:
    """ADR-007 §6.1: one table per reporting year, one line per key, one
    column per quantile ``row`` was perturbed to. ``relative`` divides each
    year by the base's net worth that year (blank where it's near zero)."""
    try:
        by_quantile = sensitivity.oat_attribution(batch_dir, row, by=by, dollars=dollar_mode)
        if relative:
            base = sensitivity.base_net_worth(batch_dir, dollar_mode).abs()
            base = base.where(base >= sensitivity.RELATIVE_NEAR_ZERO_DOLLARS)
            by_quantile = {q: t.div(base.reindex(t.index), axis=0) for q, t in by_quantile.items()}
    except EnsembleError as exc:
        raise click.ClickException(str(exc)) from None
    tables = list(by_quantile.values())
    years = _report_years(batch_dir, [int(y) for y in tables[0].index], years)
    unit = "% of base" if relative else (
        "CPI-constant $" if dollar_mode == DollarMode.CPI_CONSTANT else "nominal $"
    )
    click.echo(f"attribution {scenario_name} ({batch_dir.name}): {row} by {by}, {unit}")
    quantiles = list(by_quantile)
    keys = [k for t in tables for k in t.columns if k not in (sensitivity.UNATTRIBUTED, sensitivity.TOTAL)]
    keys = list(dict.fromkeys(keys))
    header = "".join(f"{'@' + format(q, 'g'):>14}" for q in quantiles)
    for year in years:
        click.echo(f"\n  {year}{' ' * 27}{header}")
        for key in [*keys, sensitivity.UNATTRIBUTED, sensitivity.TOTAL]:
            values = [t.loc[year, key] if key in t.columns else 0.0 for t in tables]
            if key == sensitivity.UNATTRIBUTED and all(v == 0 or pd.isna(v) for v in values):
                continue
            label = f"{key} (!)" if key == sensitivity.UNATTRIBUTED else key
            cells = ("-" if pd.isna(v) else f"{v:+.2%}" if relative else f"{v:+,.0f}" for v in values)
            click.echo(f"    {label:<30}" + "".join(f"{c:>14}" for c in cells))


def _signed(value: float) -> str:
    """A correlation or coefficient for a report line; n/a where undefined."""
    return "n/a" if pd.isna(value) else f"{value:+.2f}"


def _report_correlation(
    scenario_name: str, batch_dir: Path, dollar_mode: DollarMode, *,
    measure: str, years: list[int] | None, negligible: float,
) -> None:
    """ADR-007 §6.2: rho [interval], SRRC [interval] and PRCC per row and
    year, R^2 per year, each latent factor's rho, the achieved score
    correlations of a correlated batch, and each row's keep / pin_candidate /
    inconclusive status -- by rho, or by SRRC when rows are correlated."""
    try:
        report = sensitivity.mc_report(batch_dir, measure=measure, dollars=dollar_mode)
    except EnsembleError as exc:
        raise click.ClickException(str(exc)) from None
    table, by = report.table, report.by
    n = int(table["n"].iloc[0])
    years = _report_years(batch_dir, [int(y) for y in table["year"].unique()], years)
    click.echo(
        f"correlation {scenario_name} ({batch_dir.name}): {measure}, {n} drawn member(s) "
        "(member 0 excluded)"
    )
    if report.correlated:
        click.echo(
            "  note: this batch's rows are correlated (ADR-008), so a row's rho includes its "
            "correlates' effects; rows are ranked and pinned by SRRC, which holds the others fixed"
        )
    # The member count rho needs follows from its interval alone. SRRC's
    # interval also narrows as R^2 rises and widens with collinearity, so
    # there's no fixed count to warn against; its intervals speak for it.
    needed = sensitivity.required_members(negligible)
    if by == "rho" and n < needed:
        click.echo(
            f"  warning: {n} members can't call a row negligible at {negligible:.0%}; "
            f"that needs about {needed} -- expect 'inconclusive'"
        )
    if not report.scores.empty:
        click.echo("\n  correlations achieved (rho from the scores' Kendall tau) vs expected:")
        for _, pair in report.scores.iterrows():
            flag = "  -- outside the interval" if pair["outside"] else ""
            click.echo(
                f"    {pair['a'] + ' ~ ' + pair['b']:<32} {pair['kind']:<8} "
                f"{_signed(pair['expected'])}  achieved {_signed(pair['achieved'])} "
                f"[{_signed(pair['low'])}, {_signed(pair['high'])}]{flag}"
            )
    factors = set(table.loc[table["factor"], "row"])
    for year in years:
        at = table[table["year"] == year]
        r2 = at["r2"].iloc[0]
        rows = at[~at["factor"]]
        if rows["srrc"].isna().all() and rows["rho"].isna().all():
            # correlation_table estimates nothing below 4 members, varying
            # outcome or not; say which it is.
            why = "too few members to estimate" if n < 4 else "outcome doesn't vary across members"
            click.echo(f"\n  {year}: {why}")
            continue
        if pd.isna(r2):
            # The regression ran, but with no residual degrees of freedom
            # there's no R^2 to adjust, so nothing in this year is trusted.
            click.echo(f"\n  {year}  R2=n/a  -- too few members for {len(rows)} rows")
        else:
            trust = "" if r2 >= sensitivity.R2_TRUST else (
                "  -- low: interactions or transitions dominate; try an OAT ladder"
            )
            click.echo(f"\n  {year}  R2={r2:.2f}{trust}")
        for _, row in rows.reindex(rows[by].abs().sort_values(ascending=False).index).iterrows():
            click.echo(
                f"    {row['row']:<24} rho={_signed(row['rho'])} [{_signed(row['rho_low'])}, "
                f"{_signed(row['rho_high'])}]  srrc={_signed(row['srrc'])} [{_signed(row['srrc_low'])}, "
                f"{_signed(row['srrc_high'])}]  prcc={_signed(row['prcc'])}"
            )
        for _, row in at[at["factor"]].iterrows():
            click.echo(
                f"    {sensitivity.row_label(row['row'], factors):<24} rho={_signed(row['rho'])} "
                f"[{_signed(row['rho_low'])}, {_signed(row['rho_high'])}]  (its family, through it)"
            )
    status = sensitivity.classify(table, negligible, by=by)
    click.echo(f"\n  status ({by}^2 vs {negligible:.0%}, every year):")
    for name, value in status.items():
        click.echo(f"    {name:<24} {value}")


def _fmt_delta(value: float, relative: bool) -> str:
    if pd.isna(value):
        return "-"
    return f"{value:+.1%}" if relative else f"{value:+,.0f}"


def _report_oat(
    scenario_name: str, batch_dir: Path, dollar_mode: DollarMode, *,
    relative: bool, measure: str, years: list[int] | None, negligible: float,
) -> None:
    try:
        deltas = sensitivity.oat_deltas(batch_dir, dollar_mode)
    except EnsembleError as exc:
        raise click.ClickException(str(exc)) from None
    statuses = member_run_statuses(batch_dir)
    failed = sorted(stem for stem, status in statuses.items() if status != "succeeded")
    years = _report_years(batch_dir, [int(y) for y in deltas["year"].unique()], years)
    factors = sensitivity.factor_rows(deltas)
    unit = "% of base" if relative else (
        "CPI-constant $" if dollar_mode == DollarMode.CPI_CONSTANT else "nominal $"
    )
    click.echo(
        f"sensitivity {scenario_name} ({batch_dir.name}): {measure}, {unit}; "
        f"{len(statuses) - len(failed)} succeeded member(s), {len(failed)} failed"
    )
    for year in years:
        table = sensitivity.oat_table(deltas, year, measure=measure, relative=relative)
        click.echo(f"\n  {year}")
        value_cols = [c for c in table.columns if c.startswith("value@")]
        delta_cols = [c for c in table.columns if c.startswith("delta@")]
        for name, row in table.iterrows():
            label = sensitivity.row_label(str(name), factors)
            values = " ".join(f"{c[6:]}={row[c]}" for c in value_cols)
            deltas_text = " ".join(f"{_fmt_delta(row[c], relative):>12}" for c in delta_cols)
            click.echo(
                f"    {label:<24} {deltas_text}  swing={_fmt_delta(row['swing'], relative):>12}  "
                f"impact={_fmt_delta(row['impact'], relative).lstrip('+'):>11}  "
                f"share={'-' if pd.isna(row['share']) else format(row['share'], '.0%'):>4}  "
                f"{row['flags']:<16} ({values})"
            )

    grid = sensitivity.rank_by_year(deltas, measure=measure, relative=relative, value="rank")
    click.echo("\n  rank by year:")
    click.echo("    " + " " * 24 + " ".join(f"{str(y)[-2:]:>3}" for y in grid.columns))
    for name, ranks in grid.iterrows():
        label = sensitivity.row_label(str(name), factors)
        click.echo(f"    {label:<24}" + " ".join(f"{'-' if pd.isna(r) else r:>3}" for r in ranks))

    pins = sensitivity.pin_candidates(deltas, negligible, measure=measure)
    if pins:
        click.echo(
            f"\n  pin candidates (share < {negligible:.0%} in every year): {', '.join(pins)}"
        )
    if failed:
        click.echo(f"\n  {len(failed)} failed member(s), excluded above:")
        for stem in failed:
            click.echo(f"    {stem}: {statuses[stem]}")


@sim_group_command.command(name="all")
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Data repo (default: $ANALYSIS_DATA_DIR)",
)
@click.option(
    "--intent-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Default: data-repo state/intent when activated, else state/intent.",
)
@click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Default: data-repo state/runs when activated, else state/runs.",
)
@click.option("--allow-dirty-data", is_flag=True, default=False)
@click.option("--allow-dirty-code", is_flag=True, default=False)
@click.option(
    "--run-by",
    default=None,
    help="Identifier recorded in simulation_run.run_by (default: $USER)",
)
@click.option(
    "--force", is_flag=True, default=False,
    help="Rerun every scenario, even one whose latest run can't have changed.",
)
@click.pass_context
@_builds
def all_scenarios(
    ctx: click.Context,
    data_dir: Path | None,
    intent_dir: Path | None,
    runs_dir: Path | None,
    allow_dirty_data: bool,
    allow_dirty_code: bool,
    run_by: str | None,
    force: bool,
) -> None:
    """Load and run every scenario in the data repo.

    A scenario whose latest run (or ensemble batch) finished with the same
    code (src/, templates/, pyproject.toml, uv.lock), Python and data tree it would run
    with now, all committed, is skipped: rerunning it would give the same
    numbers. --force reruns it anyway.

    A disabled scenario (`sim scenario disable`) isn't built.

    Reports failures per scenario and keeps going; does not stop or fail the
    command itself.
    """
    from analysis.sim.freshness import code_tree, unchanged_run

    resolved = _resolve_data_dir(data_dir)
    try:
        names = list_scenario_names(resolved)
    except LoadError as exc:
        raise click.ClickException("; ".join(exc.issues)) from None
    if not names:
        raise click.ClickException(f"no scenarios found under {resolved / 'scenarios'}")

    failures: list[tuple[str, str]] = []
    skipped: list[str] = []
    code = code_tree()
    resolved_runs_dir = _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR, resolved)
    if disabled := sorted(set(names) & read_disabled_scenarios(resolved)):
        names = [name for name in names if name not in disabled]
        click.echo(f"disabled, not built: {', '.join(disabled)} (`sim run NAME` builds one)")
    if mixins := [name for name in names if is_mixin_only(resolved / "scenarios", name)]:
        names = [name for name in names if name not in mixins]
        click.echo(f"mix-in only, not run: {', '.join(mixins)}")
    for name in names:
        click.echo(f"=== {name} ===")
        unchanged = None if force else unchanged_run(resolved, resolved_runs_dir, name, code)
        if unchanged is not None:
            skipped.append(name)
            click.echo(f"  unchanged since {unchanged.path.name}; skipped (--force reruns it)")
            continue
        try:
            ctx.invoke(
                load,
                scenario_name=name,
                data_dir=data_dir,
                intent_dir=intent_dir,
                runs_dir=runs_dir,
                allow_dirty_data=allow_dirty_data,
                allow_dirty_code=allow_dirty_code,
                run_by=run_by,
                run_after_load=True,
            )
        except click.ClickException as exc:
            failures.append((name, exc.format_message()))
            click.echo(f"  error: {exc.format_message()}", err=True)

    click.echo()
    unchanged_note = f" ({len(skipped)} unchanged, skipped)" if skipped else ""
    if failures:
        click.echo(f"{len(failures)} of {len(names)} scenario(s) failed{unchanged_note}:")
        for name, message in failures:
            click.echo(f"  {name}: {message}")
    else:
        click.echo(f"all {len(names)} scenario(s) succeeded{unchanged_note}")


def _read_data_version(intent_path: Path) -> str | None:
    con = connect_sim_file(intent_path)
    try:
        row = con.execute(
            "SELECT data_version FROM intent_manifest WHERE id = 1"
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


@sim_group_command.command(name="list")
@click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Default: data-repo state/runs when activated, else state/runs.",
)
def list_runs(runs_dir: Path | None) -> None:
    """List run files in --runs-dir with status, scenario, who, when."""
    resolved_runs_dir = _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR)
    if not resolved_runs_dir.exists():
        click.echo(f"(no runs dir: {resolved_runs_dir})")
        return
    files = sorted(resolved_runs_dir.glob("*.sqlite3"))
    if not files:
        click.echo(f"(no run files in {resolved_runs_dir})")
        return
    header = f"{'status':<10}  {'scenario':<24}  {'finished_at':<20}  {'run_by':<16}  file"
    click.echo(header)
    click.echo("-" * len(header))
    for f in files:
        try:
            con = connect_sim_file(f"file:{f}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT status, scenario_name, finished_at, run_by "
                    "FROM simulation_run WHERE id = 1"
                ).fetchone()
            finally:
                con.close()
        except sqlite3.DatabaseError as exc:
            click.echo(f"{'unreadable':<10}  {'-':<24}  {'-':<20}  {'-':<16}  {f.name} ({exc})")
            continue
        if row is None:
            click.echo(f"{'empty':<10}  {'-':<24}  {'-':<20}  {'-':<16}  {f.name}")
            continue
        status, name, finished, by = row
        click.echo(
            f"{status:<10}  {name:<24}  {(finished or '-'):<20}  {(by or '-'):<16}  {f.name}"
        )


def _run_target_options(fn):
    """The options ``sim explain`` and ``sim networth`` share for finding a run."""
    for option in reversed([
        click.option("--runfile", type=click.Path(exists=True, dir_okay=False, path_type=Path),
                     default=None, help="Run file (default: the scenario's latest run)."),
        click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
                     default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)."),
        click.option("--runs-dir", type=click.Path(file_okay=False, path_type=Path),
                     default=None, help="Directory to search for the latest run."),
    ]):
        fn = option(fn)
    return fn


def _resolve_run_target(
    target: str | None, runfile: Path | None, data_dir: Path | None, runs_dir: Path | None,
) -> Path:
    """The run file a run-summary command reads: ``--runfile``, a TARGET that
    names an existing file, or else the latest run of the scenario TARGET names
    (the current scenario when it is omitted)."""
    if runfile is not None:
        if target is not None:
            raise click.ClickException("give a scenario or --runfile, not both")
        return runfile
    if target is not None and Path(target).is_file():
        return Path(target)
    ref = _resolve_scenario(_resolve_data_dir(data_dir), target, runs_dir=runs_dir)
    if ref.run_path is None:
        raise click.ClickException(
            f"no runs for {ref.name!r} in {ref.runs_dir}; run `sim run` first"
        )
    return ref.run_path


@sim_group_command.command()
@click.argument("target", type=completion.SCENARIO_OR_RUNFILE, metavar="[SCENARIO | RUNFILE]", required=False)
@_run_target_options
def explain(
    target: str | None, runfile: Path | None, data_dir: Path | None, runs_dir: Path | None,
) -> None:
    """Dump scenario name, versions, counts, lots, year-end net worth,
    negative-balance warnings and basis step-ups at a death for a run: the
    scenario's latest (the current scenario's by default), or a run file.
    Sanity dump."""
    runfile = _resolve_run_target(target, runfile, data_dir, runs_dir)
    con = connect_sim_file(f"file:{runfile}?mode=ro", uri=True)
    try:
        run = con.execute(
            "SELECT scenario_name, status, started_at, finished_at, failure_reason, "
            "code_version, code_dirty, data_version, data_dirty, "
            "run_by, host, python_version "
            "FROM simulation_run WHERE id = 1"
        ).fetchone()
        if run is None:
            raise click.ClickException(f"no simulation_run row in {runfile}")
        (name, status, started, finished, failure, cv, cd, dv, dd,
         by, host, py) = run
        click.echo(f"file:           {runfile}")
        click.echo(f"scenario:       {name}")
        click.echo(f"status:         {status}" + (f"  ({failure})" if failure else ""))
        click.echo(f"started:        {started}")
        click.echo(f"finished:       {finished or '-'}")
        click.echo(f"code:           {cv[:12]}{' (dirty)' if cd else ''}")
        click.echo(f"data:           {dv[:12]}{' (dirty)' if dd else ''}")
        click.echo(f"run_by:         {by}@{host}  python {py}")

        steps = con.execute("SELECT COUNT(*) FROM simulation_step").fetchone()[0]
        states = con.execute("SELECT COUNT(*) FROM account_state").fetchone()[0]
        xfers = con.execute("SELECT COUNT(*) FROM transfer_event").fetchone()[0]
        click.echo(
            f"counts:         steps={steps}  account_states={states}  transfers={xfers}"
        )

        lots = con.execute(
            "SELECT a.name, a.subkind, m.lot, m.symbol, ls.quantity_micros, "
            "       ls.balance, ls.cost_basis "
            "FROM account_lot m "
            "JOIN account a ON a.id = m.account_id "
            "JOIN lot_state ls ON ls.lot_id = m.id AND ls.step_id = "
            "  (SELECT id FROM simulation_step ORDER BY step_index DESC LIMIT 1) "
            "WHERE a.subkind IN ({}) OR a.id IN "
            "  (SELECT account_id FROM account_lot "
            "   GROUP BY account_id HAVING COUNT(*) > 1) "
            "ORDER BY a.name, m.id".format(
                ",".join("?" * len(INVESTMENT_ASSET_SUBKINDS))
            ),
            sorted(INVESTMENT_ASSET_SUBKINDS),
        ).fetchall()
        if lots:
            click.echo("")
            click.echo("lots at the final step:")
            for name, subkind, lot, symbol, quantity, balance, basis in lots:
                click.echo(
                    f"  {_account_with_lot(name, subkind, lot):<44}"
                    f"{(symbol or '-'):<8}{_fmt_quantity(quantity):>16}"
                    f"  balance {_fmt_cents(balance)}  basis {_fmt_cents(basis)}"
                )

        click.echo("")
        click.echo(f"year-end net worth:  {'nominal':>18}  {'CPI-constant':>18}")
        rows = con.execute(
            "WITH year_end AS ("
            "  SELECT substr(month,1,4) AS year, MAX(month) AS m "
            "  FROM simulation_step GROUP BY substr(month,1,4)"
            ") "
            "SELECT y.year, nw.net_worth, cpi.constant_cents "
            "FROM year_end y JOIN net_worth nw ON nw.month = y.m "
            "LEFT JOIN net_worth_constant_cpi cpi ON cpi.month = y.m "
            "ORDER BY y.year"
        ).fetchall()
        for year, nw, real in rows:
            click.echo(f"  {year}  {'':13}{_fmt_cents(nw)}  {_fmt_cents(real):>18}")

        click.echo("")
        _echo_negative_balance_warnings(con)
        _echo_basis_step_ups(con)
    finally:
        con.close()
    _echo_guard_rail_episodes(runfile, data_dir)


def _echo_guard_rail_episodes(runfile: Path, data_dir: Path | None) -> None:
    """Each stretch of years the run spent past one of its scenario's guard
    rails (#305), annual and cumulative, in CPI-constant dollars. The rails
    are read from the data repo, so a run file read without one, or whose
    scenario is gone, says nothing of them."""
    try:
        scenarios_dir = _resolve_data_dir(data_dir) / "scenarios"
    except click.ClickException:
        return
    with RunConnection(runfile) as rc:
        try:
            rails = guard_rails.run_rails(rc, scenarios_dir)
        except guard_rails.GuardRailError as exc:
            click.echo(f"guard rails: not read, {len(exc.issues)} issue(s); `sim check` lists them")
            return
        except LoadError:
            return
        if not rails:
            return
        mode = DollarMode.CPI_CONSTANT if rc.cpi_index().notna().all() else DollarMode.NOMINAL
        lines = guard_rails.episode_lines(guard_rails.evaluate_run(rc, rails, mode))
    if not lines:
        click.echo(f"guard rails: never past any of {len(rails)}")
        return
    click.echo(f"guard rails past, {_DOLLAR_LABELS[mode]}:")
    for line in lines:
        click.echo(f"  {line}")


def _echo_basis_step_ups(con: sqlite3.Connection) -> None:
    """The run's basis step-ups at a death (#255): the month, the lot, whose
    death, and its basis before and after. A run file from before them has
    no table, and says nothing."""
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'basis_step_up'"
    ).fetchone() is None:
        return
    rows = con.execute(
        "SELECT b.date, a.name, a.subkind, m.lot, b.person, b.balance, "
        "       b.basis_before, b.basis_after "
        "FROM basis_step_up b JOIN account_lot m ON m.id = b.lot_id "
        "JOIN account a ON a.id = m.account_id ORDER BY b.id"
    ).fetchall()
    if not rows:
        click.echo("basis step-ups at a death: none")
        return
    click.echo(f"basis step-ups at a death ({len(rows)}):")
    names = [_account_with_lot(name, subkind, lot) for _d, name, subkind, lot, *_ in rows]
    width = max(len(n) for n in names)
    who = max(len(row[4]) for row in rows)

    def money(cents: int) -> str:
        return f"${cents / 100:,.2f}"

    for name, (month, _a, _s, _l, person, balance, before, after) in zip(names, rows, strict=True):
        click.echo(
            f"  {month[:7]}  {name:<{width}}  {person:<{who}}  balance {money(balance)}"
            f"  basis {money(before)} -> {money(after)}"
        )


def _echo_negative_balance_warnings(con: sqlite3.Connection) -> None:
    """The run's recorded negative-balance warnings (ADR-003 §4.7): the
    month each asset account went negative, its balance then, and what its
    refill chain did, worded as the run's warning was."""
    (raw,) = con.execute(
        "SELECT json_extract(run_args_json, '$.negative_balance_warnings') "
        "FROM simulation_run WHERE id = 1"
    ).fetchone()
    events = json.loads(raw) if raw else []
    if not events:
        click.echo("negative-balance warnings: none")
        return
    click.echo(f"negative-balance warnings ({len(events)}):")
    width = max(len(str(e.get("account", ""))) for e in events)
    for event in events:
        click.echo(
            f"  {event.get('month', '-')}  {str(event.get('account', '-')):<{width}}"
            f"  {_fmt_cents(event.get('balance_cents')).strip()}"
            f"  {describe_negative_balance_event(event)}"
        )


@sim_group_command.command("networth")
@click.argument("scenario", type=completion.SCENARIO, required=False)
@click.option("--monthly", "period", flag_value="monthly",
              help="One row per month.")
@click.option("--yearly", "period", flag_value="yearly", default=True,
              help="One row per year, at its last month (the default).")
@click.option("--dollars", type=click.Choice([m.value for m in DollarMode]),
              default=DollarMode.CPI_CONSTANT.value, show_default=True,
              help="cpi_constant (deflated by the run's CPI series, so decades "
                   "compare at a glance), or nominal.")
@_PRE_TAX_OPTION
@_run_target_options
def networth_command(
    scenario: str | None, period: str, dollars: str, pre_tax: bool,
    runfile: Path | None, data_dir: Path | None, runs_dir: Path | None,
) -> None:
    """Net worth by year or month from a run: the scenario's latest (the
    current scenario's by default), or --runfile. As in net_worth.ipynb.
    It is after the tax still owed on it (#251) unless --pre-tax."""
    runfile = _resolve_run_target(scenario, runfile, data_dir, runs_dir)
    if not pre_tax:
        _echo_after_tax_networth(runfile, period, DollarMode(dollars))
        return
    # Nominal reads net_worth itself, so a run without a CPI series still has it.
    view, column = (("net_worth_constant_cpi", "constant_cents")
                    if dollars == DollarMode.CPI_CONSTANT else ("net_worth", "net_worth"))
    if period == "monthly":
        where = ""
    else:
        # A year's row is its last simulated month, as in `sim explain`.
        where = ("WHERE month IN (SELECT MAX(month) FROM simulation_step "
                 "GROUP BY substr(month, 1, 4)) ")
    con = connect_sim_file(f"file:{runfile}?mode=ro", uri=True)
    try:
        (name,) = con.execute(
            "SELECT scenario_name FROM simulation_run WHERE id = 1"
        ).fetchone() or ("?",)
        rows = con.execute(
            f"SELECT month, {column} FROM {view} {where}ORDER BY month"
        ).fetchall()
        if not rows and view != "net_worth" and con.execute(
            "SELECT 1 FROM net_worth LIMIT 1"
        ).fetchone():
            # The CPI view joins the run's 'cpi' inflation index; a run without
            # one has net worth but nothing to deflate it by.
            raise click.ClickException(
                f"{runfile.name} has no 'cpi' inflation index, so no CPI-constant "
                "net worth; pass --dollars nominal"
            )
    finally:
        con.close()
    label = "CPI-constant" if dollars == DollarMode.CPI_CONSTANT else "nominal"
    click.echo(f"net worth, {label} dollars ({name}, run {runfile.name}):")
    for month, cents in rows:
        when = month if period == "monthly" else month[:4]
        click.echo(f"  {when}  {_fmt_cents(cents)}")


def _echo_after_tax_networth(runfile: Path, period: str, mode: DollarMode) -> None:
    """``sim networth`` without --pre-tax: net worth, the deferred and unpaid tax
    taken from it, and what's left, from the run's RunConnection."""
    with RunConnection(runfile) as rc:
        name = rc.name
        frame = rc.after_tax_net_worth_frame()
        pre = rc.net_worth_series(mode)
        taxes = frame[["deferred_tax", "unpaid_tax"]]
        if mode == DollarMode.CPI_CONSTANT:
            if pre.isna().all() and not pre.empty:
                raise click.ClickException(
                    f"{runfile.name} has no 'cpi' inflation index, so no CPI-constant "
                    "net worth; pass --dollars nominal")
            taxes = rc.to_cpi_constant(taxes)
    table = pd.DataFrame({
        "net_worth": pre, "deferred_tax": taxes["deferred_tax"],
        "unpaid_tax": taxes["unpaid_tax"],
        "after_tax": frame["cpi_constant" if mode == DollarMode.CPI_CONSTANT else "nominal"],
    })
    if period != "monthly":
        table = table.groupby(index_years(table)).tail(1)
    label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
    click.echo(f"after-tax net worth, {label} dollars ({name}, run {runfile.name}):")
    _echo_table(
        ["month" if period == "monthly" else "year", *table.columns],
        ([month.strftime("%Y-%m") if period == "monthly" else str(month.year),
          *(_cell(float(v)) for v in row)]
         for month, row in zip(table.index, table.itertuples(index=False), strict=True)),
    )


COMPARE_SECTIONS = ("runs", "differences", "net-worth", "break-even", "guard-rails", "account")
"""``sim compare``'s sections, in the order it prints them (#188)."""

_COMPARE_MAX_GRAPHED = 4
"""The most variants ``sim compare --graph`` draws: more lines than that,
and the chart can't be read (the notebook stops at four too)."""


def _echo_table(columns: Sequence[str], rows: Iterable[Sequence[str]], *, left: int = 1) -> None:
    """``rows`` under a header of ``columns``, two spaces in: the first
    ``left`` columns (names) aligned left, the rest (values) right."""
    rows = [list(row) for row in rows]
    widths = [max(len(cell) for cell in column) for column in zip(columns, *rows, strict=True)]
    for line in (list(columns), *rows):
        click.echo("  " + "  ".join(
            cell.ljust(width) if i < left else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(line, widths, strict=True))
        ).rstrip())


def _cell(value: Any) -> str:
    """A table cell: missing as an em dash, a float as whole dollars."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "—"
    if isinstance(value, float):
        return f"{value:,.0f}"
    return str(value)


def _echo_frame(frame: pd.DataFrame) -> None:
    """``frame`` as a table, its index levels as the leading name columns."""
    names = [str(name or "") for name in frame.index.names]
    _echo_table(
        [*names, *map(str, frame.columns)],
        ([*map(str, key if isinstance(key, tuple) else (key,)), *map(str, row)]
         for key, row in zip(frame.index, frame.itertuples(index=False), strict=True)),
        left=len(names),
    )


def _compare_variants(data: Path, runs_dir: Path, baseline: str) -> list[str]:
    """A variant to compare ``baseline`` with, asked for from the scenarios
    that have a run."""
    choices = [name for name in list_scenario_names(data)
               if name != baseline and scenario_run_path(runs_dir, name, wait=False) is not None]
    return [_ask_name("scenario", choices, prompt="variant", plural="scenarios with runs")]


@sim_group_command.command("compare")
@click.argument("baseline", type=completion.SCENARIO, required=False)
@click.argument("variants", type=completion.SCENARIO, nargs=-1)
@click.option("--section", "sections", type=click.Choice(COMPARE_SECTIONS), multiple=True,
              help="Print only this section; repeat for more (default: all, the account "
                   "section only with --account).")
@click.option("--dollars", type=click.Choice([m.value for m in DollarMode]),
              default=DollarMode.CPI_CONSTANT.value, show_default=True,
              help="Dollars net worth and balances are reported in.")
@_PRE_TAX_OPTION
@click.option("--account", type=completion.ACCOUNT, default=None,
              help="Also compare this account's balance (a section of its own).")
@click.option("--lot", type=completion.LotName("account"), default=None,
              help="With --account: one lot of the account instead of its total.")
@click.option("--graph", "as_graph", is_flag=True,
              help="After the tables, draw net worth (or --account's balance) and each "
                   "variant's difference in the terminal (kitty or iTerm2 image protocol), "
                   "or write a PNG file when the terminal can't show images.")
@click.option("--graph-file", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="After the tables, write that chart to this .png or .svg file.")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
@click.option("--runs-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Directory to find each scenario's latest run in.")
def compare_command(
    baseline: str | None, variants: tuple[str, ...], sections: tuple[str, ...], dollars: str,
    pre_tax: bool, account: str | None, lot: str | None, as_graph: bool, graph_file: Path | None,
    data_dir: Path | None, runs_dir: Path | None,
) -> None:
    """Compare VARIANTS with BASELINE (#188), each from its latest run (an
    ensemble's from member 0 of its latest batch), as scenario_compare.ipynb
    does: whether the runs are comparable, what the scenarios define
    differently, net worth at each year end with each variant's difference,
    where each variant breaks even (net worth after tax unless --pre-tax),
    each scenario's guard rail margins (#305) when any has rails, and with
    --account, that account's balance.

    BASELINE left out is the current scenario; with no VARIANTS, one is
    asked for. A scenario with no accounts is a valid compare point."""
    from analysis.sim import compare

    if lot is not None and account is None:
        raise click.UsageError("--lot needs --account")
    if "account" in sections and account is None:
        raise click.UsageError("--section account needs --account")
    _check_graph_options(as_graph, graph_file)
    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, baseline, runs_dir=runs_dir)
    baseline = ref.name
    if not variants:
        variants = tuple(_compare_variants(data, ref.runs_dir, baseline))
    if baseline in variants or len(set(variants)) < len(variants):
        raise click.UsageError("name each scenario once, the baseline only as BASELINE")
    # Every scenario compared is in use (#322), the baseline topmost.
    working_set.use(data, baseline, *variants)
    if (as_graph or graph_file is not None) and len(variants) > _COMPARE_MAX_GRAPHED:
        raise click.UsageError(
            f"--graph draws at most {_COMPARE_MAX_GRAPHED} variants; name fewer, "
            "or leave --graph off for the tables")
    after_tax = not pre_tax
    shown = set(sections) or {*COMPARE_SECTIONS[:-1], *(["account"] if account else [])}
    mode = DollarMode(dollars)
    mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
    scenarios = [baseline, *variants]
    with ExitStack() as stack:
        connections: dict[str, RunConnection] = {}
        # Matched against the folder list, not joined onto a path, so a name
        # like "../x" can't reach outside scenarios/ or the runs directory.
        known = set(list_scenario_names(data, ref.scenarios_dir))
        for name in scenarios:
            if name not in known:
                raise click.ClickException(f"no scenario {name!r} in {ref.scenarios_dir}")
            run_path = scenario_run_path(ref.runs_dir, name)
            if run_path is None:
                raise click.ClickException(
                    f"no runs for {name!r} in {ref.runs_dir}; run `sim run {name}` first")
            connections[name] = stack.enter_context(
                open_run(run_path, name, scenarios_dir=ref.scenarios_dir, data_dir=data))
        click.echo(f"{', '.join(variants)} vs {baseline}, {mode_label} dollars")

        if "runs" in shown:
            meta, warnings = compare.comparability(connections, scenarios)
            click.echo("\nRuns:")
            _echo_table(
                ["scenario", "status", "finished", "code", "data"],
                ([row.scenario_name, row.status, row.finished_at or "—",
                  row.code_version + ("+dirty" if row.code_dirty else ""),
                  row.data_version + ("+dirty" if row.data_dirty else "")]
                 for row in named_rows(meta)),
                left=5,
            )
            for warning in warnings:
                click.secho(f"  warning: {warning}", fg="yellow")
            if not warnings:
                click.echo("  Same code and data version; the differences are the scenarios.")

        if "differences" in shown:
            rules = compare.rule_differences(connections, scenarios, baseline)
            click.echo("\nRules that differ, as each run ended:")
            if rules.empty:
                click.echo("  none")
            else:
                money = rules.index.get_level_values("field") == "amount_cents"
                cells = rules.astype(object)
                cells[money] = cells[money].map(
                    lambda v: v if pd.isna(v) else f"${int(v) / 100:,.2f}")
                cells = cells.rename(index={"amount_cents": "amount"}, level="field")
                _echo_frame(cells.map(_cell))
            events = compare.event_differences(connections, scenarios)
            click.echo("\nCalendar events that differ:")
            if events.empty:
                click.echo("  none")
            else:
                _echo_frame(events.set_index(["scenario", "date"]).map(_cell))
            accounts = compare.account_definition_differences(connections, scenarios, baseline)
            click.echo("\nAccounts and lots defined differently:")
            if accounts.empty:
                click.echo("  none")
            else:
                _echo_frame(accounts.map(_cell))
            ytd = compare.year_to_date_differences(connections, scenarios, baseline)
            click.echo("\nYear-to-date rows that differ:")
            if ytd.empty:
                click.echo("  none")
            else:
                _echo_frame(ytd.map(_cell))

        graphed = as_graph or graph_file is not None
        nw = pd.DataFrame()
        # Only the sections that use it read net worth, so --section runs, say,
        # still works on a run with no CPI series.
        if shown & {"net-worth", "break-even"} or (graphed and account is None):
            nw = compare.net_worth_by_month(connections, scenarios, mode, after_tax=after_tax)
            if no_cpi := compare.all_nan_columns(nw):
                raise click.ClickException(
                    f"no 'cpi' inflation index in the run of {', '.join(no_cpi)}, so no "
                    "CPI-constant net worth; pass --dollars nominal")
        if "net-worth" in shown:
            click.echo(f"\n{'After-tax net worth' if after_tax else 'Net worth'} "
                       f"at year end ({mode_label}):")
            _echo_frame(compare.year_end_comparison(nw, baseline, variants).map(_cell))

        if "break-even" in shown:
            delta = compare.variant_delta(nw, baseline, variants)
            summary = compare.breakeven_summary(nw, delta, baseline, variants)
            summary.index.name = "variant"
            click.echo(f"\nBreak-even vs {baseline}: the month after which a variant stays ahead")
            _echo_frame(summary.map(_cell))

        if "guard-rails" in shown:
            _echo_guard_rail_comparison(connections, ref.scenarios_dir, baseline, variants,
                                        mode, asked="guard-rails" in sections)

        balances = None
        if account is not None and ("account" in shown or graphed):
            balances = compare.balance_by_month(connections, scenarios, account, lot, mode)
            subject = f"{account}" + (f", lot {lot}" if lot else "")
            if balances.empty:  # a balance of zero throughout is still a balance
                raise click.ClickException(
                    f"no {'lot ' + lot + ' of ' if lot else ''}{account} with a balance "
                    f"in {', '.join(scenarios)}")
            if no_cpi := compare.all_nan_columns(balances):
                raise click.ClickException(
                    f"no 'cpi' inflation index in the run of {', '.join(no_cpi)}, so no "
                    "CPI-constant balance; pass --dollars nominal")
            if "account" in shown:
                click.echo(f"\n{subject} at year end ({mode_label}):")
                _echo_frame(compare.year_end_comparison(balances, baseline, variants).map(_cell))

        if graphed:
            from analysis.sim.compare_charts import comparison_figure

            deaths = compare.scenario_deaths(connections, scenarios)
            if balances is not None:
                figure = comparison_figure(
                    balances, baseline, list(variants), f"{subject}, {mode_label}",
                    ylabel="balance (USD)", deaths=deaths)
            else:
                figure = comparison_figure(
                    nw, baseline, list(variants), f"Net worth, {mode_label}", deaths=deaths)
            _draw_figure(figure, graph_file, "compare-")


def _echo_guard_rail_comparison(
    connections: Mapping[str, RunConnection], scenarios_dir: Path, baseline: str,
    variants: Sequence[str], mode: DollarMode, *, asked: bool,
) -> None:
    """``sim compare``'s guard-rails section (#305): per rail, each scenario's
    margins by year and each variant's change, annual then cumulative. Each
    scenario's own rails, matched by name. Said to be empty only when
    ``asked`` for: by default, scenarios without rails leave no trace."""
    results: dict[str, list[guard_rails.RailResult]] = {}
    for name, rc in connections.items():
        try:
            rails = guard_rails.run_rails(rc, scenarios_dir)
        except guard_rails.GuardRailError as exc:
            for issue in exc.issues:
                click.echo(f"  error: {issue}", err=True)
            raise click.ClickException(
                f"the guard rails of {name!r} don't load ({len(exc.issues)} issue(s))") from None
        if rails and mode == DollarMode.CPI_CONSTANT and rc.cpi_index().isna().all():
            raise click.ClickException(
                f"no 'cpi' inflation index in the run of {name}, so no CPI-constant "
                "guard rail margins; pass --dollars nominal")
        results[name] = guard_rails.evaluate_run(rc, rails, mode) if rails else []
    frames = guard_rails.comparison_frames(results, baseline, variants)
    if not frames:
        if asked:
            click.echo("\nGuard rails: none in these scenarios")
        return
    label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
    click.echo(f"\nGuard rail margins ({label}): negative is past the rail; cum is the years "
               "so far summed")
    for rail, frame in frames.items():
        click.echo(f"\n{rail}:")
        _echo_frame(frame.map(_cell))


@sim_group_command.command("switch")
@click.argument("scenario", type=completion.SCENARIO, required=False)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def switch_command(scenario: str | None, data_dir: Path | None) -> None:
    """Show the current scenario, or make SCENARIO current (#236).

    With SCENARIO, prints the scenario that was current, then the new one.
    Naming a scenario on any command makes it current too; this command does
    only that, and says what changed.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    previous = read_current_scenario(data_dir_p)
    click.echo(f"Current scenario: {previous or '(none)'}")
    if scenario is None:
        return
    # It says what changed itself, in place of the usual notice.
    ref = _resolve_scenario(data_dir_p, scenario, remember=False)
    working_set.use(data_dir_p, ref.name)
    if ref.name == previous:
        click.echo(f"Unchanged: {ref.name}")
    else:
        write_current_scenario(data_dir_p, ref.name)
        click.echo(f"Switched to: {ref.name}")


@sim_group_command.command("items")
@click.option("--account", type=completion.ACCOUNT, default=None,
              help="Account name in the selected run (asked for when left out).")
@click.option("--lot", type=completion.LotName("account"), default=None,
              help="Only events touching this lot of the account (default: every lot).")
@click.option("--start", required=True, help="First month (YYYY-MM or YYYY-MM-DD).")
@click.option("--end", default=None, help="Last month (YYYY-MM or YYYY-MM-DD); overrides --months.")
@click.option("--months", default=1, show_default=True, type=click.IntRange(min=1),
              help="Number of months to include when --end is omitted.")
@click.option("--runfile", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="Run file (default: selected scenario's latest run).")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario name used to find the latest run.")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
@click.option("--runs-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory to search for the latest run.")
def items(
    account: str | None,
    lot: str | None,
    start: str,
    end: str | None,
    months: int,
    runfile: Path | None,
    scenario: str | None,
    data_dir: Path | None,
    runs_dir: Path | None,
) -> None:
    """Show ledger events for one account with their originating rules."""
    start = month_end_iso(_validate_month_date(start))
    if end is not None:
        end = month_end_iso(_validate_month_date(end))
        if end < start:
            raise click.ClickException("--end must be on or after --start")
    else:
        # start is a month end, so MonthEnd(n) lands n month ends later.
        end = str((pd.Timestamp(start) + pd.offsets.MonthEnd(months - 1)).date())

    if runfile is None:
        data_dir_p = _resolve_data_dir(data_dir)
        scenario_ref = _resolve_scenario(data_dir_p, scenario)
        resolved_runs_dir = runs_dir or scenario_ref.runs_dir
        from analysis.sim.loader import latest_run_path

        runfile = latest_run_path(resolved_runs_dir, scenario_ref.name)
        if runfile is None:
            raise click.ClickException(
                f"no runs for {scenario_ref.name!r} in {resolved_runs_dir}; run `sim run` first"
            )

    con = connect_sim_file(f"file:{runfile}?mode=ro", uri=True)
    try:
        if account is None:
            account = _ask_name("account", (r[0] for r in con.execute("SELECT name FROM account ORDER BY name")))
        account_row = con.execute("SELECT id FROM account WHERE name = ?", (account,)).fetchone()
        if account_row is None:
            raise click.ClickException(f"unknown account in run: {account!r}")
        account_id = account_row[0]
        lot_id = None
        if lot is not None:
            lot_row = con.execute(
                "SELECT id FROM account_lot WHERE account_id = ? AND lot = ?",
                (account_id, lot),
            ).fetchone()
            if lot_row is None:
                raise click.ClickException(f"unknown lot {lot!r} of account {account!r}")
            lot_id = lot_row[0]
        rows = con.execute(
            "SELECT te.date, fa.name, fa.subkind, fm.lot, te.from_account, te.from_lot_id, "
            "ta.name, ta.subkind, tm.lot, te.to_account, te.to_lot_id, "
            "te.amount, te.requested_cents, te.remainder_cents, te.tax_character, "
            "te.reason_kind, s.name, s.amount_rule_kind "
            "FROM transfer_event te "
            "JOIN account fa ON fa.id = te.from_account "
            "JOIN account ta ON ta.id = te.to_account "
            "JOIN account_lot fm ON fm.id = te.from_lot_id "
            "JOIN account_lot tm ON tm.id = te.to_lot_id "
            "LEFT JOIN scheduled_transaction s "
            "  ON s.id = te.reason_id AND te.reason_kind IN ('rule', 'ytd_rule') "
            "WHERE ((? IS NULL AND ? IN (te.from_account, te.to_account)) "
            "       OR ? IN (te.from_lot_id, te.to_lot_id)) "
            "  AND te.date BETWEEN ? AND ? "
            "ORDER BY te.date, te.id",
            (lot_id, account_id, lot_id, start, end),
        ).fetchall()
    finally:
        con.close()

    click.echo(f"run: {runfile}")
    click.echo(
        f"account: {account}{f' (lot {lot})' if lot else ''}  window: {start} through {end}"
    )
    if not rows:
        click.echo("(no matching ledger events)")
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(title=f"ledger events ({account})")
    table.add_column("Date")
    table.add_column("Lot")
    table.add_column("Direction")
    table.add_column("Amount", justify="right")
    table.add_column("Requested", justify="right")
    table.add_column("Remainder", justify="right")
    table.add_column("Tax")
    table.add_column("Origin")
    for (date_, from_name, from_subkind, from_lot, from_account_id, from_lot_id,
         to_name, to_subkind, to_lot, _to_account_id, _to_lot_id,
         amount, requested, remainder, tax_character,
         reason_kind, rule_name, rule_kind) in rows:
        own_is_from = (from_lot_id == lot_id) if lot_id is not None else (
            from_account_id == account_id
        )
        if own_is_from:
            direction = "to " + _account_with_lot(to_name, to_subkind, to_lot)
            own = from_lot
        else:
            direction = "from " + _account_with_lot(from_name, from_subkind, from_lot)
            own = to_lot
        origin = (
            f"{reason_kind}: {rule_name} ({rule_kind})"
            if rule_name is not None
            else reason_kind
        )
        table.add_row(
            date_, own, direction, _fmt_cents(amount).strip(),
            _fmt_cents(requested).strip(), _fmt_cents(remainder).strip(),
            tax_character, origin,
        )
    Console(width=200).print(table)


def _fmt_quantity(micros: int | None) -> str:
    """Shares from micros, with trailing zeros dropped (``-`` when untracked)."""
    if micros is None:
        return "-"
    text = f"{micros / 1_000_000:,.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_cents(cents: int | None) -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else " "
    n = abs(int(cents))
    dollars, rem = divmod(n, 100)
    return f"{sign}${dollars:>14,}.{rem:02d}"


# ---------- add: append new rows to a scenario ----------

def _resolve_scenario_dir(data_dir: Path, scenario: str | None) -> tuple[Path, str]:
    ref = _resolve_scenario(data_dir, scenario)
    return ref.path, ref.name


def _line_completions(line: str, word_start: int, choices: list[str]) -> list[str]:
    """What readline should offer for the word at ``line[word_start:]``.

    Readline completes a *word*, and breaks words at hyphens, spaces and other
    punctuation that names here contain. So the whole line is matched against
    ``choices``, and each match is returned without the part before
    ``word_start``, which readline leaves in place.
    """
    typed = line.lower()
    return [c[word_start:] for c in choices if c.lower().startswith(typed)]


def _prompt_with_completion(text: str, choices: list[str] | None = None,
                            default: str | None = None) -> str:
    """Prompt with readline tab-completion over ``choices``."""
    try:
        import readline
    except ImportError:
        return click.prompt(text, default=default, show_default=bool(default))
    completer_choices = list(choices or [])

    def completer(prefix: str, state: int) -> str | None:  # noqa: ARG001
        line = readline.get_line_buffer()[:readline.get_endidx()]
        opts = _line_completions(line, readline.get_begidx(), completer_choices)
        return opts[state] if state < len(opts) else None

    old = readline.get_completer()
    old_delims = readline.get_completer_delims()
    readline.set_completer(completer)
    # No word breaks: the whole answer is one word, so the list of matches
    # shows full names and a completion replaces what was typed, case included.
    # _line_completions still copes if the library keeps breaking words.
    readline.set_completer_delims("")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    try:
        return click.prompt(text, default=default, show_default=bool(default))
    finally:
        readline.set_completer(old)
        readline.set_completer_delims(old_delims)


def _ask_name(what: str, choices: Iterable[str], *, prompt: str | None = None,
              plural: str | None = None) -> str:
    """One of ``choices``, listed and then asked for with completion until
    the answer is one: how a command takes a name left off its line (#240)."""
    shown = list(dict.fromkeys(choices))
    listed = plural or (what if what.endswith("series") else f"{what}s")
    if not shown:
        raise click.ClickException(f"there are no {listed} to choose from")
    click.echo(f"{listed}: {', '.join(shown)}")
    while (answer := _prompt_with_completion(prompt or what, choices=shown).strip()) not in shown:
        click.echo(f"  one of: {', '.join(shown)}")
    return answer


def _ask_new_name(prompt: str) -> str:
    """A name for something new, asked for until it isn't blank. The command
    checks it as it would one given on its line."""
    while not (answer := _prompt_with_completion(prompt).strip()):
        pass
    return answer


def _name_and_tags(
    name: str | None, tag_tokens: tuple[str, ...], existing: Callable[[], Iterable[str]],
) -> tuple[str | None, tuple[str, ...]]:
    """A tag command's NAME and TAG_TOKENS, given that click hands the first
    word to NAME: when that word looks like a ``role:name`` tag and isn't one
    of the ``existing`` names (a rule or row may have a colon in its name),
    NAME was left out and is asked for."""
    if (name is not None and registries.TAG_TOKEN_RE.match(name)
            and name not in set(existing())):
        return None, (name, *tag_tokens)
    return name, tag_tokens


def _ask_tags(given: tuple[str, ...], choices: Iterable[str]) -> tuple[str, ...]:
    """``given``, or when none were, tag tokens asked for (completing the
    first from ``choices``, the tags already in use)."""
    if given:
        return given
    while not (answer := _prompt_with_completion(
            "tags (role:name, space-separated)", choices=list(choices)).split()):
        pass
    return tuple(answer)


def _interactive() -> bool:
    """Whether a mistyped name can be asked for again: stdin is a terminal.
    Scripts, pipes and tests get the command's own error, as before."""
    return sys.stdin.isatty()


def _ask_again_for_mistyped(ctx: click.Context, params: dict[str, Any]) -> dict[str, Any]:
    """``params`` with each name the scenario doesn't have asked for again
    (#240). The scenario goes first, since the other names are its."""
    checked = [
        p for p in ctx.command.params
        if p.name and isinstance(p.type, completion.NameType) and p.type.check
    ]
    ctx.params.update(params)  # what the completion listers read

    def fix(param: click.Parameter) -> None:
        name_type = param.type
        assert isinstance(name_type, completion.NameType) and param.name
        value = params.get(param.name)
        if value in (None, "", ()) or not (choices := name_type.choices(ctx)):
            return
        values = value if isinstance(value, tuple) else (value,)
        fixed = []
        for v in values:
            if v not in choices and not name_type.is_known(ctx, v):
                click.echo(f"unknown {name_type.what}: {v!r}", err=True)
                v = _ask_name(name_type.what, choices, plural=name_type.plural)
            fixed.append(v)
        params[param.name] = ctx.params[param.name] = (
            tuple(fixed) if isinstance(value, tuple) else fixed[0])

    for param in checked:
        if param.name in completion.SCENARIO_PARAMS:
            fix(param)
    if any(p.type is completion.ENSEMBLE and params.get(p.name) is None for p in ctx.command.params):
        # An ensemble left off is asked for later (compare's variant too),
        # so whose names to check against isn't known yet.
        return params
    if "tag_tokens" in params:  # a tag command's NAME may be its first tag
        first = next(p for p in ctx.command.params if isinstance(p, click.Argument))
        if first.name and first.name != "tag_tokens":
            first_type = first.type
            params[first.name], params["tag_tokens"] = _name_and_tags(
                params[first.name], params["tag_tokens"],
                lambda: first_type.choices(ctx) if isinstance(first_type, completion.NameType) else [])
            ctx.params.update(params)
    for param in checked:
        if param.name not in completion.SCENARIO_PARAMS:
            fix(param)
    return params


def _names_typed(ctx: click.Context) -> bool:
    """Whether ``ctx``'s command was typed at a terminal, where a mistyped
    name is asked for again. A command a guide invokes with its answers
    (`calendar edit` running `calendar add set-rate`) has its own review
    for a rejected value."""
    return ctx.parent is not None and ctx.parent.invoked_subcommand is not None and _interactive()


def _ask_again_once_known(**asked: str) -> dict[str, Any]:
    """The current command's params, with the ensembles just ``asked`` for,
    and at a terminal its other names checked against them: they waited
    for the ensembles (#240)."""
    ctx = click.get_current_context()
    params = {**ctx.params, **asked}
    return _ask_again_for_mistyped(ctx, params) if _names_typed(ctx) else params


def _wrap_to_ask_again(command: click.Command) -> None:
    """Make ``command`` re-ask, at a terminal, for a name it was given that
    isn't one (#240), by wrapping its callback."""
    original = command.callback
    if original is None or not any(
        isinstance(p.type, completion.NameType) and p.type.check for p in command.params
    ):
        return

    @functools.wraps(original)
    def callback(*args: Any, **kwargs: Any) -> Any:
        ctx = click.get_current_context()
        # A run file holds its own names, whatever the current scenario has.
        if ctx.command is command and _names_typed(ctx) and not kwargs.get("runfile"):
            kwargs = _ask_again_for_mistyped(ctx, kwargs)
        return original(*args, **kwargs)

    command.callback = callback


def _parse_amount_cents(s: str) -> int:
    raw = s.strip().lstrip("$").replace(",", "")
    try:
        d = Decimal(raw)
    except InvalidOperation as e:
        raise click.ClickException(f"invalid amount: {s!r}") from e
    # Decimal parses NaN and Infinity, which no amount can be.
    if not d.is_finite():
        raise click.ClickException(f"invalid amount: {s!r}")
    cents = d * 100
    if cents != cents.to_integral_value():
        raise click.ClickException(f"amount has sub-cent precision: {s!r}")
    return int(cents)


def _validate_month_date(s: str) -> str:
    """``s`` as typed, once it's a ``YYYY-MM`` or ``YYYY-MM-DD`` (#34).

    Written to the CSV unchanged -- a bare month is the honest form -- and
    the loader puts it on the right day for its column. Shares the loader's
    own parser, so it accepts exactly what the loader will later accept
    (e.g. not a non-zero-padded '2029-1-1'), not a superset.
    """
    if not is_month_or_date(s):
        raise click.ClickException(f"date must be {MONTH_FORMAT_HINT}: {s!r}")
    return s.strip()


_CALENDAR_DATE_PROMPT = "date (YYYY-MM, YYYY-MM-DD, or a date_marker: NAME, NAME+Nm, NAME-Ny)"


def _validate_calendar_date(s: str) -> str:
    """``_validate_month_date``, or a date relative to a date_marker
    (``NAME``, ``NAME+2y``, ``NAME-6m``, #281): written as typed, and the
    marker's existence is the validating load's to check."""
    if parse_relative_date(s) is not None:
        return s.strip()
    if not is_month_or_date(s):
        raise click.ClickException(
            f"date must be {MONTH_FORMAT_HINT}, or relative to a date_marker "
            f"(NAME, NAME+Nm, NAME-Ny): {s!r}"
        )
    return s.strip()


# calendar.csv columns a file may legitimately lack (ADR-006): optional,
# nullable additions to an already-established header. Any other missing
# column, in any CSV kind, is schema drift to report, not paper over --
# an accounts.csv missing e.g. `kind` is a malformed file, not an old one.
_BACKFILLABLE_CALENDAR_COLUMNS = frozenset({
    "name", "vary_target", "vary_distribution", "vary_low", "vary_high",
    "vary_median", "vary_sd", "vary_skew", "vary_tailweight", "vary_mode", "vary_ramp_months",
    "vary_correlate_with", "vary_correlation", "vary_tail_dependence",
    "vary_process", "vary_persistence", "vary_shock_sd", "vary_process_start",
    "vary_resume",
    "vary_outcomes",
})


# lifespan.csv's life_table (#315) is optional too.
_BACKFILLABLE_COLUMNS = _BACKFILLABLE_CALENDAR_COLUMNS | {"life_table"}


def _append_csv_row(path: Path, header: tuple[str, ...],
                    row: dict[str, str], *, replace: int | None = None) -> None:
    """Append a row to a CSV, aligning columns by name via pandas. With
    ``replace``, overwrite that data row (0-based) instead.

    If the file exists, its header must contain every column of ``header``
    except the backfillable ones (calendar.csv's ADR-006 additions and
    lifespan.csv's life_table), which are added blank on every existing row instead; this
    still catches real schema drift rather than silently writing misaligned
    columns.
    """
    cols = list(header)
    if path.exists() and path.stat().st_size > 0:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        existing = list(df.columns)
        missing = [c for c in header if c not in existing]
        not_backfillable = [c for c in missing if c not in _BACKFILLABLE_COLUMNS]
        if not_backfillable:
            raise click.ClickException(
                f"CSV header mismatch in {path}\n"
                f"  expected: {list(header)}\n"
                f"  found:    {list(existing)}\n"
                f"  missing:  {not_backfillable}"
            )
        if missing:
            df = df.reindex(columns=[*existing, *missing], fill_value="")
        cols = list(df.columns)
    else:
        df = pd.DataFrame(columns=cols)
    if replace is not None:
        df.loc[replace, cols] = [row.get(c, "") for c in cols]
    else:
        new_row = pd.DataFrame([{c: row.get(c, "") for c in cols}], columns=cols)
        df = pd.concat([df, new_row], ignore_index=True)
    df.to_csv(path, index=False)


@contextmanager
def _scenario_edit(
    data_dir: Path, scenario: str | None, *, remember: bool = True, template_parent: bool = False,
) -> Generator[StagedEdit]:
    """``staged_edit`` for a CLI command: edits land only if every affected
    scenario still loads (issue #44). Issues go to stderr as ``error:`` lines,
    like ``sim check``, and the command fails with nothing written.
    ``scenario`` and ``remember`` are as for ``_resolve_scenario``;
    ``template_parent`` lets ``scenario`` be a template, for creating a
    scenario from one."""
    scenario = _resolve_scenario(data_dir, scenario, remember=remember).name
    try:
        with staged_edit(data_dir, scenario, template_parent=template_parent) as edit:
            yield edit
    except ScenarioAlreadyBroken as exc:
        for issue in exc.issues:
            click.echo(f"  error: {issue}", err=True)
        raise click.ClickException(
            f"scenario {exc.scenario!r} already fails to load, before this change "
            f"({len(exc.issues)} issue(s)); fix it first ('sim check {exc.scenario}'). "
            "Nothing was written."
        ) from None
    except EditRejected as exc:
        _echo_rejected(exc)
        raise click.ClickException(
            f"this change would leave {len(exc.issues)} issue(s); nothing was written"
        ) from None
    except ConcurrentEdit as exc:
        raise click.ClickException(str(exc)) from None
    except chain_edit.AmbiguousChain as exc:  # nothing is written; say which files clash
        raise click.ClickException(str(exc)) from None
    except LoadError as exc:  # e.g. an unknown scenario, before anything was staged
        raise click.ClickException("; ".join(exc.issues)) from None
    for warning in edit.warnings:
        click.echo(f"WARN: {warning}", err=True)  # ADR-004 §6's format


def _echo_rejected(exc: EditRejected) -> None:
    """A rejected edit's issues, the edited scenario's first. A change is also
    checked against every scenario built on the one changed, so each of those
    that fails gets a line saying so, or its errors would read as if the
    command had edited it (#233)."""
    for issue in exc.own:
        click.echo(f"  error: {issue}", err=True)
    for dependent, issues in exc.dependents.items():
        click.echo(
            f"{exc.scenario} {'also fails, and' if exc.own else 'loads with this change, but'} "
            f"{dependent}, which builds on it, would not load "
            f"(a change is checked against every scenario built on {exc.scenario}):",
            err=True,
        )
        for issue in issues:
            click.echo(f"  error: {issue}", err=True)


@contextmanager
def _intent_db(data_dir: Path, scenario: str | None) -> Generator[tuple[sqlite3.Connection, str]]:
    """``open_intent_db`` for an edit command's up-front checks: a scenario
    that doesn't load says so plainly, rather than as a traceback or as if the
    row being added were at fault."""
    scenario = _resolve_scenario(data_dir, scenario).name
    with ExitStack() as stack:
        try:
            con, name = stack.enter_context(open_intent_db(data_dir, scenario))
        except LoadError as exc:
            for issue in exc.issues:
                click.echo(f"  error: {issue}", err=True)
            raise click.ClickException(
                f"scenario {scenario!r} already fails to load "
                f"({len(exc.issues)} issue(s)); fix it first (sim check). Nothing was written."
            ) from None
        yield con, name


def _append_and_validate(
    path: Path, header: tuple[str, ...], row: dict[str, str],
    *, data_dir: Path, scenario_name: str,
) -> None:
    """``_append_csv_row``, written only if the scenario still loads with it.

    A CLI-authored row can be individually well-formed and still break the
    scenario -- e.g. a series/aux_kind pair nothing reads (ADR-006) -- so the
    row is appended to a staged copy and validated there by ``_scenario_edit``.
    """
    with _scenario_edit(data_dir, scenario_name) as edit:
        _append_csv_row(edit.path(path), header, row)


# click context ``meta`` key: ``(index, row)`` for the data row (0-based) of
# the scenario's own calendar.csv that a calendar command's row replaces
# instead of being appended, and that row as it was when chosen. Set by ``sim
# calendar edit`` around the kind's own command, so that command builds and
# validates the row exactly as it does for ``add``.
_CALENDAR_REPLACE_ROW = "analysis.sim.calendar.replace_row"

# Under ``sim calendar edit`` of a row the scenario inherits: that row, merged
# as the load leaves it, and the scenario it comes from. The kind's command
# then appends its row as an override instead (#202).
_CALENDAR_OVERRIDE_ROW = "analysis.sim.calendar.override_row"

# params_json keys each kind's command writes itself. Any other key in a row
# being replaced was put there by hand and is kept -- including one of these
# names on a kind whose command does not write it.
_CALENDAR_OWN_PARAM_KEYS: dict[str, frozenset[str]] = {
    "set_rate": frozenset({"aux_kind"}),
    "one_off_transfer": frozenset({"description"}),
}


def _read_calendar_rows(path: Path) -> list[dict[str, str]]:
    """A scenario's own calendar.csv rows, every CALENDAR_HEADER column present."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [
        {column: row.get(column, "") for column in registries.CALENDAR_HEADER}
        for row in df.to_dict("records")
    ]


def _calendar_row_label(row: dict[str, str]) -> str:
    """One row, as a line a person can recognize it by."""
    kind = row.get("kind", "")
    if row.get("account_1") or row.get("account_2"):
        what = f"{row.get('account_1', '')} -> {row.get('account_2', '')}"
    else:
        what = row.get("scheduled_name") or row.get("series") or ""
    # The row is as authored (dollars or a percent, by kind), not stored.
    amount = row.get("amount", "")
    unit = units.CALENDAR_AMOUNT_UNITS.get(kind)
    if amount and unit is not None:
        try:
            amount = registries.format_calendar_amount(kind, units.parse(unit, amount))
        except ValueError:
            pass
    name = f"[{row['name']}]" if row.get("name") else ""
    parts = [f"{row.get('date', ''):<10}", f"{kind:<16}", what, amount, name]
    return "  ".join(part for part in parts if part.strip())


def _keep_unexpressed(row: dict[str, str], chosen: dict[str, str]) -> dict[str, str]:
    """``row`` as a kind's command built it, plus what the command has no way
    to express that the ``chosen`` row it replaces or overrides had: its tags,
    and params_json keys put there by hand."""
    row = dict(row)
    if "tags" not in row or (row["kind"] == "one_off_transfer" and not row["tags"]):
        row["tags"] = chosen["tags"]
    old_params = json.loads(chosen["params_json"]) if chosen["params_json"] else {}
    new_params = json.loads(row["params_json"]) if row.get("params_json") else {}
    own = _CALENDAR_OWN_PARAM_KEYS.get(row["kind"], frozenset())
    # A transfer's command copies its description into params_json; a row
    # that kept it only in the description column goes on doing so.
    if "description" in own and "description" not in old_params:
        new_params.pop("description", None)
    params = {**{k: v for k, v in old_params.items() if k not in own}, **new_params}
    row["params_json"] = json.dumps(params, separators=(",", ":")) if params else ""
    return row


def _write_calendar_override(
    staged: Path, path: Path, row: dict[str, str], chosen: dict[str, str], origin: str,
) -> bool:
    """Append ``row`` to the staged calendar.csv as an override of the
    inherited ``chosen`` row, from scenario ``origin`` (#202), unless it
    changes nothing. Whether it was written.

    The loader matches a named row to its override by name, and an unnamed
    one by its key columns (``_merge_calendar_rows``), so the override must
    keep the name, or, for an unnamed row, every key column; otherwise it
    would be a second event beside the inherited one.
    """
    name = chosen["name"].strip()
    if name and row.get("name", "").strip() != name:
        raise click.ClickException(
            f"row {name!r} comes from scenario {origin!r}; `sim calendar rename` "
            "renames it there and in every scenario built on it. Nothing was written."
        )
    if not name and calendar_row_key(row) != calendar_row_key(chosen):
        changed = [
            column for column in registries.CALENDAR_KEY_COLUMNS
            if calendar_row_key({**chosen, column: row.get(column, "")}) != calendar_row_key(chosen)
        ]
        raise click.ClickException(
            f"this row comes from scenario {origin!r} and has no name, so an override "
            f"is matched to it by its {', '.join(changed) or 'key fields'}, which this "
            f"edit changes: it would add a second event instead. Give the row a name "
            f"in {origin!r} first (`sim calendar edit --scenario {origin}`). "
            "Nothing was written."
        )
    for other_index, other in enumerate(_read_calendar_rows(staged)):
        same_name = name and other["name"].strip() == name
        if same_name or calendar_row_key(other) == calendar_row_key(row):
            raise click.ClickException(
                f"row {other_index + 1} of {path.name} ({_calendar_row_label(other)}) "
                "already overrides or duplicates it; edit that row instead. Nothing was written."
            )
    row = _keep_unexpressed(row, chosen)
    if calendar_row_key(row) == calendar_row_key(chosen) and all(
        row.get(column, "").strip() == chosen.get(column, "").strip()
        for column in registries.CALENDAR_HEADER
        if column not in registries.CALENDAR_KEY_COLUMNS
    ):
        return False
    _append_csv_row(staged, registries.CALENDAR_HEADER, row)
    return True


def _write_calendar_row(
    path: Path, row: dict[str, str], *, data_dir: Path, scenario_name: str,
) -> str:
    """Append ``row`` to the scenario's calendar.csv -- or, under ``sim
    calendar edit``, replace the row being edited, or override the inherited
    row being edited -- if the scenario still loads with it. Returns
    "appended", "updated", or how an override went.

    An edited row may not end up alike in every key column to another row of
    the same file: the loader would treat the two as one event, so the edit
    would quietly merge them. (Appending such a row stays allowed: it is how
    an existing row is given a name and a variation, ADR-006.)
    """
    context = click.get_current_context(silent=True)
    meta = context.meta if context is not None else {}
    replacing = meta.get(_CALENDAR_REPLACE_ROW)
    overriding = meta.get(_CALENDAR_OVERRIDE_ROW)
    with _scenario_edit(data_dir, scenario_name) as edit:
        staged = edit.path(path)
        if overriding is not None:
            if _write_calendar_override(staged, path, row, *overriding):
                return "wrote an override:"
            return "nothing changed; no override written for"
        if replacing is None:
            _append_csv_row(staged, registries.CALENDAR_HEADER, row)
            return "appended"
        index, chosen = replacing
        # Read inside the staged edit: the prompts may have taken a while, and
        # the index is only good if the file still has the chosen row there.
        existing = _read_calendar_rows(staged)
        if index >= len(existing) or existing[index] != chosen:
            raise click.ClickException(
                f"{path} changed while this row was being edited, so row {index + 1} "
                "is no longer the row that was chosen. Nothing was written; start again."
            )
        for other_index, other in enumerate(existing):
            if other_index != index and calendar_row_key(other) == calendar_row_key(row):
                raise click.ClickException(
                    f"this would duplicate row {other_index + 1} of {path.name} "
                    f"({_calendar_row_label(other)}); change one of them. Nothing was written."
                )
        _append_csv_row(
            staged, registries.CALENDAR_HEADER, _keep_unexpressed(row, chosen), replace=index,
        )
    return "updated"


def _account_with_lot(name: str | None, subkind: str | None, lot: str | None) -> str:
    """``name``, plus ``(lot)`` where the lot is worth showing."""
    if not name:
        return ""
    if lot and (subkind in INVESTMENT_ASSET_SUBKINDS or lot != registries.DEFAULT_LOT):
        return f"{name} ({lot})"
    return name


def _lot_info(con: sqlite3.Connection) -> dict[str, tuple[bool, list[str]]]:
    """Per account: (is an investment account, its lot labels)."""
    info: dict[str, tuple[bool, list[str]]] = {}
    for name, subkind, lot in con.execute(
        "SELECT a.name, a.subkind, m.lot FROM account_lot m "
        "JOIN account a ON a.id = m.account_id ORDER BY m.id"
    ):
        info.setdefault(name, (subkind in INVESTMENT_ASSET_SUBKINDS, []))[1].append(lot)
    return info


def _interior_chains(con: sqlite3.Connection, role: str | None = None) -> list[str]:
    """The chains a transfer may name in place of an account: every chain but
    the implicit leaf chain of each lot; with ``role``, only the chains of
    that role (``source`` for a from side, ``destination`` for a to side)."""
    if role is None:
        return [r[0] for r in con.execute("SELECT name FROM chain WHERE lot_id IS NULL ORDER BY name")]
    return [r[0] for r in con.execute(
        "SELECT name FROM chain WHERE lot_id IS NULL AND role = ? ORDER BY name", (role,))]


def _chain_roles(con: sqlite3.Connection) -> dict[str, str]:
    """Each named chain's role (ADR-003 §4.7)."""
    return dict(con.execute("SELECT name, role FROM chain WHERE role IS NOT NULL").fetchall())


def _echo_endpoints(
    accounts: list[str], lot_info: dict[str, tuple[bool, list[str]]], chains: list[str],
) -> None:
    """List what a transfer can name, with the lots of each account that must
    name one."""
    shown = [
        f"{a} (lots: {', '.join(lot_info[a][1])})"
        if calendar_guide.lot_needed(lot_info.get(a)) else a
        for a in accounts
    ]
    click.echo(f"accounts: {', '.join(shown)}")
    if chains:
        click.echo(f"chains: {', '.join(chains)}")


def _pick_endpoint(
    label: str, name: str | None, lot: str | None, *,
    accounts: list[str], lot_info: dict[str, tuple[bool, list[str]]], chains: list[str],
    allow_chain: bool = True, lot_option: str | None = None,
    roles: Mapping[str, str] | None = None, role: str | None = None,
) -> tuple[str, str]:
    """One side of a transfer: an account and its lot, or an interior chain
    and no lot (#98). A value given on the command line is checked against the
    same choices as one typed at a prompt, and fails if it doesn't fit; a value
    left out is asked, again until it fits. The lot is asked whenever
    ``calendar_guide.lot_needed`` says a blank one wouldn't do, as the guided
    ``sim calendar add`` does. ``chains`` are the ones of ``role``, the role
    this side takes; given ``roles`` (every chain's role), naming a chain of
    the other role says so."""

    def endpoint_problem(answer: str) -> str | None:
        if not allow_chain and (answer in chains or answer in (roles or ())):
            return f"{answer!r} is a chain, and a transfer can't run from one chain into another"
        if roles and role and answer not in chains and answer in roles:
            return f"{answer!r} is a {roles[answer]} chain; only a {role} chain or an account fits here"
        if answer not in accounts and answer not in chains:
            return f"unknown {label} account or chain: {answer!r}"
        return None

    if name is None:
        choices = [*accounts, *chains] if allow_chain else list(accounts)
        while (problem := endpoint_problem(
            name := _prompt_with_completion(f"{label} account or chain", choices=choices).strip()
        )):
            click.echo(f"  {problem}")
    elif problem := endpoint_problem(name):
        raise click.ClickException(problem)

    if name in chains:
        if lot:
            raise click.ClickException(f"{name!r} is a chain, so {lot_option or f'--{label}-lot'} must be blank")
        return name, ""
    entry = lot_info.get(name, (False, [registries.DEFAULT_LOT]))
    lots = list(entry[1])
    if lot:
        if lot not in lots:
            raise click.ClickException(
                f"{name!r} has no lot {lot!r}; its lots: {', '.join(lots)}"
            )
        return name, lot
    if not calendar_guide.lot_needed(entry):
        return name, ""
    click.echo(f"  lots of {name}: {', '.join(lots)}")
    while (lot := _prompt_with_completion(f"{label} lot", choices=lots).strip()) not in lots:
        click.echo(f"  not one of: {', '.join(lots)}")
    return name, lot


class _Endpoints(NamedTuple):
    """What either side of a transfer may name."""
    accounts: list[str]
    lot_info: dict[str, tuple[bool, list[str]]]
    chains: list[str]
    roles: dict[str, str]
    """Each chain's role: a from side names a source chain, a to side a destination."""

    def chains_for(self, role: str) -> list[str]:
        return [c for c in self.chains if self.roles.get(c) == role]


def _endpoints(con: sqlite3.Connection) -> _Endpoints:
    return _Endpoints(
        [r[0] for r in con.execute("SELECT name FROM account ORDER BY name")],
        _lot_info(con), _interior_chains(con), _chain_roles(con),
    )


def _pick_endpoints(
    endpoints: _Endpoints,
    from_account: str | None, from_lot: str | None,
    to_account: str | None, to_lot: str | None,
) -> tuple[str, str, str, str]:
    """Both sides of a transfer, from then to, each with its lot (#98)."""
    accounts, lot_info, chains, roles = endpoints
    # Each side lists what it takes just before it is asked: its own role's chains.
    if from_account is None:
        _echo_endpoints(accounts, lot_info, endpoints.chains_for("source"))
    from_account, from_lot = _pick_endpoint(
        "from", from_account, from_lot, accounts=accounts, lot_info=lot_info,
        chains=endpoints.chains_for("source"), roles=roles, role="source",
    )
    if to_account is None:
        _echo_endpoints(accounts, lot_info, endpoints.chains_for("destination") if from_account not in chains else [])
    to_account, to_lot = _pick_endpoint(
        "to", to_account, to_lot, accounts=accounts, lot_info=lot_info,
        chains=endpoints.chains_for("destination"), roles=roles, role="destination",
        allow_chain=from_account not in chains,
    )
    return from_account, from_lot, to_account, to_lot


def _percent_to_rate_micros(s: str) -> int:
    """Parse a human-facing rate percentage (e.g. ``4.5``) into millionths.

    Unlike an allocation weight, a rate may be negative (depreciation) or
    exceed 100%, so this doesn't reuse the loader's range-limited helper.
    """
    try:
        # A typed argument is already named as a percent, so its sign is optional.
        return units.percent_to_micros(s, require_sign=False)
    except ValueError as e:
        raise click.ClickException(str(e)) from None


# What each calendar amount unit is typed as (units.CALENDAR_AMOUNT_UNITS).
_AMOUNT_PARSERS: dict[str, Callable[[str], int]] = {
    "dollars": _parse_amount_cents,
    "percent": _percent_to_rate_micros,
}


_TAIL_DEPENDENCE_HELP = (
    "On the root of a correlation tree only: the Student-t copula's degrees of freedom nu "
    "for every row in the tree (#68), so their extremes tend to coincide. Lower is stronger; "
    "about 3 to 30. Blank keeps the Gaussian copula."
)


def _vary_process_options(f):
    """``--vary-process`` and its parameters (ADR-006 §4a, #115), for
    ``set-rate`` only: a year-by-year path around the drawn level."""
    f = click.option(
        "--vary-process-start", type=click.Choice(sorted(registries.VARY_PROCESS_STARTS)),
        default=None,
        help="With --vary-process: 'current' starts the path at the rate in effect just "
             "before DATE (from the series file or an earlier row) and reverts to the "
             "drawn level at --vary-persistence. Left out, year 0 is already a draw.",
    )(f)
    f = click.option(
        "--vary-shock-sd", default=None,
        help="With --vary-process: one year's shock, in percent like the rate (e.g. 17).",
    )(f)
    f = click.option(
        "--vary-persistence", default=None,
        help="With --vary-process: how much of one year's departure from the level "
             "carries into the next, at least 0 and below 1 (0: years independent).",
    )(f)
    f = click.option(
        "--vary-process", type=click.Choice(sorted(registries.VARY_PROCESSES)), default=None,
        help="Move the rate year by year around the drawn level instead of holding it "
             "(--vary-target amount only).",
    )(f)
    return f


def _process_fields(
    vary_process: str | None, vary_persistence: str | None, vary_shock_sd: str | None,
    *, vary_target: str, vary_ramp_months: int | None,
    amount_unit: str | None, vary_process_start: str | None = None,
) -> dict[str, str]:
    """``vary_process``/``vary_persistence``/``vary_shock_sd``/
    ``vary_process_start`` cells (ADR-006 §4a), checked as far as the row
    alone allows; the level's bound, a tree's nu and a rate for a
    ``current`` start to start from are the validating load's job."""
    if vary_process is None:
        if (vary_persistence is not None or vary_shock_sd is not None
                or vary_process_start is not None):
            raise click.ClickException(
                "--vary-persistence, --vary-shock-sd and --vary-process-start "
                "go with --vary-process")
        return {}
    if vary_target != "amount" or amount_unit is None:
        raise click.ClickException("--vary-process needs --vary-target amount")
    if vary_ramp_months is not None:
        raise click.ClickException("--vary-process and --vary-ramp-months don't combine")
    if vary_persistence is None or vary_shock_sd is None:
        raise click.ClickException(
            "--vary-process requires --vary-persistence and --vary-shock-sd")
    try:
        phi = float(vary_persistence)
    except ValueError:
        raise click.ClickException(
            f"--vary-persistence must be a number (got {vary_persistence!r})") from None
    if not 0.0 <= phi < 1.0:
        raise click.ClickException(
            f"--vary-persistence must be at least 0 and below 1 (got {vary_persistence})")
    sd = _AMOUNT_PARSERS[amount_unit](vary_shock_sd)
    if sd <= 0:
        raise click.ClickException(
            f"--vary-shock-sd must be positive (got {vary_shock_sd})")
    return {"vary_process": vary_process, "vary_persistence": vary_persistence,
            "vary_shock_sd": units.format_stored(amount_unit, sd),
            "vary_process_start": vary_process_start or ""}


def _vary_row_options(f):
    """Shared ``--vary-*`` options for a calendar.csv ``add`` command (ADR-006).

    Applied to every kind's subcommand uniformly; a kind with no meaningful
    amount to vary (e.g. ``retire_scheduled``) rejects ``--vary-target
    amount`` itself rather than omitting the options.
    """
    f = click.option(
        "--vary-ramp-months", type=int, default=None,
        help="Ramp to the drawn target over this many months "
             "(set-rate, --vary-target amount, only).",
    )(f)
    f = click.option(
        "--vary-tail-dependence", default=None,
        help=_TAIL_DEPENDENCE_HELP,
    )(f)
    f = click.option(
        "--vary-correlation", default=None,
        help="rho, between -1 and 1: how strongly this row moves with --vary-correlate-with "
             "(ADR-008). Siblings under one parent correlate at the product of their rhos.",
    )(f)
    f = click.option(
        "--vary-correlate-with", type=completion.CORRELATE_WITH, default=None,
        help="Another varying row's or a latent factor's name: this row's parent in a "
             "correlation tree (ADR-008). Requires --vary-correlation.",
    )(f)
    f = click.option(
        "--vary-outcomes", default=None,
        help="discrete's weighted outcomes, each a value or a low..high range (same unit "
             "as the value above, or YYYY-MM[-DD] for --vary-target date), weights adding "
             "to 100%%: e.g. '30%%: 0; 25%%: 1000..6000; 45%%: 6000..20000'.",
    )(f)
    f = click.option(
        "--vary-mode", default=None,
        help="triangular's peak, the most likely value (same unit as the value above, "
             "or YYYY-MM[-DD] for --vary-target date; a bare month is mid-month). "
             "Defaults to midway between "
             "vary_low and vary_high.",
    )(f)
    f = click.option(
        "--vary-tailweight", default=None,
        help="truncated_normal tail weight: 1 (default) is normal, below 1 heavier "
             "tails (more extreme values), above 1 lighter. Requires --vary-sd.",
    )(f)
    f = click.option(
        "--vary-skew", default=None,
        help="truncated_normal skew: 0 (default) is symmetric, positive a longer "
             "high tail, negative a longer low tail. Requires --vary-sd.",
    )(f)
    f = click.option(
        "--vary-sd", default=None,
        help="truncated_normal scale (same unit as the value above): the standard "
             "deviation of the underlying normal before clipping, when skew and tail "
             "weight are at their defaults; with a shape set it's a scale factor, not "
             "the SD (the parameters table shows the implied SD). May be omitted, for "
             "the default shape, if vary_median plus vary_low or vary_high is given.",
    )(f)
    f = click.option(
        "--vary-median", default=None,
        help="truncated_normal median, after clipping: the base case exactly (same "
             "unit as the value above). May be omitted if both vary_low and vary_high "
             "are given (it's then their midpoint).",
    )(f)
    f = click.option(
        "--vary-high", default=None,
        help="uniform's or triangular's upper bound, or truncated_normal's upper clip "
             "bound (same unit as the value above, or YYYY-MM[-DD] for --vary-target "
             "date; a bare month is its last day). For truncated_normal, omitting it "
             "places it at the underlying distribution's 3-standard-normal-score point "
             "(3 x vary_sd for the default shape) instead of leaving that side unbounded.",
    )(f)
    f = click.option(
        "--vary-low", default=None,
        help="uniform's or triangular's lower bound, or truncated_normal's lower clip "
             "bound (same unit as the value above, or YYYY-MM[-DD] for --vary-target "
             "date; a bare month is its first day). For truncated_normal, omitting it places it at the underlying "
             "distribution's 3-standard-normal-score point (3 x vary_sd for the "
             "default shape) instead of leaving that side unbounded.",
    )(f)
    f = click.option(
        "--vary-distribution", type=click.Choice(sorted(registries.ENSEMBLE_DISTRIBUTIONS)),
        default=None,
        help="Mark this row as varying per ensemble member; omit for an ordinary literal row.",
    )(f)
    f = click.option(
        "--vary-target", type=click.Choice(sorted(registries.VARY_TARGETS)),
        default=None, help="Which of this row's own columns is drawn; required with "
                           "--vary-distribution.",
    )(f)
    f = click.option(
        "--name", "row_name", default=None,
        help="calendar.csv name -- required and unique when varying, optional otherwise.",
    )(f)
    return f


def _correlation_fields(
    correlate_with: str | None, correlation: str | None, tail_dependence: str | None = None,
) -> dict[str, str]:
    """``vary_correlate_with``/``vary_correlation``/``vary_tail_dependence``
    cells (ADR-008, #68), checked as far as the row alone allows; the
    parent's existence, cycles, and whether a ``nu`` row is its tree's root
    are the validating load's job, over the whole merged calendar."""
    if correlate_with is not None and tail_dependence is not None:
        raise click.ClickException(
            "--vary-tail-dependence goes on the root of a correlation tree, so not with "
            "--vary-correlate-with: it applies to every row in the tree below the root"
        )
    if tail_dependence is not None:
        try:
            nu = float(tail_dependence)
        except ValueError:
            raise click.ClickException(
                f"--vary-tail-dependence must be a number (got {tail_dependence!r})"
            ) from None
        if not registries.MIN_TAIL_DEPENDENCE <= nu < math.inf:
            raise click.ClickException(
                f"--vary-tail-dependence must be at least {registries.MIN_TAIL_DEPENDENCE:g} "
                f"(got {tail_dependence})"
            )
    if (correlate_with is None) != (correlation is None):
        raise click.ClickException("--vary-correlate-with and --vary-correlation go together")
    if correlation is not None:
        try:
            rho = float(correlation)
        except ValueError:
            raise click.ClickException(f"--vary-correlation must be a number (got {correlation!r})") from None
        if not -1.0 <= rho <= 1.0:
            raise click.ClickException(f"--vary-correlation must be between -1 and 1 (got {correlation})")
    return {
        "vary_correlate_with": correlate_with or "", "vary_correlation": correlation or "",
        "vary_tail_dependence": tail_dependence or "",
    }


def _outcomes_cell(raw: str | None, vary_target: str, amount_unit: str | None) -> str:
    """A ``--vary-outcomes`` value (#165), checked whole -- each value parses,
    the weights add to 100%, nothing overlaps -- and written with each value
    in its canonical form, the weights as typed."""
    if raw is None:
        return ""

    def value(text: str, role: OutcomeRole) -> float:
        if vary_target == "date":
            return float(parse_vary_date(text, role).toordinal())
        assert amount_unit is not None  # an amount target without one is rejected first
        return float(_AMOUNT_PARSERS[amount_unit](text))

    def canonical(text: str, _role: OutcomeRole) -> str:
        if vary_target == "date":
            return _validate_month_date(text)
        assert amount_unit is not None
        return units.format_stored(amount_unit, _AMOUNT_PARSERS[amount_unit](text))

    try:
        Discrete(parse_outcomes(raw, value))
        return rewrite_outcomes(raw, canonical)
    except DistributionError as e:
        raise click.ClickException(f"--vary-outcomes: {e}") from None


def _vary_row_fields(
    *,
    row_name: str | None,
    vary_target: str | None,
    vary_distribution: str | None,
    vary_low: str | None,
    vary_high: str | None,
    vary_median: str | None,
    vary_sd: str | None,
    vary_skew: str | None,
    vary_tailweight: str | None,
    vary_mode: str | None,
    vary_outcomes: str | None,
    vary_correlate_with: str | None,
    vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
    amount_unit: str | None,
    vary_process: str | None = None,
    vary_persistence: str | None = None,
    vary_shock_sd: str | None = None,
    vary_process_start: str | None = None,
) -> dict[str, str]:
    """ADR-006: the ``vary_*`` CSV cells for one calendar.csv row, from CLI options.

    A bound value (``--vary-low`` etc.) is given in the same human unit as the
    row's own literal value (percent for set-rate, dollars for set-amount/
    transfer) for an ``amount`` target, or ``YYYY-MM`` / ``YYYY-MM-DD`` for a
    ``date`` target (a bare month is resolved per bound by the loader, #34);
    ``amount_unit`` (``dollars`` or ``percent``) is that unit: a bound is
    checked by parsing it, and written back in its canonical form (#130).
    """
    if not vary_distribution:
        if any([
            vary_target, vary_low, vary_high, vary_median, vary_sd,
            vary_skew, vary_tailweight, vary_mode, vary_outcomes, vary_ramp_months is not None,
            vary_correlate_with, vary_correlation, vary_tail_dependence,
            vary_process, vary_persistence, vary_shock_sd, vary_process_start,
        ]):
            raise click.ClickException("--vary-* options require --vary-distribution")
        return {"name": row_name or ""}
    if not vary_target:
        raise click.ClickException("--vary-distribution requires --vary-target")
    if not row_name:
        raise click.ClickException("--vary-distribution requires --name")
    if vary_target == "amount" and amount_unit is None:
        raise click.ClickException("this calendar kind has no amount column to vary")

    # Catch a distribution/bound mismatch here too, not just at 'sim load' --
    # writing the row anyway (to be rejected later) means a bad combination
    # looks like it worked until the separate load step.
    allowed = registries.DISTRIBUTION_PARAMS.get(vary_distribution, frozenset())
    provided = {
        "vary_low": vary_low, "vary_high": vary_high,
        "vary_median": vary_median, "vary_sd": vary_sd, "vary_mode": vary_mode,
        "vary_skew": vary_skew, "vary_tailweight": vary_tailweight,
        "vary_outcomes": vary_outcomes,
    }
    for col, raw in provided.items():
        if raw is not None and col not in allowed:
            raise click.ClickException(
                f"--{col.replace('_', '-')} is not used by {vary_distribution} "
                f"(it uses: {', '.join(sorted(c.replace('vary_', '') for c in allowed))})"
            )
    missing = [
        col for col in registries.DISTRIBUTION_REQUIRED_PARAMS.get(vary_distribution, ())
        if provided[col] is None
    ]
    if missing:
        flags = ", ".join(f"--{c.replace('_', '-')}" for c in missing)
        raise click.ClickException(f"{vary_distribution} requires {flags}")

    def convert(raw: str | None) -> str:
        if raw is None:
            return ""
        if vary_target == "date":
            return _validate_calendar_date(raw)
        assert amount_unit is not None  # an amount target without one was rejected above
        return units.format_stored(amount_unit, _AMOUNT_PARSERS[amount_unit](raw))

    shape: dict[str, float] = {}
    for col, raw in (("vary_skew", vary_skew), ("vary_tailweight", vary_tailweight)):
        if raw is not None:
            try:
                shape[col] = float(raw)
            except ValueError:
                raise click.ClickException(f"--{col.replace('_', '-')} must be a number") from None

    if vary_distribution == "truncated_normal" and vary_target == "amount":
        # Not in DISTRIBUTION_REQUIRED_PARAMS: any of mean/sd/low/high may be
        # inferred from the other three (ADR-006), so no fixed column list
        # is "required" -- but *some* combination must be enough to resolve
        # every one, which is worth catching now rather than only at the
        # separate 'sim load' step.
        assert amount_unit is not None  # rejected above for an amount target
        amount_parser = _AMOUNT_PARSERS[amount_unit]
        skew = shape.get("vary_skew", 0.0)
        tailweight = shape.get("vary_tailweight", 1.0)
        try:
            median, sd, low, high = resolve_truncated_normal_bounds(
                float(amount_parser(vary_median)) if vary_median is not None else None,
                float(amount_parser(vary_sd)) if vary_sd is not None else None,
                float(amount_parser(vary_low)) if vary_low is not None else None,
                float(amount_parser(vary_high)) if vary_high is not None else None,
                skew=skew, tailweight=tailweight,
            )
            TruncatedNormal(median, sd, low, high, skew=skew, tailweight=tailweight)
        except DistributionError as e:
            raise click.ClickException(str(e)) from None

    return {
        "name": row_name,
        "vary_target": vary_target,
        "vary_distribution": vary_distribution,
        "vary_outcomes": _outcomes_cell(vary_outcomes, vary_target, amount_unit),
        "vary_low": convert(vary_low),
        "vary_high": convert(vary_high),
        "vary_median": convert(vary_median),
        "vary_sd": convert(vary_sd),
        "vary_skew": vary_skew if vary_skew is not None else "",
        "vary_tailweight": vary_tailweight if vary_tailweight is not None else "",
        "vary_mode": convert(vary_mode),
        **_correlation_fields(vary_correlate_with, vary_correlation, vary_tail_dependence),
        "vary_ramp_months": str(vary_ramp_months) if vary_ramp_months is not None else "",
        **_process_fields(
            vary_process, vary_persistence, vary_shock_sd, vary_target=vary_target,
            vary_ramp_months=vary_ramp_months, amount_unit=amount_unit,
            vary_process_start=vary_process_start,
        ),
    }


@sim_group_command.group(name="transfer")
def transfer_group_command() -> None:
    """Inspect or modify one-time calendar transfers in a scenario."""


def _do_add_transfer(
    *,
    date: str | None,
    from_account: str | None,
    to_account: str | None,
    amount: str | None,
    from_lot: str | None,
    to_lot: str | None,
    scenario: str | None,
    data_dir: Path | None,
    tax_character: str,
    description: str,
    series: str,
    row_name: str | None,
    vary_target: str | None,
    vary_distribution: str | None,
    vary_low: str | None,
    vary_high: str | None,
    vary_median: str | None,
    vary_sd: str | None,
    vary_skew: str | None,
    vary_tailweight: str | None,
    vary_mode: str | None,
    vary_outcomes: str | None,
    vary_correlate_with: str | None,
    vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
) -> None:
    """Shared body of ``sim transfer add`` / ``sim calendar add transfer``."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    with _intent_db(data_dir_p, scenario) as (con, _name):
        endpoints = _endpoints(con)

    if not date:
        date = _prompt_with_completion(_CALENDAR_DATE_PROMPT, names.date_marker_names(data_dir_p, scenario_name))
    date = _validate_calendar_date(date)

    from_account, from_lot, to_account, to_lot = _pick_endpoints(
        endpoints, from_account or None, from_lot, to_account or None, to_lot,
    )

    if amount is None:
        amount = _prompt_with_completion("amount (dollars, e.g. 1234.56)")
    cents = _parse_amount_cents(amount)
    if cents <= 0:
        raise click.ClickException("amount must be positive")

    if tax_character and tax_character not in registries.TAX_CHARACTERS:
        raise click.ClickException(
            f"unknown tax_character: {tax_character!r}; "
            f"allowed: {sorted(registries.TAX_CHARACTERS)}"
        )

    vary_fields = _vary_row_fields(
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
        vary_outcomes=vary_outcomes,
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence,
        vary_ramp_months=vary_ramp_months, amount_unit="dollars",
    )

    # account_1/account_2/amount/tax_character are already carried by the
    # row's own promoted columns below -- _build_cal_payload sets them from
    # those columns, then overlays this JSON on top, so duplicating them here
    # would silently shadow a resolved (ADR-006 vary_target='amount') amount
    # with this row's stale placeholder. Only description has no promoted
    # column of its own.
    payload = {}
    if description:
        payload["description"] = description
    row = {
        "date": date,
        "kind": "one_off_transfer",
        "account_1": from_account,
        "lot_1": from_lot,
        "account_2": to_account,
        "lot_2": to_lot,
        "series": series,
        "amount": units.cents_to_dollars(cents),
        "tax_character": tax_character,
        "params_json": json.dumps(payload, separators=(",", ":")),
        "description": description,
        "tags": "",
        **vary_fields,
    }
    path = scenario_dir / "calendar.csv"
    verb = _write_calendar_row(path, row, data_dir=data_dir_p, scenario_name=scenario_name)
    click.echo(
        f"{verb} one_off_transfer {from_account} -> {to_account} "
        f"{_fmt_cents(cents).strip()} on {date} to {path}"
    )


@transfer_group_command.command("add")
@click.argument("date", required=False, type=completion.CALENDAR_DATE)
@click.argument("from_account", required=False, type=completion.ENDPOINT)
@click.argument("to_account", required=False, type=completion.ENDPOINT)
@click.argument("amount", required=False)
@click.option("--from-lot", type=completion.LotName("from_account"), default=None,
              help="Lot of the from account; asked for when a blank one would not say which.")
@click.option("--to-lot", type=completion.LotName("to_account"), default=None,
              help="Lot of the to account; asked for when a blank one would not say which.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--tax-character", default="",
              help="ADR-003 tax_character (e.g. ordinary, ltcg, none); "
                   "left out, it follows from the accounts (ADR-003 §3.4).")
@click.option("--description", default="", help="Free-text description.")
@click.option("--series", type=completion.SeriesName(), default="", help="Optional series tag.")
@_vary_row_options
def transfer_add(
    date: str | None,
    from_account: str | None,
    to_account: str | None,
    amount: str | None,
    from_lot: str | None,
    to_lot: str | None,
    scenario: str | None,
    data_dir: Path | None,
    tax_character: str,
    description: str,
    series: str,
    row_name: str | None,
    vary_target: str | None,
    vary_distribution: str | None,
    vary_low: str | None,
    vary_high: str | None,
    vary_median: str | None,
    vary_sd: str | None,
    vary_skew: str | None,
    vary_tailweight: str | None,
    vary_mode: str | None,
    vary_outcomes: str | None,
    vary_correlate_with: str | None,
    vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
) -> None:
    """Append a one-time transfer to <scenario>/calendar.csv."""
    _do_add_transfer(
        date=date, from_account=from_account, to_account=to_account, amount=amount,
        from_lot=from_lot, to_lot=to_lot, scenario=scenario, data_dir=data_dir,
        tax_character=tax_character, description=description, series=series,
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
        vary_outcomes=vary_outcomes,
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence,
        vary_ramp_months=vary_ramp_months,
    )


# ---------- calendar (every kind, ADR-003 §3) ----------


@sim_group_command.group(name="calendar")
def calendar_group_command() -> None:
    """Append or change calendar.csv rows of any kind, including ensemble
    variation (ADR-006)."""


_CALENDAR_COMMAND_OF_KIND = {
    "set_rate": "set-rate",
    "set_amount": "set-amount",
    "retire_scheduled": "retire-scheduled",
    "one_off_transfer": "transfer",
    "transfer_balance": "transfer-balance",
    "swap_aux_table": "swap-aux-table",
    registries.LATENT_FACTOR_KIND: "latent-factor",
    registries.DATE_MARKER_KIND: "date-marker",
}


def _calendar_row_answers(row: dict[str, str]) -> tuple[calendar_guide.KindSpec, dict[str, str]] | None:
    """A calendar.csv row as the guide's answers for its kind -- the inverse
    of what the kind's command writes -- or None for a kind with no command."""
    command = _CALENDAR_COMMAND_OF_KIND.get(row.get("kind", ""))
    if command is None:
        return None
    spec = calendar_guide.kind_named(command)
    try:
        params = json.loads(row["params_json"]) if row.get("params_json") else {}
    except ValueError:
        params = {}

    varying = bool(row.get("vary_distribution"))
    correlated = bool(row.get("vary_correlate_with"))
    values = {
        "date": row["date"],
        "aux_kind": str(params.get("aux_kind", "rate")),
        "source_series": str(params.get("source_series", "")),
        "series": row["series"],
        "rate_percent": row["amount"],
        "scheduled_name": row["scheduled_name"],
        "amount": row["amount"],
        "param_date": row["param_date"],
        "from_account": row["account_1"], "from_lot": row["lot_1"],
        "to_account": row["account_2"], "to_lot": row["lot_2"],
        "tax_character": row["tax_character"],
        "description": row["description"] or (
            # Only a one_off_transfer's command keeps a copy of it in params_json.
            str(params.get("description", "")) if command == "transfer" else ""
        ),
        "tags": row["tags"],
        "name": row["name"],
        "row_name": row["name"],
        "_vary": "y" if varying else "n",
        "vary_target": row["vary_target"],
        "vary_distribution": row["vary_distribution"],
        "vary_low": row["vary_low"], "vary_high": row["vary_high"],
        "vary_median": row["vary_median"], "vary_sd": row["vary_sd"],
        "vary_mode": row["vary_mode"], "vary_outcomes": row["vary_outcomes"],
        "vary_skew": row["vary_skew"], "vary_tailweight": row["vary_tailweight"],
        "vary_ramp_months": row["vary_ramp_months"],
        "vary_process": row["vary_process"],
        "vary_persistence": row["vary_persistence"],
        "vary_shock_sd": row["vary_shock_sd"],
        "vary_process_start": row["vary_process_start"],
        "vary_resume": row.get("vary_resume", ""),
        "_correlate": "y" if correlated else "n",
        "vary_correlate_with": row["vary_correlate_with"],
        "vary_correlation": row["vary_correlation"],
        "vary_tail_dependence": row["vary_tail_dependence"],
    }
    return spec, {f.param: values[f.param] for f in spec.fields}


def _guide_context(
    data_dir: Path, scenario: str | None,
    *, rows: list[dict[str, str]] | None = None, editing: int | None = None,
) -> calendar_guide.GuideContext:
    """The scenario's names, as completions for the guided prompts (#32).

    ``rows`` are the scenario's own calendar.csv rows, whose names a new or
    edited row may not reuse. With ``editing`` (a row's index), the others
    are also what the edited row may not duplicate, and its own name is not
    offered to correlate with.
    """
    def column(con: sqlite3.Connection, sql: str, *args: str) -> list[str]:
        return sorted({r[0] for r in con.execute(sql, args) if r[0]})

    # Names are compared trimmed, as the loader trims every cell before it
    # checks them.
    others = [row for index, row in enumerate(rows or []) if index != editing]
    own_name = rows[editing]["name"].strip() if rows and editing is not None else ""
    other_rows: dict[str, list[dict[str, str]]] = {}
    if editing is not None:
        for row in others:
            converted = _calendar_row_answers(row)
            if converted is not None:
                other_rows.setdefault(converted[0].command, []).append(converted[1])

    with _intent_db(data_dir, scenario) as (con, _name):
        aux = "SELECT series FROM aux_table WHERE kind = ?"
        return calendar_guide.GuideContext(
            accounts=column(con, "SELECT name FROM account"),
            asset_accounts=column(con, "SELECT name FROM account WHERE kind = 'asset'"),
            lots=_lot_info(con),
            chains=_interior_chains(con), chain_roles=_chain_roles(con),
            scheduled_names=column(con, "SELECT name FROM scheduled_transaction"),
            series={
                # Every loaded table's series, for swap_aux_table (#197).
                **{
                    kind: column(con, aux, kind)
                    for kind in sorted(registries.AUX_KIND_COLUMNS)
                },
                "rate": sorted({
                    *column(con, aux, "rate"),
                    *column(con, "SELECT series FROM account_lot"),
                }),
                "inflation": sorted({
                    *column(con, aux, "inflation"),
                    *column(con, "SELECT series FROM scheduled_transaction"),
                } - {"fixed"}),
            },
            markers=column(
                con, "SELECT name FROM calendar_event WHERE kind = ?", registries.DATE_MARKER_KIND,
            ),
            row_names=[
                name for name in column(con, "SELECT name FROM calendar_event")
                if name != own_name
            ] + [  # a varying lifespan is a correlation parent too (#221)
                name for name in varying_names(con)
                if name.startswith(registries.LIFESPAN_NODE_PREFIX)
            ],
            other_rows=other_rows,
            taken_names=frozenset(row["name"].strip() for row in others if row["name"].strip()),
        )


def _canonical_calendar_date(spec: calendar_guide.KindSpec, typed: str) -> str:
    """A row's date as the loader will read it, for comparing rows: a bare
    month and that month's last day are the same date (#34)."""
    kind = next(k for k, command in _CALENDAR_COMMAND_OF_KIND.items() if command == spec.command)
    return calendar_row_key({"kind": kind, "date": typed})[0]


def _calendar_guide() -> calendar_guide.Guide:
    return calendar_guide.Guide(
        ask=lambda text, choices, default: _prompt_with_completion(
            text, choices=choices, default=default,
        ),
        checkers={
            "date": _validate_calendar_date,
            "dollars": _parse_amount_cents,
            "percent": _percent_to_rate_micros,
            "number": float,
            "integer": int,
            "tags": registries.parse_tags,
        },
        canonical_date=_canonical_calendar_date,
    )


def _ask_missing_arguments(
    command: str, data_dir: Path | None, scenario: str | None, **given: str | None,
) -> dict[str, str]:
    """``given`` with any positional argument of ``command`` that was left
    out prompted for (#32). Nothing is opened or asked when none is missing."""
    spec = calendar_guide.kind_named(command)
    answers = {param: value for param, value in given.items() if value is not None}
    if all(f.param in answers for f in spec.fields if f.is_argument):
        return answers
    context = _guide_context(_resolve_data_dir(data_dir), scenario)
    return _calendar_guide().complete(spec, context, answers, arguments_only=True)


@calendar_group_command.group(name="add", invoke_without_command=True)
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="For the guided mode: scenario folder name "
                   "(default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="For the guided mode: data repo (default: $ANALYSIS_DATA_DIR).")
@click.pass_context
def calendar_add_group_command(
    ctx: click.Context, scenario: str | None, data_dir: Path | None,
) -> None:
    """Append one calendar.csv row. Each kind has its own subcommand -- the
    row shape differs by kind (ADR-003 §4.8).

    With no kind, asks for everything a question at a time, shows the row and
    the equivalent command, and writes it once you confirm. A subcommand
    given without its arguments asks for just those.
    """
    if ctx.invoked_subcommand is not None:
        if scenario is not None or data_dir is not None:
            raise click.UsageError(
                "--scenario and --data-dir go after the kind: "
                f"sim calendar add {ctx.invoked_subcommand} ... --scenario NAME"
            )
        return
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "calendar.csv"
    _echo_guided_scenario(scenario_name, path, defaulted=scenario is None)
    context = _guide_context(data_dir_p, scenario_name, rows=_read_calendar_rows(path))
    guide = _calendar_guide()
    click.echo("What kind of calendar row?")
    for spec in calendar_guide.KINDS:
        click.echo(f"  {spec.command:<17} {spec.summary}")
    commands = [spec.command for spec in calendar_guide.KINDS]
    while (kind := _prompt_with_completion("kind", choices=commands).strip()) not in commands:
        click.echo(f"  not one of: {', '.join(commands)}")
    spec = calendar_guide.kind_named(kind)
    answers = guide.complete(spec, context, {})

    def submit(params: dict[str, object]) -> None:
        # The scenario announced above, by name: a defaulted scenario is not
        # resolved again, in case the default changed during the prompts.
        ctx.invoke(
            calendar_add_group_command.commands[kind],
            scenario=scenario_name, data_dir=data_dir_p, **params,
        )

    if not guide.review(
        spec, context, answers, submit,
        scenario=scenario, data_dir=str(data_dir) if data_dir is not None else None,
    ):
        ctx.exit(1)


def _echo_guided_scenario(name: str, path: Path, *, defaulted: bool) -> None:
    """Say which scenario file a guided command is about to change, before
    it asks anything. A defaulted scenario was already named by its
    ``Using scenario`` line (``_resolve_scenario``), so only its file follows."""
    if not defaulted:
        click.echo(f"Scenario: {name}")
    click.echo(f"  {path}")


def _select_calendar_row(
    rows: list[dict[str, str]], wanted: str | None, notes: list[str] | None = None,
) -> int:
    """Narrow ``rows`` to one, by its number, its name, or text it contains;
    asks (again) while that leaves more or fewer than one. ``notes``, one
    per row, are added to the rows' labels."""
    labels = [_calendar_row_label(row) for row in rows]
    if notes is not None:
        labels = [f"{label}  {note}" if note else label for label, note in zip(labels, notes, strict=True)]
    candidates = list(range(len(rows)))
    while True:
        if wanted is None:
            for index in candidates:
                click.echo(f"  {index + 1:>3}  {labels[index]}")
            names = [rows[index]["name"] for index in candidates if rows[index]["name"]]
            wanted = _prompt_with_completion(
                "row (its number or name, or text to narrow the list)",
                choices=[*names, *(str(index + 1) for index in candidates)],
            )
        text, wanted = wanted.strip(), None
        if text.isdigit() and 1 <= int(text) <= len(rows):
            return int(text) - 1
        named = [index for index, row in enumerate(rows) if text and row["name"] == text]
        if len(named) == 1:
            return named[0]
        matches = [index for index in candidates if text.lower() in labels[index].lower()]
        if len(matches) == 1:
            return matches[0]
        if matches:
            candidates = matches
        else:
            click.echo(f"  no row matches {text!r}")


def _calendar_entries(
    scenario_dir: Path, scenario_name: str, verb: str,
) -> tuple[list[_CalendarRow], list[str]]:
    """The scenario's calendar rows for a command to pick from: its own
    first, numbered as in its own file, then those it inherits; and a note
    for each saying where an inherited row or an override comes from."""
    overlaid = _scenario_calendar_rows(scenario_dir)
    entries = [
        *sorted((e for e in overlaid if e.index is not None), key=lambda e: e.index or 0),
        *(e for e in overlaid if e.index is None),
    ]
    if not entries:
        raise click.ClickException(f"scenario {scenario_name!r} has no calendar rows to {verb}")
    notes = [
        f"[inherited from {entry.inherited_from}]" if entry.index is None
        else f"[overrides {entry.inherited_from}]" if entry.inherited_from
        else ""
        for entry in entries
    ]
    return entries, notes


def _check_own_calendar_row(staged: Path, path: Path, index: int, chosen: dict[str, str]) -> None:
    """Refuse when the staged calendar.csv no longer has ``chosen`` at
    ``index``: the file changed since the row was picked."""
    existing = _read_calendar_rows(staged)
    if index >= len(existing) or existing[index] != chosen:
        raise click.ClickException(
            f"{path} changed after row {index + 1} was chosen, so it is no longer "
            "that row. Nothing was written; start again."
        )


@calendar_group_command.command("edit")
@click.argument("row", type=completion.CALENDAR_ROW, required=False)
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.pass_context
def calendar_edit(
    ctx: click.Context, row: str | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Change one row of the scenario's calendar, its own or inherited.

    ROW is the row's number, its name, or text that picks it out; without it,
    or if it matches several rows, the rows are listed to choose from. The
    row's fields are then shown: name one to change it, as often as needed,
    and write when done. The row keeps its kind and its place in the file.

    A row the scenario inherits is changed here by an override: the whole
    row, edited, appended to this scenario's calendar.csv; the parent keeps
    its own. The override keeps the row's name (`sim calendar rename`
    renames it everywhere), and one of an unnamed row keeps its date, kind,
    rule, accounts, lots, series and amount, which are how the load finds the row it
    overrides.

    A change that would make the row a copy of another row, or reuse another
    row's name, is refused.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "calendar.csv"
    _echo_guided_scenario(scenario_name, path, defaulted=scenario is None)
    entries, notes = _calendar_entries(scenario_dir, scenario_name, "edit")
    rows = [entry.row for entry in entries]
    # The own rows as the file has them, which the write checks are still there.
    own_rows = _read_calendar_rows(path)
    index = _select_calendar_row(rows, row, notes)
    chosen = entries[index]
    converted = _calendar_row_answers(rows[index])
    if converted is None:
        where = path if chosen.index is not None else f"scenario {chosen.inherited_from!r}"
        raise click.ClickException(
            f"row {index + 1} is a {rows[index]['kind']!r} row, which this command "
            f"cannot edit; change it in {where}"
        )
    spec, answers = converted
    click.echo(f"Editing row {index + 1}: {_calendar_row_label(rows[index])}  {notes[index]}".rstrip())
    context = _guide_context(data_dir_p, scenario_name, rows=rows, editing=index)
    guide = _calendar_guide()
    guide.complete(spec, context, answers)

    def submit(params: dict[str, object]) -> None:
        if chosen.index is None:
            key, value = _CALENDAR_OVERRIDE_ROW, (rows[index], chosen.inherited_from)
        else:
            key, value = _CALENDAR_REPLACE_ROW, (chosen.index, own_rows[chosen.index])
        ctx.meta[key] = value
        try:
            # The scenario announced above, by name: a defaulted scenario is
            # not resolved again, in case the default changed meanwhile.
            ctx.invoke(
                calendar_add_group_command.commands[spec.command],
                scenario=scenario_name, data_dir=data_dir_p, **params,
            )
        finally:
            del ctx.meta[key]

    if not guide.review(spec, context, answers, submit, show_command=False):
        ctx.exit(1)


def _dated_from(row: Mapping[str, str], marker: str) -> bool:
    """Whether any of ``row``'s dates is written relative to ``marker`` (#281)."""
    return any(
        (rel := parse_relative_date(row.get(column) or "")) is not None and rel.anchor == marker
        for column in registries.RELATIVE_DATE_COLUMNS
    )


@calendar_group_command.command("delete")
@click.argument("row", type=completion.CALENDAR_ROW, required=False)
@click.option("--yes", is_flag=True, help="Don't ask before deleting.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def calendar_delete(row: str | None, yes: bool, scenario: str | None, data_dir: Path | None) -> None:
    """Delete one row of the scenario's calendar.

    ROW is picked as `sim calendar edit` picks it: the row's number, its
    name, or text that picks it out; without it, or if it matches several
    rows, the rows are listed to choose from.

    A row other rows correlate with (their vary_correlate_with names it) is
    refused, naming them. Deleting this scenario's override of an inherited
    row brings back the inherited row. A row the scenario inherits can't be
    deleted here, since an overlay can't remove a row: delete it in the
    scenario it comes from.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "calendar.csv"
    entries, notes = _calendar_entries(scenario_dir, scenario_name, "delete")
    rows = [entry.row for entry in entries]
    index = _select_calendar_row(rows, row, notes)
    chosen = entries[index]
    label = _calendar_row_label(chosen.row)
    if chosen.index is None:
        raise click.ClickException(
            f"row {label!r} comes from scenario {chosen.inherited_from!r}, and "
            f"{scenario_name!r} can't remove what it inherits; delete it in "
            f"{chosen.inherited_from!r} to remove it from every scenario built on it"
        )
    name = chosen.row["name"].strip()
    if name and chosen.inherited_from is None:
        dependents = [
            other for other in rows
            if other is not chosen.row and other["vary_correlate_with"].strip() == name
        ]
        if dependents:
            raise click.ClickException(
                f"row {name!r} is correlated with by "
                + "; ".join(_calendar_row_label(other) for other in dependents)
                + ". Change or delete those rows first. Nothing was written."
            )
        dated = [other for other in rows if other is not chosen.row and _dated_from(other, name)]
        if dated:
            raise click.ClickException(
                f"date_marker {name!r} dates "
                + "; ".join(_calendar_row_label(other) for other in dated)
                + ". Change or delete those rows first. Nothing was written."
            )
    own = _read_calendar_rows(path)[chosen.index]
    click.echo(f"row {chosen.index + 1} of {path}: {label}  {notes[index]}".rstrip())
    if not yes and not click.confirm("delete it?", default=False):
        click.echo("aborted")
        return
    with _scenario_edit(data_dir_p, scenario_name) as edit:
        staged = edit.path(path)
        _check_own_calendar_row(staged, path, chosen.index, own)
        frame = pd.read_csv(staged, dtype=str, keep_default_na=False)
        frame.drop(index=frame.index[chosen.index]).to_csv(staged, index=False)
    if chosen.inherited_from is not None:
        click.echo(
            f"deleted this scenario's override from {path}; the row is inherited "
            f"from {chosen.inherited_from!r} again"
        )
    else:
        click.echo(f"deleted row {chosen.index + 1} from {path}")


@calendar_group_command.group(name="tag")
def calendar_tag_group() -> None:
    """Add or remove tags on a calendar row."""


def _calendar_tag(
    row: str | None, tag_tokens: tuple[str, ...], scenario: str | None, data_dir: Path | None,
    op: str,
) -> None:
    """``sim calendar tag add|rm``: change one row's tags. An inherited row
    gets an override carrying the new tags (#202). A row or tags left out
    are asked for."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "calendar.csv"
    entries, notes = _calendar_entries(scenario_dir, scenario_name, "tag")
    row, tag_tokens = _name_and_tags(
        row, tag_tokens, lambda: [(e.row.get("name") or "").strip() for e in entries])
    rows = [entry.row for entry in entries]
    chosen = entries[_select_calendar_row(rows, row, notes)]
    tag_tokens = _ask_tags(tag_tokens, names.tag_tokens(data_dir_p, scenario_name))
    try:
        pairs = registries.parse_tags(" ".join(tag_tokens))
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    old = registries.parse_tags(chosen.row["tags"])
    new = [*old, *(p for p in pairs if p not in old)] if op == "add" else [p for p in old if p not in pairs]
    tags = registries.format_tags(new)
    label = _calendar_row_label(chosen.row)
    if set(new) == set(old):
        click.echo(f"calendar row {label!r} tags unchanged: {tags or '(none)'}")
        return
    with _scenario_edit(data_dir_p, scenario_name) as edit:
        staged = edit.path(path)
        if chosen.index is None:
            # The whole row, so the loader finds the inherited one by its
            # name or key and the override means the same however a blank
            # cell merges.
            _append_csv_row(staged, registries.CALENDAR_HEADER, {**chosen.row, "tags": tags})
        else:
            own = _read_calendar_rows(path)[chosen.index]
            _check_own_calendar_row(staged, path, chosen.index, own)
            _append_csv_row(
                staged, registries.CALENDAR_HEADER, {**own, "tags": tags}, replace=chosen.index,
            )
    if chosen.index is None:
        click.echo(
            f"wrote an override of calendar row {label!r} (inherited from "
            f"{chosen.inherited_from!r}) to {path}; tags: {tags or '(none)'}"
        )
    else:
        click.echo(f"calendar row {label!r} tags: {tags or '(none)'}")


@calendar_tag_group.command("add")
@click.argument("row", required=False, type=completion.CALENDAR_ROW)
@click.argument("tag_tokens", type=completion.ANY_TAG, nargs=-1, required=False)
@click.option("--scenario", type=completion.SCENARIO, default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def calendar_tag_add(
    row: str | None, tag_tokens: tuple[str, ...], scenario: str | None, data_dir: Path | None,
) -> None:
    """Add one or more '<role>:<name>' tags to a calendar row.

    ROW is picked as `sim calendar edit` picks it (its number, its name, or
    text that picks it out). A row the scenario inherits gets an override in
    this scenario carrying the new tags; the parent keeps its own.
    """
    _calendar_tag(row, tag_tokens, scenario, data_dir, "add")


@calendar_tag_group.command("rm")
@click.argument("row", required=False, type=completion.CALENDAR_ROW)
@click.argument("tag_tokens", type=completion.TAG, nargs=-1, required=False)
@click.option("--scenario", type=completion.SCENARIO, default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def calendar_tag_rm(
    row: str | None, tag_tokens: tuple[str, ...], scenario: str | None, data_dir: Path | None,
) -> None:
    """Remove one or more '<role>:<name>' tags from a calendar row; an
    inherited row gets an override without them."""
    _calendar_tag(row, tag_tokens, scenario, data_dir, "rm")


def _scheduled_names(data_dir: Path, scenario: str | None) -> set[str]:
    with _intent_db(data_dir, scenario) as (con, _name):
        return {r[0] for r in con.execute("SELECT name FROM scheduled_transaction")}


@calendar_add_group_command.command("set-rate")
@click.argument("date", required=False, type=completion.CALENDAR_DATE)
@click.argument("series", type=completion.SeriesName(kind_param="aux_kind"), required=False)
@click.argument("rate_percent", required=False)
@click.option(
    "--aux-kind", type=completion.AUX_KIND, default="rate", show_default=True,
    help="aux_table kind this rate belongs to. The default 'rate' is an asset's own "
         "growth series (accounts.csv's own series column); use 'inflation' to vary an "
         "inflation/COLA series such as cpi (scheduled.csv rows reference it by series name).",
)
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default="", help="Free-text description.")
@click.option("--tags", default="",
              help="Space-separated '<role>:<name>' tokens (e.g. 'expense:discretionary').")
@click.option(
    "--resume", "vary_resume", type=completion.CALENDAR_DATE, default=None,
    help="Instead of a rate: from DATE, put SERIES back to whatever governed it the day "
         "before this date, usually an episode's start marker (#291). A process resumes "
         "with its own level, persistence and yearly shocks; a fixed rate comes back at "
         "once. Takes no RATE_PERCENT or --vary-* options.",
)
@_vary_row_options
@_vary_process_options
def calendar_add_set_rate(
    date: str | None, series: str | None, rate_percent: str | None, aux_kind: str,
    vary_resume: str | None,
    scenario: str | None, data_dir: Path | None, description: str, tags: str,
    row_name: str | None, vary_target: str | None, vary_distribution: str | None,
    vary_low: str | None, vary_high: str | None, vary_median: str | None, vary_sd: str | None,
    vary_skew: str | None, vary_tailweight: str | None, vary_mode: str | None,
    vary_outcomes: str | None,
    vary_correlate_with: str | None, vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
    vary_process: str | None, vary_persistence: str | None, vary_shock_sd: str | None,
    vary_process_start: str | None,
) -> None:
    """Append a set_rate event: SERIES starts earning RATE_PERCENT (e.g. 4.5) from DATE.

    RATE_PERCENT is a schema placeholder, ignored, when --vary-target=amount.
    With --resume there is no RATE_PERCENT: the row puts back what governed
    SERIES before the --resume date. Arguments left out are asked for.
    """
    try:
        registries.parse_tags(tags)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    vary_resume = vary_resume or None
    if vary_resume is not None:
        given_vary = [
            name for name, value in (
                ("--vary-target", vary_target), ("--vary-distribution", vary_distribution),
                ("--vary-low", vary_low), ("--vary-high", vary_high),
                ("--vary-median", vary_median), ("--vary-sd", vary_sd),
                ("--vary-skew", vary_skew), ("--vary-tailweight", vary_tailweight),
                ("--vary-mode", vary_mode), ("--vary-outcomes", vary_outcomes),
                ("--vary-correlate-with", vary_correlate_with),
                ("--vary-correlation", vary_correlation),
                ("--vary-tail-dependence", vary_tail_dependence),
                ("--vary-ramp-months", vary_ramp_months), ("--vary-process", vary_process),
                ("--vary-persistence", vary_persistence), ("--vary-shock-sd", vary_shock_sd),
                ("--vary-process-start", vary_process_start),
            ) if value not in (None, "")
        ]
        if rate_percent:
            given_vary.insert(0, "RATE_PERCENT")
        if given_vary:
            raise click.ClickException(
                f"--resume takes no {', '.join(given_vary)}: the row puts back what "
                "governed the series before the --resume date"
            )
        vary_resume = _validate_calendar_date(vary_resume)
    given = _ask_missing_arguments(
        "set-rate", data_dir, scenario,
        date=date, series=series, rate_percent=rate_percent, aux_kind=aux_kind,
        vary_resume=vary_resume,
    )
    date, series = given["date"], given["series"]
    date = _validate_calendar_date(date)
    if vary_resume is not None:
        _write_resume_row(
            date=date, series=series, aux_kind=aux_kind, vary_resume=vary_resume,
            row_name=row_name, description=description, tags=tags,
            scenario=scenario, data_dir=data_dir,
        )
        return
    rate_percent = given["rate_percent"]
    micros = _percent_to_rate_micros(rate_percent)
    vary_fields = _vary_row_fields(
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
        vary_outcomes=vary_outcomes,
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence,
        vary_ramp_months=vary_ramp_months, amount_unit="percent",
        vary_process=vary_process, vary_persistence=vary_persistence,
        vary_shock_sd=vary_shock_sd, vary_process_start=vary_process_start,
    )

    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    row = {
        "date": date, "kind": "set_rate", "series": series, "amount": units.micros_to_percent(micros),
        # aux_kind has no promoted column of its own -- _build_cal_payload
        # only ever learns it from params_json (defaulting to 'rate' when
        # absent), so it's only written here when it differs from that.
        "params_json": json.dumps({"aux_kind": aux_kind}) if aux_kind != "rate" else "",
        "description": description, "tags": tags, **vary_fields,
    }
    path = scenario_dir / "calendar.csv"
    verb = _write_calendar_row(path, row, data_dir=data_dir_p, scenario_name=scenario_name)
    click.echo(
        f"{verb} set_rate {series!r} -> {row["amount"]} on {date} "
        f"(aux_kind={aux_kind!r}) to {path}"
    )


def _write_resume_row(
    *, date: str, series: str, aux_kind: str, vary_resume: str, row_name: str | None,
    description: str, tags: str, scenario: str | None, data_dir: Path | None,
) -> None:
    """``calendar add set-rate --resume``: a set_rate row with no rate (#291)."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    row = {
        "date": date, "kind": "set_rate", "series": series, "amount": "",
        "params_json": json.dumps({"aux_kind": aux_kind}) if aux_kind != "rate" else "",
        "description": description, "tags": tags, "name": row_name or "",
        "vary_resume": vary_resume,
    }
    path = scenario_dir / "calendar.csv"
    verb = _write_calendar_row(path, row, data_dir=data_dir_p, scenario_name=scenario_name)
    click.echo(
        f"{verb} set_rate {series!r} -> resume what governed before {vary_resume} "
        f"on {date} (aux_kind={aux_kind!r}) to {path}"
    )


@calendar_add_group_command.command("set-amount")
@click.argument("date", required=False, type=completion.CALENDAR_DATE)
@click.argument("scheduled_name", type=completion.RULE, required=False)
@click.argument("amount", required=False)
@click.option("--param-date", default="",
              help="New base date for the rule (YYYY-MM or YYYY-MM-DD), optional.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default="", help="Free-text description.")
@click.option("--tags", default="",
              help="Space-separated '<role>:<name>' tokens (e.g. 'expense:discretionary').")
@_vary_row_options
def calendar_add_set_amount(
    date: str | None, scheduled_name: str | None, amount: str | None, param_date: str,
    scenario: str | None, data_dir: Path | None, description: str, tags: str,
    row_name: str | None, vary_target: str | None, vary_distribution: str | None,
    vary_low: str | None, vary_high: str | None, vary_median: str | None, vary_sd: str | None,
    vary_skew: str | None, vary_tailweight: str | None, vary_mode: str | None,
    vary_outcomes: str | None,
    vary_correlate_with: str | None, vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
) -> None:
    """Append a set_amount event: SCHEDULED_NAME's amount changes to AMOUNT
    (dollars) from DATE.

    AMOUNT is a schema placeholder, ignored, when --vary-target=amount.
    Arguments left out are asked for.
    """
    try:
        registries.parse_tags(tags)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    given = _ask_missing_arguments(
        "set-amount", data_dir, scenario,
        date=date, scheduled_name=scheduled_name, amount=amount,
    )
    date, scheduled_name, amount = given["date"], given["scheduled_name"], given["amount"]
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    names = _scheduled_names(data_dir_p, scenario)
    if scheduled_name not in names:
        raise click.ClickException(f"unknown scheduled_name: {scheduled_name!r}")

    date = _validate_calendar_date(date)
    if param_date:
        param_date = _validate_month_date(param_date)
    cents = _parse_amount_cents(amount)
    vary_fields = _vary_row_fields(
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
        vary_outcomes=vary_outcomes,
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence,
        vary_ramp_months=vary_ramp_months, amount_unit="dollars",
    )

    row = {
        "date": date, "kind": "set_amount", "scheduled_name": scheduled_name,
        "amount": units.cents_to_dollars(cents), "param_date": param_date,
        "description": description, "tags": tags, **vary_fields,
    }
    path = scenario_dir / "calendar.csv"
    verb = _write_calendar_row(path, row, data_dir=data_dir_p, scenario_name=scenario_name)
    click.echo(
        f"{verb} set_amount {scheduled_name!r} -> {_fmt_cents(cents).strip()} "
        f"on {date} to {path}"
    )


@calendar_add_group_command.command("retire-scheduled")
@click.argument("date", required=False, type=completion.CALENDAR_DATE)
@click.argument("scheduled_name", type=completion.RULE, required=False)
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default="", help="Free-text description.")
@click.option("--tags", default="",
              help="Space-separated '<role>:<name>' tokens (e.g. 'expense:discretionary').")
@_vary_row_options
def calendar_add_retire_scheduled(
    date: str | None, scheduled_name: str | None,
    scenario: str | None, data_dir: Path | None, description: str, tags: str,
    row_name: str | None, vary_target: str | None, vary_distribution: str | None,
    vary_low: str | None, vary_high: str | None, vary_median: str | None, vary_sd: str | None,
    vary_skew: str | None, vary_tailweight: str | None, vary_mode: str | None,
    vary_outcomes: str | None,
    vary_correlate_with: str | None, vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
) -> None:
    """Append a retire_scheduled event: SCHEDULED_NAME stops firing from DATE.

    Only --vary-target=date is meaningful here (no amount column to vary).
    Arguments left out are asked for.
    """
    if vary_target == "amount":
        raise click.ClickException(
            "retire_scheduled has no amount column to vary; use --vary-target date"
        )
    try:
        registries.parse_tags(tags)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    given = _ask_missing_arguments(
        "retire-scheduled", data_dir, scenario, date=date, scheduled_name=scheduled_name,
    )
    date, scheduled_name = given["date"], given["scheduled_name"]
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    names = _scheduled_names(data_dir_p, scenario)
    if scheduled_name not in names:
        raise click.ClickException(f"unknown scheduled_name: {scheduled_name!r}")

    date = _validate_calendar_date(date)
    vary_fields = _vary_row_fields(
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
        vary_outcomes=vary_outcomes,
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence,
        vary_ramp_months=vary_ramp_months, amount_unit=None,
    )

    row = {
        "date": date, "kind": "retire_scheduled", "scheduled_name": scheduled_name,
        "description": description, "tags": tags, **vary_fields,
    }
    path = scenario_dir / "calendar.csv"
    verb = _write_calendar_row(path, row, data_dir=data_dir_p, scenario_name=scenario_name)
    click.echo(f"{verb} retire_scheduled {scheduled_name!r} on {date} to {path}")


@calendar_add_group_command.command("latent-factor")
@click.argument("name", required=False)
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default="", help="What this factor stands for.")
@click.option("--vary-correlate-with", type=completion.CORRELATE_WITH, default=None,
              help="Another factor (or varying row) this factor depends on, for a hierarchy.")
@click.option("--vary-correlation", default=None,
              help="rho with --vary-correlate-with, between -1 and 1.")
@click.option("--vary-tail-dependence", default=None, help=_TAIL_DEPENDENCE_HELP)
def calendar_add_latent_factor(
    name: str | None, scenario: str | None, data_dir: Path | None, description: str,
    vary_correlate_with: str | None, vary_correlation: str | None,
    vary_tail_dependence: str | None,
) -> None:
    """Append a latent factor NAME (ADR-008): a shared influence ("the
    inflation regime") that varying rows correlate with through
    --vary-correlate-with, but that isn't itself a modeled value.

    A factor with no dependents yet affects nothing; `sim check` warns of
    it. NAME is asked for if left out.
    """
    name = _ask_missing_arguments("latent-factor", data_dir, scenario, name=name)["name"]
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    row = {
        "kind": registries.LATENT_FACTOR_KIND, "name": name, "description": description,
        **_correlation_fields(vary_correlate_with, vary_correlation, vary_tail_dependence),
    }
    path = scenario_dir / "calendar.csv"
    verb = _write_calendar_row(path, row, data_dir=data_dir_p, scenario_name=scenario_name)
    click.echo(f"{verb} latent_factor {name!r} to {path}")


@calendar_add_group_command.command("date-marker")
@click.argument("name", required=False)
@click.argument("date", required=False, type=completion.CALENDAR_DATE)
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default="", help="What this date stands for.")
@click.option("--tags", default="",
              help="Space-separated '<role>:<name>' tokens (e.g. 'expense:discretionary').")
@click.option("--vary-target", type=click.Choice(["date"]), default=None,
              help="date, the only thing a marker can vary; implied by --vary-distribution.")
@click.option("--vary-distribution",
              type=click.Choice(sorted(registries.ENSEMBLE_DISTRIBUTIONS - {"truncated_normal"})),
              default=None, help="Draw the date per ensemble member from this distribution.")
@click.option("--vary-low", default=None,
              help="The earliest date (a bare month is its first day).")
@click.option("--vary-high", default=None,
              help="The latest date (a bare month is its last day).")
@click.option("--vary-mode", default=None,
              help="triangular's most likely date (a bare month is mid-month).")
@click.option("--vary-outcomes", default=None,
              help="discrete's weighted dates or date ranges, e.g. "
                   "'60%%: 2030-06; 40%%: 2031-01..2031-12'.")
@click.option("--vary-correlate-with", type=completion.CORRELATE_WITH, default=None,
              help="Another varying row's or a latent factor's name: this marker's parent in "
                   "a correlation tree (ADR-008). Requires --vary-correlation.")
@click.option("--vary-correlation", default=None,
              help="rho with --vary-correlate-with, between -1 and 1.")
@click.option("--vary-tail-dependence", default=None, help=_TAIL_DEPENDENCE_HELP)
def calendar_add_date_marker(
    name: str | None, date: str | None, scenario: str | None, data_dir: Path | None,
    description: str, tags: str, vary_target: str | None, vary_distribution: str | None,
    vary_low: str | None, vary_high: str | None, vary_mode: str | None,
    vary_outcomes: str | None, vary_correlate_with: str | None,
    vary_correlation: str | None, vary_tail_dependence: str | None,
) -> None:
    """Append a date marker NAME on DATE (#281): a named date that other rows'
    dates can be written relative to, as NAME, NAME+2y or NAME-6m. It
    changes nothing by itself; vary it to move all of them together.

    A date past the scenario's end drops every row dated on or after it. A marker
    no row refers to yet affects nothing; `sim check` says so. Arguments
    left out are asked for.
    """
    try:
        registries.parse_tags(tags)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    given = _ask_missing_arguments("date-marker", data_dir, scenario, name=name, date=date)
    name, date = given["name"], _validate_calendar_date(given["date"])
    if not is_marker_name(name):
        raise click.ClickException(
            f"date_marker name {name!r} can't be written in a date: start with a letter or '_', "
            "use only letters, digits, '_', '.' and '-', and don't end in an offset like '-6m'"
        )
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    varies = vary_distribution is not None
    if vary_target is not None and not varies:
        raise click.ClickException("--vary-target goes with --vary-distribution")
    vary_fields = _vary_row_fields(
        row_name=name, vary_target="date" if varies else None,
        vary_distribution=vary_distribution, vary_low=vary_low, vary_high=vary_high,
        vary_median=None, vary_sd=None, vary_skew=None, vary_tailweight=None,
        vary_mode=vary_mode, vary_outcomes=vary_outcomes,
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence, vary_ramp_months=None, amount_unit=None,
    )
    row = {
        "date": date, "kind": registries.DATE_MARKER_KIND, "description": description,
        "tags": tags, **vary_fields, "name": name,
    }
    path = scenario_dir / "calendar.csv"
    verb = _write_calendar_row(path, row, data_dir=data_dir_p, scenario_name=scenario_name)
    click.echo(f"{verb} date_marker {name!r} on {date} to {path}")


@calendar_add_group_command.command("transfer")
@click.argument("date", required=False, type=completion.CALENDAR_DATE)
@click.argument("from_account", required=False, type=completion.ENDPOINT)
@click.argument("to_account", required=False, type=completion.ENDPOINT)
@click.argument("amount", required=False)
@click.option("--from-lot", type=completion.LotName("from_account"), default=None,
              help="Lot of the from account; asked for when a blank one would not say which.")
@click.option("--to-lot", type=completion.LotName("to_account"), default=None,
              help="Lot of the to account; asked for when a blank one would not say which.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--tax-character", default="",
              help="ADR-003 tax_character (e.g. ordinary, ltcg, none); "
                   "left out, it follows from the accounts (ADR-003 §3.4).")
@click.option("--description", default="", help="Free-text description.")
@click.option("--series", type=completion.SeriesName(), default="", help="Optional series tag.")
@_vary_row_options
def calendar_add_transfer(
    date: str | None,
    from_account: str | None,
    to_account: str | None,
    amount: str | None,
    from_lot: str | None,
    to_lot: str | None,
    scenario: str | None,
    data_dir: Path | None,
    tax_character: str,
    description: str,
    series: str,
    row_name: str | None,
    vary_target: str | None,
    vary_distribution: str | None,
    vary_low: str | None,
    vary_high: str | None,
    vary_median: str | None,
    vary_sd: str | None,
    vary_skew: str | None,
    vary_tailweight: str | None,
    vary_mode: str | None,
    vary_outcomes: str | None,
    vary_correlate_with: str | None,
    vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
) -> None:
    """Append a one-time transfer (same as ``sim transfer add``)."""
    _do_add_transfer(
        date=date, from_account=from_account, to_account=to_account, amount=amount,
        from_lot=from_lot, to_lot=to_lot, scenario=scenario, data_dir=data_dir,
        tax_character=tax_character, description=description, series=series,
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
        vary_outcomes=vary_outcomes,
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence,
        vary_ramp_months=vary_ramp_months,
    )


@calendar_add_group_command.command("transfer-balance")
@click.argument("date", required=False, type=completion.CALENDAR_DATE)
@click.argument("from_account", required=False, type=completion.ENDPOINT)
@click.argument("to_account", required=False, type=completion.ENDPOINT)
@click.option("--from-lot", type=completion.LotName("from_account"), default=None,
              help="Lot of the from account; asked for when a blank one would not say which.")
@click.option("--to-lot", type=completion.LotName("to_account"), default=None,
              help="Lot of the to account; asked for when a blank one would not say which.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--tax-character", default="",
              help="ADR-003 tax_character; left out, it follows from the "
                   "accounts, so a rollover is untaxed (ADR-003 §3.4).")
@click.option("--description", default="", help="Free-text description.")
@click.option("--tags", default="",
              help="Space-separated '<role>:<name>' tokens (e.g. 'expense:discretionary').")
@_vary_row_options
def calendar_add_transfer_balance(
    date: str | None, from_account: str | None, to_account: str | None,
    from_lot: str | None, to_lot: str | None,
    scenario: str | None, data_dir: Path | None, tax_character: str,
    description: str, tags: str,
    row_name: str | None, vary_target: str | None, vary_distribution: str | None,
    vary_low: str | None, vary_high: str | None, vary_median: str | None, vary_sd: str | None,
    vary_skew: str | None, vary_tailweight: str | None, vary_mode: str | None,
    vary_outcomes: str | None,
    vary_correlate_with: str | None, vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
) -> None:
    """Append a transfer_balance event (#134): on DATE, move the whole balance
    of one lot of FROM_ACCOUNT, an asset account, into TO_ACCOUNT -- a
    rollover, say. The amount is whatever the lot holds at the start of
    DATE's month.

    Only --vary-target=date is meaningful here (there is no amount to vary).
    Arguments left out are asked for.
    """
    if vary_target == "amount":
        raise click.ClickException(
            "transfer_balance has no amount to vary; use --vary-target date"
        )
    try:
        registries.parse_tags(tags)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    if tax_character and tax_character not in registries.TAX_CHARACTERS:
        raise click.ClickException(
            f"unknown tax_character: {tax_character!r}; "
            f"allowed: {sorted(registries.TAX_CHARACTERS)}"
        )
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    with _intent_db(data_dir_p, scenario) as (con, _name):
        endpoints = _endpoints(con)
        accounts, lot_info, chains, roles = endpoints
        assets = [r[0] for r in con.execute(
            "SELECT name FROM account WHERE kind = 'asset' ORDER BY name"
        )]

    if not date:
        date = _prompt_with_completion(_CALENDAR_DATE_PROMPT, names.date_marker_names(data_dir_p, scenario_name))
    date = _validate_calendar_date(date)
    # The source is one lot of an asset: "the balance" means one real balance.
    if not from_account:
        _echo_endpoints(assets, lot_info, [])
    from_account, from_lot = _pick_endpoint(
        "from", from_account or None, from_lot,
        accounts=assets, lot_info=lot_info, chains=[], allow_chain=False,
    )
    if not to_account:
        _echo_endpoints(accounts, lot_info, endpoints.chains_for("destination"))
    to_account, to_lot = _pick_endpoint(
        "to", to_account or None, to_lot, accounts=accounts, lot_info=lot_info,
        chains=endpoints.chains_for("destination"), roles=roles, role="destination",
    )

    vary_fields = _vary_row_fields(
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
        vary_outcomes=vary_outcomes,
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence,
        vary_ramp_months=vary_ramp_months, amount_unit=None,
    )
    row = {
        "date": date, "kind": "transfer_balance",
        "account_1": from_account, "lot_1": from_lot,
        "account_2": to_account, "lot_2": to_lot,
        "tax_character": tax_character, "description": description, "tags": tags,
        **vary_fields,
    }
    path = scenario_dir / "calendar.csv"
    verb = _write_calendar_row(path, row, data_dir=data_dir_p, scenario_name=scenario_name)
    click.echo(
        f"{verb} transfer_balance {from_account} -> {to_account} on {date} to {path}"
    )


@calendar_add_group_command.command("swap-aux-table")
@click.argument("date", required=False, type=completion.CALENDAR_DATE)
@click.argument("aux_kind", type=completion.AUX_KIND, required=False)
@click.argument("source_series", type=completion.SeriesName(kind_param="aux_kind"), required=False)
@click.option("--series", type=completion.SeriesName(kind_param="aux_kind"), default="",
              help="The series replaced; blank for a kind without one (federal_brackets).")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default="", help="Free-text description.")
@click.option("--tags", default="",
              help="Space-separated '<role>:<name>' tokens (e.g. 'expense:discretionary').")
@_vary_row_options
def calendar_add_swap_aux_table(
    date: str | None, aux_kind: str | None, source_series: str | None, series: str,
    scenario: str | None, data_dir: Path | None, description: str, tags: str,
    row_name: str | None, vary_target: str | None, vary_distribution: str | None,
    vary_low: str | None, vary_high: str | None, vary_median: str | None, vary_sd: str | None,
    vary_skew: str | None, vary_tailweight: str | None, vary_mode: str | None,
    vary_outcomes: str | None,
    vary_correlate_with: str | None, vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
) -> None:
    """Append a swap_aux_table event (#197): from DATE, the AUX_KIND table
    --series (blank for a kind without one) is SOURCE_SERIES's table instead
    -- a move to another state is `swap-aux-table DATE state_brackets WA
    --series CA`.

    Only --vary-target=date is meaningful here (there is no amount to vary).
    Arguments left out are asked for.
    """
    if vary_target == "amount":
        raise click.ClickException(
            "swap_aux_table has no amount to vary; use --vary-target date"
        )
    try:
        registries.parse_tags(tags)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    given = _ask_missing_arguments(
        "swap-aux-table", data_dir, scenario,
        date=date, aux_kind=aux_kind, source_series=source_series, series=series,
    )
    date, aux_kind, source_series = given["date"], given["aux_kind"], given["source_series"]
    date = _validate_calendar_date(date)
    if aux_kind not in registries.SWAPPABLE_AUX_KINDS:
        raise click.ClickException(
            f"aux kind {aux_kind!r} can't be swapped; allowed: "
            f"{sorted(registries.SWAPPABLE_AUX_KINDS)}"
        )
    vary_fields = _vary_row_fields(
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
        vary_outcomes=vary_outcomes,
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence,
        vary_ramp_months=vary_ramp_months, amount_unit=None,
    )
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    row = {
        "date": date, "kind": "swap_aux_table", "series": series,
        "params_json": json.dumps({"aux_kind": aux_kind, "source_series": source_series}),
        "description": description, "tags": tags, **vary_fields,
    }
    path = scenario_dir / "calendar.csv"
    verb = _write_calendar_row(path, row, data_dir=data_dir_p, scenario_name=scenario_name)
    replaced = f"{aux_kind}__{series}" if series else aux_kind
    click.echo(
        f"{verb} swap_aux_table {replaced} -> {aux_kind}__{source_series} on {date} to {path}"
    )


# ---------- scheduled ----------


@sim_group_command.group(name="scheduled")
def scheduled_group_command() -> None:
    """Inspect or modify recurring scheduled transactions."""


def _check_json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise click.ClickException("must be a JSON object, {...}")
    return value


def _check_month_of_year(text: str) -> int:
    if not text.strip().isdigit() or not 1 <= int(text) <= 12:
        raise click.ClickException(f"month of the year must be 1 to 12: {text!r}")
    return int(text)


def _check_tax_year(text: str) -> int:
    if len(text.strip()) != 4 or not text.strip().isdigit():
        raise click.ClickException(f"tax year must be a four-digit year: {text!r}")
    return int(text)


def _check_integer(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise click.ClickException(f"not a whole number: {text!r}") from None


_SCHEDULED_CHECKERS: dict[str, Callable[[str], object]] = {
    "date": _validate_month_date,
    "dollars": _parse_amount_cents,
    "percent": _percent_to_rate_micros,
    "integer": _check_integer,
    "month": _check_month_of_year,
    "year": _check_tax_year,
    "json": _check_json_object,
    "tags": registries.parse_tags,
}


def _scheduled_guide() -> calendar_guide.Guide:
    return calendar_guide.Guide(
        ask=lambda text, choices, default: _prompt_with_completion(
            text, choices=choices, default=default,
        ),
        checkers=_SCHEDULED_CHECKERS,
    )


def _scheduled_context(data_dir: Path, scenario: str) -> calendar_guide.GuideContext:
    """The scenario's accounts, chains, people and series, as completions
    for a scheduled rule's prompts, and the rule names already taken."""
    def column(con: sqlite3.Connection, sql: str) -> list[str]:
        return sorted({r[0] for r in con.execute(sql) if r[0]})

    with _intent_db(data_dir, scenario) as (con, _name):
        return calendar_guide.GuideContext(
            accounts=column(con, "SELECT name FROM account"),
            subkinds=dict(con.execute("SELECT name, subkind FROM account")),
            lots=_lot_info(con),
            chains=_interior_chains(con), chain_roles=_chain_roles(con),
            persons=column(con, "SELECT id FROM person"),
            series={
                "rate": sorted({
                    *column(con, "SELECT series FROM aux_table WHERE kind = 'rate'"),
                    *column(con, "SELECT series FROM account_lot"),
                }),
                "inflation": column(con, "SELECT series FROM aux_table WHERE kind = 'inflation'"),
            },
            taken_names=frozenset(column(con, "SELECT name FROM scheduled_transaction")),
        )


def _scheduled_row(
    answers: dict[str, object], base: dict[str, str],
    context: calendar_guide.GuideContext | None = None,
) -> dict[str, str]:
    """The scheduled.csv row for a rule's ``answers``, over ``base``: the
    row being edited, or a new row's name and meta_json.

    When editing (``context`` given), a field the guide doesn't ask about
    keeps ``base``'s cell -- the rule's kind may have nothing to say about
    it -- unless the kind changed and the new kind doesn't read that column,
    or it is a lot, which follows its account. An unchanged cell is kept as
    authored; money and rates are written in the CSV's units."""
    kind = str(answers.get("amount_rule_kind") or "fixed_amount").strip()
    if kind not in registries.AMOUNT_RULE_KINDS:
        raise click.ClickException(
            f"unknown rule kind {kind!r}; allowed: {sorted(registries.AMOUNT_RULE_KINDS)}"
        )
    kind_changed = kind != base.get("amount_rule_kind", "").strip()
    text = {column: str(answers.get(column, "") or "").strip() for column in scheduled_guide.RULE_COLUMNS}
    row = {column: base.get(column, "") for column in registries.SCHEDULED_CSV_HEADER}
    for field_ in scheduled_guide.RULE.fields:
        column = field_.param
        value = text[column]
        unread = (column in scheduled_guide.KIND_ONLY_COLUMNS
                  and column not in scheduled_guide.KIND_COLUMNS[kind])
        if unread and (context is None or kind_changed):
            value = ""
        elif context is not None and not field_.applies(context, text):
            # Not asked, so not changed -- except a lot whose account
            # changed: the account's own lot, if any, was asked instead.
            account = column.removesuffix("_lot") + "_account"
            if column not in ("from_lot", "to_lot") or text[account] == base.get(account, "").strip():
                continue
        if value == base.get(column, "").strip():
            row[column] = base.get(column, "")
            continue
        if value and column in ("amount", "ytd_amount"):
            cents = _parse_amount_cents(value)
            sign, cents = ("-" if cents < 0 else ""), abs(cents)
            value = f"{sign}{cents // 100}.{cents % 100:02d}"
        elif value and column == "rate":
            value = units.micros_to_percent(_percent_to_rate_micros(value))
        row[column] = value
    row["amount_rule_kind"] = kind
    if row["cadence"] != "annual":
        row["month_of_year"] = ""
    elif not row["month_of_year"].strip() and len(row["start"]) >= 7:
        row["month_of_year"] = str(int(row["start"][5:7]))
    if row["series"].strip() in ("", "fixed"):
        # Nothing inflates, so nothing reads it; the loader refuses one
        # without a series.
        row["inflation_cadence"] = ""
    elif kind == "fixed_amount" and not row["base_date"].strip():
        row["base_date"] = row["start"]  # the engine needs one to inflate from
    if row["ytd_amount"].strip():
        row["ytd_preroll"] = ""
    row["meta_json"] = row["meta_json"] or "{}"
    return row


def _refuse_unread_options(kind: str, given: Mapping[str, object]) -> None:
    """Refuse an option the rule's kind doesn't read, rather than drop it
    without a word (#267: ``--ytd-preroll 1`` on a mortgage rule vanished)."""
    own = scheduled_guide.KIND_COLUMNS.get(kind, {})
    unread = [
        field_.option or field_.param for field_ in scheduled_guide.RULE.fields
        if field_.param in scheduled_guide.KIND_ONLY_COLUMNS
        and field_.param not in own and str(given.get(field_.param) or "").strip()
    ]
    if unread:
        raise click.ClickException(f"a {kind} rule doesn't read {', '.join(unread)}")


def _check_scheduled_row(row: dict[str, str], context: calendar_guide.GuideContext) -> None:
    """What the guide checks as each answer is typed, for a row built from
    arguments and options instead. The loader judges the rest when the edit
    is staged."""
    kind = row["amount_rule_kind"]
    if kind not in registries.AMOUNT_RULE_KINDS:
        raise click.ClickException(
            f"unknown rule kind {kind!r}; allowed: {sorted(registries.AMOUNT_RULE_KINDS)}"
        )
    missing = [
        column for column, required in scheduled_guide.KIND_COLUMNS[kind].items()
        if required and not row[column].strip()
    ]
    if missing:
        flags = ", ".join(f"--{column.replace('_', '-')}" for column in missing)
        raise click.ClickException(f"rule kind {kind} needs {flags}")
    for column in ("start", "from_account", "to_account", "cadence", "tax_character"):
        if not row[column].strip():
            raise click.ClickException(f"a rule needs {column.replace('_', ' ')}")
    if row["cadence"] not in registries.CADENCES:
        raise click.ClickException(
            f"unknown cadence {row['cadence']!r}; allowed: {sorted(registries.CADENCES)}"
        )
    if kind in scheduled_guide.ANNUAL_KINDS and row["cadence"] != "annual":
        raise click.ClickException(f"a {kind} rule fires once a year: its cadence must be annual")
    if row["cadence"] == "biweekly" and kind != "fixed_amount":
        raise click.ClickException("only a fixed_amount rule may be biweekly")
    if kind != "fixed_amount" and row["to_account"] in context.chains:
        raise click.ClickException(
            f"a {kind} rule's to account must be an account, not chain {row['to_account']!r}"
        )
    if kind in scheduled_guide.ACCOUNT_SOURCE_KINDS and row["from_account"] in context.chains:
        raise click.ClickException(
            f"a {kind} rule's from account must be an account, not chain {row['from_account']!r}"
        )
    if row["cadence"] == "biweekly" and not is_full_date(row["start"]):
        raise click.ClickException(
            f"a biweekly start needs a full YYYY-MM-DD, its first payday: {row['start']!r}"
        )
    if row["tax_character"] not in registries.TAX_CHARACTERS:
        raise click.ClickException(
            f"unknown tax_character: {row['tax_character']!r}; "
            f"allowed: {sorted(registries.TAX_CHARACTERS)}"
        )
    if kind == "on_death" and row["tax_character"] != "tax_free":
        raise click.ClickException("an on_death rule's tax character must be tax_free")
    if row["person"] and row["person"] not in context.persons:
        raise click.ClickException(
            f"unknown person {row['person']!r}; allowed: {list(context.persons)}"
        )
    if row["interest_account"] and row["interest_account"] not in context.accounts:
        raise click.ClickException(f"unknown interest account: {row['interest_account']!r}")
    if row["ytd_amount"] and row["ytd_preroll"] not in ("", "0"):
        raise click.ClickException("--ytd-amount and --ytd-preroll can't both be given")
    if row["ytd_preroll"] not in ("", "0", "1"):
        raise click.ClickException("--ytd-preroll must be 0 or 1")
    if row["inflation_cadence"] not in ("", "monthly", "annual"):
        raise click.ClickException("--inflation-cadence must be monthly or annual")
    endpoints = _Endpoints(
        list(context.accounts), {k: (v[0], list(v[1])) for k, v in context.lots.items()},
        list(context.chains), dict(context.chain_roles),
    )
    for field_ in scheduled_guide.RULE.fields:
        value = row.get(field_.param, "").strip()
        checker = _SCHEDULED_CHECKERS.get(field_.type)
        if value and checker is not None:
            try:
                checker(value)
            except click.ClickException as error:
                raise click.ClickException(f"{field_.name}: {error.format_message()}") from None
            except ValueError as error:  # parse_tags says what is wrong this way
                raise click.ClickException(f"{field_.name}: {error}") from None
    if row["amount"].strip() and _parse_amount_cents(row["amount"]) <= 0:
        raise click.ClickException(f"amount must be positive: {row['amount']!r}")
    if kind == "rmd" and row["from_account"] in context.chains:
        subject = _check_json_object(row["params_json"] or "{}").get("subject_account")
        if not subject:
            raise click.ClickException(
                f"an rmd drawing from chain {row['from_account']!r} needs --params-json "
                '\'{"subject_account": "<the account the RMD is for>"}\''
            )
        if subject not in context.accounts:
            raise click.ClickException(f"unknown subject_account: {subject!r}")
    _pick_endpoints(
        endpoints, row["from_account"], row["from_lot"] or None,
        row["to_account"], row["to_lot"] or None,
    )


def _scheduled_options[F: Callable[..., object]](f: F) -> F:
    """One option per field of ``scheduled_guide.RULE``, so the guided
    questions and the options can't drift apart. Each defaults to ``None``
    (not given); ``""`` clears a field."""
    for field_ in reversed(scheduled_guide.RULE.fields):
        if field_.option is not None:
            f = click.option(
                field_.option, field_.param, default=None, help=field_.help or field_.label,
                type=completion.type_for(field_.param, scheduled=True),
            )(f)
    return f


@scheduled_group_command.command("add")
@click.argument("date", required=False)
@click.argument("from_account", required=False, type=completion.ENDPOINT)
@click.argument("to_account", required=False, type=completion.ENDPOINT)
@click.argument("amount", required=False)
@click.option("--kind", "rule_kind", default="fixed_amount", show_default=True,
              type=click.Choice(sorted(registries.AMOUNT_RULE_KINDS)),
              help="How the amount is worked out (ADR-003 §2). AMOUNT is given only "
                   "for fixed_amount, ss_benefit (PIA at FRA) and mortgage_payment (P&I).")
@click.option("--from-lot", type=completion.LotName("from_account"), default=None,
              help="Lot of the from account; asked for when a blank one would not say which.")
@click.option("--to-lot", type=completion.LotName("to_account"), default=None,
              help="Lot of the to account; asked for when a blank one would not say which.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--name", "row_name", default=None,
              help="scheduled.csv name (default: derived from accounts and date).")
@click.option("--cadence", type=click.Choice(sorted(registries.CADENCES)), default=None,
              help="Default: monthly; annual for rmd and tax_accumulator.")
@click.option("--month-of-year", type=click.IntRange(1, 12), default=None,
              help="For annual cadence; defaults to 12 for rmd and tax_accumulator, "
                   "else the start month.")
@click.option("--end", "end_date", default="",
              help="Optional end date (YYYY-MM or YYYY-MM-DD).")
@click.option("--tax-character", default=None,
              help="Default: the kind's usual one (none; ss_benefit, tax_deferred_out for rmd, "
                   "tax_free for on_death).")
@click.option("--series", type=completion.SCHEDULED_SERIES, default=None,
              help="fixed_amount: 'fixed' (the default) for no inflation, else an inflation "
                   "series (e.g. cpi). ss_benefit: the COLA series. percent_of_balance: the rate series.")
@click.option("--base-date", default=None,
              help="The date inflation is measured from (default: DATE).")
@click.option("--inflation-cadence", type=click.Choice(["monthly", "annual"]), default=None,
              help="annual steps once each January, as a COLA does (default: monthly).")
@click.option("--person", type=completion.PERSON, default=None,
              help="Person id: required for rmd, ss_benefit and on_death.")
@click.option("--claim-age", type=int, default=None, help="Required for ss_benefit.")
@click.option("--rate", default=None,
              help="mortgage_payment: annual rate, e.g. 6%. fill_bracket: the bracket to fill, e.g. 22%.")
@click.option("--interest-account", type=completion.ACCOUNT, default=None,
              help="Interest expense account: required for mortgage_payment.")
@click.option("--tax-year", default=None,
              help="The tax year the transfer counts toward, when not its own.")
@click.option("--ytd-amount", default=None,
              help="Amount already paid this year before the scenario starts (ADR-003 §4.10).")
@click.option("--ytd-preroll", type=click.Choice(["0", "1"]), default=None,
              help="1 posts each month before the start as already paid "
                   "(fixed_amount and mortgage_payment only).")
@click.option("--priority", type=int, default=None,
              help="Default: the kind's band (ADR-003 §5): 200 for fixed_amount.")
@click.option("--description", default="")
@click.option("--params-json", default=None,
              help="JSON object: required for tax_accumulator; subject_account for an rmd from a chain.")
@click.option("--tags", default="",
              help="Space-separated '<role>:<name>' tokens (e.g. 'expense:discretionary household:rwk').")
def scheduled_add(
    date: str | None,
    from_account: str | None,
    to_account: str | None,
    amount: str | None,
    rule_kind: str,
    from_lot: str | None,
    to_lot: str | None,
    scenario: str | None,
    data_dir: Path | None,
    row_name: str | None,
    cadence: str | None,
    month_of_year: int | None,
    end_date: str,
    tax_character: str | None,
    series: str | None,
    base_date: str | None,
    inflation_cadence: str | None,
    person: str | None,
    claim_age: int | None,
    rate: str | None,
    interest_account: str | None,
    tax_year: str | None,
    ytd_amount: str | None,
    ytd_preroll: str | None,
    priority: int | None,
    description: str,
    params_json: str | None,
    tags: str,
) -> None:
    """Append a scheduled rule to <scenario>/scheduled.csv.

    The rule is a fixed amount unless --kind names another kind (ADR-003
    §2): each kind takes its own columns as options, and those it requires
    are asked for when left out. `sim scheduled edit` changes a rule later.
    """
    try:
        registries.parse_tags(tags)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    context = _scheduled_context(data_dir_p, scenario_name)
    endpoints = _Endpoints(
        list(context.accounts), {k: (v[0], list(v[1])) for k, v in context.lots.items()},
        list(context.chains), dict(context.chain_roles),
    )
    own_columns = scheduled_guide.KIND_COLUMNS[rule_kind]
    if amount is not None and "amount" not in own_columns:
        raise click.ClickException(f"rule kind {rule_kind} takes no AMOUNT")

    if not date:
        date = _prompt_with_completion("start date (YYYY-MM or YYYY-MM-DD)")
    date = _validate_month_date(date)
    if end_date:
        end_date = _validate_month_date(end_date)

    from_account, from_lot, to_account, to_lot = _pick_endpoints(
        endpoints, from_account or None, from_lot, to_account or None, to_lot,
    )

    if rule_kind == "fill_bracket":
        if priority is not None:
            raise click.ClickException(
                "a fill_bracket rule takes no --priority: it runs last in its month")
        if (rate is None) == (amount is None):
            raise click.ClickException(
                "a fill_bracket rule takes one of --rate (the bracket to fill, e.g. 22%) "
                "or --amount (the taxable income to fill up to, in today's dollars)")
    if amount is None and own_columns.get("amount"):
        amount = _prompt_with_completion(f"{scheduled_guide.AMOUNT_LABEL[rule_kind]} (e.g. 1234.56)")
    if amount is not None and _parse_amount_cents(amount) <= 0:
        raise click.ClickException("amount must be positive")
    if cadence is None:
        cadence = "annual" if rule_kind in scheduled_guide.ANNUAL_KINDS else "monthly"
    if cadence == "annual" and month_of_year is None and rule_kind in scheduled_guide.ANNUAL_KINDS:
        month_of_year = int(scheduled_guide.ANNUAL_KINDS[rule_kind])

    answers: dict[str, object] = {
        "amount_rule_kind": rule_kind, "start": date,
        "from_account": from_account, "from_lot": from_lot,
        "to_account": to_account, "to_lot": to_lot,
        "amount": amount or "", "cadence": cadence,
        "month_of_year": str(month_of_year) if month_of_year else "",
        "person": person or "", "claim_age": "" if claim_age is None else str(claim_age),
        "series": series if series is not None else ("fixed" if rule_kind == "fixed_amount" else ""),
        "base_date": base_date or "", "inflation_cadence": inflation_cadence or "",
        "rate": rate or "", "interest_account": interest_account or "",
        "tax_character": tax_character or scheduled_guide.tax_character_for(
            context, {"amount_rule_kind": rule_kind, "from_account": from_account}),
        "tax_year": tax_year or "", "end": end_date,
        "priority": str(priority) if priority is not None else str(
            registries.DEFAULT_PRIORITIES.get(rule_kind, "")),
        "ytd_amount": ytd_amount or "", "ytd_preroll": ytd_preroll or "",
        "description": description, "params_json": params_json or "", "tags": tags,
    }
    _refuse_unread_options(rule_kind, answers)
    if rule_kind in ("fixed_amount", "fill_bracket") and not answers["base_date"]:
        answers["base_date"] = date
    # Ask for what this kind requires and wasn't given, as the guide would.
    guide = _scheduled_guide()
    for field_ in scheduled_guide.RULE.fields:
        if (field_.param in own_columns and own_columns[field_.param]
                and not answers.get(field_.param)):
            answers[field_.param] = guide.ask_field(
                scheduled_guide.RULE, field_, context, {k: str(v) for k, v in answers.items()},
            )

    if not row_name:
        slug_from = "_".join(filter(None, (from_account, from_lot))).lower().replace(" ", "_")
        slug_to = "_".join(filter(None, (to_account, to_lot))).lower().replace(" ", "_")
        prefix = "xfer" if rule_kind == "fixed_amount" else rule_kind
        row_name = f"{prefix}_{slug_from}_to_{slug_to}_{date}"
    if row_name in context.taken_names:
        raise click.ClickException(
            f"scheduled transaction already exists: {row_name!r}"
        )

    row = _scheduled_row(answers, {"name": row_name, "meta_json": "{}"})
    _check_scheduled_row(row, context)
    path = scenario_dir / "scheduled.csv"
    _append_and_validate(path, registries.SCHEDULED_CSV_HEADER, row,
                         data_dir=data_dir_p, scenario_name=scenario_name)
    what = f" {_fmt_cents(_parse_amount_cents(row['amount'])).strip()}" if row["amount"] else ""
    kind_note = "" if rule_kind == "fixed_amount" else f" {rule_kind}"
    click.echo(
        f"appended scheduled row '{row_name}'{kind_note} ({row['cadence']}) "
        f"{from_account} -> {to_account}{what} to {path}"
    )


@scheduled_group_command.command("import-baseline")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--quicken-file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="Quicken export (default: sibling data-repo/quicken/Transaction Export.csv).")
@click.option("--amazon-state-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="Amazon state directory (default: sibling data-repo/state).")
@click.option("--category-map", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="Category map (default: sibling data-repo/quicken/expense_category_map.csv).")
@click.option("--category-split", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="Optional splits of a category by paying Quicken account "
                   "(default: sibling data-repo/quicken/expense_category_split.csv).")
@click.option("--excluded-payees", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="Optional payee patterns modeled separately in the scenario "
                   "(default: sibling data-repo/quicken/expense_excluded_payees.csv).")
@click.option("--account-map", type=click.Path(dir_okay=False, path_type=Path),
              default=None,
              help="Optional category account map (default: sibling data-repo/quicken/expense_account_map.csv).")
@click.option("--from-account", type=completion.ENDPOINT, required=True,
              help="Scenario account that funds all generated expense rows.")
@click.option("--from-lot", type=completion.LotName("from_account"), default="",
              help="Lot of --from-account (required for investment accounts).")
@click.option("--start", default=None,
              help="First scheduled month YYYY-MM or YYYY-MM-DD (default: scenario start).")
@click.option("--series", type=completion.SCHEDULED_SERIES, default="cpi", show_default=True,
              help="Inflation series applied to generated rows ('fixed' for none).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show generated rows without rewriting scheduled.csv.")
def scheduled_import_baseline(
    scenario: str | None,
    data_dir: Path | None,
    quicken_file: Path | None,
    amazon_state_dir: Path | None,
    category_map: Path | None,
    category_split: Path | None,
    excluded_payees: Path | None,
    account_map: Path | None,
    from_account: str,
    from_lot: str,
    start: str | None,
    series: str,
    dry_run: bool,
) -> None:
    """Replace generated baseline expense rows in scheduled.csv."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    data_repo_root = data_dir_p.parent
    quicken_path = quicken_file or data_repo_root / "quicken" / "Transaction Export.csv"
    amazon_state_path = amazon_state_dir or data_repo_root / "state"
    category_map_path = category_map or data_repo_root / "quicken" / "expense_category_map.csv"
    account_map_path = account_map or data_repo_root / "quicken" / "expense_account_map.csv"
    category_split_path = (
        category_split or data_repo_root / "quicken" / "expense_category_split.csv"
    )
    excluded_payees_path = (
        excluded_payees or data_repo_root / "quicken" / "expense_excluded_payees.csv"
    )

    for path, label in (
        (quicken_path, "Quicken export"),
        (category_map_path, "category map"),
    ):
        if not path.exists():
            raise click.ClickException(f"{label} not found: {path}")

    if start is None:
        scenario_path = scenario_dir / "scenario.csv"
        with scenario_path.open(newline="", encoding="utf-8-sig") as handle:
            start = next(_csv.DictReader(handle))["start"]
    start = _validate_month_date(start)

    merges, exclusions = load_category_map(category_map_path)
    baseline = build_expense_baseline(
        quicken_path,
        load_monthly_summaries(amazon_state_path),
        category_merges=merges,
        excluded_categories=exclusions,
        default_category="Other",
        category_splits=(
            load_category_splits(category_split_path) if category_split_path.exists() else None
        ),
        excluded_payee_patterns=(
            load_excluded_payees(excluded_payees_path) if excluded_payees_path.exists() else ()
        ),
    )
    mappings = (
        load_expense_account_map(account_map_path)
        if account_map_path.exists()
        else {}
    )
    with _intent_db(data_dir_p, scenario_name) as (con, _):
        scenario_accounts = {row[0] for row in con.execute("SELECT name FROM account")}
        scenario_sources = scenario_accounts | {
            row[0] for row in con.execute("SELECT name FROM chain")
        }
    if from_account not in scenario_sources:
        raise click.ClickException(f"unknown from account or chain: {from_account!r}")
    baseline_categories = set(baseline.category_totals["category"])
    mappings = {
        category: target for category, target in mappings.items()
        if category in baseline_categories
    }
    invalid_targets = {
        category: target for category, target in mappings.items()
        if target not in scenario_accounts
    }
    for category, target in sorted(invalid_targets.items()):
        click.echo(
            f"warning: ignoring category {category!r}; "
            f"target account {target!r} does not exist",
            err=True,
        )
    mappings = {
        category: target for category, target in mappings.items()
        if target in scenario_accounts
    }
    mappings.update({
        category: category for category in baseline.category_totals["category"]
        if category in scenario_accounts and category not in mappings
    })
    missing_mappings = {
        category for category in baseline_categories
        if category not in mappings and category not in invalid_targets
    }
    for category in sorted(missing_mappings):
        click.echo(
            f"warning: ignoring category {category!r}; "
            "no target expense account mapping exists",
            err=True,
        )
    rows = build_scheduled_baseline_rows(
        baseline,
        mappings,
        start,
        from_account,
        ignored_categories=set(invalid_targets) | missing_mappings,
        from_lot=from_lot,
        series=series,
    )
    scheduled_path = scenario_dir / "scheduled.csv"
    if dry_run:
        click.echo(f"(dry run) would replace {len(rows)} generated row(s) in {scheduled_path}")
        for row in rows:
            click.echo(f"  {row['name']}: {row['amount']} -> {row['to_account']}")
        return
    with _scenario_edit(data_dir_p, scenario_name) as edit:
        replace_generated_scheduled_rows(edit.path(scheduled_path), rows)
    click.echo(
        f"replaced {len(rows)} generated baseline row(s) in {scheduled_path} "
        f"for scenario {scenario_name}"
    )


@scheduled_group_command.command("list")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def scheduled_list(scenario: str | None, data_dir: Path | None) -> None:
    """List recurring scheduled transactions."""
    from rich.console import Console
    from rich.table import Table

    from analysis.sim.notebook_ui.scenario_view import scheduled_view

    data_dir_p = _resolve_data_dir(data_dir)
    with open_scenario(data_dir_p, _resolve_scenario(data_dir_p, scenario).name) as sc:
        scen_name = sc.name
        rows = scheduled_view(sc)
    table = Table(title=f"scheduled ({data_dir_p.name} / {scen_name})")
    for column, justify in (
        ("Name", "left"), ("Cadence", "left"), ("From", "left"), ("Lot", "left"),
        ("To", "left"), ("Lot", "left"), ("Amount", "right"), ("Start", "left"),
        ("End", "left"),
    ):
        table.add_column(column, justify=justify)
    for row in named_rows(rows):
        if row.amount_rule_kind == "fixed_amount" and pd.notna(row.amount):
            amt = _fmt_cents(round(row.amount * 100)).strip()
        elif row.amount_rule_kind == "percent_of_balance":
            amt = "% of bal"
        else:
            amt = row.amount_rule_kind
        table.add_row(
            row.name, row.cadence, row.from_account, row.from_lot,
            row.to_account, row.to_lot, amt, row.start, "" if row.end == "-" else row.end,
        )
    Console().print(table)


@scheduled_group_command.group(name="tag")
def scheduled_tag_group() -> None:
    """Add or remove tags on a scheduled transaction."""


@scheduled_tag_group.command("add")
@click.argument("name", required=False, type=completion.RULE)
@click.argument("tag_tokens", type=completion.ANY_TAG, nargs=-1, required=False)
@click.option("--scenario", type=completion.SCENARIO, default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def scheduled_tag_add(
    name: str | None,
    tag_tokens: tuple[str, ...],
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Add one or more '<role>:<name>' tags to a scheduled transaction.

    Without NAME or TAG_TOKENS, they are asked for.
    """
    _scenario_write(data_dir, scenario, "scheduled", name, tag_tokens, "add")


@scheduled_tag_group.command("rm")
@click.argument("name", required=False, type=completion.RULE)
@click.argument("tag_tokens", type=completion.TAG, nargs=-1, required=False)
@click.option("--scenario", type=completion.SCENARIO, default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def scheduled_tag_rm(
    name: str | None,
    tag_tokens: tuple[str, ...],
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Remove one or more '<role>:<name>' tags from a scheduled transaction.

    Without NAME or TAG_TOKENS, they are asked for.
    """
    _scenario_write(data_dir, scenario, "scheduled", name, tag_tokens, "rm")


def _read_scheduled_rows(path: Path) -> list[dict[str, str]]:
    """A scenario's own scheduled.csv rows, every SCHEDULED_CSV_HEADER column present."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [
        {column: str(row.get(column, "")) for column in registries.SCHEDULED_CSV_HEADER}
        for row in frame.to_dict("records")
    ]


class _PickedRule(NamedTuple):
    found: _Overlaid  # the rule as the load order leaves it
    own: dict[str, str] | None  # its row in this scenario's own file, as read then


def _pick_scheduled(scenario_dir: Path, scenario_name: str, name: str) -> _PickedRule:
    """The rule ``name``, as the scenario's load order leaves it."""
    found = _overlay_rows(scenario_dir, "scheduled.csv").get(name.strip())
    if found is None:
        raise click.ClickException(
            f"no scheduled rule {name!r} in scenario {scenario_name!r}'s scheduled.csv "
            "(an account's growth rule comes from accounts.csv; a calendar row's "
            "transfer from calendar.csv)"
        )
    own = None
    if found.index is not None:
        own = _read_scheduled_rows(scenario_dir / "scheduled.csv")[found.index]
    return _PickedRule(found, own)


def _write_scheduled(
    data_dir: Path, scenario_name: str, path: Path, picked: _PickedRule,
    row: dict[str, str] | None, *, drop_calendar: Path | None = None, name: str = "",
) -> None:
    """Write ``row`` over the scenario's own row ``chosen`` -- or append it as
    an override when ``chosen`` is inherited; ``None`` removes the own row --
    as one staged edit. With ``drop_calendar``, that calendar.csv's rows
    naming rule ``name`` go too."""
    index = picked.found.index
    with _scenario_edit(data_dir, scenario_name) as edit:
        staged = edit.path(path)
        if index is None:
            if row is not None:
                _append_csv_row(staged, registries.SCHEDULED_CSV_HEADER, row)
        else:
            # Read inside the staged edit: the prompts may have taken a while,
            # and the index is only good if the file still has the rule there.
            existing = _read_scheduled_rows(staged)
            if index >= len(existing) or existing[index] != picked.own:
                raise click.ClickException(
                    f"{path} changed while this rule was being edited. Nothing was "
                    "written; start again."
                )
            if row is None:
                frame = pd.read_csv(staged, dtype=str, keep_default_na=False)
                frame.drop(index=frame.index[index]).to_csv(staged, index=False)
            else:
                _append_csv_row(staged, registries.SCHEDULED_CSV_HEADER, row, replace=index)
        if drop_calendar is not None:
            calendar = edit.path(drop_calendar)
            frame = pd.read_csv(calendar, dtype=str, keep_default_na=False)
            keep = frame["scheduled_name"].str.strip() != name
            frame.loc[keep].to_csv(calendar, index=False)


@scheduled_group_command.command("edit")
@click.argument("name", required=False, type=completion.RULE)
@_scheduled_options
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.pass_context
def scheduled_edit(
    ctx: click.Context, name: str | None, scenario: str | None, data_dir: Path | None,
    **values: str | None,
) -> None:
    """Change a scheduled rule.

    Options set those fields and write at once ("" clears one). With none,
    the rule's fields are shown -- only those its kind reads -- name one to
    change it, as often as needed, and write when done. Changing the kind
    asks for the new kind's fields and drops those it doesn't read.

    A rule the scenario inherits is changed by an override: the whole rule,
    edited, appended to this scenario's scheduled.csv; the parent keeps its
    own. `sim scheduled rename` changes a rule's name, everywhere it is shared.

    Without NAME, it is asked for.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "scheduled.csv"
    if name is None:
        name = _ask_name("scheduled rule", names.scheduled_names(data_dir_p, scenario_name),
                         prompt="rule to edit")
    picked = _pick_scheduled(scenario_dir, scenario_name, name)
    chosen = picked.found
    current = {column: chosen.row.get(column, "") for column in registries.SCHEDULED_CSV_HEADER}
    context = _scheduled_context(data_dir_p, scenario_name)
    rule = current["name"]

    def submit(params: dict[str, object]) -> None:
        row = _scheduled_row(params, current, context)
        _check_scheduled_row(row, context)
        if all(row[column].strip() == current[column].strip()
               for column in registries.SCHEDULED_CSV_HEADER):
            click.echo("no change; nothing was written")
            return
        _write_scheduled(data_dir_p, scenario_name, path, picked, row)
        if chosen.index is None:
            click.echo(
                f"wrote an override of scheduled rule '{rule}' "
                f"(inherited from {chosen.inherited_from!r}) to {path}"
            )
        else:
            click.echo(f"updated scheduled rule '{rule}' in {path}")

    answers = scheduled_guide.answers_of(current)
    given = {param: value.strip() for param, value in values.items() if value is not None}
    if given:
        _refuse_unread_options(
            given.get("amount_rule_kind") or current["amount_rule_kind"].strip(), given,
        )
        submit({**answers, **given})
        return
    _echo_guided_scenario(scenario_name, path, defaulted=scenario is None)
    click.echo(f"Editing scheduled rule '{rule}' ({current['amount_rule_kind']})")
    if chosen.index is None:
        click.echo(
            f"  It comes from scenario {chosen.inherited_from!r}: writing puts an override "
            "of the whole rule in this scenario."
        )
    guide = _scheduled_guide()
    guide.complete(scheduled_guide.RULE, context, answers)
    if not guide.review(scheduled_guide.RULE, context, answers, submit, show_command=False):
        ctx.exit(1)


def _month_before(iso: str) -> str:
    """The ``YYYY-MM`` before the month of ISO date ``iso``."""
    year, month = int(iso[:4]), int(iso[5:7])
    year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return f"{year:04d}-{month:02d}"


@scheduled_group_command.command("delete")
@click.argument("name", required=False, type=completion.RULE)
@click.option("--yes", is_flag=True, help="Don't ask first.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def scheduled_delete(name: str | None, yes: bool, scenario: str | None, data_dir: Path | None) -> None:
    """Delete a scheduled rule.

    A rule of this scenario's own is removed, with this scenario's calendar
    rows that name it (set_amount, retire_scheduled), which are listed
    first. Deleting this scenario's override of an inherited rule brings
    back the inherited rule.

    A rule the scenario inherits can't be removed from here, since an
    overlay can't remove a row (ADR-003 §4.5). Instead it is ended: an
    override whose end is the month before the scenario starts, so it never
    fires, and without a year-to-date catch-up.

    Without NAME, it is asked for.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "scheduled.csv"
    if name is None:
        name = _ask_name("scheduled rule", names.scheduled_names(data_dir_p, scenario_name),
                         prompt="rule to delete")
    picked = _pick_scheduled(scenario_dir, scenario_name, name)
    chosen = picked.found
    rule = chosen.row["name"]

    if chosen.index is not None and chosen.inherited_from is not None:
        if not yes and not click.confirm(
            f"delete this scenario's override of '{rule}', so the rule from "
            f"{chosen.inherited_from!r} applies again?", default=False,
        ):
            click.echo("aborted")
            return
        _write_scheduled(data_dir_p, scenario_name, path, picked, None)
        click.echo(
            f"deleted this scenario's override of scheduled rule '{rule}' from {path}; "
            f"it is inherited from {chosen.inherited_from!r} again"
        )
        return

    if chosen.index is not None:
        calendar = scenario_dir / "calendar.csv"
        references = [
            f"  calendar.csv row {index + 2}: {_calendar_row_label(row)}"
            for index, row in enumerate(_read_calendar_rows(calendar))
            if row["scheduled_name"].strip() == rule
        ]
        if references:
            click.echo(f"scheduled rule '{rule}' is named by:")
            click.echo("\n".join(references))
        prompt = "delete the rule and these rows?" if references else f"delete scheduled rule '{rule}'?"
        if not yes and not click.confirm(prompt, default=False):
            click.echo("aborted")
            return
        _write_scheduled(
            data_dir_p, scenario_name, path, picked, None,
            drop_calendar=calendar if references else None, name=rule,
        )
        click.echo(f"deleted scheduled rule '{rule}' from {path}")
        return

    with _intent_db(data_dir_p, scenario_name) as (con, _name):
        start = con.execute("SELECT start FROM scenario").fetchone()[0]
    end = _month_before(start)
    current = {column: chosen.row.get(column, "") for column in registries.SCHEDULED_CSV_HEADER}
    if current["end"] and month_end_iso(current["end"]) < start:
        click.echo(f"scheduled rule '{rule}' already ends before the scenario starts; nothing was written")
        return
    click.echo(
        f"scheduled rule '{rule}' comes from scenario {chosen.inherited_from!r}, and "
        f"{scenario_name!r} can't remove what it inherits. It can end it instead: an "
        f"override with end {end}, the month before the scenario starts, so it never fires."
    )
    if not yes and not click.confirm("write that override?", default=False):
        click.echo("aborted")
        return
    row = {**current, "end": end, "ytd_amount": "", "ytd_preroll": ""}
    _write_scheduled(data_dir_p, scenario_name, path, picked, row)
    click.echo(f"wrote an override ending scheduled rule '{rule}' in {end} to {path}")


def _ask_growth_character(account: str, subkind: str) -> str:
    """Ask how ``account``'s growth is taxed (#233): without a tax character
    the loader refuses a lot that grows."""
    choices = sorted(registries.GROWTH_COUNTERPARTY_ACCOUNTS)
    click.echo(f"{account!r} will grow, so its growth needs a tax character: {', '.join(choices)}")
    while (answer := _prompt_with_completion(
            "tax character", choices=choices, default=lot_guide.GROWTH_CHARACTER.get(subkind, "gains"),
    ).strip()) not in choices:
        click.echo(f"  not a tax character: {answer!r}")
    return answer


def _complete_kind(ctx, param, incomplete):  # noqa: ARG001
    return [k for k in sorted(registries.ACCOUNT_SUBKINDS) if k.startswith(incomplete)]


def _complete_subkind(ctx, param, incomplete):  # noqa: ARG001
    all_subs = sorted({s for v in registries.ACCOUNT_SUBKINDS.values() for s in v})
    return [s for s in all_subs if s.startswith(incomplete)]


@sim_group_command.group(name="account")
def account_group_command() -> None:
    """Inspect or modify accounts in the data repo."""


@account_group_command.command("add")
@click.argument("name", required=False)
@click.argument("kind", required=False, shell_complete=_complete_kind)
@click.argument("subkind", required=False, shell_complete=_complete_subkind)
@click.option("--owner", type=completion.OWNER, default=None,
              help="Person id from persons.csv (default: joint).")
@click.option("--tax-treatment", default=None,
              type=click.Choice(sorted(registries.TAX_TREATMENTS)),
              help="Default inferred from kind.")
@click.option("--series", type=completion.SeriesName("rate"), default="",
              help="Aux rate series; gives an asset account monthly growth.")
@click.option("--tax-character", default="",
              type=click.Choice(["", *sorted(registries.GROWTH_COUNTERPARTY_ACCOUNTS)]),
              help="Character of the growth flow; asked if --series is given without "
                   "it, except for a retirement account, whose lot subkind decides.")
@click.option("--opened", "opened", default="",
              help="Open date (YYYY-MM or YYYY-MM-DD).")
@click.option("--closed", "closed", default="",
              help="Close date (YYYY-MM or YYYY-MM-DD), if any.")
@click.option("--opening-balance", default=None,
              help="Initial balance in dollars; appended to the scenario's "
                   "account_lots.csv.")
@click.option("--lot-subkind", default=None,
              type=click.Choice(sorted(registries.RETIREMENT_LOT_SUBKINDS)),
              help="A retirement account's first lot: Roth or traditional, IRA or 401k.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (for opening-balance lookup).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--tags", default="",
              help="Space-separated '<role>:<name>' tokens.")
def add_account(
    name: str | None,
    kind: str | None,
    subkind: str | None,
    owner: str | None,
    tax_treatment: str | None,
    series: str,
    tax_character: str,
    opened: str,
    closed: str,
    opening_balance: str | None,
    lot_subkind: str | None,
    scenario: str | None,
    data_dir: Path | None,
    tags: str,
) -> None:
    """Append an account to accounts.csv (and opening balance to the model).

    A retirement account always gets its first lot, ``default``, whose
    --lot-subkind (asked if not given) says whether it is Roth or traditional.
    """
    try:
        registries.parse_tags(tags)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    data_dir_p = _resolve_data_dir(data_dir)
    with _intent_db(data_dir_p, scenario) as (con, scen_name):
        existing = {r[0] for r in con.execute("SELECT name FROM account")}
        persons = [r[0] for r in con.execute("SELECT id FROM person ORDER BY id")]

    if not name:
        name = _prompt_with_completion("account name").strip()
    if not name:
        raise click.ClickException("name required")
    if name in existing:
        raise click.ClickException(f"account already exists: {name!r}")

    kinds = sorted(registries.ACCOUNT_SUBKINDS)
    if not kind:
        click.echo(f"kinds: {', '.join(kinds)}")
        kind = _prompt_with_completion("kind", choices=kinds)
    if kind not in registries.ACCOUNT_SUBKINDS:
        raise click.ClickException(
            f"unknown kind: {kind!r}; allowed: {kinds}"
        )

    subkinds = sorted(registries.ACCOUNT_SUBKINDS[kind])
    if not subkind:
        click.echo(f"subkinds for {kind}: {', '.join(subkinds)}")
        subkind = _prompt_with_completion("subkind", choices=subkinds)
    if subkind not in registries.ACCOUNT_SUBKINDS[kind]:
        raise click.ClickException(
            f"subkind {subkind!r} not allowed for kind {kind!r}; "
            f"allowed: {subkinds}"
        )

    if owner is None:
        choices = (persons or []) + ["joint"]
        owner = _prompt_with_completion(
            "owner", choices=choices, default="joint",
        )
    if persons and owner not in persons and owner != "joint":
        raise click.ClickException(
            f"unknown owner {owner!r}; allowed: {persons + ['joint']}"
        )

    retirement = subkind == registries.RETIREMENT_SUBKIND
    if retirement:
        if tax_treatment:
            raise click.ClickException(
                "a retirement account's tax treatment is its lots' subkinds'; "
                "give --lot-subkind instead of --tax-treatment"
            )
        tax_treatment = ""
        if lot_subkind is None:
            lot_subkinds = sorted(registries.RETIREMENT_LOT_SUBKINDS)
            click.echo(f"lot subkinds: {', '.join(lot_subkinds)}")
            lot_subkind = _prompt_with_completion("lot subkind", choices=lot_subkinds)
        if lot_subkind not in registries.RETIREMENT_LOT_SUBKINDS:
            raise click.ClickException(
                f"unknown lot subkind {lot_subkind!r}; "
                f"allowed: {sorted(registries.RETIREMENT_LOT_SUBKINDS)}"
            )
    elif lot_subkind:
        raise click.ClickException("--lot-subkind is only for a retirement account")
    if tax_treatment is None:
        tax_treatment = _default_tax_treatment(kind, subkind)
    if tax_treatment and tax_treatment not in registries.TAX_TREATMENTS:
        raise click.ClickException(
            f"unknown tax_treatment: {tax_treatment!r}; "
            f"allowed: {sorted(registries.TAX_TREATMENTS)}"
        )

    if opened:
        opened = _validate_month_date(opened)
    if closed:
        closed = _validate_month_date(closed)

    if series and kind != "asset":
        raise click.ClickException("--series is only supported for asset accounts")
    # A retirement lot's subkind picks its growth character (ADR-003 §2.2, #224).
    if series and not tax_character and not retirement:
        tax_character = _ask_growth_character(name, subkind)

    row = {
        "name": name, "kind": kind, "subkind": subkind,
        "owner": owner, "tax_treatment": tax_treatment,
        "series": series, "tax_character": tax_character,
        "opened": opened, "closed": closed, "meta_json": "{}",
        "tags": tags,
    }
    if opening_balance is None and kind in {"asset", "liability"}:
        if click.confirm("set opening balance now?", default=True):
            opening_balance = _prompt_with_completion(
                "opening balance (dollars, e.g. 1234.56)"
            )
    cents = _parse_amount_cents(opening_balance) if opening_balance else None
    if cents is None and retirement:
        cents = 0  # its lot row is what carries the lot subkind

    # Both rows go in as one edit, after every prompt: the account and its
    # opening balance are validated together and land together or not at all.
    accounts_path = data_dir_p / "scenarios" / scen_name / "accounts.csv"
    values_path = data_dir_p / "scenarios" / scen_name / "account_lots.csv"
    with _scenario_edit(data_dir_p, scen_name) as edit:
        _append_csv_row(edit.path(accounts_path), registries.ACCOUNTS_CSV_HEADER, row)
        if cents is not None:
            _append_csv_row(
                edit.path(values_path),
                registries.ACCOUNT_LOTS_CSV_HEADER,
                {
                    "account_name": name,
                    "lot": registries.DEFAULT_LOT if subkind in INVESTMENT_ASSET_SUBKINDS else "",
                    "subkind": lot_subkind or "",
                    "opening_balance": f"{cents // 100}.{cents % 100:02d}",
                    "opening_cost_basis": "",
                    "prior_year_end_balance": "",
                },
            )
    click.echo(f"appended account '{name}' to {accounts_path}")
    if cents is not None:
        click.echo(
            f"appended opening balance {_fmt_cents(cents).strip()} to {values_path}"
        )


_LOT_CHAIN_FIELDS = ("refill_chain", "overflow_chain")

# A lot's chain column, and the columns that mean nothing without it:
# clearing the chain clears them too, or the loader rejects the row.
_CHAIN_DEPENDENT_FIELDS: dict[str, tuple[str, ...]] = {
    "refill_chain": ("refill_lot", "refill_target", "refill_amount"),
    "overflow_chain": ("overflow_lot",),
}

_ACCOUNT_REFERENCE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("account_lots.csv", ("account_name",)),
    ("scheduled.csv", ("from_account", "to_account", "interest_account")),
    ("calendar.csv", ("account_1", "account_2")),
    ("quicken_account_map.csv", ("account_name",)),
    ("year_to_date.csv", ("from_account", "to_account")),
)


def _own_growth_series(con: sqlite3.Connection, name: str, series: str) -> str | None:
    """``series`` (account ``name``'s accounts.csv growth series) if nothing
    else reads it.

    calendar.csv set_rate rows for that series (aux_kind 'rate') have no
    reader once the account is gone, and the loader warns about them as dead,
    so ``account delete`` removes them with the account. A series another
    account or a scheduled row also uses is left alone: if the rows are still
    dead without this account, validation warns rather than guessing.
    """
    if not series:
        return None
    # The loader turns an account's growth series into a scheduled row into
    # that account; rows touching the account are deleted along with it.
    # Compare the chains' accounts, not their names: an investment account's
    # leaf chain is named '<account>:<lot>'.
    shared = con.execute(
        "WITH gone AS (SELECT id FROM account WHERE name = :name) "
        "SELECT 1 FROM scheduled_transaction st "
        "JOIN chain cf ON cf.id = st.from_account JOIN chain ct ON ct.id = st.to_account "
        "WHERE st.series = :series "
        "AND coalesce(cf.account_id, -1) NOT IN (SELECT id FROM gone) "
        "AND coalesce(ct.account_id, -1) NOT IN (SELECT id FROM gone) "
        "UNION ALL SELECT 1 FROM account_lot m "
        "WHERE m.series = :series AND m.account_id NOT IN (SELECT id FROM gone)",
        {"name": name, "series": series},
    ).fetchone()
    return None if shared else series


def _is_growth_rate_row(row: pd.Series, series: str | None) -> bool:
    """A calendar.csv set_rate row for ``series`` under aux_kind 'rate'."""
    if not series or row.get("kind") != "set_rate" or row.get("series") != series:
        return False
    try:
        params = json.loads(row.get("params_json") or "{}")
    except json.JSONDecodeError:
        return False
    return params.get("aux_kind", "rate") == "rate"


def _account_reference_rows(scenario_dir: Path, name: str, series: str | None = None) -> list[str]:
    """Return direct CSV references to an account in deterministic display order,
    plus calendar.csv's set_rate rows for its own growth ``series``."""
    references: list[str] = []
    values_path = scenario_dir / "account_lots.csv"
    if values_path.exists():
        values = pd.read_csv(values_path, dtype=str, keep_default_na=False)
        chain_fields = [
            field for field in _LOT_CHAIN_FIELDS if field in values.columns
        ]
        for idx, (_, row) in enumerate(values.iterrows()):
            if row.get("account_name") == name:
                continue
            matched_fields = [field for field in chain_fields if row[field] == name]
            if matched_fields:
                references.append(
                    f"  account_lots.csv row {idx + 2}: {', '.join(matched_fields)}"
                )

    paths = [
        (scenario_dir / filename, fields)
        for filename, fields in _ACCOUNT_REFERENCE_FIELDS
    ]
    paths.extend((path, chain_edit.ACCOUNT_COLUMNS) for path in chain_edit.chain_files(scenario_dir))

    for path, fields in paths:
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        available_fields = tuple(field for field in fields if field in df.columns)
        for idx, (_, row) in enumerate(df.iterrows()):
            matched_fields = [field for field in available_fields if row[field] == name]
            if path.name == "calendar.csv" and _is_growth_rate_row(row, series):
                matched_fields.append("series (its growth rate)")
            if matched_fields:
                references.append(
                    f"  {path.relative_to(scenario_dir)} row {idx + 2}: "
                    f"{', '.join(matched_fields)}"
                )
    return references


def _delete_account_references(scenario_dir: Path, name: str, series: str | None = None) -> None:
    """Remove rows dependent on an account, and calendar.csv's set_rate rows
    for its own growth ``series``, preserving the CSV headers."""
    paths = [
        (scenario_dir / filename, fields)
        for filename, fields in _ACCOUNT_REFERENCE_FIELDS
    ]
    paths.extend((path, chain_edit.ACCOUNT_COLUMNS) for path in chain_edit.chain_files(scenario_dir))

    for path, fields in paths:
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        available_fields = [field for field in fields if field in df.columns]
        if not available_fields:
            continue
        keep = ~df[available_fields].eq(name).any(axis=1)
        if path.name == "calendar.csv" and series:
            keep &= pd.Series(
                [not _is_growth_rate_row(row, series) for _, row in df.iterrows()],
                index=df.index, dtype=bool,
            )
        if not keep.all():
            df.loc[keep].to_csv(path, index=False)

    values_path = scenario_dir / "account_lots.csv"
    if values_path.exists():
        values = pd.read_csv(values_path, dtype=str, keep_default_na=False)
        blanked = False
        for field, dependents in _CHAIN_DEPENDENT_FIELDS.items():
            if field in values.columns:
                hit = values[field] == name
                if hit.any():
                    values.loc[hit, [field, *(c for c in dependents if c in values.columns)]] = ""
                    blanked = True
        if blanked:
            values.to_csv(values_path, index=False)

    accounts_path = scenario_dir / "accounts.csv"
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    keep = df["name"] != name
    if not keep.all():
        df.loc[keep].to_csv(accounts_path, index=False)


@account_group_command.command("delete")
@click.argument("name", required=False, type=completion.ACCOUNT)
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def delete_account(name: str | None, scenario: str | None, data_dir: Path | None) -> None:
    """Delete an account and any direct CSV rows that reference it. Without
    NAME, it is asked for."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    if name is None:
        name = _ask_name("account", names.account_names(data_dir_p, scenario_name),
                         prompt="account to delete")
    accounts_path = scenario_dir / "accounts.csv"
    # Looked up first: a derived scenario may leave accounts.csv out entirely.
    found = _overlay_rows(scenario_dir, "accounts.csv").get(name)
    if found is not None and found.inherited_from is not None and found.index is not None:
        # Deleting an override restores the inherited row; the account and
        # everything that uses it stay (#202).
        with _scenario_edit(data_dir_p, scenario_name) as edit:
            staged = edit.path(accounts_path)
            own = pd.read_csv(staged, dtype=str, keep_default_na=False)
            own.drop(index=own.index[found.index]).to_csv(staged, index=False)
        click.echo(
            f"deleted this scenario's override of account '{name}' from {accounts_path}; "
            f"it is inherited from {found.inherited_from!r} again"
        )
        return
    if found is not None and found.inherited_from is not None:
        # The loader has no way for a scenario to drop a row it inherits, and
        # the account's inherited chains, rules and lots would still name it.
        # Deleting it where it is defined is safer.
        raise click.ClickException(
            f"account {name!r} comes from scenario {found.inherited_from!r}, "
            f"and {scenario_name!r} can't remove what it inherits; delete it in "
            f"{found.inherited_from!r} to remove it from every scenario built on it"
        )
    if found is None:
        raise click.ClickException(f"unknown account: {name!r}")

    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    own_series = df.loc[df["name"] == name, "series"].iloc[0] if "series" in df.columns else ""
    with _intent_db(data_dir_p, scenario_name) as (con, _name):
        series = _own_growth_series(con, name, own_series)
    references = _account_reference_rows(scenario_dir, name, series)
    if references:
        click.echo(f"account '{name}' is referenced by:")
        click.echo("\n".join(references))
        if not click.confirm("delete the account and these referenced rows?", default=False):
            click.echo("aborted")
            return

    with _scenario_edit(data_dir_p, scenario_name) as edit:
        _delete_account_references(edit.scenario_dir, name, series)
    click.echo(f"deleted account '{name}' from {accounts_path}")


@account_group_command.command("edit")
@click.argument("name", required=False, type=completion.ACCOUNT)
@click.option("--new-name", default=None, help="Update the account name.")
@click.option("--kind", default=None, help="New account kind.")
@click.option("--subkind", default=None, help="New account subkind.")
@click.option("--owner", type=completion.OWNER, default=None, help="New owner (person id or 'joint').")
@click.option("--tax-treatment", default=None, help="New tax treatment.")
@click.option("--opened", default=None, help="Open date (YYYY-MM or YYYY-MM-DD).")
@click.option("--closed", default=None, help="Close date (YYYY-MM or YYYY-MM-DD).")
@click.option("--meta-json", default=None, help="JSON payload for the account metadata.")
@click.option("--tags", default=None, help="Space-separated '<role>:<name>' tokens.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def edit_account(
    name: str | None,
    new_name: str | None,
    kind: str | None,
    subkind: str | None,
    owner: str | None,
    tax_treatment: str | None,
    opened: str | None,
    closed: str | None,
    meta_json: str | None,
    tags: str | None,
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Edit an account's fields, prompting with current values as defaults when needed.

    An account the scenario inherits is changed here by an override: its
    whole row, edited, written to this scenario's accounts.csv. The parent
    keeps its own. A new name renames the account everywhere it is shared,
    as ``sim account rename`` does.

    Without NAME, it is asked for.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    accounts_path = scenario_dir / "accounts.csv"
    if name is None:
        name = _ask_name("account", names.account_names(data_dir_p, scenario_name),
                         prompt="account to edit")

    chosen = _overlay_rows(scenario_dir, "accounts.csv").get(name)
    if chosen is None:
        raise click.ClickException(f"unknown account: {name!r}")
    if chosen.index is None:
        if accounts_path.exists() and accounts_path.stat().st_size > 0:
            df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
        else:
            df = pd.DataFrame(columns=list(registries.ACCOUNTS_CSV_HEADER))
        idx = None
    else:
        df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
        idx = df.index[chosen.index]
    current = chosen.row

    with _intent_db(data_dir_p, scenario_name) as (con, _name):
        persons = [r[0] for r in con.execute("SELECT id FROM person ORDER BY id")]
        existing = {r[0] for r in con.execute("SELECT name FROM account")}

    direct_values = (
        new_name, kind, subkind, owner, tax_treatment,
        opened, closed, meta_json, tags,
    )
    if any(v is not None for v in direct_values):
        new_name = new_name or current.get("name", "")
        kind = kind or current.get("kind", "")
        subkind = subkind or current.get("subkind", "")
        owner = owner or current.get("owner") or "joint"
        tax_treatment = tax_treatment or current.get("tax_treatment", "")
        opened = opened if opened is not None else current.get("opened", "")
        closed = closed if closed is not None else current.get("closed", "")
        meta_json = meta_json if meta_json is not None else current.get("meta_json", "{}")
        tags = tags if tags is not None else current.get("tags", "")
    else:
        new_name = click.prompt("name", default=current.get("name", ""), show_default=True)
        kind = click.prompt(
            "kind",
            default=current.get("kind", ""),
            show_default=True,
            type=click.Choice(sorted(registries.ACCOUNT_SUBKINDS), case_sensitive=False),
        )
        kind = kind.lower() if kind else kind
        allowed_subkinds = sorted(registries.ACCOUNT_SUBKINDS.get(kind, {current.get("subkind", "")}))
        subkind = click.prompt(
            "subkind",
            default=current.get("subkind", ""),
            show_default=True,
            type=click.Choice(allowed_subkinds, case_sensitive=False),
        )
        subkind = subkind.lower() if subkind else subkind
        owner = click.prompt(
            "owner",
            default=current.get("owner") or "joint",
            show_default=True,
        )
        owner = owner.strip() or "joint"
        # A retirement account's lots set its tax treatment (#217).
        tax_treatment = "" if subkind == registries.RETIREMENT_SUBKIND else click.prompt(
            "tax_treatment",
            default=current.get("tax_treatment", ""),
            show_default=True,
            type=click.Choice(sorted(registries.TAX_TREATMENTS), case_sensitive=False),
        )
        opened = click.prompt("opened", default=current.get("opened", ""), show_default=True)
        if opened:
            opened = _validate_month_date(opened)
        closed = click.prompt("closed", default=current.get("closed", ""), show_default=True)
        if closed:
            closed = _validate_month_date(closed)
        meta_json = click.prompt("meta_json", default=current.get("meta_json", "{}"), show_default=True)
        if meta_json:
            try:
                json.loads(meta_json)
            except json.JSONDecodeError as exc:
                raise click.ClickException(f"invalid meta_json: {meta_json!r}") from exc
        tags = click.prompt("tags", default=current.get("tags", ""), show_default=True)
        if tags:
            registries.parse_tags(tags)

    if kind not in registries.ACCOUNT_SUBKINDS:
        raise click.ClickException(f"unknown kind: {kind!r}; allowed: {sorted(registries.ACCOUNT_SUBKINDS)}")
    if subkind not in registries.ACCOUNT_SUBKINDS.get(kind, set()):
        raise click.ClickException(
            f"subkind {subkind!r} not allowed for kind {kind!r}; allowed: {sorted(registries.ACCOUNT_SUBKINDS[kind])}"
        )
    if (subkind == registries.RETIREMENT_SUBKIND) != (current.get("subkind") == registries.RETIREMENT_SUBKIND):
        # Its lots' subkinds, which set how it is taxed, would have to change too.
        raise click.ClickException(
            f"{name!r} can't change to or from subkind {registries.RETIREMENT_SUBKIND!r}; "
            "add a new account and move the money (sim invest, or a transfer_balance event)"
        )
    if owner not in persons and owner != "joint":
        raise click.ClickException(
            f"unknown owner {owner!r}; allowed: {persons + ['joint']}"
        )
    if tax_treatment and tax_treatment not in registries.TAX_TREATMENTS:
        raise click.ClickException(
            f"unknown tax_treatment: {tax_treatment!r}; allowed: {sorted(registries.TAX_TREATMENTS)}"
        )
    if opened:
        opened = _validate_month_date(opened)
    if closed:
        closed = _validate_month_date(closed)
    if meta_json:
        try:
            json.loads(meta_json)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"invalid meta_json: {meta_json!r}") from exc
    if tags:
        registries.parse_tags(tags)

    # The row and the references must carry the same name; references are
    # renamed to the stripped one.
    new_name = new_name.strip()
    fields = {
        "name": new_name, "kind": kind, "subkind": subkind, "owner": owner,
        "tax_treatment": tax_treatment, "opened": opened, "closed": closed,
        "meta_json": meta_json, "tags": tags,
    }
    if new_name != name and new_name in existing:
        raise click.ClickException(f"account already exists: {new_name!r}")
    if idx is None:
        # An override carries the whole row, so what it means doesn't hang on
        # how the loader merges a blank cell over the parent's value.
        row = {**current, **fields}
        override = any(row[k] != current.get(k, "") for k in fields if k != "name")
        with _scenario_edit(data_dir_p, scenario_name) as edit:
            if new_name != name:
                _rename_account(edit, name, new_name)
            if override:
                _append_csv_row(edit.path(accounts_path), tuple(df.columns), row)
        if override:
            click.echo(
                f"wrote an override of account '{new_name}' "
                f"(inherited from {chosen.inherited_from!r}) to {accounts_path}"
            )
        else:
            click.echo(
                f"renamed account '{name}' to '{new_name}' where it is defined, "
                f"in {chosen.inherited_from!r}"
            )
        return
    df.loc[idx, list(fields)] = list(fields.values())
    with _scenario_edit(data_dir_p, scenario_name) as edit:
        if new_name != name:
            # Renames the references everywhere the account is shared; the
            # write below then carries this scenario's other changes.
            _rename_account(edit, name, new_name)
        df.to_csv(edit.path(accounts_path), index=False)

    click.echo(f"updated account '{new_name}' in {accounts_path}")


@account_group_command.command("rename")
@click.argument("old_name", required=False, type=completion.ACCOUNT)
@click.argument("new_name", required=False)
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def rename_account(
    old_name: str | None,
    new_name: str | None,
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Rename account OLD_NAME to NEW_NAME, and every reference to it, in
    every scenario that shares the account: where it is defined, a parent or
    a mix-in included, and every scenario built on those. Either name left
    out is asked for."""
    data_dir_p = _resolve_data_dir(data_dir)
    if old_name is None or new_name is None:
        scenario_name = _resolve_scenario(data_dir_p, scenario).name
        if old_name is None:
            old_name = _ask_name("account", names.account_names(data_dir_p, scenario_name),
                                 prompt="account to rename")
        if new_name is None:
            new_name = _ask_new_name(f"new name for {old_name}")
    old_name = old_name.strip()
    new_name = new_name.strip()
    if not old_name:
        raise click.ClickException("old account name required")
    if not new_name:
        raise click.ClickException("new account name required")

    with _scenario_edit(data_dir_p, scenario) as edit:
        changed = _rename_account(edit, old_name, new_name)
    _echo_renamed("account", old_name, new_name, changed)


def _default_tax_treatment(kind: str, subkind: str) -> str:
    match (kind, subkind):
        case ("asset", "hsa"):
            return "tax_deferred"
        case ("asset", registries.RETIREMENT_SUBKIND):
            return ""  # each lot's subkind says (#217)
        case ("asset", "real_estate" | "vehicle"):
            return "none"
        case ("asset", _):
            return "taxable"
        case ("liability", _):
            return "none"
        case ("income", _):
            return "taxable"
        case ("expense", _):
            return "none"
        case _:
            return "none"


@account_group_command.command("list")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--as-of", default=None,
              help="Resolve rates effective on or before this date (YYYY-MM or YYYY-MM-DD).")
@click.option("--kind", default=None,
              help="Filter to accounts of this kind (e.g. asset, liability).")
@click.option("--owner", type=completion.OWNER, default=None,
              help="Filter to accounts owned by this person or 'joint'.")
def list_accounts(
    scenario: str | None,
    data_dir: Path | None,
    as_of: str | None,
    kind: str | None,
    owner: str | None,
) -> None:
    """List accounts with opening balance and applicable rate (rich table)."""
    from rich.console import Console

    data_dir_p = _resolve_data_dir(data_dir)
    if as_of:
        as_of = month_end_iso(_validate_month_date(as_of))
    with open_scenario(data_dir_p, _resolve_scenario(data_dir_p, scenario).name) as sc:
        rows = load_accounts_view(sc, as_of=as_of)
    if kind is not None:
        rows = [r for r in rows if r.kind == kind]
    if owner is not None:
        rows = [r for r in rows if r.owner == owner]
    rows.sort(key=lambda r: (r.kind, r.owner, r.name))
    title = f"accounts ({data_dir_p.name}"
    if scenario:
        title += f" / {scenario}"
    if kind:
        title += f" / kind={kind}"
    if owner:
        title += f" / owner={owner}"
    title += ")"
    Console().print(render_accounts_table(rows, title=title))


def _holdings_options(fn):
    """The options ``account holdings`` and ``account holders`` share."""
    for option in reversed([
        click.option("--scenario", type=completion.SCENARIO, default=None,
                     help="Scenario folder name (default: the current scenario)."),
        click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
                     default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)."),
        click.option("--month", default=None,
                     help="Month of the run to show, YYYY-MM (default: the current month, "
                          "or the run's nearest one)."),
        click.option("--opening", is_flag=True, default=False,
                     help="Show the scenario's opening values instead of a run."),
        click.option("--runfile", type=click.Path(exists=True, dir_okay=False, path_type=Path),
                     default=None, help="Run file (default: the scenario's latest run)."),
    ]):
        fn = option(fn)
    return fn


@contextmanager
def _holdings_source(
    scenario: str | None, data_dir: Path | None, month: str | None,
    opening: bool, runfile: Path | None,
) -> Generator[tuple[Connection, str]]:
    """The connection holdings are read from, and a description of it: the run
    (latest, or ``--runfile``) when there is one, else the opening values."""
    data_dir_p = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data_dir_p, scenario)
    with open_scenario(data_dir_p, ref.name, scenarios_dir=ref.scenarios_dir) as sc:
        if opening:
            if month or runfile:
                raise click.ClickException("--opening cannot be combined with --month or --runfile")
            yield sc, f"opening values, {ref.name}"
            return
        run_path = runfile or ref.run_path
        if run_path is None:
            if month:
                raise click.ClickException(f"no runs for {ref.name!r}; run `sim run` first")
            click.echo("(no run yet; showing opening values)", err=True)
            yield sc, f"opening values, {ref.name}"
            return
        with open_run(run_path, sc, scenarios_dir=ref.scenarios_dir) as rc:
            yield rc, f"{ref.name}, run {Path(run_path).name}"


@account_group_command.command("holdings")
@click.argument("account", required=False, type=completion.ACCOUNT)
@_holdings_options
def account_holdings_command(
    account: str | None, scenario: str | None, data_dir: Path | None, month: str | None,
    opening: bool, runfile: Path | None,
) -> None:
    """Show the lots in ACCOUNT: symbol, quantity, balance, basis and unrealized gain.

    Without ACCOUNT, it is asked for.
    """
    from rich.console import Console

    with _holdings_source(scenario, data_dir, month, opening, runfile) as (source, described):
        if account is None:
            account = _ask_name("account", holding_accounts(source))
        try:
            frame = account_holdings(source, account, month=month)
        except (KeyError, ValueError) as exc:
            raise click.ClickException(exc.args[0]) from None
    title = f"holdings of {account} ({described}; {frame['as_of'].iloc[0]})"
    Console().print(render_holdings_table(frame, title=title))


@account_group_command.command("holders")
@click.argument("symbol", required=False, type=completion.SYMBOL)
@_holdings_options
def account_holders_command(
    symbol: str | None, scenario: str | None, data_dir: Path | None, month: str | None,
    opening: bool, runfile: Path | None,
) -> None:
    """Show every account holding SYMBOL, lot by lot, with totals.

    Without SYMBOL, it is asked for, from the symbols held.
    """
    from rich.console import Console

    with _holdings_source(scenario, data_dir, month, opening, runfile) as (source, described):
        if symbol is None:
            symbol = _ask_name("symbol", holding_symbols(source))
        try:
            frame = symbol_holders(source, symbol, month=month)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
    if frame.empty:
        click.echo(f"no lot holds {symbol!r} ({described})")
        return
    name = frame["name"].iloc[0]
    named = f"{symbol} ({name})" if isinstance(name, str) else symbol
    title = f"accounts holding {named} ({described}; {frame['as_of'].iloc[0]})"
    Console().print(render_holders_table(frame, title=title))


@sim_group_command.command("allocation")
@click.option("--exclude", "excluded", multiple=True,
              type=click.Choice([*registries.ASSET_CLASSES, allocation.UNCLASSIFIED]),
              help="Leave an asset class out (repeatable), e.g. --exclude real_estate --exclude unclassified.")
@click.option("--by", "by", type=click.Choice(allocation.GROUPINGS), default=None,
              help="Split each asset class by account, owner, subkind, tax treatment or security (symbol).")
@_holdings_options
def allocation_command(
    excluded: tuple[str, ...], by: str | None, scenario: str | None, data_dir: Path | None, month: str | None,
    opening: bool, runfile: Path | None,
) -> None:
    """Show the whole portfolio by asset class, from the lots and allocations.csv.

    A symbol with no allocations.csv rows shows as unclassified: give it some
    with `sim symbol add SYMBOL --weight CLASS=PERCENT ...`. An AI assistant
    can look the weights up from the fund's published breakdown; see the user
    guide, Part 4.11 "Add a stock or fund".
    """
    with _holdings_source(scenario, data_dir, month, opening, runfile) as (source, described):
        try:
            frame = allocation.lot_classes(source, month)
            table = allocation.portfolio_allocation(source, month, by, excluded)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
    as_of = frame["as_of"].iloc[0] if len(frame) else "no lots"
    if as_of not in ("opening", "no lots"):
        as_of += ", nominal dollars"
    title = f"asset allocation ({described}; {as_of})"
    if excluded:
        title += f", excluding {', '.join(excluded)}"
    _print_unclipped(allocation.render_allocation_table(table, title=title))
    if allocation.UNCLASSIFIED in table.index:
        click.echo("unclassified: lots whose symbol has no rows in allocations.csv"
                   " (user guide Part 4.11: Add a stock or fund)", err=True)


def _print_unclipped(rendered) -> None:
    """Print a ``rich`` table at its full width, never cropping amounts to fit
    the terminal: a wide table prints whole (the terminal wraps it;
    `| less -S` scrolls it) rather than losing columns."""
    from rich.console import Console
    from rich.measure import Measurement

    console = Console()
    needed = Measurement.get(console, console.options.update(max_width=10_000), rendered).maximum
    if needed > console.width:
        console = Console(width=needed)
    console.print(rendered)


# ---------- Result reports (#187): account history, sim report ----------

_DOLLAR_LABELS = {DollarMode.NOMINAL: "nominal dollars", DollarMode.CPI_CONSTANT: "CPI-constant dollars"}


def _report_options(fn):
    """The run and dollar options every result report takes."""
    for option in reversed([
        click.option("--dollars", type=click.Choice([m.value for m in DollarMode]),
                     default=DollarMode.CPI_CONSTANT.value, show_default=True,
                     help="CPI-constant (today's) dollars, or nominal dollars."),
        click.option("--runfile", type=click.Path(exists=True, dir_okay=False, path_type=Path),
                     default=None, help="Run file (default: the scenario's latest run)."),
        click.option("--scenario", type=completion.SCENARIO, default=None,
                     help="Scenario whose latest run to read (default: the current scenario)."),
        click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
                     default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)."),
    ]):
        fn = option(fn)
    return fn


@contextmanager
def _report_run(
    scenario: str | None, data_dir: Path | None, runfile: Path | None,
) -> Generator[tuple[RunConnection, str]]:
    """The run a report reads (``--runfile``, else the scenario's latest), and
    a description of it for the report's title.

    A run file is a snapshot holding everything a report reads, so
    ``--runfile`` needs no data repo and no scenario: the run names its own.
    """
    if runfile is not None:
        if scenario is not None:
            raise click.ClickException("--scenario cannot be combined with --runfile")
        with RunConnection(runfile) as rc:
            yield rc, f"{rc.name}, run {runfile.name}"
        return
    data_dir_p = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data_dir_p, scenario)
    run_path = ref.run_path
    if run_path is None:
        raise click.ClickException(f"no runs for {ref.name!r}; run `sim run` first")
    with RunConnection(run_path, ref.name) as rc:
        yield rc, f"{ref.name}, run {Path(run_path).name}"


def _print_report(frame: pd.DataFrame, what: str, described: str, mode: DollarMode, *,
                  total_row: bool = False) -> None:
    """Print a report: a line saying what it shows, in which dollars and from
    which run, then the table. Not the table's own title, which would wrap
    over a narrow table."""
    click.echo(f"{what}, {_DOLLAR_LABELS[mode]} ({described})")
    _print_unclipped(reports.render_report_table(frame, total_row=total_row))


@account_group_command.command("history")
@click.argument("account", required=False, type=completion.ACCOUNT)
@click.option("--lot", type=completion.LotName("account"), default=None,
              help="One lot of the account: adds its basis, unrealized gain and quantity.")
@click.option("--start", default=None, help="First month (YYYY-MM or YYYY-MM-DD).")
@click.option("--end", default=None, help="Last month (YYYY-MM or YYYY-MM-DD).")
@click.option("--yearly", is_flag=True, default=False, help="Year-end rows instead of month-end.")
@_report_options
def account_history_command(
    account: str | None, lot: str | None, start: str | None, end: str | None, yearly: bool,
    dollars: str, runfile: Path | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Show ACCOUNT's balance month by month (or year by year) from a run.

    Change is the balance's change since the row before; for an income or
    expense account, whose balance is a running total, it is the period's
    flow. With --lot, an asset lot also shows its cost basis, unrealized gain
    and quantity where the run tracks them.

    Without ACCOUNT, it is asked for.
    """
    start_iso = month_end_iso(_validate_month_date(start)) if start else None
    end_iso = month_end_iso(_validate_month_date(end)) if end else None
    if start_iso and end_iso and end_iso < start_iso:
        raise click.ClickException("--end must be on or after --start")
    mode = DollarMode(dollars)
    with _report_run(scenario, data_dir, runfile) as (rc, described):
        if account is None:
            account = _ask_name("account", rc.items("account")["name"])
        try:
            frame = reports.account_history(rc, account, lot=lot, start=start_iso, end=end_iso,
                                            yearly=yearly, mode=mode)
        except KeyError as exc:
            raise click.ClickException(exc.args[0]) from None
    if frame.empty:
        click.echo(f"no balances for {account} in that range ({described})")
        return
    _print_report(frame, f"{account}/{lot}" if lot else account, described, mode)


@sim_group_command.group(name="report")
def report_group_command() -> None:
    """Income, expenses, gains and tax by year, from a run."""


def _flow_report(side: reports.FlowSide, year: int | None, by: str, untagged: bool,
                 dollars: str, runfile: Path | None, scenario: str | None,
                 data_dir: Path | None, *, pre_tax: bool = False) -> None:
    if by not in ("category", "account") and not (by.startswith("tag:") and by != "tag:"):
        raise click.ClickException("--by must be category, account or tag:ROLE")
    if untagged and not by.startswith("tag:"):
        raise click.ClickException("--untagged needs --by tag:ROLE")
    mode = DollarMode(dollars)
    with _report_run(scenario, data_dir, runfile) as (rc, described):
        try:
            frame = (reports.flows_by_year(rc, side, by=by, mode=mode, untagged=untagged,
                                           after_tax=not pre_tax)
                     if year is None
                     else reports.flows_for_year(rc, side, year, by=by, mode=mode,
                                                 untagged=untagged))
        except KeyError as exc:
            raise click.ClickException(exc.args[0]) from None
    period = "" if year is None else f" in {year}"
    shown = (frame.index if year is not None
             else frame.columns).difference(["total", reports.ECONOMIC_INCOME, reports.CASH_INCOME,
                                             reports.CASH_FLOW, *reports.NET_WORTH_COLUMNS])
    if by.startswith("tag:") and shown.empty:
        role = by.removeprefix("tag:")
        click.echo(f"no {side}{period} is tagged in role {role} ({described});"
                   " --untagged shows it all")
        return
    if year is None:
        # A column with nothing in any year (no business, no inheritance)
        # only widens the table; the totals stay, whatever they hold.
        kept = {"total", reports.ECONOMIC_INCOME, reports.NET_WORTH_CHANGE}
        empty = [c for c in frame.columns
                 if c not in kept and frame[c].fillna(0).round(2).eq(0).all()]
        frame = frame.drop(columns=empty)
    _print_report(frame, f"{side}{period} by {by.removeprefix('tag:')}", described, mode,
                  total_row=year is not None)


_BY_HELP = ("Group by category (default), account, or tag:ROLE: each flow's tag in ROLE,"
            " from its scheduled rule or else its account.")
_UNTAGGED_HELP = ("With --by tag:ROLE, also show flows with no tag in ROLE as (untagged),"
                  " counted in the total. Left out by default.")


@report_group_command.command("income")
@click.option("--year", type=int, default=None, help="One year, a row per group, with shares.")
@click.option("--by", "by", default="category", show_default=True, help=_BY_HELP)
@click.option("--untagged", is_flag=True, help=_UNTAGGED_HELP)
@click.option(
    "--pre-tax", is_flag=True,
    help="Carry economic income down to the change in pre-tax net worth, through the tax "
         "paid in the year. By default it is after tax (#251): the year's tax liability "
         "and the change in tax owed later.",
)
@_report_options
def report_income_command(
    year: int | None, by: str, untagged: bool, pre_tax: bool, dollars: str,
    runfile: Path | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Show income by year: what each income account paid out, and where it went.

    Categories are salary, soc security, investment (interest, dividends,
    capital gains, and the growth of what stays invested, a house included,
    less market drops and a vehicle's depreciation), business (its income less
    its expenses, the Schedule C net) and other. Their total is
    economic_income: income plus paper gains, net of losses. The columns
    after it follow it down to the change in net worth:

    \b
      economic_income - tax - Δdeferred_tax = after_tax_income
      after_tax_income - spending - inflation = Δnet_worth

    tax is the year's liability, whenever it is paid; Δdeferred_tax the
    change in tax still owed on traditional balances and unrealized gains;
    spending every other expense but business costs; inflation (CPI-constant dollars only) what
    inflation took from what was held. With --pre-tax the tax line is
    tax_paid and there is no deferred line.

    Each year also shows cash_income, the income paid into a cash account
    (no paper growth), and cash_flow, that plus what was withdrawn into cash
    from investments and retirement accounts.

    By tag, flows with no tag in the role are left out (and cash_flow and
    the net worth columns with them, as they aren't tagged) unless
    --untagged is given, and their sum is named total.

    A column that is zero in every year is left out.
    """
    _flow_report("income", year, by, untagged, dollars, runfile, scenario, data_dir,
                 pre_tax=pre_tax)


@report_group_command.command("expenses")
@click.option("--year", type=int, default=None, help="One year, a row per group, with shares.")
@click.option("--by", "by", default="category", show_default=True, help=_BY_HELP)
@click.option("--untagged", is_flag=True, help=_UNTAGGED_HELP)
@_report_options
def report_expenses_command(
    year: int | None, by: str, untagged: bool, dollars: str,
    runfile: Path | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Show expenses by year: what was paid into each expense account.

    Categories are housing, healthcare, taxes, living and other. Depreciation
    is left out: it records value a vehicle loses, not money spent.

    By tag, expenses with no tag in the role are left out unless --untagged
    is given. A column that is zero in every year is left out.
    """
    _flow_report("expenses", year, by, untagged, dollars, runfile, scenario, data_dir)


@report_group_command.command("gains")
@_report_options
def report_gains_command(
    dollars: str, runfile: Path | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Show realized vs. unrealized gains by year, against that year's expenses.

    taxable and non-taxable are income that reached cash (Social Security
    split by the year's computed taxable share); unrealized is growth that
    stayed invested. distribution_coverage is realized_total minus expenses:
    a negative year spent principal.
    """
    mode = DollarMode(dollars)
    with _report_run(scenario, data_dir, runfile) as (rc, described):
        frame = reports.gains_by_year(rc, mode)
    _print_report(frame, "realized vs. unrealized gains", described, mode)


@report_group_command.command("tax")
@click.option("--reconciliation", is_flag=True, default=False,
              help="Each year's liability, what was paid during it, and the April balance due or refund.")
@_report_options
def report_tax_command(
    reconciliation: bool, dollars: str,
    runfile: Path | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Show federal and state tax by year, and the income each was figured on.

    For federal, and for state when the run computes it: AGI, the larger of
    the standard and itemized deduction, the taxable income (income) the brackets
    taxed, and the tax. effective_rate is total tax over federal AGI. Tax
    belongs to the year the income was earned in; its balance due or refund
    settles the April after (see --reconciliation).
    """
    mode = DollarMode(dollars)
    with _report_run(scenario, data_dir, runfile) as (rc, described):
        frame = (reports.tax_reconciliation(rc, mode) if reconciliation
                 else reports.tax_by_year(rc, mode))
    if frame.empty:
        click.echo(f"no tax was computed in this run ({described})")
        return
    _print_report(frame, "tax reconciliation" if reconciliation else "tax and taxable income",
                  described, mode)


# ---------- guard rails (#305) ----------

# How a margin past a rail prints: red past a rail the household means to
# stay inside, blue past a positive one (room to spend more).
_PAST_STYLES = {"negative": "red", "positive": "blue"}


def _guard_rails_of_run(rc: RunConnection, data_dir: Path | None) -> list[guard_rails.GuardRail]:
    """The rails of the run's scenario, read from the data repo now: rails
    are a lens on a run, not part of it, so even ``--runfile`` reads them."""
    scenarios_dir = _resolve_data_dir(data_dir) / "scenarios"
    try:
        return guard_rails.run_rails(rc, scenarios_dir)
    except guard_rails.GuardRailError as exc:
        for issue in exc.issues:
            click.echo(f"  error: {issue}", err=True)
        raise click.ClickException(
            f"the guard rails of {rc.name!r} don't load ({len(exc.issues)} issue(s))"
        ) from None


def _margin_style(results: Sequence[guard_rails.RailResult]) -> Callable[[str, float], str | None]:
    """Each margin column's style for a value past its rail."""
    senses: dict[str, str] = {}
    for r in results:
        for suffix in ("", guard_rails.CUMULATIVE_SUFFIX, guard_rails.CARRIED_SUFFIX):
            senses[r.rail.name + suffix] = r.rail.sense

    def style(column: str, value: float) -> str | None:
        sense = senses.get(column)
        return _PAST_STYLES[sense] if sense is not None and value < 0 else None
    return style


def _echo_rails(results: Sequence[guard_rails.RailResult]) -> None:
    width = max(len(r.rail.name) for r in results)
    def month(text: str) -> str:  # a month end as its month; a date_marker as written
        return text if not text or parse_relative_date(text) is not None else text[:7]

    for r in results:
        start, end = month(r.rail.start), month(r.rail.end)
        window = (f", {start} to {end}" if start and end else f", from {start}" if start
                  else f", until {end}" if end else "")
        click.echo(f"  {r.rail.name:<{width}}  {r.rail.describe()}{window}"
                   + ("  [positive]" if r.rail.positive else ""))


def _render_rail_summary(summary: pd.DataFrame):
    from rich.table import Table
    from rich.text import Text

    table = Table(header_style="bold")
    for column, justify in (("rail", "left"), ("form", "left"), ("years past", "right"),
                            ("worst", "right"), ("in", "right"), ("at end", "right"),
                            ("RMS past", "right")):
        table.add_column(column, justify=justify, no_wrap=True)

    def money(value: float) -> str:
        return f"-${-value:,.0f}" if round(value) < 0 else f"${value:,.0f}"

    for row in named_rows(summary):
        style = _PAST_STYLES[str(row.sense)]
        table.add_row(
            str(row.rail), str(row.form), f"{row.years_past} of {row.years}",
            Text(money(float(row.worst)), style=style if float(row.worst) < 0 else ""),
            str(row.worst_year),
            Text(money(float(row.end)), style=style if float(row.end) < 0 else ""),
            money(float(row.rms_over)),
        )
    return table


@report_group_command.command("guard-rails")
@click.option("--rail", "rail_names", type=completion.GUARD_RAIL, multiple=True,
              help="Only this rail; repeat for more. One rail alone also shows its "
                   "measure and threshold.")
@click.option("--carried", is_flag=True,
              help="Add each rail's cumulative margin carried at the portfolio's return.")
@_report_options
def report_guard_rails_command(
    rail_names: tuple[str, ...], carried: bool, dollars: str,
    runfile: Path | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Show where a run stands against its scenario's guard rails, year by year.

    A margin is the room left before a rail: threshold - measure for a rail
    above (a ceiling), measure - threshold for one below (a floor). So a
    negative margin is always past the rail, printed red for a rail to stay
    inside and blue for a positive one, where past it is room to spend more.

    Each rail has two columns: the year's margin, and its cumulative margin
    (`cum`), the years since the rail began summed: room banked, or
    overdrawn. --carried adds the cumulative margin grown at the portfolio's
    return, which is what an overdraft costs once the money not left
    invested doesn't grow (and is still only a guide: a different path
    would have had different returns).

    A summary follows: per rail and form, the years past it, the worst
    margin and its year, the margin at the end, and the RMS overshoot (the
    root mean square of how far past, years inside counting as zero).

    Rails are read from the scenario's guard_rails.csv now, not from the
    run, so a run file needs the data repo too.
    """
    mode = DollarMode(dollars)
    with _report_run(scenario, data_dir, runfile) as (rc, described):
        rails = _guard_rails_of_run(rc, data_dir)
        if rail_names:
            known = {rail.name: rail for rail in rails}
            if missing := [n for n in rail_names if n not in known]:
                raise click.ClickException(
                    f"no guard rail {', '.join(map(repr, missing))} in {rc.name!r}"
                    + (f"; it has {', '.join(sorted(known))}" if known else ""))
            rails = [known[n] for n in dict.fromkeys(rail_names)]
        if not rails:
            click.echo(f"no guard rails in {rc.name!r} ({described}); "
                       "`sim guard-rail add` adds one")
            return
        if mode == DollarMode.CPI_CONSTANT and rc.cpi_index().isna().all():
            raise click.ClickException(
                f"{described} has no 'cpi' inflation index, so no CPI-constant dollars; "
                "pass --dollars nominal")
        results = guard_rails.evaluate_run(rc, rails, mode)
    shown = [r for r in results if r.applies]
    for r in results:
        if not r.applies:
            click.echo(f"  {r.rail.name} covers none of this run's years")
    if not shown:
        return
    style = _margin_style(shown)
    if len(rail_names) == 1:
        r = shown[0]
        frame = guard_rails.rail_frame(r)
        if not carried:
            frame = frame.drop(columns="carried")
        senses = {"margin": r.rail.sense, "cumulative": r.rail.sense, "carried": r.rail.sense}

        def one_style(column: str, value: float) -> str | None:
            sense = senses.get(column)
            return _PAST_STYLES[sense] if sense is not None and value < 0 else None

        click.echo(f"guard rail {r.rail.name}, {_DOLLAR_LABELS[mode]} ({described})")
        _echo_rails([r])
        _print_unclipped(reports.render_report_table(frame, cell_style=one_style))
    else:
        click.echo(f"guard rail margins, {_DOLLAR_LABELS[mode]} ({described})")
        _echo_rails(shown)
        frame = guard_rails.margins_frame(shown, carried=carried)
        _print_unclipped(reports.render_report_table(frame, cell_style=style))
    click.echo("")
    _print_unclipped(_render_rail_summary(guard_rails.summary_frame(shown)))


@account_group_command.group(name="tag")
def account_tag_group() -> None:
    """Add or remove tags on an account."""


@account_tag_group.command("add")
@click.argument("name", required=False, type=completion.ACCOUNT)
@click.argument("tag_tokens", type=completion.ANY_TAG, nargs=-1, required=False)
@click.option("--scenario", type=completion.SCENARIO, default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def account_tag_add(
    name: str | None,
    tag_tokens: tuple[str, ...],
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Add one or more '<role>:<name>' tags to an account.

    Without NAME or TAG_TOKENS, they are asked for.
    """
    _scenario_write(data_dir, scenario, "account", name, tag_tokens, "add")


@account_tag_group.command("rm")
@click.argument("name", required=False, type=completion.ACCOUNT)
@click.argument("tag_tokens", type=completion.TAG, nargs=-1, required=False)
@click.option("--scenario", type=completion.SCENARIO, default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def account_tag_rm(
    name: str | None,
    tag_tokens: tuple[str, ...],
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Remove one or more '<role>:<name>' tags from an account.

    Without NAME or TAG_TOKENS, they are asked for.
    """
    _scenario_write(data_dir, scenario, "account", name, tag_tokens, "rm")


# ---------- account lot: one account_lots.csv row (#124) ----------

# Per file, the (account, lot) column pairs that name a lot. A blank lot names
# the account's `default` lot, the only one a blank may name (ADR-003 §7).
_LOT_REFERENCE_FIELDS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("scheduled.csv", (("from_account", "from_lot"), ("to_account", "to_lot"))),
    ("calendar.csv", (("account_1", "lot_1"), ("account_2", "lot_2"))),
    ("year_to_date.csv", (("from_account", "from_lot"), ("to_account", "to_lot"))),
)

# account_lots.csv's own references: another lot's refill or overflow chain.
_LOT_CHAIN_LOT_FIELDS = (("refill_chain", "refill_lot"), ("overflow_chain", "overflow_lot"))

_LOT_MONEY_COLUMNS = frozenset({
    "opening_balance", "opening_cost_basis", "prior_year_end_balance",
    "minimum_balance", "maximum_balance", "refill_target", "refill_amount",
})


class _Lot(NamedTuple):
    """One lot of a scenario, as its load order leaves it."""
    row: dict[str, str]  # as authored, in the last file of the load order to have it
    index: int | None  # its data row (0-based) in this scenario's own file, if there
    inherited_from: str | None  # the last scenario before this one to have it
    # The scenario, in the load order, whose zeroed override deletes the lot:
    # how ``lot delete`` stands in for removing a row it can't remove (#124).
    deleted_in: str | None = None
    # The empty default lot an account with no lot rows loads with: no file
    # has it, so editing it adds its row (#233).
    implicit: bool = False

    @property
    def account(self) -> str:
        return self.row["account_name"].strip()

    @property
    def lot(self) -> str:
        return self.row["lot"].strip() or registries.DEFAULT_LOT

    @property
    def deleted(self) -> bool:
        return self.deleted_in is not None

    @property
    def deleted_where(self) -> str:
        return "here" if self.index is not None else f"in {self.deleted_in}"


def _zero_lot(row: dict[str, str]) -> bool:
    """Empty in every way ``lot delete`` empties a lot: no balance, shares or
    basis, and no balance management. A blank balance is derived, not zero,
    and a lot that only opens at zero (it has a basis, or refills) is an
    ordinary lot."""
    def zero(column: str, *, blank: bool) -> bool:
        text = row[column].strip().replace(",", "")
        if not text:
            return blank
        try:
            return Decimal(text) == 0
        except InvalidOperation:
            return False
    return (
        zero("opening_balance", blank=False)
        and all(zero(column, blank=True)
                for column in ("quantity", "opening_cost_basis", "prior_year_end_balance"))
        and not any(row[column].strip() for column in registries.CASH_MANAGEMENT_COLUMNS)
    )


def _read_values_rows(path: Path) -> list[dict[str, str]]:
    """A scenario's own account_lots.csv rows, every column present."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame = frame.reindex(columns=registries.ACCOUNT_LOTS_CSV_HEADER, fill_value="")
    return [{str(k): str(v) for k, v in record.items()} for record in frame.to_dict("records")]


def _lineage_dir(scenario_dir: Path, name: str) -> Path:
    """The folder of ``name``, a scenario in ``scenario_dir``'s lineage: a
    sibling folder, or a template from the code repository."""
    return _folder_of(scenario_dir.parent, name)


def _scenario_lots(scenario_dir: Path, name: str) -> list[_Lot]:
    """Every lot of scenario ``name``, overlaid down its load order as the
    loader overlays them, in the order they first appear."""
    lots: dict[tuple[str, str], _Lot] = {}
    for scenario_name in load_order(scenario_dir.parent, name):
        rows = _read_values_rows(_lineage_dir(scenario_dir, scenario_name) / "account_lots.csv")
        for index, row in enumerate(rows):
            key = (row["account_name"].strip(), row["lot"].strip() or registries.DEFAULT_LOT)
            prior = lots.get(key)
            # A zeroed override deletes the lot it overrides, for this
            # scenario and every one below it, until one overrides it again.
            deleted_in = scenario_name if prior is not None and _zero_lot(row) else None
            if scenario_name != name:
                lots[key] = _Lot(row, None, scenario_name, deleted_in)
            else:
                lots[key] = _Lot(row, index, prior.inherited_from if prior else None, deleted_in)
    return list(lots.values())


def _implicit_default_lot(
    data_dir: Path, scenario: str, lots: list[_Lot], account: str,
) -> list[_Lot]:
    """The empty ``default`` lot of ``account``, when the account holds lots
    but has no lot rows in the scenario's lineage: the loader gives such an
    account that lot, so ``lot edit`` can change it like any other. An
    investment account's lots need a symbol, so it gets none."""
    if any(one.account == account for one in lots):
        return []
    with _intent_db(data_dir, scenario) as (con, _name):
        found = con.execute(
            "SELECT subkind FROM account WHERE name = ? AND kind IN ('asset', 'liability')",
            (account,),
        ).fetchone()
    if found is None or found[0] in INVESTMENT_ASSET_SUBKINDS:
        return []
    row = dict.fromkeys(registries.ACCOUNT_LOTS_CSV_HEADER, "")
    row |= {"account_name": account, "opening_balance": "0.00"}
    return [_Lot(row, None, None, implicit=True)]


def _lot_label(lot: _Lot) -> str:
    row = lot.row
    parts = [f"{lot.account} ({lot.lot})"]
    if row["symbol"]:
        parts.append(row["symbol"])
    if row["quantity"]:
        parts.append(f"{row['quantity']} sh")
    if row["opening_balance"]:
        parts.append(row["opening_balance"])
    if lot.deleted:
        parts.append(f"[deleted: zeroed {lot.deleted_where}, inherited from {lot.inherited_from}]")
    elif lot.implicit:
        parts.append("[no row yet: the account's empty default lot]")
    elif lot.index is None:
        parts.append(f"[inherited from {lot.inherited_from}]")
    elif lot.inherited_from:
        parts.append(f"[overrides {lot.inherited_from}]")
    return "  ".join(parts)


def _select_lot(lots: list[_Lot], account: str | None, lot: str | None) -> _Lot:
    """The lot ``account`` and ``lot`` name. Without them, or with an account
    that has several, the lots are listed, and one is picked by its number or
    by text that narrows the list, asked again until one is left."""
    candidates = list(range(len(lots)))
    if account is not None:
        account = account.strip()
        candidates = [i for i in candidates if lots[i].account == account]
        if not candidates:
            raise click.ClickException(f"account {account!r} has no lots in this scenario")
        if lot is not None:
            wanted = lot.strip() or registries.DEFAULT_LOT
            match = [i for i in candidates if lots[i].lot == wanted]
            if not match:
                raise click.ClickException(
                    f"{account!r} has no lot {wanted!r}; its lots: "
                    f"{', '.join(lots[i].lot for i in candidates)}"
                )
            return lots[match[0]]
        if len(candidates) == 1:
            return lots[candidates[0]]
    if not candidates:
        raise click.ClickException("this scenario has no lots")
    labels = [_lot_label(one) for one in lots]
    while True:
        for i in candidates:
            click.echo(f"  {i + 1:>3}  {labels[i]}")
        text = _prompt_with_completion(
            "lot (its number, or text to narrow the list)",
            choices=[str(i + 1) for i in candidates],
        ).strip()
        if text.isdigit() and int(text) - 1 in candidates:
            return lots[int(text) - 1]
        matches = [i for i in candidates if text.lower() in labels[i].lower()]
        if len(matches) == 1:
            return lots[matches[0]]
        if matches:
            candidates = matches
        else:
            click.echo(f"  no lot matches {text!r}")


def _names_lot(row: dict[str, str] | pd.Series, account_field: str, lot_field: str,
               account: str, lot: str) -> bool:
    return (str(row.get(account_field, "")).strip() == account
            and (str(row.get(lot_field, "")).strip() or registries.DEFAULT_LOT) == lot)


def _lot_reference_files(scenario_dir: Path) -> list[tuple[Path, tuple[tuple[str, str], ...]]]:
    paths = [(scenario_dir / name, pairs) for name, pairs in _LOT_REFERENCE_FIELDS]
    paths.extend((path, chain_edit.LOT_COLUMNS) for path in chain_edit.chain_files(scenario_dir))
    return [(path, pairs) for path, pairs in paths if path.exists() and path.stat().st_size > 0]


def _lot_descendants(scenarios_dir: Path, name: str) -> list[str]:
    """The scenarios that inherit from ``name``, directly or not."""
    return [
        path.name for path in sorted(scenarios_dir.iterdir())
        if path.is_dir() and path.name != name and name in load_order(scenarios_dir, path.name)
    ]


def _lot_reference_rows(
    scenario_dir: Path, account: str, lot: str, *, own_row: bool = False,
) -> list[str]:
    """The rows of one scenario folder that name ``account``'s ``lot``, as
    display lines; with ``own_row``, its own account_lots.csv row too (an
    inheriting scenario's override of it)."""
    references: list[str] = []
    values = _read_values_rows(scenario_dir / "account_lots.csv")
    for index, row in enumerate(values):
        if _names_lot(row, "account_name", "lot", account, lot):
            if own_row:
                references.append(f"  account_lots.csv row {index + 2}: an override of the lot")
            continue
        matched = [f"{a}/{b}" for a, b in _LOT_CHAIN_LOT_FIELDS if _names_lot(row, a, b, account, lot)]
        if matched:
            references.append(f"  account_lots.csv row {index + 2}: {', '.join(matched)}")
    for path, pairs in _lot_reference_files(scenario_dir):
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        for index, (_, row) in enumerate(frame.iterrows()):
            matched = [f"{a}/{b}" for a, b in pairs if _names_lot(row, a, b, account, lot)]
            if matched:
                references.append(
                    f"  {path.relative_to(scenario_dir)} row {index + 2}: {', '.join(matched)}"
                )
    return references


def _lot_family_references(scenario_dir: Path, name: str, account: str, lot: str) -> list[str]:
    """``_lot_reference_rows`` for scenario ``name`` and every scenario that
    inherits from it, whose rows a rename or delete rewrites too; the
    inheriting scenarios' lines are labelled with their names."""
    references = _lot_reference_rows(scenario_dir, account, lot)
    for descendant in _lot_descendants(scenario_dir.parent, name):
        references += [
            f"  {descendant}: {line.strip()}"
            for line in _lot_reference_rows(scenario_dir.parent / descendant, account, lot, own_row=True)
        ]
    return references


def _emptied_chains(scenario_dir: Path, name: str, account: str, lot: str) -> list[str]:
    """The chain files, in scenario ``name`` and those inheriting from it,
    whose every step is ``account``'s ``lot``: deleting the lot would leave
    them with no steps, which the loader rejects."""
    emptied: list[str] = []
    for scenario_name in (name, *_lot_descendants(scenario_dir.parent, name)):
        for path in chain_edit.chain_files(scenario_dir.parent / scenario_name):
            if path.stat().st_size == 0:
                continue
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
            if len(frame) and all(_names_lot(row, "account_name", "lot", account, lot)
                                  for _, row in frame.iterrows()):
                emptied.append(f"{scenario_name}/chains/{path.parent.name}/{path.name}")
    return emptied


def _rewrite_lot_references(
    scenario_dir: Path, account: str, lot: str, new_lot: str | None, *, own_row: bool = False,
) -> None:
    """Point one scenario folder's references to ``account``'s ``lot`` at
    ``new_lot``, or, with ``None``, delete the rows that move money through
    it and clear other lots' refill or overflow settings that name it. With
    ``own_row``, the lot's own row (an inheriting scenario's override of it)
    is renamed or deleted too."""
    values_path = scenario_dir / "account_lots.csv"
    if values_path.exists() and values_path.stat().st_size > 0:
        values = pd.read_csv(values_path, dtype=str, keep_default_na=False)
        changed = False
        own = pd.Series(
            [_names_lot(row, "account_name", "lot", account, lot) for _, row in values.iterrows()],
            index=values.index, dtype=bool,
        )
        for account_field, lot_field in _LOT_CHAIN_LOT_FIELDS:
            # A missing lot column means the default lot, as _names_lot reads it.
            if account_field not in values.columns:
                continue
            hit = pd.Series(
                [_names_lot(row, account_field, lot_field, account, lot) for _, row in values.iterrows()],
                index=values.index, dtype=bool,
            ) & ~own
            if not hit.any():
                continue
            changed = True
            if new_lot is None:
                dependents = _CHAIN_DEPENDENT_FIELDS[account_field]
                values.loc[hit, [account_field, *(c for c in dependents if c in values.columns)]] = ""
            else:
                if lot_field not in values.columns:
                    values[lot_field] = ""
                values.loc[hit, lot_field] = new_lot
        if own_row and own.any():
            changed = True
            if new_lot is None:
                values = values.loc[~own]
            else:
                if "lot" not in values.columns:
                    values["lot"] = ""
                values.loc[own, "lot"] = new_lot
        if changed:
            values.to_csv(values_path, index=False)
    for path, pairs in _lot_reference_files(scenario_dir):
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        changed = False
        keep = pd.Series(True, index=frame.index, dtype=bool)
        for account_field, lot_field in pairs:
            # A chain file may omit its lot column: its steps then name the
            # default lot, which a rename or delete must reach too.
            if account_field not in frame.columns:
                continue
            hit = pd.Series(
                [_names_lot(row, account_field, lot_field, account, lot) for _, row in frame.iterrows()],
                index=frame.index, dtype=bool,
            )
            if not hit.any():
                continue
            changed = True
            if new_lot is None:
                keep &= ~hit
            else:
                if lot_field not in frame.columns:
                    frame[lot_field] = ""
                frame.loc[hit, lot_field] = new_lot
        if changed:
            frame.loc[keep].to_csv(path, index=False)


def _rewrite_lot_family(
    edit: StagedEdit, account: str, lot: str, new_lot: str | None,
) -> None:
    """``_rewrite_lot_references`` in the edited scenario and, own rows
    included, in every scenario that inherits from it: they name the lot
    too, and the staged edit loads them all."""
    _rewrite_lot_references(edit.scenario_dir, account, lot, new_lot)
    for descendant in _lot_descendants(edit.scenario_dir.parent, edit.name):
        _rewrite_lot_references(edit.scenario_dir.parent / descendant, account, lot, new_lot, own_row=True)


def _money_cell(text: str) -> str:
    """A money answer as account_lots.csv holds it: dollars and cents, or
    an ``@name`` reference as typed."""
    text = text.strip()
    if not text or text.startswith("@"):
        return text
    cents = _parse_amount_cents(text)
    dollars, rem = divmod(abs(cents), 100)
    return f"{'-' if cents < 0 else ''}{dollars}.{rem:02d}"


# The lot checkers return the answer in a form that compares equal however
# it was typed, as the guide compares key fields with them.
def _check_money(text: str) -> int | str:
    return text.strip() if text.strip().startswith("@") else _parse_amount_cents(text)


def _check_day(text: str) -> date:
    try:
        return parse_day(text.strip())
    except ValueError:
        raise click.ClickException(f"date must be YYYY-MM-DD: {text!r}") from None


def _check_quantity(text: str) -> Decimal:
    try:
        quantity = Decimal(text.strip().replace(",", ""))
    except InvalidOperation:
        raise click.ClickException(f"not a number: {text!r}") from None
    if not quantity.is_finite() or quantity < 0:
        raise click.ClickException(f"quantity must be a number of shares, 0 or more: {text!r}")
    return quantity


def _check_lot_label(text: str) -> str:
    if ":" in text:
        raise click.ClickException(f"a lot label may not contain ':': {text!r}")
    return text.strip()


_LOT_CHECKERS: dict[str, Callable[[str], object]] = {
    "money": _check_money, "day": _check_day, "number": _check_quantity, "lot": _check_lot_label,
}


def _lot_guide() -> calendar_guide.Guide:
    return calendar_guide.Guide(
        ask=lambda text, choices, default: _prompt_with_completion(
            text, choices=choices, default=default,
        ),
        checkers=_LOT_CHECKERS,
    )


def _lot_guide_context(
    data_dir: Path, scenario: str, lots: list[_Lot], *, editing: _Lot | None = None,
) -> calendar_guide.GuideContext:
    """The scenario's accounts, chains, symbols and series, as completions
    for the lot prompts, and the lots a new or renamed lot may not copy. A
    deleted (zeroed) lot is free to be added again."""
    with _intent_db(data_dir, scenario) as (con, _name):
        accounts = con.execute(
            "SELECT name, subkind, tax_treatment FROM account "
            "WHERE kind IN ('asset', 'liability') ORDER BY name"
        ).fetchall()
        rate_series = sorted({
            r[0] for r in con.execute(
                "SELECT series FROM aux_table WHERE kind = 'rate' "
                "UNION SELECT series FROM account_lot"
            ) if r[0]
        })
        growing = frozenset(r[0] for r in con.execute(
            "SELECT symbol FROM security_allocation UNION SELECT series FROM aux_table WHERE kind = 'rate'"
        ) if r[0])
        overlaid = _overlay_rows(data_dir / "scenarios" / scenario, "accounts.csv")
        return calendar_guide.GuideContext(
            accounts=[name for name, _subkind, _tax in accounts],
            lots=_lot_info(con),
            chains=_interior_chains(con), chain_roles=_chain_roles(con),
            series={"rate": rate_series},
            other_rows={lot_guide.LOT.command: [
                {"account": one.account, "lot": one.lot}
                for one in lots if one is not editing and not one.deleted
            ]},
            subkinds={name: subkind for name, subkind, _tax in accounts},
            tax_treatments={name: tax for name, _subkind, tax in accounts},
            securities=[r[0] for r in con.execute("SELECT symbol FROM security ORDER BY symbol")],
            growing=growing,
            growth_characters={name: one.row.get("tax_character", "") for name, one in overlaid.items()},
        )


def _lot_growth_character(
    context: calendar_guide.GuideContext, params: dict[str, object],
) -> str | None:
    """The tax character to give the lot's account, when the lot is the
    account's first to grow (#233): the one given, or else the subkind's
    usual one. None when the account needs none."""
    answers = {key: str(value) for key, value in params.items()}
    given = answers.get("tax_character", "").strip()
    if not lot_guide.needs_growth_character(context, answers):
        account = answers.get("account", "").strip()
        if given and given != context.growth_characters.get(account):
            raise click.ClickException(
                "--tax-character only sets the character of an account's growth along with "
                f"its first lot to grow; {account!r} "
                + ("already has one" if context.growth_characters.get(account) else
                   "doesn't grow with this lot")
            )
        return None
    if given and given not in registries.GROWTH_COUNTERPARTY_ACCOUNTS:
        raise click.ClickException(
            f"unknown tax character {given!r}; allowed: {sorted(registries.GROWTH_COUNTERPARTY_ACCOUNTS)}"
        )
    return given or lot_guide.GROWTH_CHARACTER.get(context.subkinds.get(answers["account"].strip(), ""), "gains")


def _echo_growth_character(account: str, character: str | None) -> None:
    if character is not None:
        click.echo(f"set the tax_character of {account!r} to {character!r}, so the lot grows")


def _check_lot_params(
    context: calendar_guide.GuideContext, params: dict[str, object], *, editing: _Lot | None = None,
) -> None:
    """What the guide checks as each answer is typed, for answers given as
    arguments and options instead. The loader judges the rest when the edit
    is staged."""
    account = str(params.get("account", "")).strip()
    lot = str(params.get("lot", "")).strip()
    if account not in context.accounts:
        raise click.ClickException(f"unknown account (or one that holds no lots): {account!r}")
    if not lot:
        raise click.ClickException("a lot label is required (e.g. default)")
    if editing is not None and account != editing.account:
        raise click.ClickException(
            "a lot can't move to another account; add it there and delete it here"
        )
    taken = context.other_rows.get(lot_guide.LOT.command, ())
    if {"account": account, "lot": lot} in taken:
        raise click.ClickException(
            f"{account!r} already has a lot {lot!r}; change it with 'sim account lot edit'"
        )
    for field_ in lot_guide.LOT.fields:
        value = str(params.get(field_.param, ""))
        checker = _LOT_CHECKERS.get(field_.type)
        if value and checker is not None:
            try:
                checker(value)
            except click.ClickException as error:
                raise click.ClickException(f"{field_.name}: {error.format_message()}") from None


def _lot_row(params: dict[str, object], original: dict[str, str] | None, *, investment: bool) -> dict[str, str]:
    """The account_lots.csv row for a lot's answers. An unchanged cell is
    kept as authored, and an ordinary account's ``default`` lot is written
    blank unless it was spelled out, as ``account add`` writes it."""
    lot = str(params["lot"]).strip()
    spelled = original is not None and bool(original["lot"].strip())
    row = {
        "account_name": str(params["account"]).strip(),
        "lot": "" if lot == registries.DEFAULT_LOT and not investment and not spelled else lot,
    }
    for column in lot_guide.VALUE_COLUMNS:
        value = str(params.get(column, "")).strip()
        if column == "symbol":
            value = value.upper()  # tickers are upper case; the loader rejects any other
        if original is not None and value == original[column].strip():
            row[column] = original[column]
        else:
            row[column] = _money_cell(value) if column in _LOT_MONEY_COLUMNS else value
    return row


def _write_lot(
    data_dir: Path, scenario_name: str, path: Path, row: dict[str, str] | None,
    *, replace: _Lot | None = None, rename: tuple[str, str] | None = None,
    delete_references: tuple[str, str] | None = None, growth_character: str | None = None,
) -> None:
    """Write ``row`` into the scenario's own account_lots.csv -- appended,
    or in place of ``replace``'s row; ``None`` removes that row instead --
    and rewrite the references ``rename`` (old, new lot) or
    ``delete_references`` (account, lot) asks for, as one staged edit.
    ``growth_character`` sets the account's tax character with it."""
    with _scenario_edit(data_dir, scenario_name) as edit:
        staged = edit.path(path)
        if growth_character is not None and row is not None:
            # A whole-row override when the account is inherited (#202).
            accounts = path.parent / "accounts.csv"
            found = _overlay_rows(path.parent, "accounts.csv")[row["account_name"]]
            _append_csv_row(edit.path(accounts), registries.ACCOUNTS_CSV_HEADER,
                            {**found.row, "tax_character": growth_character}, replace=found.index)
        if replace is not None and replace.index is not None:
            # Read inside the staged edit: the prompts may have taken a while,
            # and the index is only good if the file still has the lot there.
            existing = _read_values_rows(staged)
            if replace.index >= len(existing) or existing[replace.index] != replace.row:
                raise click.ClickException(
                    f"{path} changed while this lot was being edited, so row "
                    f"{replace.index + 2} is no longer the lot that was chosen. "
                    "Nothing was written; start again."
                )
            if row is None:
                frame = pd.read_csv(staged, dtype=str, keep_default_na=False)
                frame.drop(index=replace.index).to_csv(staged, index=False)
            else:
                _append_csv_row(staged, registries.ACCOUNT_LOTS_CSV_HEADER, row, replace=replace.index)
        elif row is not None:
            _append_csv_row(staged, registries.ACCOUNT_LOTS_CSV_HEADER, row)
        if rename is not None and row is not None:
            _rewrite_lot_family(edit, row["account_name"], *rename)
        if delete_references is not None:
            _rewrite_lot_family(edit, *delete_references, None)


def _lot_value_options[F: Callable[..., object]](f: F) -> F:
    """One option per optional lot field, from ``lot_guide.LOT``, so the
    guided questions and the options can't drift apart. Each defaults to
    ``None`` (not given); ``""`` clears a field."""
    for field_ in reversed(lot_guide.LOT.fields):
        if field_.option is not None:
            f = click.option(field_.option, default=None, help=field_.help or field_.label,
                             type=completion.type_for(field_.param))(f)
    return f


def _is_investment(context: calendar_guide.GuideContext, account: str) -> bool:
    return context.subkinds.get(account) in INVESTMENT_ASSET_SUBKINDS


@account_group_command.group(name="lot")
def lot_group_command() -> None:
    """Add, change or delete an account's lots (account_lots.csv rows, ADR-005)."""


@lot_group_command.command("list")
@click.argument("account", required=False, type=completion.ACCOUNT)
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def lot_list(account: str | None, scenario: str | None, data_dir: Path | None) -> None:
    """List an account's lots, as the scenario's load order leaves them, or
    every account's without ACCOUNT. A column no listed lot uses is left out.

    The last column says where each lot comes from: this scenario, a parent
    it inherits from, or this scenario's override of a parent's lot, and
    whether the lot is deleted (zeroed here).
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    lots = _scenario_lots(scenario_dir, scenario_name)
    if account is not None:
        account = account.strip()
        lots = [one for one in lots if one.account == account]
        if not lots:
            with _intent_db(data_dir_p, scenario_name) as (con, _name):
                held = con.execute(
                    "SELECT 1 FROM account WHERE name = ? AND kind IN ('asset', 'liability')", (account,)
                ).fetchone()
            if held is None:
                raise click.ClickException(
                    f"no account {account!r} that holds lots in scenario {scenario_name!r}"
                )
            # The loader gives an account with no account_lots.csv rows an
            # empty default lot; say so rather than list nothing.
            click.echo(
                f"{account!r} has no lots in account_lots.csv, so it loads with an "
                f"empty {registries.DEFAULT_LOT!r} lot"
            )
            return

    def source(one: _Lot) -> str:
        if one.deleted:
            return f"deleted (zeroed {one.deleted_where}; from {one.inherited_from})"
        if one.index is None:
            return f"inherited from {one.inherited_from}"
        return f"overrides {one.inherited_from}" if one.inherited_from else scenario_name

    columns = [column for column in lot_guide.VALUE_COLUMNS if any(one.row[column] for one in lots)]
    header = ["account", "lot", *columns, "from"]
    table = [[one.account, one.lot, *(one.row[column] for column in columns), source(one)] for one in lots]
    widths = [max(len(cell) for cell in column) for column in zip(header, *table, strict=True)]
    for line in (header, *table):
        click.echo("  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip())


@lot_group_command.command("add")
@click.argument("account", required=False, type=completion.ACCOUNT)
@click.argument("lot", required=False)
@_lot_value_options
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.pass_context
def lot_add(
    ctx: click.Context, account: str | None, lot: str | None,
    scenario: str | None, data_dir: Path | None, **values: str | None,
) -> None:
    """Add a lot to an account: one row of the scenario's account_lots.csv.

    With ACCOUNT and LOT, writes the lot from the options. Without them, asks
    for what applies to the account a question at a time (a cost basis only
    for a taxable account, balance management only if wanted), then shows the
    lot and the equivalent command, and writes it once you confirm.

    A lot a parent scenario has that this one, or a scenario between, deleted
    (zeroed) may be added again; it replaces or overrides the zeroed one.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "account_lots.csv"
    lots = _scenario_lots(scenario_dir, scenario_name)
    context = _lot_guide_context(data_dir_p, scenario_name, lots)

    def submit(params: dict[str, object]) -> None:
        _check_lot_params(context, params)
        key = (str(params["account"]).strip(), str(params["lot"]).strip())
        zeroed = next((one for one in lots if one.deleted and (one.account, one.lot) == key), None)
        row = _lot_row(params, None, investment=_is_investment(context, key[0]))
        character = _lot_growth_character(context, params)
        # The scenario resolved above, by name: a defaulted scenario is not
        # resolved again, in case the default changed during the prompts.
        _write_lot(data_dir_p, scenario_name, path, row, replace=zeroed, growth_character=character)
        _echo_growth_character(key[0], character)
        click.echo(f"{'re-added' if zeroed else 'added'} lot {key[1]!r} of {key[0]!r} in {path}")

    answers = {param: value for param, value in values.items() if value is not None}
    if account is not None and lot is not None:
        submit({"account": account, "lot": lot, **answers})
        return
    _echo_guided_scenario(scenario_name, path, defaulted=scenario is None)
    answers |= {param: value for param, value in (("account", account), ("lot", lot)) if value is not None}
    guide = _lot_guide()
    guide.complete(lot_guide.LOT, context, answers)
    if not guide.review(
        lot_guide.LOT, context, answers, submit,
        scenario=scenario, data_dir=str(data_dir) if data_dir is not None else None,
    ):
        ctx.exit(1)


@lot_group_command.command("edit")
@click.argument("account", required=False, type=completion.ACCOUNT)
@click.argument("lot", type=completion.LotName("account"), required=False)
@click.option("--new-lot", default=None,
              help="Rename the lot; this scenario's references to it follow.")
@_lot_value_options
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.pass_context
def lot_edit(
    ctx: click.Context, account: str | None, lot: str | None, new_lot: str | None,
    scenario: str | None, data_dir: Path | None, **values: str | None,
) -> None:
    """Change one of an account's lots.

    ACCOUNT and LOT pick the lot; without them, or with an account that has
    several, the lots are listed to choose from. Options set those fields and
    write at once ("" clears one). With none, the lot's fields are shown: name
    one to change it, as often as needed, and write when done.

    Renaming the lot (--new-lot, or its lot field) also renames this
    scenario's references to it. A lot inherited from a parent scenario is
    changed by writing an override of the whole lot into this scenario;
    renaming one is refused, since the parent's lot would remain: `sim
    account lot rename` renames it everywhere it is shared.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "account_lots.csv"
    lots = _scenario_lots(scenario_dir, scenario_name)
    if account is not None:
        lots.extend(_implicit_default_lot(data_dir_p, scenario_name, lots, account.strip()))
    chosen = _select_lot(lots, account, lot)
    context = _lot_guide_context(data_dir_p, scenario_name, lots, editing=chosen)

    def submit(params: dict[str, object]) -> None:
        _check_lot_params(context, params, editing=chosen)
        new = str(params["lot"]).strip()
        renamed = new != chosen.lot
        if renamed and chosen.inherited_from:
            raise click.ClickException(
                f"lot {chosen.lot!r} of {chosen.account!r} comes from scenario "
                f"{chosen.inherited_from!r}; `sim account lot rename` renames it there "
                "and in every scenario built on it"
            )
        row = _lot_row(params, chosen.row, investment=_is_investment(context, chosen.account))
        character = _lot_growth_character(context, params)
        if row == chosen.row and chosen.index is not None and character is None:
            click.echo("no change; nothing was written")
            return
        _write_lot(
            data_dir_p, scenario_name, path, row, replace=chosen,
            rename=(chosen.lot, new) if renamed else None, growth_character=character,
        )
        _echo_growth_character(chosen.account, character)
        if chosen.implicit:
            click.echo(f"added lot {new!r} of {chosen.account!r} to {path}")
        elif chosen.index is None:
            click.echo(
                f"wrote an override of lot {chosen.lot!r} of {chosen.account!r} "
                f"(inherited from {chosen.inherited_from!r}) to {path}"
            )
        else:
            click.echo(f"updated lot {new!r} of {chosen.account!r} in {path}")

    answers = lot_guide.answers_of(chosen.row)
    given = {param: value.strip() for param, value in values.items() if value is not None}
    if given or new_lot is not None:
        answers |= given
        if new_lot is not None:
            answers["lot"] = new_lot.strip()
        if any(answers.get(column) for column in registries.CASH_MANAGEMENT_COLUMNS):
            answers[lot_guide.MANAGE] = "y"
        submit(calendar_guide.command_params(lot_guide.LOT, answers))
        return
    _echo_guided_scenario(scenario_name, path, defaulted=scenario is None)
    click.echo(f"Editing lot: {_lot_label(chosen)}")
    if chosen.implicit:
        click.echo("  The account has no lot rows, so writing adds this one to the scenario.")
    elif chosen.index is None:
        click.echo(
            f"  It comes from scenario {chosen.inherited_from!r}: writing puts an override "
            "of the whole lot in this scenario."
        )
    guide = _lot_guide()
    guide.complete(lot_guide.LOT, context, answers)
    if not guide.review(lot_guide.LOT, context, answers, submit, show_command=False):
        ctx.exit(1)


@lot_group_command.command("delete")
@click.argument("account", required=False, type=completion.ACCOUNT)
@click.argument("lot", type=completion.LotName("account"), required=False)
@click.option("--yes", is_flag=True, help="Don't ask before deleting the rows that reference the lot.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def lot_delete(
    account: str | None, lot: str | None, yes: bool, scenario: str | None, data_dir: Path | None,
) -> None:
    """Delete one of an account's lots.

    A lot only this scenario has is removed, with the rows that move money
    through it (rules, calendar rows, year-to-date rows, chain steps) here and
    in the scenarios that inherit from this one, and their overrides of it;
    other lots' refill or overflow settings that name it are cleared. Those
    rows are listed, and you're asked first. A chain whose only step is the
    lot is not deleted with it: change or delete the chain first. An account
    left with no lots loads with an empty default lot, as such an account
    always has.

    A lot inherited from a parent scenario can't be removed from here, so it is
    zeroed instead: an override with no balance, shares or basis and no balance
    management, which these commands treat as deleted. Rows anywhere in the
    scenario's lineage that still name it are listed, for checking; they go on
    naming an empty lot.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "account_lots.csv"
    chosen = _select_lot(_scenario_lots(scenario_dir, scenario_name), account, lot)
    label = f"lot {chosen.lot!r} of {chosen.account!r}"

    if chosen.inherited_from is None:
        emptied = _emptied_chains(scenario_dir, scenario_name, chosen.account, chosen.lot)
        if emptied:
            raise click.ClickException(
                f"{label} is the only step of {', '.join(emptied)}, which would be left with "
                "no steps; change or delete that chain first. Nothing was written."
            )
        references = _lot_family_references(scenario_dir, scenario_name, chosen.account, chosen.lot)
        if references:
            click.echo(f"{label} is referenced by:")
            click.echo("\n".join(references))
            if not yes and not click.confirm("delete the lot and these rows?", default=False):
                click.echo("aborted")
                return
        _write_lot(
            data_dir_p, scenario_name, path, None, replace=chosen,
            delete_references=(chosen.account, chosen.lot),
        )
        click.echo(f"deleted {label} from {path}")
        if not any(one.account == chosen.account
                   for one in _scenario_lots(scenario_dir, scenario_name)):
            click.echo(
                f"{chosen.account!r} has no lots left, so it loads with an empty "
                f"{registries.DEFAULT_LOT!r} lot, as an account without lots does"
            )
        return

    if chosen.deleted:
        click.echo(f"{label} is already deleted (zeroed {chosen.deleted_where}); nothing was written")
        return
    zeroed = {
        **chosen.row,
        **dict.fromkeys(registries.CASH_MANAGEMENT_COLUMNS, ""),
        "opening_balance": "0.00",
    }
    # Kept set where they were set: the loader wants prior_year_end_balance
    # on every lot of an account or on none.
    for column, zero in (("quantity", "0"), ("opening_cost_basis", "0.00"),
                         ("prior_year_end_balance", "0.00")):
        if zeroed[column].strip():
            zeroed[column] = zero
    _write_lot(data_dir_p, scenario_name, path, zeroed, replace=chosen)
    click.echo(
        f"zeroed {label} in {path}: it comes from scenario {chosen.inherited_from!r}, "
        "so an empty override stands in for deleting it"
    )
    # Down the whole family: the parents' rows still run, and so do those of
    # the scenarios inheriting the zeroed lot from this one.
    family = [*load_order(scenario_dir.parent, scenario_name),
              *_lot_descendants(scenario_dir.parent, scenario_name)]
    references = [
        f"  {name}: {line.strip()}"
        for name in family
        for line in _lot_reference_rows(_lineage_dir(scenario_dir, name), chosen.account, chosen.lot)
    ]
    if references:
        click.echo("still named by:")
        click.echo("\n".join(references))


@account_group_command.command("check")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def check_account_lots(
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Check for account_lots alignment issues (missing, duplicate, unknown accounts)."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scen_name = _resolve_scenario_dir(data_dir_p, scenario)

    accounts_path = scenario_dir / "accounts.csv"
    values_path = scenario_dir / "account_lots.csv"

    if not accounts_path.exists():
        raise click.ClickException(f"accounts.csv not found: {accounts_path}")
    if not values_path.exists():
        raise click.ClickException(f"account_lots.csv not found: {values_path}")

    # Read accounts
    account_names = set()
    with accounts_path.open(newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        if reader.fieldnames is None or "name" not in reader.fieldnames:
            raise click.ClickException("accounts.csv missing 'name' column")
        for row in reader:
            if row.get("name"):
                account_names.add(row["name"])

    # Read account_lots
    values_accounts = {}
    seen_lots: set[tuple[str, str]] = set()
    with values_path.open(newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        if reader.fieldnames is None or "account_name" not in reader.fieldnames:
            raise click.ClickException("account_lots.csv missing 'account_name' column")
        for i, row in enumerate(reader, start=2):
            acct = row.get("account_name", "")
            if (acct, row.get("lot", "")) in seen_lots:
                click.echo(
                    f"  warning: duplicate account_name and lot '{acct}' at row {i}", err=True
                )
            seen_lots.add((acct, row.get("lot", "")))
            values_accounts.setdefault(acct, i)
            if acct and acct not in account_names:
                click.echo(f"  warning: unknown account_name '{acct}' at row {i}", err=True)

    missing = account_names - set(values_accounts.keys())
    if missing:
        click.echo(f"missing account_lots rows for: {', '.join(sorted(missing))}", err=True)
    if not missing and not any(v for k, v in values_accounts.items() if k not in account_names):
        click.echo("ok")


@account_group_command.command("sync")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def sync_account_lots(
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Sync account_lots.csv: create missing rows for accounts (with zero opening_balance)."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scen_name = _resolve_scenario_dir(data_dir_p, scenario)

    accounts_path = scenario_dir / "accounts.csv"
    values_path = scenario_dir / "account_lots.csv"

    if not accounts_path.exists():
        raise click.ClickException(f"accounts.csv not found: {accounts_path}")
    if not values_path.exists():
        raise click.ClickException(f"account_lots.csv not found: {values_path}")

    # Read accounts
    account_names = []
    account_subkinds: dict[str, str] = {}
    with accounts_path.open(newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        if reader.fieldnames is None or "name" not in reader.fieldnames:
            raise click.ClickException("accounts.csv missing 'name' column")
        for row in reader:
            if row.get("name"):
                account_names.append(row["name"])
                account_subkinds[row["name"]] = row.get("subkind", "")

    # Read existing account_lots
    values_accounts = set()
    values_rows = []
    with values_path.open(newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        if reader.fieldnames is None or "account_name" not in reader.fieldnames:
            raise click.ClickException("account_lots.csv missing 'account_name' column")
        values_header = list(reader.fieldnames)
        for row in reader:
            values_rows.append(row)
            if row.get("account_name"):
                values_accounts.add(row["account_name"])

    # Find missing. A retirement account's lot must say Roth or traditional,
    # which sync can't know, so those are left to `sim account lot add`.
    missing = [name for name in account_names if name not in values_accounts]
    needs_subkind = [name for name in missing
                     if account_subkinds.get(name) == registries.RETIREMENT_SUBKIND]
    missing = [name for name in missing if name not in needs_subkind]
    for name in needs_subkind:
        click.echo(f"  skipped {name}: a retirement account's lot needs a subkind; "
                   f"add it with `sim account lot add {name} default --subkind ...`", err=True)
    if not missing:
        if not needs_subkind:
            click.echo("ok (no missing accounts)")
        return

    # Create rows for missing accounts
    for name in missing:
        values_rows.append({
            "account_name": name,
            "lot": registries.DEFAULT_LOT if account_subkinds.get(name) in INVESTMENT_ASSET_SUBKINDS else "",
            "opening_balance": "0.00",
            "opening_cost_basis": "",
            "prior_year_end_balance": "",
        })

    # Write back
    with _scenario_edit(data_dir_p, scen_name) as edit:
        with edit.path(values_path).open("w", newline="", encoding="utf-8") as f:
            writer = _csv.DictWriter(f, fieldnames=values_header, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(values_rows)

    click.echo(f"synced {len(missing)} missing account(s) to {values_path}")
    for name in missing:
        click.echo(f"  + {name}")


@account_group_command.command("import-quicken")
@click.argument("quicken_file",
                type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--map-file", "map_file",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="quicken_name -> account_name mapping CSV "
                   "(default: <scenario>/quicken_account_map.csv).")
@click.option("--year-end", is_flag=True, default=False,
              help="Treat quicken_file as a Quicken \"Year End Export\" and write "
                   "prior_year_end_balance instead of opening_balance, for "
                   "investment asset accounts only.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would change without writing.")
def import_quicken(
    quicken_file: Path,
    map_file: Path | None,
    year_end: bool,
    scenario: str | None,
    data_dir: Path | None,
    dry_run: bool,
) -> None:
    """Update account_lots.csv from a Quicken account-tree report export.

    Without ``--year-end``, sets ``opening_balance`` from a "Net Worth Export"
    (and seeds ``prior_year_end_balance`` only when it's still blank, and only
    for investment asset accounts). With ``--year-end``, treats the file as a
    "Year End Export" and always overwrites ``prior_year_end_balance`` for
    investment asset accounts, leaving ``opening_balance`` untouched.

    Every account in ``quicken_file`` must appear in the mapping CSV, either
    mapped to an ``account_name`` or marked ``skip`` -- this catches
    new/renamed Quicken accounts instead of silently ignoring them.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scen_name = _resolve_scenario_dir(data_dir_p, scenario)

    map_path = map_file or (scenario_dir / "quicken_account_map.csv")
    if not map_path.exists():
        raise click.ClickException(f"mapping file not found: {map_path}")

    accounts_path = scenario_dir / "accounts.csv"
    values_path = scenario_dir / "account_lots.csv"
    if not accounts_path.exists():
        raise click.ClickException(f"accounts.csv not found: {accounts_path}")
    if not values_path.exists():
        raise click.ClickException(f"account_lots.csv not found: {values_path}")

    account_info: dict[str, tuple[str, str]] = {}
    with accounts_path.open(newline="", encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            if row.get("name"):
                account_info[row["name"]] = (row.get("kind", ""), row.get("subkind", ""))

    try:
        balances = parse_net_worth_export(quicken_file)
        account_map = load_account_map(map_path)
    except ValueError as e:
        raise click.ClickException(str(e)) from None

    unmapped: list[str] = []
    unknown_target: list[str] = []
    skipped: list[str] = []
    ineligible: list[str] = []
    updates: dict[str, int] = {}

    for bal in balances:
        entry = account_map.get(bal.quicken_name)
        if entry is None:
            unmapped.append(bal.quicken_name)
            continue
        if entry.action == "skip":
            skipped.append(bal.quicken_name)
            continue
        info = account_info.get(entry.account_name)
        if info is None:
            unknown_target.append(f"{bal.quicken_name} -> {entry.account_name}")
            continue
        kind, subkind = info

        if year_end:
            if not is_investment_asset(kind, subkind):
                ineligible.append(entry.account_name)
                continue
            updates[entry.account_name] = bal.cents
            continue

        magnitude = -bal.cents if kind == "liability" else bal.cents
        if kind == "liability" and magnitude < 0:
            click.echo(
                f"warning: {entry.account_name!r} has an unusual credit balance "
                "in Quicken; storing magnitude only", err=True,
            )
            magnitude = abs(magnitude)
        updates[entry.account_name] = magnitude

    if unmapped:
        raise click.ClickException(
            f"unmapped Quicken account(s); add to {map_path}:\n  "
            + "\n  ".join(sorted(unmapped))
        )
    if unknown_target:
        raise click.ClickException(
            "mapping target(s) not found in accounts.csv:\n  "
            + "\n  ".join(sorted(unknown_target))
        )

    df = pd.read_csv(values_path, dtype=str, keep_default_na=False)
    if "account_name" not in df.columns:
        raise click.ClickException(f"account_lots.csv missing 'account_name' column: {values_path}")
    known = set(df["account_name"])

    changed = 0
    for name, cents in updates.items():
        if name not in known:
            raise click.ClickException(
                f"mapped account not found in account_lots.csv: {name!r}"
            )
        dollars = format_dollars(cents)
        rows = df.index[df["account_name"] == name]
        if len(rows) > 1:
            click.echo(
                f"warning: {name!r} has {len(rows)} lots; import-quicken only "
                "updates single-lot accounts, so it was skipped", err=True,
            )
            continue
        idx = rows[0]

        if year_end:
            old = df.at[idx, "prior_year_end_balance"]
            if old != dollars:
                click.echo(f"  {name}: prior_year_end_balance {old or '(none)'} -> {dollars}")
                changed += 1
            df.at[idx, "prior_year_end_balance"] = dollars
            continue

        old = df.at[idx, "opening_balance"]
        if old != dollars:
            click.echo(f"  {name}: opening_balance {old or '(none)'} -> {dollars}")
            changed += 1
        df.at[idx, "opening_balance"] = dollars

        kind, subkind = account_info[name]
        if is_investment_asset(kind, subkind) and not df.at[idx, "prior_year_end_balance"]:
            df.at[idx, "prior_year_end_balance"] = dollars
            click.echo(f"  {name}: prior_year_end_balance (none) -> {dollars} [seeded from opening]")

    if dry_run:
        click.echo(
            f"(dry run) {changed} account(s) would change, "
            f"{len(skipped)} skipped, {len(ineligible)} ineligible, "
            f"{len(updates) - changed} unchanged"
        )
        return

    with _scenario_edit(data_dir_p, scen_name) as edit:
        df.to_csv(edit.path(values_path), index=False)
    click.echo(
        f"updated {changed} account(s) in {values_path} "
        f"({len(skipped)} skipped, {len(ineligible)} ineligible, "
        f"{len(updates) - changed} unchanged)"
    )


@transfer_group_command.command("list")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def list_transfers(scenario: str | None, data_dir: Path | None) -> None:
    """List one-time transfers: calendar one_off_transfer and transfer_balance
    events, and scheduled rows with cadence one_off."""
    from rich.console import Console
    from rich.table import Table

    data_dir_p = _resolve_data_dir(data_dir)
    with open_intent_db(data_dir_p, _resolve_scenario(data_dir_p, scenario).name) as (con, scen_name):
        table = Table(title=f"transfers ({data_dir_p.name} / {scen_name})")
        table.add_column("Date")
        table.add_column("Source")
        table.add_column("From")
        table.add_column("To")
        table.add_column("Amount", justify="right")
        table.add_column("Tax")
        table.add_column("Description")

        rows: list[tuple[str, ...]] = []
        for row in con.execute(
            "SELECT ce.date, ce.kind, ce.amount, "
            "       COALESCE(ce.tax_character, '') AS tax_character, "
            "       COALESCE(ce.description, '') AS description, "
            "       ce.account_1 AS chain_1, a1.name AS name_1, a1.subkind AS subkind_1, "
            "       m1.lot AS lot_1, "
            "       ce.account_2 AS chain_2, a2.name AS name_2, a2.subkind AS subkind_2, "
            "       m2.lot AS lot_2 "
            "FROM calendar_event ce "
            "LEFT JOIN chain c1 ON c1.name = ce.account_1 "
            "LEFT JOIN account_lot m1 ON m1.id = c1.lot_id "
            "LEFT JOIN account a1 ON a1.id = c1.account_id "
            "LEFT JOIN chain c2 ON c2.name = ce.account_2 "
            "LEFT JOIN account_lot m2 ON m2.id = c2.lot_id "
            "LEFT JOIN account a2 ON a2.id = c2.account_id "
            "WHERE ce.kind IN ('one_off_transfer', 'transfer_balance') "
        ):
            if row["kind"] == "transfer_balance":
                amount = "balance"  # whatever the lot holds when it fires
            elif row["amount"] is not None:
                amount = _fmt_cents(row["amount"]).strip()
            else:
                amount = ""
            rows.append((
                row["date"], "calendar",
                _account_with_lot(row["name_1"] or row["chain_1"], row["subkind_1"], row["lot_1"]),
                _account_with_lot(row["name_2"] or row["chain_2"], row["subkind_2"], row["lot_2"]),
                amount,
                row["tax_character"], row["description"],
            ))
        for row in con.execute(
            "SELECT s.start AS date, s.amount_cents, s.amount_rule_kind, s.tax_character, "
            "       COALESCE(s.description, s.name) AS description, "
            "       c1.name AS chain_1, a1.name AS name_1, a1.subkind AS subkind_1, m1.lot AS lot_1, "
            "       c2.name AS chain_2, a2.name AS name_2, a2.subkind AS subkind_2, m2.lot AS lot_2 "
            "FROM scheduled_transaction s "
            "JOIN chain c1 ON c1.id = s.from_account "
            "LEFT JOIN account_lot m1 ON m1.id = c1.lot_id "
            "LEFT JOIN account a1 ON a1.id = c1.account_id "
            "JOIN chain c2 ON c2.id = s.to_account "
            "LEFT JOIN account_lot m2 ON m2.id = c2.lot_id "
            "LEFT JOIN account a2 ON a2.id = c2.account_id "
            "WHERE s.cadence = 'one_off' AND s.created_by_event_id IS NULL"
        ):
            if row["amount_cents"] is not None:
                amt = _fmt_cents(row["amount_cents"]).strip()
            else:
                amt = row["amount_rule_kind"]
            rows.append((
                row["date"], "scheduled",
                _account_with_lot(row["name_1"] or row["chain_1"], row["subkind_1"], row["lot_1"]),
                _account_with_lot(row["name_2"] or row["chain_2"], row["subkind_2"], row["lot_2"]),
                amt, row["tax_character"], row["description"],
            ))
        for record in sorted(rows, key=lambda r: (r[0], r[1])):
            table.add_row(*record)

    Console().print(table)



# ---------- tags: cross-type listing ----------


def _scenario_write(
    data_dir: Path | None,
    scenario: str | None,
    item_type: ItemType,
    item_name: str | None,
    tag_tokens: tuple[str, ...],
    op: str,  # 'add' | 'rm'
) -> None:
    read, what = ((names.account_names, "account") if item_type == "account"
                  else (names.scheduled_names, "scheduled rule"))

    def existing() -> list[str]:
        data = _resolve_data_dir(data_dir)
        return read(data, _resolve_scenario(data, scenario).name)

    item_name, tag_tokens = _name_and_tags(item_name, tag_tokens, existing)
    if item_name is None or not tag_tokens:
        data = _resolve_data_dir(data_dir)
        scenario_name = _resolve_scenario(data, scenario).name
        if item_name is None:
            item_name = _ask_name(what, read(data, scenario_name))
        tag_tokens = _ask_tags(tag_tokens, names.tag_tokens(data, scenario_name))
    try:
        registries.parse_tags(" ".join(tag_tokens))
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    data_dir_p = _resolve_data_dir(data_dir)
    with _scenario_edit(data_dir_p, scenario) as edit:
        try:
            with open_scenario(edit.data_dir, edit.name) as sc:
                method = sc.add_tags if op == "add" else sc.remove_tags
                new_val = method(item_type, item_name, tag_tokens)
        except (FileNotFoundError, KeyError, ValueError) as e:
            raise click.ClickException(str(e)) from None
    click.echo(f"{item_type} '{item_name}' tags: {new_val or '(none)'}")


# ---------- scenario: create derived scenarios ----------


@sim_group_command.group(name="prices")
def prices_group_command() -> None:
    """Security prices for the lots that have a quantity.

    `sim symbol add` adds a new symbol, fetching its prices with its name and
    allocation; `sim prices show` shows what is stored. For one the provider has no prices for, ask an AI assistant
    for its month-end prices or type them into
    prices/security_price__<SYMBOL>.csv; see the user guide, Part 4.11 "Add a
    stock or fund".
    """


@prices_group_command.command("fetch")
@click.argument("symbols", type=completion.SYMBOL, nargs=-1)
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)")
@click.option("--since", "since_text", default=None, metavar="YYYY-MM-DD",
              help="Earliest date to fetch (default: the December before the earliest scenario start's year).")
@click.option("--provider", "provider_name", default="yahoo", show_default=True,
              type=click.Choice(sorted(prices.PRICE_PROVIDERS)), help="Where prices come from.")
@click.option("--refresh", is_flag=True,
              help="Overwrite prices already in the files (default: only add new dates).")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing files.")
def prices_fetch_command(
    symbols: tuple[str, ...], data_dir: Path | None,
    since_text: str | None, provider_name: str, refresh: bool, dry_run: bool,
) -> None:
    """Fetch prices into prices/security_price__<SYMBOL>.csv.

    With no SYMBOLS, fetches every symbol the data repo knows: those on a lot
    with a quantity in any scenario, in securities.csv or allocations.csv, or
    with a prices/ file already (CASH and CD are skipped). Stores each month's
    last close since the December before the earliest scenario start's year
    (so a prior year-end balance can be derived), plus the latest close. Only
    the ticker symbols are sent to the provider; they are upper-cased first.

    A symbol the provider doesn't have fails, unless no lot holds it and you
    didn't name it: then it is only reported, so a fund whose prices you type
    in yourself doesn't fail every fetch. See the user guide, Part 4.11 "Add a
    stock or fund", for other ways to get its prices. `sim prices show` shows
    what was stored.
    """
    data = _resolve_data_dir(data_dir)
    scenarios_dir = data / "scenarios"
    if not scenarios_dir.is_dir():
        raise click.ClickException(f"no scenarios/ in {data}")
    held = prices.prices_in_use(scenarios_dir).symbols
    chosen = _upper(symbols) or sorted({*held, *prices.known_symbols(data)})
    if not chosen:
        click.echo("no symbol in securities.csv, allocations.csv, prices/ or on a lot; nothing to fetch")
        return
    optional = set() if symbols else set(chosen) - set(held)
    failed = _fetch_prices(data, chosen, since_text, provider_name,
                           refresh=refresh, dry_run=dry_run, optional=optional)
    if dry_run:
        click.echo("dry run: no files written")
    if failed:
        raise click.ClickException(f"{failed} of {len(chosen)} symbol(s) failed")


def _upper(symbols: tuple[str, ...]) -> list[str]:
    """Typed symbols as tickers: upper case, each once, in the order given."""
    return list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))


def _fetch_prices(
    data: Path, symbols: list[str], since_text: str | None, provider_name: str,
    *, refresh: bool = False, dry_run: bool = False, optional: Set[str] = frozenset(),
) -> int:
    """Fetch ``symbols``' prices into ``data``'s prices/, echoing a line per
    symbol; returns how many failed. A symbol in ``optional`` that fails is
    reported but not counted."""
    use = prices.prices_in_use(data / "scenarios")
    today = datetime.now(UTC).date()
    try:
        since = (date.fromisoformat(since_text) if since_text
                 else prices.default_since(use.earliest_start, today))
    except ValueError:
        raise click.ClickException(f"--since {since_text!r} is not a YYYY-MM-DD date") from None
    provider = prices.PRICE_PROVIDERS[provider_name]()
    results = prices.fetch_prices(
        data / "prices", symbols, provider, since=since, today=today, refresh=refresh, dry_run=dry_run,
    )
    failed = 0
    for r in results:
        if r.error and r.symbol in optional:
            click.echo(f"{r.symbol}: {r.error} (no lot holds it, so not counted as a failure)", err=True)
            continue
        if r.error:
            failed += 1
            click.echo(f"{r.symbol}: {r.error}", err=True)
            continue
        assert r.latest is not None  # set whenever there is no error
        day, price = r.latest
        change = f"{r.added} added" + (f", {r.changed} changed" if r.changed else "")
        click.echo(f"{r.symbol}: {change} (latest {prices.format_price(price)} on {day})")
    return failed


@prices_group_command.command("show")
@click.argument("symbols", type=completion.SYMBOL, nargs=-1)
@click.option("--since", "since_text", default=None, metavar="YYYY-MM-DD",
              help="With SYMBOLS, show only prices from this date on.")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)")
def prices_show_command(symbols: tuple[str, ...], since_text: str | None, data_dir: Path | None) -> None:
    """Show the prices stored in prices/.

    With no SYMBOLS, one row per symbol: how many prices are stored, the first
    and latest date, the latest price, and how far it has moved since the end
    of the month before and since the end of last year. A symbol with no
    prices is shown in red if a lot holds it with a quantity (so its balance
    needs a price) or securities.csv or allocations.csv names it; a lot with
    only a balance needs none. With SYMBOLS, each one's
    stored prices, a row per date, with the change from the row before.

    These are the shared prices/ files; `sim symbol list` notes a scenario
    with its own file for a symbol.
    """
    from rich.console import Console
    from rich.table import Table

    data = _resolve_data_dir(data_dir)
    prices_dir = data / "prices"
    console = Console()

    def change(v):
        return "" if v is None or pd.isna(v) else f"{v * 100:+.1f}%"

    if not symbols:
        if since_text:
            raise click.ClickException("--since needs a SYMBOL")
        scenarios_dir = data / "scenarios"
        held = prices.prices_in_use(scenarios_dir).symbols if scenarios_dir.is_dir() else []
        frame = prices.price_summary(prices_dir, {*held, *prices.known_symbols(data)})
        if frame.empty:
            click.echo("no prices in prices/, and no symbol in securities.csv, allocations.csv or on a lot")
            return
        table = Table(title="stored prices", header_style="bold")
        for name, justify in (("Symbol", "left"), ("Prices", "right"), ("From", "left"),
                              ("Latest", "left"), ("Latest price", "right"),
                              ("Since month-end", "right"), ("Since year-end", "right")):
            table.add_column(name, justify=justify, no_wrap=True)
        for r in named_rows(frame):
            if not r.prices:
                table.add_row(r.symbol, "[red]none[/red]", "", "", "", "", "")
                continue
            table.add_row(r.symbol, str(r.prices), str(r.first_date), str(r.latest_date),
                          prices.format_price(r.latest_price), change(r.month_change),
                          change(r.year_change))
        console.print(table)
        return

    try:
        since = date.fromisoformat(since_text) if since_text else None
    except ValueError:
        raise click.ClickException(f"--since {since_text!r} is not a YYYY-MM-DD date") from None
    missing = []
    for symbol in _upper(symbols):
        history = prices.price_history(prices_dir, symbol, since)
        if history is None:
            missing.append(symbol)
            click.echo(f"{symbol}: no prices/security_price__{symbol}.csv; "
                       f"`sim prices fetch {symbol}` gets them", err=True)
            continue
        table = Table(title=f"{symbol} prices", header_style="bold")
        for name in ("Date", "Price", "Change"):
            table.add_column(name, justify="left" if name == "Date" else "right", no_wrap=True)
        for r in named_rows(history):
            table.add_row(str(r.effective_date), prices.format_price(r.price), change(r.change))
        if not len(history):
            table.caption = f"no prices since {since}"
        console.print(table)
    if missing:
        raise click.ClickException(f"no prices for {', '.join(missing)}")


@prices_group_command.command("check")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)")
@click.option("--tolerance", type=float, default=1.0, show_default=True,
              help="Percent difference between a stated balance and quantity x price that is flagged.")
def prices_check_command(scenario: str | None, data_dir: Path | None, tolerance: float) -> None:
    """Compare each lot's stated balance with quantity x price.

    For lots with a symbol and a quantity, shows the balance in account_lots.csv
    against quantity times the price on or before the scenario start (they should
    agree, so a difference points at stale or wrong data) and against the latest
    price (how far the value has moved since). Exits nonzero if any lot is flagged.
    """
    from rich.console import Console
    from rich.table import Table

    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, scenario)
    with open_scenario(data, ref.name, scenarios_dir=ref.scenarios_dir) as sc:
        frame = prices.check_prices(sc, tolerance / 100)
    if frame.empty:
        click.echo(f"no lot with a symbol and a quantity in {ref.name}")
        return

    def money(v):
        return "" if pd.isna(v) else f"${v:,.2f}"

    def price(v, when):
        return "" if pd.isna(v) else f"{v:,.4f} ({when})"

    table = Table(title=f"stated balance vs quantity x price ({ref.name})", header_style="bold")
    for name, justify in (("Account / lot", "left"), ("Symbol", "left"), ("Quantity", "right"),
                          ("Balance", "right"), ("Start price", "right"), ("Value", "right"),
                          ("Diff", "right"), ("Latest price", "right"), ("Latest value", "right"),
                          ("", "left")):
        table.add_column(name, justify=justify, no_wrap=True)
    for r in named_rows(frame):
        diff = "" if pd.isna(r.difference) else f"{r.difference * 100:+.1f}%"
        flag = r.flag if isinstance(r.flag, str) else ""
        table.add_row(
            f"{r.account} ({r.lot})", r.symbol, f"{r.quantity:,.3f}", money(r.balance),
            price(r.price, r.price_date), money(r.value), diff,
            price(r.latest_price, r.latest_date), money(r.latest_value), flag,
            style="red" if flag else None,
        )
    Console().print(table)
    flagged = int(frame["flag"].notna().sum())
    if flagged:
        raise click.ClickException(f"{flagged} lot(s) flagged")


@sim_group_command.group(name="chain")
def chain_group_command() -> None:
    """See the scenario's chains: where money is drawn from, or swept to.

    A chain is an ordered list of steps, each an account's lot or another
    chain. See docs/guides/chains.md for how the steps, a lot's minimum and
    maximum, refill and overflow fit together.
    """


@chain_group_command.command("list")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)")
def chain_list_command(scenario: str | None, data_dir: Path | None) -> None:
    """List the chains with their steps, where each is defined, and what uses it.

    Only chains written in a chain file are listed; every lot is also a
    one-step chain of its own, usable wherever a chain is.
    """
    from rich.console import Console
    from rich.markup import escape
    from rich.measure import Measurement
    from rich.table import Table

    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, scenario)
    with open_scenario(data, ref.name, scenarios_dir=ref.scenarios_dir) as scn:
        cmap = chain_map.chain_map(scn)
    if not cmap.steps:
        click.echo(f"no chains in {ref.name}; every lot is still a one-step chain of its own")
        return
    table = Table(title=f"chains ({ref.name})", header_style="bold", show_lines=True)
    for column in ("Chain", "Role", "Steps", "Defined in", "Used by"):
        table.add_column(column)
    for name, steps in sorted(cmap.steps.items()):
        try:
            defined = chain_edit.defining_scenario(ref.scenarios_dir, ref.name, name) or ""
            where = "this scenario" if defined == ref.name else escape(defined)
        except chain_edit.AmbiguousChain as exc:
            where = f"[red]{escape(str(exc))}[/red]"
        uses = cmap.uses.get(name, [])
        # Names come from the data and may hold '[', so only "nothing" and a
        # clash are markup.
        table.add_row(
            escape(name), escape(cmap.roles[name]),
            escape("\n".join(f"{s.order}. {cmap.label(s.target)}: {chain_map.share_text(s)}" for s in steps)),
            where,
            escape("\n".join(chain_map.use_text(cmap, u) for u in uses)) or "[red]nothing[/red]",
        )
    console = Console()
    # Wrap long steps rather than cropping names, as `sim allocation` does.
    needed = Measurement.get(console, console.options.update(max_width=10_000), table).maximum
    if needed > console.width:
        console = Console(width=min(needed, max(console.width, 160)))
    console.print(table)


@chain_group_command.command("show")
@click.argument("names", type=completion.ENDPOINT, nargs=-1)
@click.option("--mermaid", "as_mermaid", is_flag=True,
              help="Print the flowchart's Mermaid source (to paste into markdown) instead of a tree.")
@click.option("--graph", "as_graph", is_flag=True,
              help="Draw the flowchart in the terminal (kitty or iTerm2 image protocol), "
                   "or write it to a PNG file when the terminal can't show images.")
@click.option("--graph-file", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the flowchart to this .png or .svg file instead of showing it.")
@click.option("--rules", "with_rules", is_flag=True,
              help="With --mermaid, --graph or --graph-file, also draw the rules and calendar "
                   "transfers that use each chain.")
@click.option("--depth", type=click.Choice(chain_map.DEPTHS), default=None,
              help="With --mermaid, --graph or --graph-file, how far to follow each chain: "
                   "1 step, as far as the first lot (lots), or all the way (all, the default).")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)")
def chain_show_command(
    names: tuple[str, ...], as_mermaid: bool, as_graph: bool, graph_file: Path | None,
    with_rules: bool, depth: chain_map.Depth | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Show chains as trees: each step, what share it takes, and its lot's policy.

    NAMES are chains, or accounts to show the chains that reach them; with
    none, shows every chain. A step naming another chain is nested under it,
    like a parenthesized expression. Each lot shows the minimum it keeps, the
    chain it refills from, and the maximum it holds before overflowing.
    """
    from rich.console import Console

    if sum([as_mermaid, as_graph, graph_file is not None]) > 1:
        raise click.UsageError("use one of --mermaid, --graph and --graph-file")
    if depth is not None and not (as_mermaid or as_graph or graph_file is not None):
        raise click.UsageError("--depth needs --mermaid, --graph or --graph-file")
    _check_graph_file(graph_file)
    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, scenario)
    with open_scenario(data, ref.name, scenarios_dir=ref.scenarios_dir) as scn:
        cmap = chain_map.chain_map(scn)
    roots: list[str] = []
    for name in names:
        if cmap.is_interior(name):
            roots += [name] if name not in roots else []
            continue
        leaves = [leaf for leaf, (account, _lot) in cmap.leaves.items() if account == name or leaf == name]
        if not leaves:
            raise click.ClickException(f"{name} is neither a chain nor an account in {ref.name}")
        reaching = [chain for chain in cmap.steps
                    if any(leaf in cmap.descendants(chain) for leaf in leaves)]
        for leaf in leaves:
            policy = cmap.policies.get(leaf)
            if policy is not None:
                reaching += [c for c in (policy.refill_chain, policy.overflow_chain) if c]
        if not reaching:
            raise click.ClickException(f"no chain reaches {name} in {ref.name}")
        # Several lots may share a refill or overflow chain; show it once.
        roots += [chain for chain in dict.fromkeys(reaching) if chain not in roots]
    if not names:
        roots = sorted(cmap.steps)
    if not roots:
        click.echo(f"no chains in {ref.name}; every lot is still a one-step chain of its own")
        return
    if as_mermaid:
        click.echo(chain_map.mermaid(cmap, roots, rules=with_rules, depth=depth or "all"))
        return
    if as_graph or graph_file is not None:
        _draw_chain_graph(chain_map.mermaid(cmap, roots, rules=with_rules, depth=depth or "all"), graph_file)
        return
    console = Console()
    for i, root in enumerate(roots):
        if i:
            console.print()
        console.print(chain_map.chain_tree(cmap, root))
        for use in cmap.uses.get(root, []):
            console.print(f"  {chain_map.use_text(cmap, use)}", style="dim", markup=False)


def _draw_chain_graph(source: str, graph_file: Path | None) -> None:
    """Draw the Mermaid ``source`` into ``graph_file``, or show it in the
    terminal; a terminal that can't show images gets a PNG file instead,
    and a pointer to one that can."""
    from analysis.sim import chain_graph

    protocol = chain_graph.terminal_protocol() if graph_file is None else None
    # Draw for the terminal's own cells, so the chart's text matches the
    # terminal's on a Retina screen and off it.
    cell = chain_graph.cell_size() if protocol else None
    # As wide as the terminal, short of its last column, which some
    # terminals wrap at.
    fit_width = (shutil.get_terminal_size().columns - 1) * cell[0] if cell else None
    try:
        image = chain_graph.render(source, _graph_format(graph_file),
                                   scale=chain_graph.terminal_scale(cell), fit_width=fit_width)
    except chain_graph.GraphError as error:
        raise click.ClickException(str(error)) from error
    _show_image(image, graph_file, protocol, cell, "chain-")


def _draw_figure(
    fig: Any, graph_file: Path | None, prefix: str,
    fit: Callable[[tuple[float, float]], Any] | None = None,
) -> None:
    """Draw the matplotlib figure ``fig`` as ``_draw_chain_graph`` draws a
    chart: into ``graph_file``, else in the terminal, else into a PNG file
    named with ``prefix``. For the terminal, at the resolution that makes the
    figure's text the size of the terminal's own; and when ``fit`` is given
    and the terminal reports its size, the figure ``fit`` draws at the
    terminal's size in inches instead (``_terminal_figure_size``)."""
    from analysis.sim import chain_graph
    from analysis.sim.ensemble_plots import figure_bytes

    protocol = chain_graph.terminal_protocol() if graph_file is None else None
    cell = chain_graph.cell_size() if protocol else None
    if fit is not None and cell is not None:
        fig = fit(_terminal_figure_size(cell))
    try:
        image = figure_bytes(fig, _graph_format(graph_file), dpi=_figure_dpi(cell))
    except (OSError, ValueError) as error:
        raise click.ClickException(f"couldn't draw the chart: {error}") from error
    _show_image(image, graph_file, protocol, cell, prefix)


# Matplotlib's text is 10pt; a terminal cell is about 1.2 times its font's height.
_FIGURE_FONT_PT = 10
_LINE_HEIGHT = 1.2


# Terminal lines left free under a chart drawn to fit, for the shell's prompt.
_PROMPT_LINES = 3


def _terminal_figure_size(cell: tuple[float, float]) -> tuple[float, float]:
    """The terminal's width, and its height less room for the prompt, in
    inches at ``_figure_dpi(cell)``: the size of a figure that fills it."""
    size = shutil.get_terminal_size()
    dpi = _figure_dpi(cell)
    return (size.columns * cell[0] / dpi,
            max(1, size.lines - _PROMPT_LINES) * cell[1] / dpi)


def _figure_dpi(cell: tuple[float, float] | None) -> float:
    """The resolution that draws a figure's 10pt text as tall as the
    terminal's text, ``cell`` being its cell size in the pixels it draws
    images in; twice matplotlib's 100 dpi when the terminal doesn't say."""
    if cell is None:
        return 200.0
    return min(400.0, max(72.0, cell[1] / _LINE_HEIGHT * 72 / _FIGURE_FONT_PT))


def _graph_format(graph_file: Path | None) -> Literal["png", "svg"]:
    return "svg" if graph_file is not None and graph_file.suffix.lower() == ".svg" else "png"


def _check_graph_file(graph_file: Path | None) -> None:
    if graph_file is not None and graph_file.suffix.lower() not in (".png", ".svg"):
        raise click.UsageError(f"--graph-file {graph_file} must end in .png or .svg")


def _show_image(
    image: bytes, graph_file: Path | None, protocol: str | None,
    cell: tuple[float, float] | None, prefix: str,
) -> None:
    """Write ``image`` to ``graph_file``; else show it in the terminal with
    ``protocol``; else, when the terminal takes no image protocol, write it
    to a temporary PNG named with ``prefix`` and say where."""
    from analysis.sim import chain_graph

    if graph_file is not None:
        try:
            graph_file.write_bytes(image)
        except OSError as error:
            raise click.ClickException(f"couldn't write {graph_file}: {error}") from error
        click.echo(f"wrote {graph_file}")
        return
    if protocol is None:
        name = None
        try:
            with tempfile.NamedTemporaryFile(prefix=prefix, suffix=".png", delete=False) as f:
                name = f.name
                f.write(image)
        except OSError as error:
            if name is not None:
                Path(name).unlink(missing_ok=True)  # don't leave half a chart behind
            raise click.ClickException(f"couldn't write the chart to a temporary file: {error}") from error
        click.echo(f"wrote {name}")
        click.echo("This terminal can't show images. iTerm2, kitty, Ghostty, WezTerm or "
                   "VS Code's terminal draws the chart in place.", err=True)
        return
    columns = chain_graph.fit_columns(chain_graph.png_size(image)[0],
                                      shutil.get_terminal_size().columns, cell and cell[0])
    if protocol == "kitty":
        click.echo(chain_graph.kitty_sequence(image, columns), nl=False)
        chain_graph.swallow_replies()
    else:
        click.echo(chain_graph.iterm_sequence(image, columns), nl=False)


_STEP_OPTIONS_HELP = {
    "lot": "The account's lot; needed for an investment account, or one with several lots.",
    "percent": "Share of what is still outstanding when the step is reached, e.g. 50% (default: all of it).",
    "maximum": "Most the step supplies or takes, in dollars (default: no limit).",
    "series": "Inflation series that grows --maximum from the scenario start, e.g. cpi.",
    "cadence": "How --series compounds: monthly or annual.",
    "basis_of_lot": "The lot of --basis-of, for an account with several.",
    "basis_of": "Asset whose cost basis the step recovers (a sale) or adds to (a capital improvement).",
    "tax_character": ("Tax character of the money the step places, and of later steps' until another sets "
                      "one; a destination chain's (e.g. home_sale, excluded_gain, ltcg)."),
}

_STEP_OPTION_TYPES = {
    "lot": completion.LotName("account"),
    "basis_of": completion.ACCOUNT,
    "basis_of_lot": completion.LotName("basis_of"),
    "series": completion.SeriesName("inflation"),
}

_STEP_TAX_COLUMNS = chain_edit.TAX_COLUMNS


def _step_options(f):
    for option in ("basis_of_lot", "basis_of", "tax_character", "cadence", "series", "maximum", "percent", "lot"):
        f = click.option(f"--{option.replace('_', '-')}", option, default=None,
                         help=_STEP_OPTIONS_HELP[option], type=_STEP_OPTION_TYPES.get(option))(f)
    return f


def _scenario_options(f):
    f = click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
                     default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)")(f)
    return click.option("--scenario", type=completion.SCENARIO, default=None,
                        help="Scenario folder name (default: the current scenario).")(f)


def _step_row(
    chain: str, account: str | None, lot: str | None, percent: str | None, maximum: str | None,
    series: str | None, cadence: str | None, endpoints: _Endpoints, role: str,
    tax: Mapping[str, str | None] | None = None,
) -> dict[str, str]:
    """One step of a ``role`` chain as its CSV row, checked: a known account
    (and lot) or chain of the same role, and the percent and dollars in the
    forms the loader accepts."""
    if account == chain:
        raise click.ClickException(f"chain {chain!r} can't be a step of itself")
    name, lot_ = _pick_endpoint("step", account, lot, accounts=endpoints.accounts,
                                lot_info=endpoints.lot_info, chains=endpoints.chains_for(role),
                                lot_option="--lot", roles=endpoints.roles, role=role)
    row = {"account_name": name, "lot": lot_, "percent": "", "maximum": "",
           "series": series or "", "cadence": cadence or "",
           **{column: (tax or {}).get(column) or "" for column in _STEP_TAX_COLUMNS}}
    if row["tax_character"] and row["tax_character"] not in registries.TAX_CHARACTERS:
        raise click.ClickException(
            f"unknown tax character {row['tax_character']!r}; "
            f"one of {', '.join(sorted(registries.TAX_CHARACTERS))}")
    try:
        if percent:
            row["percent"] = _step_percent(percent)
        if maximum:
            row["maximum"] = _step_maximum(maximum)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    return row


def _step_percent(text: str) -> str:
    """A step's percent as its CSV cell: blank for 100%, which is the default."""
    micros = units.percent_to_micros(text, require_sign=False)
    if not 0 < micros <= 1_000_000:
        raise ValueError(f"percent {text!r} must be more than 0% and at most 100%")
    return "" if micros == 1_000_000 else units.micros_to_percent(micros)


def _step_maximum(text: str) -> str:
    cents = units.dollars_to_cents(text)
    if cents < 0:
        raise ValueError(f"maximum {text!r} can't be negative")
    return units.cents_to_dollars(cents)


_STEP_CADENCES = {"monthly": "grow it every month", "annual": "grow it once a year"}


def _ask_step_column(text: str, current: str, blank: str, parse: Callable[[str], str],
                     choices: list[str] | None = None) -> str:
    """One column of a guided step: Enter keeps ``current`` (or leaves the
    column blank, meaning ``blank``), '-' clears it, and anything else is
    ``parse``d, asking again until it parses."""
    hint = f"Enter keeps it, - for {blank}" if current else f"blank for {blank}"
    while True:
        answer = _prompt_with_completion(f"  {text} ({hint})", choices=choices, default=current).strip()
        if answer in ("", "-"):
            return ""
        try:
            return parse(answer)
        except ValueError as exc:
            click.echo(f"  {exc}")


def _ask_step_columns(row: dict[str, str], inflation: list[str], given: Set[str] = frozenset()) -> None:
    """Ask a guided step's percent, maximum, and the series and cadence that
    grow the maximum, each starting from ``row``'s value; a column in
    ``given`` came as an option and is kept. Series and cadence are only
    asked once there is a maximum for them to grow, since the loader refuses
    a series without one."""

    def series(answer: str) -> str:
        if answer not in inflation:
            raise ValueError(f"no inflation series {answer!r}; the scenario's: {', '.join(inflation)}")
        return answer

    def cadence(answer: str) -> str:
        if answer not in _STEP_CADENCES:
            raise ValueError(f"one of: {', '.join(_STEP_CADENCES)}")
        return answer

    if "percent" not in given:
        row["percent"] = _ask_step_column(
            "share of what is still outstanding when this step is reached, in %", row.get("percent", ""),
            "all of it", _step_percent)
    if "maximum" not in given:
        row["maximum"] = _ask_step_column(
            "at most how much, in dollars", row.get("maximum", ""), "no limit", _step_maximum)
    if not row["maximum"]:
        # Nothing for a series to grow; one given anyway is left for the
        # loader to refuse by name.
        for column in {"series", "cadence"} - given:
            row[column] = ""
        return
    if "series" not in given and (inflation or row.get("series")):
        row["series"] = _ask_step_column(
            "inflation series that grows the maximum", row.get("series", ""), "a fixed maximum", series,
            choices=inflation)
    if "cadence" in given:
        return
    if not row.get("series"):
        row["cadence"] = ""
        return
    row["cadence"] = _ask_step_column(
        "how the series compounds, monthly or annual", row.get("cadence", ""), "monthly", cadence,
        choices=list(_STEP_CADENCES))


def _inflation_series(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT series FROM aux_table WHERE kind = 'inflation' AND series != '' ORDER BY series")]


def _step_summary(row: Mapping[str, str]) -> str:
    """A step's account and the columns it sets, for a numbered list."""
    parts = [f"{row['percent']} of what is left" if row.get("percent") else ""]
    if row.get("maximum"):
        grows = f", grown by {row['series']} ({row.get('cadence') or 'monthly'})" if row.get("series") else ""
        parts.append(f"at most {row['maximum']}{grows}")
    if row.get("tax_character"):
        parts.append(f"taxed as {row['tax_character']}")
    if row.get("basis_of"):
        lot = f" ({row['basis_of_lot']})" if row.get("basis_of_lot") else ""
        parts.append(f"basis of {row['basis_of']}{lot}")
    return ", ".join([_step_label(row), *filter(None, parts)])


def _echo_steps(name: str, steps: list[dict[str, str]]) -> None:
    click.echo(f"steps of {name}:")
    for n, row in enumerate(steps, 1):
        click.echo(f"  {n}. {_step_summary(row)}")


def _ask_step_number(steps: list[dict[str, str]], name: str, text: str = "step number", *,
                     show: bool = True) -> int:
    if show:
        _echo_steps(name, steps)
    while True:
        answer = _prompt_with_completion(f"{text} (1 to {len(steps)})").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(steps):
            return int(answer)
        click.echo(f"  {name} has steps 1 to {len(steps)}")


def _echo_equivalent(words: list[str]) -> None:
    click.echo("equivalent command:")
    click.echo("  " + " ".join(shlex.quote(w) for w in words))


def _step_words(row: Mapping[str, str]) -> list[str]:
    """``row``'s step as command-line words: the account, then its options."""
    words = [row["account_name"]]
    for column in ("lot", "percent", "maximum", "series", "cadence", *_STEP_TAX_COLUMNS):
        if row.get(column):
            words += [f"--{column.replace('_', '-')}", row[column]]
    return words


@contextmanager
def _editing_chain(
    data_dir: Path | None, scenario: str | None, name: str | None,
) -> Generator[tuple[StagedEdit, str, list[dict[str, str]]]]:
    """Stage an edit of chain ``name``'s steps: the body changes the list it is
    handed, which is then written back, all only if the scenario still loads.
    A chain the scenario inherits is written as its own copy, overriding the
    parent's, as an inherited lot is. Without ``name``, asks which chain."""
    data = _resolve_data_dir(data_dir)
    with _scenario_edit(data, scenario) as edit:
        if name is None:
            name = _ask_chain(edit.con)
        scenarios = edit.data_dir / "scenarios"
        owner = chain_edit.defining_scenario(scenarios, edit.name, name)
        if owner is None:
            raise click.ClickException(_no_chain(edit.con, name, edit.name))
        source = chain_edit.chain_file(_folder_of(scenarios, owner), name)
        assert source is not None  # defining_scenario found it
        steps = chain_edit.read_steps(source)
        yield edit, name, steps
        target = (source if owner == edit.name
                  else chain_edit.new_chain_file(edit.scenario_dir, source.parent.name, name))
        chain_edit.write_steps(target, steps)
    if owner != edit.name:
        click.echo(f"{name} came from {owner}; wrote {edit.name}'s own copy of it, which replaces it here")


def _no_chain(con: sqlite3.Connection, name: str, scenario: str) -> str:
    accounts = {r[0] for r in con.execute("SELECT name FROM account")}
    if name in accounts:
        return (f"{name} is an account, which is already a one-step chain; "
                f"`sim chain add` makes a chain of several steps")
    return f"no chain {name!r} in {scenario} (see `sim chain list`)"


def _ask_chain(con: sqlite3.Connection, text: str = "chain") -> str:
    """Ask which of the scenario's chains, the ones a chain file defines
    (an account's own one-step chain is left out, as it can't be edited)."""
    accounts = {r[0] for r in con.execute("SELECT name FROM account")}
    chains = [c for c in _interior_chains(con) if c not in accounts]
    if not chains:
        raise click.ClickException("the scenario has no chains yet; `sim chain add` makes one")
    click.echo(f"chains: {', '.join(chains)}")
    while (answer := _prompt_with_completion(text, choices=chains).strip()) not in chains:
        click.echo(f"  one of: {', '.join(chains)}")
    return answer


def _ask_new_chain_name(taken: set[str], text: str = "chain name (one word, no spaces)") -> str:
    while True:
        name = _prompt_with_completion(text).strip()
        if problem := chain_edit.name_problem(name):
            click.echo(f"  {problem}")
        elif name in taken:
            click.echo(f"  {name!r} is already a chain or an account's name")
        else:
            return name


def _step_index(steps: list[dict[str, str]], number: int, name: str) -> int:
    if not 1 <= number <= len(steps):
        raise click.ClickException(f"{name} has steps 1 to {len(steps)}, not {number}")
    return number - 1


@chain_group_command.command("add")
@click.argument("name", required=False)
@click.argument("account", required=False, type=completion.ENDPOINT)
@click.option("--role", type=click.Choice(registries.CHAIN_ROLES), default=None,
              help="source: money is drawn from it; destination: money is paid into it (asked if left out).")
@_step_options
@_scenario_options
def chain_add_command(
    name: str | None, account: str | None, role: str | None,
    lot: str | None, percent: str | None, maximum: str | None, series: str | None, cadence: str | None,
    tax_character: str | None, basis_of: str | None, basis_of_lot: str | None,
    scenario: str | None, data_dir: Path | None,
) -> None:
    """Make a new chain, with ACCOUNT (or another chain) as its first step.

    Add more steps with `sim chain step add`. Without NAME, asks what the chain
    is for and builds it a step at a time: whether it is a source, where money
    comes from, or a destination, where money goes, and whether to drain the steps in order,
    split the total by shares, or take up to an amount from each. It then
    prints the equivalent commands, and how to put the chain to use.
    """
    data = _resolve_data_dir(data_dir)
    # The questions are asked inside the staged edit, so a change made to the
    # scenario while they are answered is caught rather than overwritten.
    with _scenario_edit(data, scenario) as edit:
        con, scenario_name = edit.con, edit.name
        endpoints = _endpoints(con)
        # Chains and accounts share one namespace, and an investment account's
        # own chains are named account:lot, so its bare name isn't a chain row.
        taken = {r[0] for r in con.execute("SELECT name FROM chain UNION SELECT name FROM account")}
        guided = name is None
        if guided:
            plan = _guided_chain(endpoints, taken, role)
            name, steps, role = plan.name, plan.steps, plan.role
        else:
            assert name is not None
            if problem := chain_edit.name_problem(name):
                raise click.ClickException(problem)
            if name in taken:
                raise click.ClickException(f"{name!r} is already a chain or an account's name")
            if role is None:
                role = _ask_role()
            if account is None:
                _echo_endpoints(endpoints.accounts, endpoints.lot_info, endpoints.chains_for(role))
            steps = [_step_row(name, account, lot, percent, maximum, series, cadence, endpoints, role,
                               tax={"tax_character": tax_character, "basis_of": basis_of,
                                    "basis_of_lot": basis_of_lot})]
        assert name is not None
        chain_edit.write_steps(chain_edit.new_chain_file(edit.scenario_dir, role, name), steps)
    click.echo(f"added {role} chain {name} ({len(steps)} step(s)) in {scenario_name}")
    if guided:
        first, *rest = steps
        commands = [["sim", "chain", "add", name, *_step_words(first), "--role", role]]
        commands += [["sim", "chain", "step", "add", name, *_step_words(step)] for step in rest]
        click.echo("equivalent commands:")
        for words in commands:
            click.echo("  " + " ".join(shlex.quote(w) for w in words))
        _echo_chain_next_steps(name, role)


class _ChainPlan(NamedTuple):
    name: str
    steps: list[dict[str, str]]
    role: str


_ROLES = {
    "source": "where money comes from: a rule draws from it, or a lot refills from it",
    "destination": "where money goes: a rule pays into it, or a lot overflows into it",
}
_SPLITS = {
    "order": "drain them in order: all from the first, then the next when it runs out",
    "shares": "split the total by shares, such as 60% here and 40% there",
    "caps": "take up to an amount from each, then move on to the next",
}


def _ask_choice(text: str, choices: Mapping[str, str]) -> str:
    for key, meaning in choices.items():
        click.echo(f"  {key}: {meaning}")
    while (answer := _prompt_with_completion(text, choices=list(choices)).strip()) not in choices:
        click.echo(f"  one of: {', '.join(choices)}")
    return answer


def _ask_role() -> str:
    return _ask_choice("is this chain a source or a destination? (source/destination)", _ROLES)


def _guided_chain(endpoints: _Endpoints, taken: set[str], role: str | None) -> _ChainPlan:
    """``chain add``'s questions: what the chain is for (unless ``role`` says),
    then its steps, then the percent or maximum columns that do what was asked."""
    role = role or _ask_role()
    verb = "draw from" if role == "source" else "put into"
    split = _ask_choice(f"how should it {verb} its steps? (order/shares/caps)", _SPLITS)
    name = _ask_new_chain_name(taken)
    chains = endpoints.chains_for(role)
    _echo_endpoints(endpoints.accounts, endpoints.lot_info, chains)
    steps: list[dict[str, str]] = []
    shares: list[Decimal] = []
    while True:
        n = len(steps) + 1
        if shares and sum(shares) == 100:
            break
        prompt = f"step {n}: account or chain to {verb}" + (" (blank to finish)" if steps and split != "shares" else "")
        account = _prompt_with_completion(prompt, choices=[*endpoints.accounts, *chains],
                                          default="" if steps and split != "shares" else None).strip()
        if not account:
            # Shares go on until they reach 100%, so only order and caps may stop early.
            if steps and split != "shares":
                break
            continue
        try:
            row = _step_row(name, account, None, None, None, None, None, endpoints, role)
        except click.ClickException as exc:
            click.echo(f"  {exc.message}")
            continue
        if split == "shares":
            left = 100 - sum(shares)
            while True:
                text = _prompt_with_completion(f"  its share of the total, in % ({left:g}% left)").strip()
                try:
                    share = Decimal(units.percent_to_micros(text, require_sign=False)) / 10_000
                except ValueError as exc:
                    click.echo(f"  {exc}")
                    continue
                if not 0 < share <= left:
                    click.echo(f"  a share is more than 0% and at most the {left:g}% left")
                    continue
                break
            shares.append(share)
        elif split == "caps":
            while True:
                text = _prompt_with_completion("  at most how much, in dollars (blank for no limit)",
                                               default="").strip()
                try:
                    if text:
                        row["maximum"] = _step_maximum(text)
                except ValueError as exc:
                    click.echo(f"  {exc}")
                    continue
                break
            if not text:
                click.echo("  no limit: later steps are reached only when this one runs out")
        steps.append(row)
    if split == "shares":
        for row, percent in zip(steps, chain_edit.shares_to_percents(shares), strict=True):
            row["percent"] = percent
    elif split == "caps" and steps[-1].get("maximum"):
        click.echo("  note: every step has a limit, so beyond their total nothing more moves")
    return _ChainPlan(name, steps, role)


def _echo_chain_next_steps(name: str, role: str) -> None:
    if role == "source":
        click.echo(f"to use it: a rule's from account (`sim scheduled add DATE {name} ACCOUNT AMOUNT`), or a lot's "
                   f"refill (`sim account lot edit ACCOUNT LOT --refill-chain {name} --minimum-balance DOLLARS`)")
    else:
        click.echo(f"to use it: a rule's to account (`sim scheduled add DATE ACCOUNT {name} AMOUNT`), or a lot's "
                   f"overflow (`sim account lot edit ACCOUNT LOT --overflow-chain {name} --maximum-balance DOLLARS`)")
    click.echo(f"see it with `sim chain show {name}`")


@chain_group_command.group(name="step")
def chain_step_group() -> None:
    """Add, change, remove or reorder a chain's steps. Each command asks for
    the chain, and the step, when they are left out."""


@chain_step_group.command("add")
@click.argument("name", type=completion.CHAIN, required=False)
@click.argument("account", required=False, type=completion.ENDPOINT)
@click.option("--position", type=int, default=None,
              help="Step number the new step takes, moving later steps down (default: last).")
@_step_options
@_scenario_options
def chain_step_add_command(
    name: str | None, account: str | None, position: int | None,
    lot: str | None, percent: str | None, maximum: str | None, series: str | None, cadence: str | None,
    tax_character: str | None, basis_of: str | None, basis_of_lot: str | None,
    scenario: str | None, data_dir: Path | None,
) -> None:
    """Add a step to chain NAME: ACCOUNT (and its lot), or another chain.

    Without ACCOUNT, shows the chain's steps and asks for the new one: its
    account, then its percent, maximum, series and cadence (unless given as
    options), and where it goes; it then prints the equivalent command.
    """
    with _editing_chain(data_dir, scenario, name) as (edit, name, steps):
        endpoints = _endpoints(edit.con)
        guided = account is None
        if guided:
            # A chain can't be its own step, so it isn't offered as one.
            endpoints = endpoints._replace(chains=[c for c in endpoints.chains if c != name])
            _echo_steps(name, steps)
            _echo_endpoints(endpoints.accounts, endpoints.lot_info, endpoints.chains_for(endpoints.roles[name]))
        row = _step_row(name, account, lot, percent, maximum, series, cadence, endpoints,
                        endpoints.roles[name],
                        tax={"tax_character": tax_character, "basis_of": basis_of,
                             "basis_of_lot": basis_of_lot})
        if guided:
            given = {k for k, v in
                     {"percent": percent, "maximum": maximum, "series": series, "cadence": cadence}.items()
                     if v is not None}
            _ask_step_columns(row, _inflation_series(edit.con), given=given)
            if position is None and steps:
                while not (answer := _prompt_with_completion(
                    f"  its step number, 1 to {len(steps) + 1}", default=str(len(steps) + 1),
                ).strip()).isdigit() or not 1 <= int(answer) <= len(steps) + 1:
                    click.echo(f"  a step number from 1 to {len(steps) + 1}")
                position = int(answer)
        index = len(steps) if position is None else position - 1
        if not 0 <= index <= len(steps):
            raise click.ClickException(f"--position is 1 to {len(steps) + 1} for {name}")
        steps.insert(index, row)
    click.echo(f"{name}: step {index + 1} is {_step_summary(row)}")
    if guided:
        where = [] if index == len(steps) - 1 else ["--position", str(index + 1)]
        _echo_equivalent(["sim", "chain", "step", "add", name, *_step_words(row), *where])


def _step_label(row: Mapping[str, str]) -> str:
    """A step's account, with its lot when it names one."""
    return f"{row['account_name']} ({row['lot']})" if row.get("lot") else row["account_name"]


@chain_step_group.command("edit")
@click.argument("name", type=completion.CHAIN, required=False)
@click.argument("number", type=int, required=False)
@_step_options
@_scenario_options
def chain_step_edit_command(
    name: str | None, number: int | None,
    lot: str | None, percent: str | None, maximum: str | None, series: str | None, cadence: str | None,
    tax_character: str | None, basis_of: str | None, basis_of_lot: str | None,
    scenario: str | None, data_dir: Path | None,
) -> None:
    """Change step NUMBER of chain NAME. Each option given replaces that
    column; give it as '' to clear it (a percent cleared is 100%).

    Without NUMBER, shows the steps and asks which. Without any option, asks
    for each column in turn, starting from its current value, and prints the
    equivalent command.
    """
    given = {k: v for k, v in
             {"lot": lot, "percent": percent, "maximum": maximum, "series": series, "cadence": cadence,
              "tax_character": tax_character, "basis_of": basis_of, "basis_of_lot": basis_of_lot}.items()
             if v is not None}
    with _editing_chain(data_dir, scenario, name) as (edit, name, steps):
        if number is None:
            number = _ask_step_number(steps, name)
        index = _step_index(steps, number, name)
        endpoints = _endpoints(edit.con)
        before = dict(steps[index])
        merged = {**before, **given}
        entry = endpoints.lot_info.get(merged["account_name"])
        if given and not merged["lot"] and entry is not None and calendar_guide.lot_needed(entry):
            raise click.ClickException(
                f"{merged['account_name']} needs a lot; its lots: {', '.join(entry[1])}")
        if not given:
            click.echo(f"step {number}: {_step_summary(before)}")
            if entry is not None and calendar_guide.lot_needed(entry):
                lots = list(entry[1])
                while (answer := _prompt_with_completion(
                    "  lot", choices=lots, default=merged["lot"] or None,
                ).strip()) not in lots:
                    click.echo(f"  not one of: {', '.join(lots)}")
                merged["lot"] = answer
            _ask_step_columns(merged, _inflation_series(edit.con))
        steps[index] = _step_row(
            name, merged["account_name"], merged["lot"] or None, merged["percent"] or None,
            merged["maximum"] or None, merged["series"] or None, merged["cadence"] or None,
            endpoints, endpoints.roles[name], tax=merged,
        )
        after = steps[index]
    click.echo(f"{name}: step {number} is {_step_summary(after)}")
    if not given:
        changed = [c for c in ("lot", "percent", "maximum", "series", "cadence", *_STEP_TAX_COLUMNS)
                   if before.get(c, "") != after.get(c, "")]
        if changed:
            words = ["sim", "chain", "step", "edit", name, str(number)]
            for column in changed:
                words += [f"--{column.replace('_', '-')}", after.get(column, "")]
            _echo_equivalent(words)
        else:
            click.echo("  (nothing changed)")


@chain_step_group.command("rm")
@click.argument("name", type=completion.CHAIN, required=False)
@click.argument("number", type=int, required=False)
@_scenario_options
def chain_step_rm_command(name: str | None, number: int | None, scenario: str | None, data_dir: Path | None) -> None:
    """Remove step NUMBER from chain NAME (without NUMBER, shows the steps
    and asks which). A chain keeps at least one step; use `sim chain delete`
    to remove the whole chain."""
    with _editing_chain(data_dir, scenario, name) as (_edit, name, steps):
        if number is None:
            number = _ask_step_number(steps, name, "step to remove")
        index = _step_index(steps, number, name)
        if len(steps) == 1:
            raise click.ClickException(f"step {number} is {name}'s only step; `sim chain delete {name}` "
                                       "removes the chain")
        removed = steps.pop(index)
    click.echo(f"{name}: removed step {number} ({_step_label(removed)})")


@chain_step_group.command("move")
@click.argument("name", type=completion.CHAIN, required=False)
@click.argument("number", type=int, required=False)
@click.argument("to", type=int, required=False)
@_scenario_options
def chain_step_move_command(
    name: str | None, number: int | None, to: int | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Move step NUMBER of chain NAME to position TO, shifting the steps
    between. Either left out is asked for, after showing the steps."""
    with _editing_chain(data_dir, scenario, name) as (_edit, name, steps):
        if number is None:
            number = _ask_step_number(steps, name, "step to move")
        elif to is None:
            _echo_steps(name, steps)
        index = _step_index(steps, number, name)
        if to is None:
            to = _ask_step_number(steps, name, f"move step {number} to", show=False)
        target = _step_index(steps, to, name)
        steps.insert(target, steps.pop(index))
    click.echo(f"{name}: step {number} is now step {to}")


@chain_group_command.command("rename")
@click.argument("old", type=completion.CHAIN, required=False)
@click.argument("new", required=False)
@_scenario_options
def chain_rename_command(old: str | None, new: str | None, scenario: str | None, data_dir: Path | None) -> None:
    """Rename chain OLD to NEW, and every reference to it: rules, calendar
    transfers, lots' refill and overflow, and other chains' steps, in every
    scenario that shares the chain -- where it is defined, a parent or a
    mix-in included, and every scenario built on those. Either name left out
    is asked for."""
    data = _resolve_data_dir(data_dir)
    with _scenario_edit(data, scenario) as edit:
        if old is None:
            old = _ask_chain(edit.con, "chain to rename")
        taken = {r[0] for r in edit.con.execute("SELECT name FROM chain UNION SELECT name FROM account")}
        if new is None:
            new = _ask_new_chain_name(taken, f"new name for {old} (one word, no spaces)")
        elif problem := chain_edit.name_problem(new):
            raise click.ClickException(problem)
        elif new in rename.RESERVED_ACCOUNTS:
            _not_reserved(new)
        elif new in taken:
            raise click.ClickException(f"{new!r} is already a chain or an account's name")
        _not_reserved(new)  # also when asked: this scenario may not have made it
        changed = _rename_across(
            edit, "chain", old, new, defines=_chain_defined,
            taken=lambda name: _either(_chain_defined(name), rename.account_defined(name)),
            rewrite=chain_edit.rename_references,
            missing=lambda: _no_chain(edit.con, old, edit.name),
        )
    _echo_renamed("chain", old, new, changed)


@chain_group_command.command("delete")
@click.argument("name", type=completion.CHAIN, required=False)
@_scenario_options
def chain_delete_command(name: str | None, scenario: str | None, data_dir: Path | None) -> None:
    """Delete chain NAME. Refused while a rule, a calendar transfer, a lot's
    refill or overflow, or another chain uses it: change those first. A
    scenario's own copy of a chain it inherits is deleted, and the parent's
    applies again. Without NAME, asks which chain, shows its steps, and asks
    to confirm."""
    data = _resolve_data_dir(data_dir)
    with _scenario_edit(data, scenario) as edit:
        guided = name is None
        if name is None:
            name = _ask_chain(edit.con, "chain to delete")
        scenarios = edit.data_dir / "scenarios"
        owner = chain_edit.defining_scenario(scenarios, edit.name, name)
        if owner is None:
            raise click.ClickException(_no_chain(edit.con, name, edit.name))
        if owner != edit.name:
            raise click.ClickException(f"{name} is defined in {owner}; delete it there (--scenario {owner})")
        own = chain_edit.chain_file(edit.scenario_dir, name)
        assert own is not None
        if guided:
            _echo_steps(name, chain_edit.read_steps(own))
            if not click.confirm(f"delete {name}?", default=False):
                click.echo("nothing was deleted")
                return
        own.unlink()
        inherited = chain_edit.defining_scenario(scenarios, edit.name, name)
        if inherited is None:
            with open_scenario(edit.real_data_dir, edit.name) as scn:
                cmap = chain_map.chain_map(scn)
            if uses := cmap.uses.get(name, []):
                raise click.ClickException(
                    f"{name} is still used; change these first:\n  "
                    + "\n  ".join(chain_map.use_text(cmap, u) for u in uses))
    if inherited:
        click.echo(f"deleted {edit.name}'s own copy of {name}; {inherited}'s applies again")
    else:
        click.echo(f"deleted chain {name}")


@sim_group_command.group(name="symbol")
def symbol_group_command() -> None:
    """The stocks and funds the data repo knows: name, allocation and prices."""


@symbol_group_command.command("add")
@click.argument("symbol", required=False)
@click.option("--name", default=None, help="The security's name, for securities.csv.")
@click.option("--weight", "weight_specs", multiple=True, metavar="CLASS=PERCENT",
              help="Its share in one asset class (repeatable), e.g. --weight stock_us=60% "
                   "--weight bond=40%; together they must add up to 100%. "
                   f"Classes: {', '.join(registries.ASSET_CLASSES)}.")
@click.option("--as-of", "as_of", default=None, metavar="YYYY-MM",
              help="The month the weights describe (default: every date).")
@click.option("--fetch/--no-fetch", default=True, show_default=True,
              help="Fetch its prices into prices/security_price__<SYMBOL>.csv.")
@click.option("--provider", "provider_name", default="yahoo", show_default=True,
              type=click.Choice(sorted(prices.PRICE_PROVIDERS)), help="Where prices come from.")
@click.option("--since", "since_text", default=None, metavar="YYYY-MM-DD",
              help="Earliest price to fetch (default: as for `sim prices fetch`).")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)")
def symbol_add_command(
    symbol: str | None, name: str | None, weight_specs: tuple[str, ...], as_of: str | None,
    fetch: bool, provider_name: str, since_text: str | None, data_dir: Path | None,
) -> None:
    """Add a stock or fund: its name, its asset-class weights and its prices.

    Writes a securities.csv row (with --name) and one set of allocations.csv
    rows (with --weight), then fetches its prices. Without SYMBOL, asks for
    each of these and prints the equivalent command. A lot then holds it with
    `sim account lot add ... --symbol SYMBOL --quantity N`.

    An AI assistant can look up a fund's asset-class weights from its published
    breakdown; see the user guide, Part 4.11 "Add a stock or fund".
    """
    data = _resolve_data_dir(data_dir)
    guided = symbol is None
    if guided:
        symbol, name, weight_specs, as_of = _ask_symbol()
    assert symbol is not None
    try:
        weights = symbols.parse_weights(list(weight_specs))
        added = symbols.add_symbol(data, symbol, name=name, weights=weights, as_of=as_of)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    symbol = added.symbol
    if added.named:
        click.echo(f"{symbol}: named {name!r} in securities.csv")
    if added.weighted:
        click.echo(f"{symbol}: {', '.join(f'{c} {w}' for c, w in weights.items())} in allocations.csv")
    else:
        click.echo(f"{symbol}: no allocation yet, so `sim allocation` shows it as unclassified; "
                   "see the user guide, Part 4.11 \"Add a stock or fund\"")
    failed = 0
    # CASH and CD have no ticker to look up (registries.SYMBOL_ASSET_CLASS).
    if fetch and symbol not in registries.SYMBOL_ASSET_CLASS:
        failed = _fetch_prices(data, [symbol], since_text, provider_name)
    if guided:
        words = ["sim", "symbol", "add", symbol]
        if name:
            words += ["--name", name]
        for spec in weight_specs:
            words += ["--weight", spec]
        if as_of:
            words += ["--as-of", as_of]
        if not fetch:
            words.append("--no-fetch")
        if fetch and provider_name != "yahoo":
            words += ["--provider", provider_name]
        if fetch and since_text:
            words += ["--since", since_text]
        click.echo("equivalent command: " + " ".join(shlex.quote(w) for w in words))
    if failed:
        raise click.ClickException(
            f"{symbol} was added, but its prices could not be fetched; type them into "
            f"prices/security_price__{symbol}.csv instead (user guide, Part 4.11)"
        )


def _ask_symbol() -> tuple[str, str | None, tuple[str, ...], str | None]:
    """``symbol add``'s arguments, asked a question at a time."""
    symbol = click.prompt("symbol (ticker)").strip()
    name = click.prompt("name (optional)", default="", show_default=False).strip() or None
    click.echo(f"  asset classes: {', '.join(registries.ASSET_CLASSES)}")
    click.echo("  e.g. stock_us=60% bond=40%; blank to add the weights later (an AI can look them up)")
    while True:
        text = click.prompt("weights (optional)", default="", show_default=False).strip()
        try:
            weights = symbols.parse_weights(text.split()) if text else {}
        except ValueError as exc:
            click.echo(f"  {exc}")
            continue
        total = sum(units.percent_to_micros(w) for w in weights.values())
        if weights and total != 1_000_000:
            click.echo(f"  these add up to {units.micros_to_percent(total)}, not 100%")
            continue
        break
    as_of = None
    if weights:
        as_of = click.prompt("as of YYYY-MM (optional; blank for every date)",
                             default="", show_default=False).strip() or None
    return symbol, name, tuple(text.split()), as_of


@symbol_group_command.command("list")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)")
def symbol_list_command(data_dir: Path | None) -> None:
    """List every symbol: its name, latest allocation, prices and the lots holding it.

    A symbol appears if any of securities.csv, allocations.csv, prices/, a
    scenario's own prices/ or its account_lots.csv names it. A missing
    allocation or price is shown in red, since `sim allocation` or a lot's
    opening balance needs it; CASH and CD need no prices.
    """
    from rich.console import Console
    from rich.table import Table

    data = _resolve_data_dir(data_dir)
    frame = symbols.symbol_table(data)
    if frame.empty:
        click.echo("no symbols in securities.csv, allocations.csv, prices/ or any lot")
        return
    table = Table(title="symbols", header_style="bold")
    for column, justify in (("Symbol", "left"), ("Name", "left"), ("Allocation (latest)", "left"),
                            ("Prices", "right"), ("Latest price", "right"), ("Held by", "left")):
        table.add_column(column, justify=justify)
    for r in named_rows(frame):
        allocation = r.allocation or "[red]none[/red]"
        if r.allocation_sets > 1:
            allocation += f" [dim]+{r.allocation_sets - 1} earlier[/dim]"
        if r.symbol in registries.SYMBOL_ASSET_CLASS:  # CASH and CD: no ticker, no prices
            count = "-"
        elif r.prices or r.scenario_prices:
            count = str(r.prices)
        else:
            count = "[red]none[/red]"
        if r.scenario_prices:
            count += f"\n[dim]own file in {', '.join(r.scenario_prices)}[/dim]"
        latest = f"{r.latest_price} ({r.latest_date})" if r.latest_date else ""
        table.add_row(r.symbol, r.name, allocation, count, latest, "\n".join(r.held_by))
    Console().print(table)


@sim_group_command.group(name="scenario")
def scenario_group_command() -> None:
    """Create and list scenarios."""


@scenario_group_command.command("list")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show a table with each scenario's parents, dates, seed, "
                   "ensemble size and description.")
@click.option("-a", "--all", "all_", is_flag=True, default=False,
              help="Include the template scenarios (t_ names) read from the code repository.")
def scenario_list(data_dir: Path | None, verbose: bool, all_: bool) -> None:
    """List the data repo's scenarios.

    Plain output is just the names, one per line, for scripts. --verbose adds
    parents (marked "(mix-in only)" for a scenario with no dates anywhere in
    its lineage, which `sim all` doesn't run), start and end months, seed, ensemble size, and
    description, and marks the current scenario with *, and build "no" for a disabled one
    (`sim scenario disable`). A value in [brackets] is inherited
    from an ancestor; a blank size means the scenario is not an ensemble.
    --all adds the template scenarios a data repo reads from the code
    repository when it has no folder of that name.
    """
    _list_scenarios(data_dir, verbose, ensembles_only=False, templates=all_)


def _list_scenarios(
    data_dir: Path | None, verbose: bool, *, ensembles_only: bool, templates: bool = False,
) -> None:
    """``sim scenario list``, or with ``ensembles_only`` ``sim ensemble list``;
    ``templates`` adds the template scenarios (``--all``)."""
    data_dir_p = _resolve_data_dir(data_dir)
    try:
        if not verbose and not ensembles_only:
            for name in sc.scenario_list_names(data_dir_p, templates=templates):
                click.echo(name)
            return
        summaries = sc.scenario_summaries(data_dir_p, templates=templates)
    except LoadError as exc:
        raise click.ClickException("; ".join(exc.issues)) from None
    if ensembles_only:
        summaries = [s for s in summaries if s.is_ensemble]
    kind = "ensembles" if ensembles_only else "scenarios"
    if not summaries:
        click.echo(f"(no {kind} in {data_dir_p / 'scenarios'})")
        return
    if not verbose:
        for s in summaries:
            click.echo(s.name)
        return

    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    def cell(value: object | None, inherited: bool) -> str:
        if value is None:
            return ""
        return escape(f"[{value}]") if inherited else escape(str(value))

    def date_cell(d: sc.ScenarioDate | None) -> str:
        return cell(d and d.month, bool(d and d.inherited))

    def value_cell(v: sc.ScenarioValue | None) -> str:
        return cell(v and v.value, bool(v and v.inherited))

    current = read_current_scenario(data_dir_p)
    disabled = read_disabled_scenarios(data_dir_p)
    table = Table(title=f"{kind} ({data_dir_p.name})",
                  caption=escape("[values] are inherited"))
    table.add_column("", no_wrap=True)
    table.add_column("scenario", no_wrap=True)
    table.add_column("parents")
    table.add_column("start", no_wrap=True)
    table.add_column("end", no_wrap=True)
    table.add_column("seed", no_wrap=True, justify="right")
    table.add_column("size", no_wrap=True, justify="right")
    table.add_column("description")
    table.add_column("build", no_wrap=True)
    for s in summaries:
        table.add_row(
            "*" if s.name == current else "",
            s.name, _parents_cell(s),
            date_cell(s.start), date_cell(s.end),
            value_cell(s.seed), value_cell(s.ensemble_size),
            escape(s.description), "no" if s.name in disabled else "",
        )
    Console().print(table)


def _parents_cell(s: sc.ScenarioSummary) -> str:
    """``scenario list -v``'s parents: a mix-in only one's are marked, and
    one with none is just the mark."""
    parents = " + ".join(s.parents)
    if not s.mixin_only:
        return parents
    return f"{parents} (mix-in only)" if parents else "(mix-in only)"


def _inherits_line(data: Path, parents: Sequence[str]) -> str:
    """What ``create`` says a new scenario inherits."""
    if not parents:
        return "nothing (a mix-in only scenario)"
    return " + ".join(_resolve_scenario(data, p, remember=False).name for p in parents)


def _create_new_scenario(
    data: Path, name: str, parents: Sequence[str], *, description: str | None, ensemble_size: int,
) -> str:
    """Create scenario ``name`` on ``parents`` (none: mix-in only), validated
    in a staged copy; returns the ``inherits`` line to show."""
    parent = parents[0] if parents else None
    mixins = list(parents[1:])
    # Preflights the parent, or for none the empty template a mix-in only
    # scenario is checked on, then creates and validates NAME.
    with _scenario_edit(data, parent or MIXIN_BASE, remember=False, template_parent=True) as edit:
        sc.create_scenario(
            edit.data_dir, name, parent, description=description, mixins=mixins,
            ensemble_size=ensemble_size,
        )
    line = _inherits_line(data, parents)
    if parents and is_mixin_only(data / "scenarios", name):
        # Built only on mix-ins, it is one too (#324): easy to miss.
        line += " (a mix-in only scenario: nothing it builds on has a start or end)"
    return line


@scenario_group_command.command("create")
@click.argument("name", required=False)
@click.argument("parent", required=False, type=completion.SCENARIO)
@click.argument("mixins", type=completion.SCENARIO, nargs=-1)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default=None,
              help="Default: 'Derived from <parent> + <mixin>...'.")
@click.option("--no-parent", is_flag=True, default=False,
              help="Build on nothing: a mix-in only scenario, for mixing into others.")
def scenario_create(
    name: str | None,
    parent: str | None,
    mixins: tuple[str, ...],
    data_dir: Path | None,
    description: str | None,
    no_parent: bool,
) -> None:
    """Create scenario NAME deriving from PARENT, and make it current.

    The new scenario inherits everything from PARENT; only the rows you add to
    its (empty) overlay CSVs differ. Each MIXIN is then applied on top of
    PARENT, in order, down to the first ancestor already inherited -- usually
    just the mix-in itself, stopping at the shared base. See ADR-003 §4.5 for
    merge semantics.

    NAME is never an ensemble: it records ``ensemble_size`` 0, so under an
    ensemble parent ``sim run NAME`` runs the base case, each varying row at
    its central value. Use ``sim ensemble create`` for an ensemble.

    With --no-parent, NAME builds on nothing and has no start or end: a
    mix-in only scenario, which `sim run` and `sim all` skip, for mixing into
    others (ADR-003 §4.5). Give it dates (`sim scenario edit`) to run it alone.

    Without NAME or PARENT, they are asked for; the parents one at a time,
    and a blank answer, for no parent, must be confirmed.
    """

    skeletons: dict[str, tuple[str, ...]] = {
        "accounts.csv": registries.ACCOUNTS_CSV_HEADER,
        "account_lots.csv": registries.ACCOUNT_LOTS_CSV_HEADER,
        "scheduled.csv": registries.SCHEDULED_CSV_HEADER,
        "calendar.csv": registries.CALENDAR_HEADER,
    }
    try:
        data_dir_p = _resolve_data_dir(data_dir)
        name, parents = _ask_new_scenario(data_dir_p, "scenario", name, parent, mixins, no_parent=no_parent)
        inherits = _create_new_scenario(data_dir_p, name, parents, description=description, ensemble_size=0)
        target = data_dir_p / "scenarios" / name
        _make_current(data_dir_p, name)
        click.echo(f"created {target}")
        click.echo(f"  inherits: {inherits}")
        for filename in ("scenario.csv", *skeletons):
            click.echo(f"  {filename}")
        click.echo("  chains/source/.gitignore")
        click.echo("  chains/source/_example.csv")
        click.echo("  chains/destination/.gitignore")
        click.echo("  series/")
        click.echo("  series/README.md")
        click.echo("  README.md")
        _suggest_readme(target)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        raise click.ClickException(
            f"failed to create scenario {name}"
        ) from None


@sim_group_command.group(name="tags", invoke_without_command=True)
@click.pass_context
@click.option("--scenario", type=completion.SCENARIO, default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--role", default=None, help="Restrict to a single role.")
def tags_group_command(
    ctx: click.Context,
    scenario: str | None,
    data_dir: Path | None,
    role: str | None,
) -> None:
    """List tags across all types (accounts, scheduled, calendar), or run a sub-command."""
    if ctx.invoked_subcommand is not None:
        return
    data_dir_p = _resolve_data_dir(data_dir)
    with open_scenario(data_dir_p, _resolve_scenario(data_dir_p, scenario).name) as sc:
        df = sc.list_tags(role=role)
    if df.empty:
        click.echo("(no tags)")
        return
    click.echo(f"{'tag':<40}  {'accts':>5}  {'sched':>5}  {'cal':>4}")
    for r in named_rows(df, index=False):
        tok = f"{r.role}:{r.name}"
        click.echo(
            f"{tok:<40}  {r.n_account:>5}  {r.n_scheduled:>5}  {r.n_calendar_event:>4}"
        )


@tags_group_command.command("items")
@click.argument("tag_token", required=False, type=completion.TAG_FILTER)
@click.option("--scenario", type=completion.SCENARIO, default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def tags_items(
    tag_token: str | None,
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """List items (accounts, scheduled, calendar events) carrying the given tag.

    ``<role>:*`` lists everything in a role (e.g. ``estimate:*``). Without
    TAG_TOKEN, it is asked for.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_name = _resolve_scenario(data_dir_p, scenario).name
    if tag_token is None:
        choices = names.tag_tokens(data_dir_p, scenario_name)
        while len(answer := _ask_tags((), choices)) != 1:
            click.echo("  one tag: role:name, or role:* for the whole role")
        tag_token = answer[0]
    role, _, name = tag_token.partition(":")
    if name != "*":
        try:
            pairs = registries.parse_tags(tag_token)
        except ValueError as e:
            raise click.ClickException(str(e)) from None
        if len(pairs) != 1:
            raise click.ClickException("exactly one tag token expected")
    elif not role:
        raise click.ClickException("wildcard needs a role, e.g. 'estimate:*'")
    with open_scenario(data_dir_p, scenario_name) as sc:
        found = False
        for it in ("account", "scheduled", "calendar_event"):
            df = sc.items(it, tags=tag_token)  # type: ignore[arg-type]
            if df.empty:
                continue
            found = True
            label_col = "date" if it == "calendar_event" else "name"
            for row in named_rows(df, index=False):
                click.echo(f"{it:<14}  {getattr(row, label_col)}")
        if not found:
            click.echo(f"(tag {tag_token} not present)")


# ---------- renames shared across scenarios (#177) ----------

def _either(*defines: rename.Defines) -> rename.Defines:
    return lambda scenario_dir: any(each(scenario_dir) for each in defines)


def _chain_defined(name: str) -> rename.Defines:
    return lambda scenario_dir: chain_edit.chain_file(scenario_dir, name) is not None


def _account_or_chain_defined(name: str) -> rename.Defines:
    # One namespace: every account is also its own one-step chain.
    return _either(rename.account_defined(name), _chain_defined(name))


def _not_reserved(new: str) -> None:
    if new.strip() in rename.RESERVED_ACCOUNTS:
        raise click.ClickException(f"{new.strip()!r} is a built-in account's name")


def _rename_with_growth_rules(
    edit: StagedEdit, what: str, old: str, new: str, *,
    defines: Callable[[str], rename.Defines], rewrite: Callable[[Path, str, str], list[str]],
    moved: Callable[[str, str], tuple[str, str] | None],
    taken: Callable[[str], rename.Defines] | None = None, missing: Callable[[], str] | None = None,
) -> list[str]:
    """``_rename_across``, and then the calendar rows naming a growth rule the
    loader generated for an (account, lot) that ``moved`` renames: that
    rule's name follows them. The names are read from loads before and after,
    in each affected scenario, as they depend on its other rules."""
    scenarios = edit.data_dir / "scenarios"
    affected = rename.scope(scenarios, edit.name, defines(old.strip()))
    before = {each: rename.generated_growth_rules(edit.data_dir, each) for each in affected}
    changed = _rename_across(edit, what, old, new, defines=defines, rewrite=rewrite, taken=taken, missing=missing)
    for each in affected:
        after = rename.generated_growth_rules(edit.data_dir, each)
        rules = {name: after[target] for key, name in before[each].items()
                 if (target := moved(*key)) is not None and target in after}
        changed += [f"{each}: {c}" for c in rename.rename_growth_rules(scenarios / each, rules)]
    return list(dict.fromkeys(changed))


def _rename_account(edit: StagedEdit, old: str, new: str) -> list[str]:
    _not_reserved(new)
    old, new = old.strip(), new.strip()
    return _rename_with_growth_rules(
        edit, "account", old, new, defines=rename.account_defined, taken=_account_or_chain_defined,
        rewrite=rename.rename_account, moved=lambda account, lot: (new, lot) if account == old else None)


def _rename_across(
    edit: StagedEdit, what: str, old: str, new: str, *,
    defines: Callable[[str], rename.Defines],
    rewrite: Callable[[Path, str, str], list[str]],
    taken: Callable[[str], rename.Defines] | None = None,
    missing: Callable[[], str] | None = None,
) -> list[str]:
    """Rename ``what`` ``old`` to ``new`` in every scenario of the staged edit
    that shares it (``rename.scope``), refusing a ``new`` that already applies
    in any of them; what changed, as ``scenario: file`` lines."""
    old, new = old.strip(), new.strip()
    if not old or not new:
        raise click.ClickException(f"both the old and the new {what} name are required")
    if old == new:
        raise click.ClickException(f"{what} is already called {new!r}")
    scenarios = edit.data_dir / "scenarios"
    affected = rename.scope(scenarios, edit.name, defines(old))
    if not affected:
        raise click.ClickException(
            missing() if missing else f"no {what} {old!r} in {edit.name} or the scenarios it builds on")
    if templates := [each for each in affected if _is_template(scenarios, each)]:
        raise click.ClickException(
            f"{what} {old!r} is defined in the template scenario{'s' if len(templates) > 1 else ''} "
            f"{', '.join(templates)}, which a data repo doesn't change; to use another name, copy "
            "the template's folder into scenarios/ to replace it whole")
    if found := rename.clashes(scenarios, affected, (taken or defines)(new)):
        raise click.ClickException(
            f"{'an' if what.startswith(('a', 'e', 'i', 'o', 'u')) else 'a'} {what} {new!r} already applies in "
            f"{rename.describe_clashes(found)}; rename or delete it first")
    try:
        return list(dict.fromkeys(
            f"{each}: {change}" for each in affected for change in rewrite(scenarios / each, old, new)))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None


def _ask_rename(what: str, old: str | None, new: str | None, choices: Iterable[str]) -> tuple[str, str]:
    """A rename's OLD and NEW, either asked for when left out."""
    if old is None:
        old = _ask_name(what, choices, prompt=f"{what} to rename")
    if new is None:
        new = _ask_new_name(f"new name for {old}")
    return old, new


def _echo_renamed(what: str, old: str, new: str, changed: list[str]) -> None:
    click.echo(f"renamed {what} {old} to {new}")
    for line in changed:
        click.echo(f"  {line}")


@scheduled_group_command.command("rename")
@click.argument("old", required=False, type=completion.RULE)
@click.argument("new", required=False)
@_scenario_options
def scheduled_rename(old: str | None, new: str | None, scenario: str | None, data_dir: Path | None) -> None:
    """Rename scheduled rule OLD to NEW, and the calendar rows that name it
    (set_amount, retire_scheduled), in every scenario that shares the rule:
    where it is defined, a parent or a mix-in included, and every scenario
    built on those.

    Either name left out is asked for."""
    with _scenario_edit(_resolve_data_dir(data_dir), scenario) as edit:
        old, new = _ask_rename("scheduled rule", old, new, names.scheduled_names(edit.data_dir, edit.name))
        changed = _rename_across(edit, "scheduled rule", old, new, defines=rename.scheduled_defined,
                                 rewrite=rename.rename_scheduled)
    _echo_renamed("scheduled rule", old, new, changed)


@calendar_group_command.command("rename")
@click.argument("old", required=False, type=completion.CALENDAR_ROW)
@click.argument("new", required=False)
@_scenario_options
def calendar_rename(old: str | None, new: str | None, scenario: str | None, data_dir: Path | None) -> None:
    """Rename the calendar row (or latent factor) named OLD to NEW, and the
    rows naming it as their correlation parent (vary_correlate_with), in
    every scenario that shares it. Dates written relative to a date marker
    (OLD+2y) follow it.

    Either name left out is asked for."""
    with _scenario_edit(_resolve_data_dir(data_dir), scenario) as edit:
        old, new = _ask_rename("calendar row", old, new, names.calendar_row_names(edit.data_dir, edit.name))
        changed = _rename_across(edit, "calendar row", old, new, defines=rename.calendar_row_defined,
                                 rewrite=rename.rename_calendar_row)
    _echo_renamed("calendar row", old, new, changed)


@sim_group_command.group(name="series")
def series_group_command() -> None:
    """Growth-rate and inflation series."""


@series_group_command.command("rename")
@click.argument("old", required=False, type=completion.SeriesName())
@click.argument("new", required=False)
@_scenario_options
def series_rename(old: str | None, new: str | None, scenario: str | None, data_dir: Path | None) -> None:
    """Rename series OLD to NEW in every scenario that shares it: its rate
    files (series/<kind>__OLD.csv and a scenario's aux/<kind>__OLD.csv), and
    every column naming it -- accounts', lots', rules', calendar rows' and
    chain steps' series.

    A series with a file in the shared aux/ folder (cpi) is read by every
    scenario and isn't renamed here, nor is an asset class's series, or one
    named for a symbol (or <symbol>.<class>), which lots read by name.

    Either name left out is asked for."""
    data = _resolve_data_dir(data_dir)
    if old is None or new is None:
        scenario_name = _resolve_scenario(data, scenario).name
        can_rename = [
            name for name in names.series_names(data, scenario_name)
            if name not in registries.ASSET_CLASSES
            and not rename.symbol_series(data, name) and not rename.shared_series_files(data, name)
        ]
        old, new = _ask_rename("series", old, new, can_rename)
    old, new = old.strip(), new.strip()
    if problem := rename.name_problem("series", new):
        raise click.ClickException(problem)
    for name in (old, new):
        if name in registries.ASSET_CLASSES:
            raise click.ClickException(
                f"{name!r} is an asset class, whose series name fund allocations rely on; it can't be renamed")
    for name in (old, new):
        if why := rename.symbol_series(data, name):
            raise click.ClickException(f"{why}; it can't be renamed")
        if shared := rename.shared_series_files(data, name):
            raise click.ClickException(
                f"series {name!r} has rates in the shared {', '.join(f'aux/{p.name}' for p in shared)}, "
                "which every scenario reads; it can't be renamed")
    with _scenario_edit(data, scenario) as edit:
        changed = _rename_across(edit, "series", old, new, defines=rename.series_mentioned,
                                 rewrite=rename.rename_series)
    _echo_renamed("series", old, new, changed)


def _lot_defined(account: str, lot: str) -> rename.Defines:
    return lambda scenario_dir: any(
        _names_lot(row, "account_name", "lot", account, lot)
        for row in _read_values_rows(scenario_dir / "account_lots.csv"))


@lot_group_command.command("rename")
@click.argument("account", required=False, type=completion.ACCOUNT)
@click.argument("lot", required=False, type=completion.LotName("account"))
@click.argument("new_lot", required=False)
@_scenario_options
def lot_rename(
    account: str | None, lot: str | None, new_lot: str | None, scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Rename ACCOUNT's LOT to NEW_LOT, and every reference to it, in every
    scenario that shares the lot: where it is defined, a parent or a mix-in
    included, and every scenario built on those. Whatever is left out is
    asked for."""
    data = _resolve_data_dir(data_dir)
    if account is None or lot is None or new_lot is None:
        scenario_name = _resolve_scenario(data, scenario).name
        if account is None:
            account = _ask_name("account", names.account_names(data, scenario_name))
        if lot is None:
            lot = _ask_name("lot", names.lot_names(data, scenario_name, account, implicit=False),
                            prompt=f"lot of {account}")
        if new_lot is None:
            new_lot = _ask_new_name(f"new name for {account}'s lot {lot}")
    account, lot, new_lot = account.strip(), lot.strip(), _check_lot_label(new_lot)

    def rewrite(scenario_dir: Path, old: str, new: str) -> list[str]:
        lines = [line.strip() for line in _lot_reference_rows(scenario_dir, account, old, own_row=True)]
        _rewrite_lot_references(scenario_dir, account, old, new, own_row=True)
        return lines

    with _scenario_edit(data, scenario) as edit:
        changed = _rename_with_growth_rules(
            edit, f"lot of {account}", lot, new_lot,
            defines=lambda name: _lot_defined(account, name), rewrite=rewrite,
            missing=lambda: (f"{account!r} has no lot {lot!r} in account_lots.csv, in {edit.name} "
                             "or the scenarios it builds on"),
            moved=lambda owner, label: (account, new_lot) if (owner, label) == (account, lot) else None,
        )
    _echo_renamed(f"lot of {account}", lot, new_lot, changed)


def _parent_choices(data: Path) -> list[str]:
    """What a scenario can build on: the data repo's scenarios and the
    templates it doesn't replace."""
    return names.scenario_names(data)


def _walk_parents(data: Path, current: Sequence[str], *, itself: str | None = None) -> list[str]:
    """``scenario_guide.walk_parents`` at the terminal, with completion."""
    return scenario_guide.walk_parents(
        current, _parent_choices(data),
        lambda text, choices, default: _prompt_with_completion(text, choices=choices, default=default),
        click.echo, itself=itself,
    )


def _confirm_no_parent(name: str, question: str, *, mixin_only: bool = True) -> bool:
    """Whether no parent at all is meant (#259): too easy to get by mistake
    to take a blank answer for it."""
    why = ", so it can only be mixed into others" if mixin_only else ""
    return click.confirm(f"{name} will have no parent{why}. {question}", default=False)


def _ask_new_scenario(
    data: Path, what: str, name: str | None, parent: str | None, mixins: Sequence[str],
    *, no_parent: bool,
) -> tuple[str, list[str]]:
    """``scenario|ensemble create``'s NAME and what it builds on: PARENT then
    the MIXINs, or none with ``no_parent``. NAME is asked for when left out;
    so are the parents, one at a time, when PARENT is, and a blank answer
    there must be confirmed."""
    if no_parent and (parent is not None or mixins):
        raise click.UsageError("--no-parent can't be given with PARENT or MIXIN")
    if name is None:
        name = _ask_new_name(f"name for the new {what}")
    if no_parent:
        return name, []
    if parent is not None:
        return name, [parent, *mixins]
    click.echo(f"what {name} builds on: a parent, then any mix-ins")
    while not (parents := _walk_parents(data, [])) and not _confirm_no_parent(
            name, "Create it with no parent?"):
        pass
    return name, parents


def _ensemble_names(data: Path) -> list[str]:
    """The scenarios that are ensembles (a merged ensemble_size above 0)."""
    return [n for n in list_scenario_names(data) if sc.ensemble_settings(data, n)[1] is not None]


def _rename_scenario(old: str | None, new: str | None, data_dir: Path | None, *, what: str) -> None:
    """``sim scenario rename`` and ``sim ensemble rename`` (``what``)."""
    data = _resolve_data_dir(data_dir)
    if old is None:
        choices = _ensemble_names(data) if what == "ensemble" else list_scenario_names(data)
        old = _ask_name(what, choices, prompt=f"{what} to rename")
    if new is None:
        new = _ask_new_name(f"new name for {old}")
    old, new = old.strip(), new.strip()
    # Only a folder's own name: anything else (a path, '..') could reach
    # outside scenarios/.
    if old not in list_scenario_names(data):
        raise click.ClickException(f"scenario not found: {data / 'scenarios' / old}")
    if not registries.SCENARIO_NAME_RE.match(new):
        raise click.ClickException(
            f"invalid scenario name: {new!r} (lowercase letters, digits, '_' and '-', "
            "starting with a letter or digit)")
    if (data / "scenarios" / new).exists():
        raise click.ClickException(f"scenario already exists: {data / 'scenarios' / new}")
    with _scenario_edit(data, old, remember=False) as edit:
        try:
            changed = rename.rename_scenario(edit.data_dir, old, new)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc).replace(str(edit.data_dir), str(data))) from None
    # The commit removes the old folder's files; a folder it leaves holding
    # only empty folders (an emptied series/, say) goes too.
    leftover = data / "scenarios" / old
    if leftover.is_dir() and not any(path.is_file() or path.is_symlink() for path in leftover.rglob("*")):
        shutil.rmtree(leftover)
    _echo_renamed("scenario", old, new, changed)
    working_set.use(data, new)  # renaming a scenario is working on it (#322)
    if rename.CURRENT_SCENARIO_FILE in changed:
        _announce_current(new, old)
    runs = find_state_dir(data, DEFAULT_STATE_DIR) / DEFAULT_RUNS_DIR
    if latest_run_path(runs, old, wait=False) or (runs / old).is_dir():
        click.echo(f"  runs already made of {old} keep that name; `sim run {new}` makes new ones")


@scenario_group_command.command("rename")
@click.argument("old", required=False, type=completion.SCENARIO)
@click.argument("new", required=False)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
def scenario_rename(old: str | None, new: str | None, data_dir: Path | None) -> None:
    """Rename scenario OLD to NEW: its folder and scenario.csv name, every
    scenario naming it as a parent or mix-in, the current scenario if it is
    OLD, and the data repo's README links and mentions (`OLD`,
    scenarios/OLD). Same as `sim ensemble rename`.

    Either name left out is asked for."""
    _rename_scenario(old, new, data_dir, what="scenario")


@ensemble_group_command.command("rename")
@click.argument("old", required=False, type=completion.ENSEMBLE)
@click.argument("new", required=False)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
def ensemble_rename(old: str | None, new: str | None, data_dir: Path | None) -> None:
    """Rename ensemble OLD to NEW; the same as `sim scenario rename`.

    Either name left out is asked for.
    """
    _rename_scenario(old, new, data_dir, what="ensemble")


def _copy_scenario(old: str | None, new: str | None, data_dir: Path | None, *, what: str) -> None:
    """``sim scenario copy`` and ``sim ensemble copy`` (``what``), issue #295."""
    data = _resolve_data_dir(data_dir)
    if old is None:
        choices = names.scenario_names(data)
        if what == "ensemble":
            choices = [n for n in choices if sc.ensemble_settings(data, n)[1] is not None]
        old = _ask_name(what, choices, prompt=f"{what} to copy")
    if new is None:
        new = _ask_new_name(f"name for the copy of {old}")
    old, new = old.strip(), new.strip()
    # Only a folder's own name (or a template's): anything else (a path,
    # '..') could reach outside scenarios/.
    if old not in names.scenario_names(data):
        raise click.ClickException(f"scenario not found: {data / 'scenarios' / old}")
    if what == "ensemble" and sc.ensemble_settings(data, old)[1] is None:
        raise click.ClickException(f"{old} is not an ensemble; `sim scenario copy` copies it")
    # Preflights OLD, then copies it and checks the copy loads, in a staged copy.
    with _scenario_edit(data, old, remember=False, template_parent=True) as edit:
        try:
            written = rename.copy_scenario(edit.data_dir, old, new, created_by=sc.current_user())
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc).replace(str(edit.data_dir), str(data))) from None
    target = data / "scenarios" / new
    _make_current(data, new)
    click.echo(f"copied {what} {old} to {target}")
    parents = read_parents(target)
    click.echo(f"  inherits: {' + '.join(parents) if parents else '(nothing)'}")
    for line in written:
        click.echo(f"  {line}")
    if what == "ensemble":
        click.echo(f"  same seed and size as {old}, so member N of each draws the same future")
    _suggest_readme(target)


@scenario_group_command.command("copy")
@click.argument("old", required=False, type=completion.SCENARIO)
@click.argument("new", required=False)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
def scenario_copy(old: str | None, new: str | None, data_dir: Path | None) -> None:
    """Copy scenario OLD to a new scenario NEW, and make NEW current.

    NEW is a sibling of OLD, not built on it: the same parents and mix-ins,
    and a copy of every file in OLD's folder, so changing one doesn't
    change the other. Use it to try an alternative, such as a different
    parent (`sim scenario edit NEW --parents ...`). Only NEW's own name
    changes: its scenario.csv name and created_by, its README's title, and
    its markdown's links to itself. OLD may be a template. Runs are not
    copied. Same as `sim ensemble copy`.

    Either name left out is asked for."""
    _copy_scenario(old, new, data_dir, what="scenario")


@ensemble_group_command.command("copy")
@click.argument("old", required=False, type=completion.ENSEMBLE)
@click.argument("new", required=False)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
def ensemble_copy(old: str | None, new: str | None, data_dir: Path | None) -> None:
    """Copy ensemble OLD to a new ensemble NEW; the same as `sim scenario
    copy`, but OLD must be an ensemble. NEW keeps OLD's seed and size, so
    the two pair member by member.

    Either name left out is asked for."""
    _copy_scenario(old, new, data_dir, what="ensemble")


# ---------- editing and deleting a scenario (#190) ----------

def _blank_or(value: str, parse: Callable[[str], object], option: str) -> object | None:
    """``value`` parsed, or ``None`` for a blank one: that option's field inherits."""
    if not value.strip():
        return None
    try:
        return parse(value.strip())
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint=option) from None


def _whole_number(text: str) -> int:
    if not text.lstrip("-").isdigit():
        raise ValueError(f"{text!r} is not a whole number")
    return int(text)


def _member_count(text: str) -> int:
    count = _whole_number(text)
    if count < 0:
        raise ValueError(f"{count} is negative; 0 makes the scenario not an ensemble")
    return count


def _reserve_json(text: str) -> int | str:
    # As the loader reads it (units.json_dollars_to_cents): whole dollars as
    # a JSON integer, cents as a string, never a float.
    units.dollars_to_cents(text)
    return int(text) if text.lstrip("-").isdigit() else text


_SCENARIO_CHECKERS: dict[str, Callable[[str], object]] = {
    "month": _validate_month_date,
    "seed": lambda text: text == scenario_guide.NEW_SEED or _whole_number(text),
    "count": _member_count,
    "money": _reserve_json,
}


def _edit_scenario(name: str | None, data_dir: Path | None, *, new_seed: bool, **fields: str | None) -> None:
    """``sim scenario edit`` and ``sim ensemble edit``: with any option, the
    change at once; with none, a guided edit of every field."""
    data = _resolve_data_dir(data_dir)
    names = list_scenario_names(data)
    if name is None:
        current = read_current_scenario(data)
        while (name := _prompt_with_completion(
                "scenario to edit", choices=names, default=current if current in names else None,
        ).strip()) not in names:
            click.echo(f"  not a scenario: {name!r}")
    name = name.strip()
    if name not in names:
        raise click.ClickException(f"scenario not found: {data / 'scenarios' / name}")
    # Named or picked at the prompt, it becomes current, even if nothing changes.
    _resolve_scenario(data, name)
    if new_seed or any(value is not None for value in fields.values()):
        _apply_scenario_edit(data, name, new_seed=new_seed, **fields)
        return

    spec = scenario_guide.spec([n for n in _parent_choices(data) if n != name])
    before = scenario_guide.answers_of(sc._scenario_row(data / "scenarios" / name))
    answers = dict(before)
    _echo_guided_scenario(name, data / "scenarios" / name / "scenario.csv", defaulted=False)

    def ask_parents(current: str | None) -> str:
        chosen = _walk_parents(data, parse_parents(current), itself=name)
        if chosen or not current:
            return ":".join(chosen)
        dated = bool(answers.get("start", "").strip() or answers.get("end", "").strip())
        if _confirm_no_parent(name, "Make it build on nothing?", mixin_only=not dated):
            return ""
        click.echo("  parents unchanged")
        return current

    guide = calendar_guide.Guide(
        ask=lambda text, choices, default: _prompt_with_completion(text, choices=choices, default=default),
        checkers=_SCENARIO_CHECKERS,
        askers={"parents": ask_parents},
    )

    def submit(_params: dict[str, object]) -> None:
        changed = {field: value for field, value in answers.items() if value != before[field]}
        if not changed:
            click.echo("no change; nothing was written")
            return
        fresh = changed.get("seed") == scenario_guide.NEW_SEED
        if fresh:
            del changed["seed"]
        _apply_scenario_edit(data, name, new_seed=fresh, **changed)

    guide.review(spec, calendar_guide.GuideContext(), answers, submit, show_command=False)


def _apply_scenario_edit(
    data: Path, name: str, *, description: str | None = None, start: str | None = None,
    end: str | None = None, parents: str | None = None, seed: str | None = None,
    new_seed: bool = False, size: str | None = None, reserve: str | None = None,
) -> None:
    """Set the given fields of scenario ``name``'s scenario.csv; see ``_edit_scenario``."""
    if seed is not None and new_seed:
        raise click.UsageError("--seed and --new-seed are exclusive")
    values: dict[str, str] = {}
    meta: dict[str, object | None] = {}
    for column, given in (("start", start), ("end", end)):
        if given is not None:
            values[column] = _validate_month_date(given) if given.strip() else ""
    if parents is not None:
        names = parse_parents(parents)
        buildable = _parent_choices(data)
        for parent in names:
            if parent not in buildable:
                raise click.BadParameter(f"scenario not found: {parent}", param_hint="--parents")
        if name in names:
            raise click.BadParameter(f"{name} can't build on itself", param_hint="--parents")
        if len(set(names)) != len(names):
            raise click.BadParameter("a scenario is named twice", param_hint="--parents")
        values["parent_scenario"] = ":".join(names)
    if description is not None:
        if description.strip():
            values["description"] = description.strip()
        else:
            # Blank: back to the default `scenario create` writes. Not the
            # parent's own, which a blank cell would inherit and which
            # describes the parent.
            builds_on = parse_parents(values["parent_scenario"]) if "parent_scenario" in values \
                else read_parents(data / "scenarios" / name)
            if not builds_on:
                raise click.BadParameter("a root scenario has no default description; give one",
                                         param_hint="--description")
            values["description"] = default_description(builds_on)
    if seed is not None:
        parsed = _blank_or(seed, _whole_number, "--seed")
        values["seed"] = "" if parsed is None else str(parsed)
    elif new_seed:
        values["seed"] = str(secrets.randbelow(2**31))
    if size is not None:
        meta["ensemble_size"] = _blank_or(size, _member_count, "--size")
    if reserve is not None:
        meta["reserve"] = _blank_or(reserve, _reserve_json, "--reserve")
    if not values and not meta:
        raise click.UsageError("nothing to change: give at least one option (see --help)")

    readme: bool | str | None = None
    runnable = [n for n in sc.list_scenario_names(data) if not is_mixin_only(data / "scenarios", n)]
    # Validates this scenario and every scenario built on it before writing.
    with _scenario_edit(data, name) as edit:
        folder = edit.scenario_dir
        old_row = sc._scenario_row(folder)
        try:
            changes = sc.edit_scenario_row(folder, values, meta)
        except LoadError as exc:
            raise click.ClickException("; ".join(exc.issues)) from None
        now_mixin_only = _new_mixin_only(edit.data_dir / "scenarios", runnable)
        new_row = sc._scenario_row(folder)
        builds_on = parse_parents(new_row.get("parent_scenario"))
        # A blank description reads as the default, as `--description ''` sets it.
        own = new_row.get("description", "").strip() or (default_description(builds_on) if builds_on else "")
        if changes and own and not (folder / "README.md").exists():
            # Every scenario has one now (#190); one made before that gets it here.
            (folder / "README.md").write_text(sc.scenario_readme(name, own, builds_on), encoding="utf-8")
            readme = "written"
        else:
            readme = sc.follow_readme(folder, old_row, new_row)
    if not changes:
        click.echo(f"scenario {name} already has those values; nothing changed")
        return
    click.echo(f"edited scenario {name}")
    for change in changes:
        click.echo(f"  {change.field}: {change.old or '(inherited)'} -> {change.new or '(inherited)'}")
    if now_mixin_only:
        click.echo(f"NOTE: now mix-in only (no start or end anywhere in the lineage), so "
                   f"`sim run` refuses and `sim all` skips: {', '.join(now_mixin_only)}", err=True)
    if readme == "written":
        click.echo(f"  README.md: written; edit {data / 'scenarios' / name / 'README.md'} "
                   "to say what the scenario is for")
    elif readme is True:
        click.echo("  README.md: updated to match")
    elif readme is False:
        click.echo(f"WARN: {data / 'scenarios' / name / 'README.md'} is what listings and notebooks show, "
                   "and it has been edited by hand; update it to match", err=True)


def _new_mixin_only(scenarios_dir: Path, runnable: Sequence[str]) -> list[str]:
    """Which ``runnable`` scenarios an edit left with no dates anywhere in
    their lineage, so mix-in only (#324): allowed, since that may be the
    point, but said, since they stop running by themselves."""
    return [n for n in runnable if is_mixin_only(scenarios_dir, n)]


def _edit_options(command: Callable[..., None]) -> Callable[..., None]:
    options = [
        click.argument("name", required=False, type=completion.SCENARIO),
        click.option("--description", default=None,
                     help="The scenario's description; '' restores the default, 'Derived from <parents>'."),
        click.option("--start", default=None, help=f"First month ({MONTH_FORMAT_HINT}); '' inherits."),
        click.option("--end", default=None, help=f"Last month ({MONTH_FORMAT_HINT}); '' inherits."),
        click.option("--parents", default=None, metavar="PARENT[:MIXIN...]", type=completion.PARENTS,
                     help="What it builds on: its parent, then any mix-ins, colon-separated. "
                          "'' makes it a root scenario."),
        click.option("--seed", default=None, help="Ensemble master seed; '' inherits."),
        click.option("--new-seed", is_flag=True, default=False, help="Draw a fresh random seed."),
        click.option("--size", default=None,
                     help="Ensemble member count (meta_json ensemble_size); 0 makes it not an "
                          "ensemble, '' inherits."),
        click.option("--reserve", default=None,
                     help="The ensemble reports' reserve, in CPI-constant dollars "
                          "(meta_json reserve); '' inherits."),
        click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
                     default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)."),
    ]
    for option in reversed(options):
        command = option(command)
    return command


@scenario_group_command.command("edit")
@_edit_options
def scenario_edit(name: str | None, data_dir: Path | None, **fields: Any) -> None:
    """Change scenario NAME's scenario.csv: its description, start and end
    months, parents and mix-ins, seed, ensemble size or reserve.

    Options set those fields and write at once; a blank value ('') clears
    one, so it inherits again (a blank description restores the default).
    With none, the fields are shown: name one to change it, with completion,
    as often as needed, and write when done. Without NAME, it is asked for.

    The change is written only if NAME and every scenario built on it still
    load. A README.md that `scenario create` wrote follows the description
    and parents. Same as `sim ensemble edit`."""
    _edit_scenario(name, data_dir, **fields)


@ensemble_group_command.command("edit")
@_edit_options
def ensemble_edit(name: str | None, data_dir: Path | None, **fields: Any) -> None:
    """Change ensemble NAME's scenario.csv; the same as `sim scenario edit`."""
    _edit_scenario(name, data_dir, **fields)


def _delete_scenario(name: str | None, data_dir: Path | None, yes: bool, *, what: str) -> None:
    """``sim scenario delete`` and ``sim ensemble delete`` (``what``)."""
    data = _resolve_data_dir(data_dir)
    if name is None:
        choices = _ensemble_names(data) if what == "ensemble" else list_scenario_names(data)
        name = _ask_name(what, choices, prompt=f"{what} to delete")
    name = name.strip()
    scenarios = data / "scenarios"
    # Only a folder's own name: anything else (a path, '..') could reach outside scenarios/.
    if name not in list_scenario_names(data):
        raise click.ClickException(f"scenario not found: {scenarios / name}")
    if users := sc.dependents(scenarios, name):
        raise click.ClickException(
            f"{name} is the parent or a mix-in of {', '.join(users)}; delete those, or build them "
            f"on something else (`sim scenario edit <scenario> --parents ...`), first")
    folder = scenarios / name
    click.echo(f"{folder}: {sum(1 for p in folder.rglob('*') if p.is_file())} file(s)")
    if not yes and not click.confirm(f"delete scenario {name}?", default=False):
        raise click.ClickException("nothing was deleted")
    mentions = rename.scenario_mentions(data, name)
    # Nothing builds on it, so no other scenario's load can change: there is
    # nothing to validate, and a scenario that fails to load can go too.
    shutil.rmtree(folder)
    click.echo(f"deleted scenario {name}")
    working_set.remove(data, name)
    if read_current_scenario(data) == name:
        current_scenario_path(data).unlink(missing_ok=True)
        _announce_current(None, name)
    for path in mentions:
        click.echo(f"  {path.relative_to(data)} still mentions it")
    runs = find_state_dir(data, DEFAULT_STATE_DIR) / DEFAULT_RUNS_DIR
    if latest_run_path(runs, name, wait=False) or (runs / name).is_dir():
        click.echo(f"  runs already made of {name} are kept, under {runs}")


@scenario_group_command.command("delete")
@click.argument("name", required=False, type=completion.SCENARIO)
@click.option("--yes", is_flag=True, help="Don't ask before deleting.")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
def scenario_delete(name: str | None, yes: bool, data_dir: Path | None) -> None:
    """Delete scenario NAME's folder. Refused while another scenario names
    it as its parent or a mix-in. Clears the current scenario if it is NAME;
    runs already made of it are kept. Same as `sim ensemble delete`.

    Without NAME, it is asked for."""
    _delete_scenario(name, data_dir, yes, what="scenario")


@ensemble_group_command.command("delete")
@click.argument("name", required=False, type=completion.ENSEMBLE)
@click.option("--yes", is_flag=True, help="Don't ask before deleting.")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
def ensemble_delete(name: str | None, yes: bool, data_dir: Path | None) -> None:
    """Delete ensemble NAME; the same as `sim scenario delete`.

    Without NAME, it is asked for.
    """
    _delete_scenario(name, data_dir, yes, what="ensemble")


def _set_enabled(name: str | None, data_dir: Path | None, *, enabled: bool, what: str) -> None:
    """``sim scenario|ensemble enable|disable`` (``what``)."""
    data = _resolve_data_dir(data_dir)
    disabled = read_disabled_scenarios(data)
    verb = "enable" if enabled else "disable"
    if name is None:
        choices = _ensemble_names(data) if what == "ensemble" else list_scenario_names(data)
        choices = [n for n in choices if (n in disabled) == enabled]
        if not choices:
            raise click.ClickException(f"no {what} to {verb}: every one is already {verb}d")
        name = _ask_name(what, choices, prompt=f"{what} to {verb}")
    name = name.strip()
    # Only a folder's own name: not a template (its folder is in the code
    # repository), a path, or '..'.
    if name not in list_scenario_names(data) or not (data / "scenarios" / name).is_dir():
        raise click.ClickException(f"scenario not found: {data / 'scenarios' / name}")
    if what == "ensemble" and sc.ensemble_settings(data, name)[1] is None:
        raise click.ClickException(f"{name} is not an ensemble; `sim scenario {verb}` takes it")
    if (name not in disabled) == enabled:
        click.echo(f"{name} is already {verb}d")
        return
    set_scenario_enabled(data, name, enabled)
    if enabled:
        click.echo(f"enabled {name}: `sim all` and the background builder build it again")
    else:
        click.echo(f"disabled {name}: `sim all`, the background builder and the notebooks' "
                   f"pickers leave it out; `sim run {name}` still builds it")
    click.echo(f"  {'removed' if enabled else 'wrote'} "
               f"{data / 'scenarios' / name / DISABLE_FILE}; commit it to share it")


_ENABLE_DOC = """{verb_cap} {what} NAME. A disabled scenario (a mix-in, or a base
    that only exists to be built on) is never built by `sim all` or the
    background builder, and the notebooks' pickers leave it out; it still
    works as a parent or mix-in, and `sim run NAME` still builds it. It is
    a {file} file in the scenario's folder: not inherited, and not part of
    any scenario's data, so changing it rebuilds nothing.

    Without NAME, it is asked for."""


def _enable_command(group: click.Group, what: str, *, enabled: bool) -> None:
    verb = "enable" if enabled else "disable"

    @group.command(verb, help=_ENABLE_DOC.format(
        verb_cap=verb.capitalize(), what=what, file=DISABLE_FILE))
    @click.argument("name", required=False,
                    type=completion.ENSEMBLE if what == "ensemble" else completion.SCENARIO)
    @click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
                  default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
    def command(name: str | None, data_dir: Path | None) -> None:
        _set_enabled(name, data_dir, enabled=enabled, what=what)


for _group, _what in ((scenario_group_command, "scenario"), (ensemble_group_command, "ensemble")):
    _enable_command(_group, _what, enabled=True)
    _enable_command(_group, _what, enabled=False)


# ---------- setup listings (#185) ----------

_LISTING_SCENARIO_OPTION = click.option(
    "--scenario", type=completion.SCENARIO, default=None,
    help="Scenario folder name (default: the current scenario).",
)
_LISTING_DATA_DIR_OPTION = click.option(
    "--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).",
)


def _print_wide(table: RenderableType) -> None:
    """Print a rich table, widening the console rather than cropping names,
    as `sim chain list` does."""
    from rich.console import Console
    from rich.measure import Measurement

    console = Console()
    needed = Measurement.get(console, console.options.update(max_width=10_000), table).maximum
    if needed > console.width:
        console = Console(width=min(needed, max(console.width, 160)))
    console.print(table)


def _tag_matches(tags: str, wanted: str) -> bool:
    """Whether a ``tags`` cell carries ``wanted`` (``role:name``, or
    ``role:*`` for any name in the role)."""
    role, _, name = wanted.partition(":")
    try:
        pairs = registries.parse_tags(tags)
    except ValueError:
        return False
    return any(r == role and (name == "*" or n == name) for r, n in pairs)


_CALENDAR_LIST_KINDS = sorted({*registries.CALENDAR_EVENT_KINDS, *_CALENDAR_COMMAND_OF_KIND.values()})


@calendar_group_command.command("list")
@click.option("--kind", type=click.Choice(_CALENDAR_LIST_KINDS), default=None,
              help="Only rows of this kind (the CSV kind, or its add command's name).")
@click.option("--tag", type=completion.TAG_FILTER, default=None,
              help="Only rows carrying this tag ('role:name', or 'role:*' for a whole role).")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Also show each row's description and params_json.")
@_LISTING_SCENARIO_OPTION
@_LISTING_DATA_DIR_OPTION
def calendar_list(
    kind: str | None, tag: str | None, verbose: bool,
    scenario: str | None, data_dir: Path | None,
) -> None:
    """List the scenario's calendar rows, its own and inherited, by date.

    # is the number `sim calendar edit` and `sim calendar delete` take.
    Amounts are as written in calendar.csv. Varies sums up a varying row's
    or latent factor's vary_* cells; "~ name rho" is what it correlates
    with. Defined in says where each row comes from: this scenario, the
    ancestor it is inherited from, or "overrides <ancestor>".
    """
    from rich.markup import escape
    from rich.table import Table

    if tag is not None and ":" not in tag:
        raise click.UsageError("--tag takes 'role:name' or 'role:*'")
    if kind is not None:
        kind = next((k for k, c in _CALENDAR_COMMAND_OF_KIND.items() if c == kind), kind)
    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, scenario)
    rows = [
        entry for entry in setup_listings.calendar_listing(ref.path)
        if (kind is None or entry.row["kind"] == kind)
        and (tag is None or _tag_matches(entry.row["tags"], tag))
    ]
    if not rows:
        click.echo(f"no calendar rows in {ref.name}" + (" match" if kind or tag else ""))
        return
    table = Table(title=f"calendar ({ref.name})", header_style="bold")
    columns = ["#", "Date", "Kind", "Name", "Target", "Amount", "Varies", "Tags", "Defined in"]
    if verbose:
        columns += ["Description", "params_json"]
    for column in columns:
        table.add_column(column, justify="right" if column in ("#", "Amount") else "left")
    for entry in rows:
        row = entry.row
        amount = row["amount"] or ("balance" if row["kind"] == "transfer_balance" else "")
        cells = [
            str(entry.number), row["date"], row["kind"], row["name"],
            setup_listings.calendar_target(row), amount,
            setup_listings.vary_summary(row), row["tags"], entry.defined_in,
        ]
        if verbose:
            cells += [row["description"], row["params_json"]]
        table.add_row(*(escape(cell) for cell in cells))
    _print_wide(table)


@sim_group_command.group(name="person")
def person_group_command() -> None:
    """See and change the household's persons and the scenario's lifespans."""


@person_group_command.command("list")
@_LISTING_SCENARIO_OPTION
@_LISTING_DATA_DIR_OPTION
def person_list(scenario: str | None, data_dir: Path | None) -> None:
    """List persons.csv's persons, with each one's lifespan in the scenario.

    Age at death is lifespan.csv's age_at_death, and final month the month
    it reaches from the birthdate: the last month the person's rules still
    fire. Both are blank when the scenario sets none. Varies is how the age
    varies across ensemble members, and defined in says which scenario's
    lifespan.csv sets it.
    """
    from rich.markup import escape
    from rich.table import Table

    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, scenario)
    persons = setup_listings.person_listing(data, ref.path)
    if not persons:
        click.echo(f"no persons in {data / 'persons.csv'}")
        return
    table = Table(title=f"persons ({ref.name})", header_style="bold")
    for column in ("Id", "Display", "Birthdate", "Sex", "Role", "Age at death", "Final month",
                   "Varies", "Defined in"):
        table.add_column(column)
    for person in persons:
        row = person.row
        table.add_row(*(escape(cell) for cell in (
            row.get("id", ""), row.get("display", ""), row.get("birthdate", ""),
            row.get("sex", ""), row.get("role", ""), person.age_at_death, person.final_month,
            person.varies, person.lifespan_defined_in,
        )))
    _print_wide(table)


@person_group_command.command("show")
@click.argument("person", required=False, type=completion.PERSON)
@click.option("--graph", "as_graph", is_flag=True,
              help="Draw the chart in the terminal (kitty or iTerm2 image protocol), filling "
                   "it less three lines for the prompt, or write a PNG file when the terminal "
                   "can't show images.")
@click.option("--graph-file", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the chart to this .png or .svg file.")
@_LISTING_SCENARIO_OPTION
@_LISTING_DATA_DIR_OPTION
def person_show(
    person: str | None, as_graph: bool, graph_file: Path | None,
    scenario: str | None, data_dir: Path | None,
) -> None:
    """Show how long PERSON may live: the age-at-death distribution the
    life table gives, from the scenario start.

    The table for their sex is read older or younger so its median is the
    scenario's age at death (for a life_table lifespan, the curve an ensemble
    draws from), and the plain table's median is shown beside it; with no
    age at death, the plain table. Prints the
    percentile ages and, every five years, the chance of having died by then.

    Without PERSON, shows every person with a sex, then the household: the
    chance that at least one is still alive each year, taking the lifespans
    as independent. --graph and --graph-file draw each person's chance of
    dying at each age and of having died by it, shaded like the ensemble
    fan chart, and the household's curve.
    """
    from rich.table import Table

    from analysis.sim.lifespan_curves import household, lifespan_figure, person_lifespans

    _check_graph_options(as_graph, graph_file)
    data = _resolve_data_dir(data_dir)
    with _intent_db(data, scenario) as (con, name):
        persons, notes = person_lifespans(con)
        if person is not None:
            person = person.strip()
            sexes = dict(con.execute("SELECT id, sex FROM person").fetchall())
    for note in notes:
        click.echo(f"  note: {note}", err=True)
    if person is not None:
        if person not in sexes:
            raise click.ClickException(f"unknown person {person!r}; persons: {sorted(sexes)}")
        if not sexes[person]:
            raise click.ClickException(
                f"{person!r} has no sex in persons.csv; the life table needs one "
                f"(sim person edit {person} --sex f|m)")
        persons = [p for p in persons if p.person == person]
        if not persons:
            raise click.ClickException(f"no life table for {person!r} in {name!r}")
    if not persons:
        raise click.ClickException(f"no person in {name!r} has a sex for the life table to read")

    quantiles = (0.05, 0.2, 0.4, 0.5, 0.6, 0.8, 0.95)
    for p in persons:
        table = f"{p.table} " if p.table else ""  # a named table (#315)
        if p.centered:
            varies = {None: "fixed", "life_table": "drawn from this curve"}.get(
                p.vary, f"{p.vary} across members")
            how = (f"age at death {p.age_at_death:g} ({varies}); the {table}{p.sex} table read "
                   f"{abs(p.set_forward):.1f} years {'older' if p.set_forward >= 0 else 'younger'} "
                   f"puts its median there (the plain table's is {p.table_median:.1f})")
        else:
            how = f"the plain {table}{p.sex} table; the scenario sets no age at death"
        click.echo(f"{p.display} ({p.person}), {p.age_now:.1f} at the start of {name}: {how}")
        ages = Table(header_style="bold", title="age at death by percentile", title_justify="left")
        for q in quantiles:
            ages.add_column(f"p{round(q * 100):02d}", justify="right")
        ages.add_row(*(f"{p.quantile(q):.1f}" for q in quantiles))
        _print_wide(ages)
        years = Table(header_style="bold")
        for column in ("Age", "Year", "Died by then", "Still alive"):
            years.add_column(column, justify="right")
        for age, _chance, by_end in p.yearly():
            if (age + 1) % 5 == 0 and by_end < 0.999:
                years.add_row(str(age + 1), str(p.birthdate.year + age + 1),
                              f"{by_end:.0%}", f"{1 - by_end:.0%}")
        _print_wide(years)
    if len(persons) > 1:
        h = household(persons)
        click.echo("household: the year the last of "
                   f"{', '.join(p.person for p in persons)} dies, by percentile")
        last = Table(header_style="bold")
        for q in quantiles:
            last.add_column(f"p{round(q * 100):02d}", justify="right")
        last.add_row(*(str(h.last_death_quantile(q).year) for q in quantiles))
        _print_wide(last)
    if as_graph or graph_file is not None:
        _draw_figure(lifespan_figure(persons), graph_file, "lifespan-",
                     fit=lambda size: lifespan_figure(persons, size=size))


# ---------- person add|edit, lifespan set|clear (#192) ----------

# Conventional roles (ADR-003 §4.1), offered at the role prompt along with
# any other role persons.csv already uses; the column itself is open.
_CONVENTIONAL_ROLES = ("primary", "secondary")

_SHARED_PERSONS_NOTE = "persons.csv is shared: this changes every scenario"


def _read_persons(path: Path) -> pd.DataFrame:
    """persons.csv as written, every PERSONS_CSV_HEADER column present."""
    if path.exists() and path.stat().st_size > 0:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    else:
        df = pd.DataFrame(columns=list(registries.PERSONS_CSV_HEADER))
    if "sex" not in df.columns and "birthdate" in df.columns:
        # #222 added sex; a persons.csv from before then gains it, blank.
        df.insert(list(df.columns).index("birthdate") + 1, "sex", "")
    missing = [c for c in registries.PERSONS_CSV_HEADER if c not in df.columns]
    if missing:
        raise click.ClickException(
            f"CSV header mismatch in {path}\n"
            f"  expected: {list(registries.PERSONS_CSV_HEADER)}\n"
            f"  found:    {list(df.columns)}\n"
            f"  missing:  {missing}"
        )
    return df


def _check_birthdate(text: str) -> str:
    """A birthdate is day-precise (ages turn on it), as the loader requires."""
    try:
        parse_day(text)
    except ValueError:
        raise click.ClickException(f"birthdate must be YYYY-MM-DD: {text!r}") from None
    return text.strip()


def _check_meta_json(text: str) -> str:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        raise click.ClickException(f"invalid meta_json: {text!r}") from None
    return text


def _check_sex(text: str) -> str:
    """Blank, or one of PERSON_SEXES (which picks a life table's column, #222)."""
    sex = text.strip().lower()
    if sex and sex not in registries.PERSON_SEXES:
        raise click.ClickException(
            f"sex must be one of {list(registries.PERSON_SEXES)} or blank: {text!r}")
    return sex


def _person_fields(
    current: dict[str, str], roles: list[str], *, guided: bool,
    display: str | None, birthdate: str | None, role: str | None, meta_json: str | None,
    sex: str | None = None,
) -> dict[str, str]:
    """A person's display, birthdate, sex, role and meta_json: each one
    given, or when ``guided``, asked for with ``current``'s value as the
    default; or else ``current``'s. A blank role is no role; a blank sex is
    none (only a life_table lifespan needs one)."""
    if guided:
        if display is None:
            display = click.prompt("display", default=current.get("display") or None)
        if birthdate is None:
            birthdate = click.prompt("birthdate (YYYY-MM-DD)",
                                     default=current.get("birthdate") or None)
        if sex is None:
            sex = _prompt_with_completion(
                "sex (f or m; blank for none, needed only for a life-table lifespan)",
                list(registries.PERSON_SEXES), default=current.get("sex") or "",
            )
        if role is None:
            role = _prompt_with_completion(
                "role (blank for none)", roles, default=current.get("role") or "",
            )
        if meta_json is None:
            meta_json = click.prompt("meta_json", default=current.get("meta_json") or "{}")
    fields = {
        "display": (display if display is not None else current.get("display", "")).strip(),
        "birthdate": _check_birthdate(
            birthdate if birthdate is not None else current.get("birthdate", "")),
        "sex": _check_sex(sex if sex is not None else current.get("sex", "")),
        "role": (role if role is not None else current.get("role", "")).strip(),
        # An edit leaves a meta_json it wasn't given exactly as it is, blank included.
        "meta_json": _check_meta_json(meta_json) if meta_json is not None
        else current.get("meta_json", "{}"),
    }
    if not fields["display"]:
        raise click.ClickException("display is required")
    return fields


def _role_choices(df: pd.DataFrame) -> list[str]:
    used = [r for r in df["role"].tolist() if r]
    return sorted({*_CONVENTIONAL_ROLES, *used})


_PERSON_OPTIONS = (
    click.option("--display", default=None, help="Full or display name."),
    click.option("--birthdate", default=None, help="Birthdate, YYYY-MM-DD."),
    click.option("--sex", default=None,
                 help="f or m ('' for none); a life_table lifespan needs it."),
    click.option("--role", default=None,
                 help="primary, secondary, or another role; '' for none."),
    click.option("--meta-json", default=None, help="JSON metadata (default {})."),
    click.option("--scenario", type=completion.SCENARIO, default=None,
                 help="Scenario checked first (default: the current one); "
                      "every scenario must still load."),
    _LISTING_DATA_DIR_OPTION,
)


def _person_options(f: Callable[..., None]) -> Callable[..., None]:
    for option in reversed(_PERSON_OPTIONS):
        f = option(f)
    return f


def _file_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _require_unchanged(edit: StagedEdit, real: Path, before: bytes | None) -> None:
    """Refuse an edit built from ``real`` as it was (``before``) once the
    staged copy shows it changed since: writing the frame read then would
    silently undo that change. ``staged_edit`` only catches changes made
    after it staged the file."""
    if _file_bytes(edit.path(real)) != before:
        raise click.ClickException(
            f"changed on disk while this edit was in progress; nothing was written: {real}")


def _write_persons(
    data_dir: Path, scenario: str | None, df: pd.DataFrame, before: bytes | None,
) -> None:
    """persons.csv, written only if every scenario still loads with it, and
    only if it is still as it was (``before``) when ``df`` was read."""
    path = data_dir / "persons.csv"
    with _scenario_edit(data_dir, scenario) as edit:
        _require_unchanged(edit, path, before)
        df.to_csv(edit.path(path), index=False)


@person_group_command.command("add")
@click.argument("person_id", required=False, metavar="ID")
@_person_options
def person_add(
    person_id: str | None, display: str | None, birthdate: str | None, sex: str | None,
    role: str | None, meta_json: str | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Add a person to persons.csv, asking for a display name and birthdate
    not given (and then the sex, role and meta_json).

    persons.csv is shared by every scenario, at the data repo's top level.

    Without ID, it is asked for.
    """
    data = _resolve_data_dir(data_dir)
    path = data / "persons.csv"
    before = _file_bytes(path)
    df = _read_persons(path)
    if person_id is None:
        person_id = _ask_new_name("id for the new person")
    person_id = person_id.strip()
    if not person_id:
        raise click.ClickException("a person needs an id")
    if person_id in set(df["id"]):
        raise click.ClickException(f"person already exists: {person_id!r} (sim person edit)")
    fields = _person_fields(
        {}, _role_choices(df), guided=display is None or birthdate is None,
        display=display, birthdate=birthdate, role=role, meta_json=meta_json, sex=sex,
    )
    row = pd.DataFrame([{"id": person_id, **fields}])
    _write_persons(data, scenario, pd.concat([df, row], ignore_index=True).fillna(""), before)
    click.echo(f"added person '{person_id}' to {path} ({_SHARED_PERSONS_NOTE})")


@person_group_command.command("edit")
@click.argument("person_id", required=False, type=completion.PERSON, metavar="ID")
@_person_options
def person_edit(
    person_id: str | None, display: str | None, birthdate: str | None, sex: str | None,
    role: str | None, meta_json: str | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Change a person in persons.csv. With no options, asks for each field
    with its current value as the default; options change just those fields.

    persons.csv is shared by every scenario, at the data repo's top level.
    The id can't change here.

    Without ID, it is asked for.
    """
    data = _resolve_data_dir(data_dir)
    path = data / "persons.csv"
    before = _file_bytes(path)
    df = _read_persons(path)
    if person_id is None:
        person_id = _ask_name("person", df["id"], prompt="person to edit")
    matches = df.index[df["id"] == person_id.strip()]
    if len(matches) == 0:
        raise click.ClickException(
            f"unknown person {person_id!r}; persons: {sorted(df['id'])}")
    idx = matches[0]
    current = {c: str(df.at[idx, c]) for c in df.columns}
    guided = all(v is None for v in (display, birthdate, sex, role, meta_json))
    fields = _person_fields(
        current, _role_choices(df), guided=guided,
        display=display, birthdate=birthdate, role=role, meta_json=meta_json, sex=sex,
    )
    if all(fields[k] == current.get(k, "") for k in fields):
        click.echo(f"person '{current['id']}' unchanged")
        return
    for column, value in fields.items():
        df.at[idx, column] = value
    _write_persons(data, scenario, df, before)
    click.echo(f"changed person '{current['id']}' in {path} ({_SHARED_PERSONS_NOTE})")


@person_group_command.group(name="lifespan")
def lifespan_group_command() -> None:
    """Set or clear a person's age at death in the scenario's lifespan.csv."""


def _own_lifespans(path: Path) -> pd.DataFrame:
    if path.exists() and path.stat().st_size > 0:
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    return pd.DataFrame(columns=list(registries.LIFESPAN_CSV_HEADER))


_LIFESPAN_VARY_COLUMNS = (
    "vary_distribution", "vary_low", "vary_high", "vary_mode",
    "vary_correlate_with", "vary_correlation",
)
_NO_VARY = "none"


def _check_age(text: str, what: str) -> str:
    """An age in years as typed, checked to be a number (the loader checks
    its range against the rest of the row)."""
    try:
        float(text)
    except ValueError:
        raise click.ClickException(f"{what} must be an age in years: {text!r}") from None
    return text.strip()


def _lifespan_fields(
    current: dict[str, str], parents: list[str], *, guided: bool,
    age: str | None, vary: str | None, low: str | None, high: str | None, mode: str | None,
    correlate_with: str | None, correlation: str | None,
    life_table: str | None = None, tables: Sequence[str] = (),
) -> dict[str, str]:
    """A lifespan row's age, vary_* and life_table cells (#221, #315): each
    one given, or when ``guided``, asked for with ``current``'s value as the
    default; or else ``current``'s. ``vary`` 'none' (or a blank one) drops
    the distribution and its bounds; a blank mode is the age; a blank
    correlate-with drops the correlation; a blank life table is the default
    life_table.csv, and ``tables`` names the others to offer."""
    if guided:
        if age is None:
            age = click.prompt("age at death (years, e.g. 84.5)",
                               default=current.get("age_at_death") or None)
        if vary is None:
            vary = click.prompt(
                "vary across ensemble members",
                type=click.Choice([_NO_VARY, *registries.LIFESPAN_DISTRIBUTIONS]),
                default=current.get("vary_distribution") or _NO_VARY,
            )
        if vary != _NO_VARY:
            # A life table draws around age_at_death as its median (#222): no bounds.
            if low is None and vary != registries.LIFE_TABLE_DISTRIBUTION:
                low = click.prompt("lowest age", default=current.get("vary_low") or None)
            if high is None and vary != registries.LIFE_TABLE_DISTRIBUTION:
                high = click.prompt("highest age", default=current.get("vary_high") or None)
            if vary == "triangular" and mode is None:
                mode = click.prompt("most likely age (blank: the age at death)",
                                    default=current.get("vary_mode") or "", show_default=False)
            if correlate_with is None:
                correlate_with = _prompt_with_completion(
                    "correlate with (a lifespan:<person>, varying row or factor; blank for none)",
                    parents, default=current.get("vary_correlate_with") or "",
                )
            if correlate_with and correlation is None:
                correlation = click.prompt(
                    "correlation (-1..1)", default=current.get("vary_correlation") or None,
                )
        if life_table is None and (tables or current.get("life_table")):
            life_table = _prompt_with_completion(
                f"life table ({', '.join(tables)}; blank for the default)", list(tables),
                default=current.get("life_table") or "",
            )

    def pick(given: str | None, column: str) -> str:
        return (given if given is not None else current.get(column, "")).strip()

    fields = {"age_at_death": pick(age, "age_at_death"), "life_table": pick(life_table, "life_table")}
    if not fields["age_at_death"]:
        raise click.ClickException("an age at death is required")
    _check_age(fields["age_at_death"], "age at death")
    distribution = pick(vary, "vary_distribution")
    if distribution in ("", _NO_VARY):
        return fields | dict.fromkeys(_LIFESPAN_VARY_COLUMNS, "")
    if distribution not in registries.LIFESPAN_DISTRIBUTIONS:
        raise click.ClickException(
            f"--vary must be {_NO_VARY} or one of {list(registries.LIFESPAN_DISTRIBUTIONS)}"
        )
    if distribution == registries.LIFE_TABLE_DISTRIBUTION:
        given = [f"--{name}" for name, value in (("low", low), ("high", high), ("mode", mode))
                 if value]
        if given:
            raise click.ClickException(
                f"life_table draws around the age at death, its median; {', '.join(given)} "
                "don't apply")
        low = high = mode = ""
    fields |= {
        "vary_distribution": distribution,
        "vary_low": pick(low, "vary_low"),
        "vary_high": pick(high, "vary_high"),
        "vary_mode": pick(mode, "vary_mode") if distribution == "triangular" else "",
        "vary_correlate_with": pick(correlate_with, "vary_correlate_with"),
        "vary_correlation": pick(correlation, "vary_correlation"),
    }
    if distribution == registries.LIFE_TABLE_DISTRIBUTION:
        fields["vary_low"] = fields["vary_high"] = ""
    for column, what in (("vary_low", "lowest age"), ("vary_high", "highest age"),
                         ("vary_mode", "most likely age")):
        if fields[column]:
            _check_age(fields[column], what)
    if not fields["vary_correlate_with"]:
        fields["vary_correlation"] = ""
    return fields


def _lifespan_summary(row: dict[str, str]) -> str:
    """A lifespan row as one phrase: ``age 84 (triangular 75..95)``."""
    vary = setup_listings.lifespan_vary_text(row)
    notes = [n for n in (vary, row.get("life_table") and f"{row['life_table']} table") if n]
    return f"age {row.get('age_at_death', '')}" + (f" ({', '.join(notes)})" if notes else "")


def _lifespan_parents(data_dir: Path, scenario: str, person: str) -> list[str]:
    """Correlation parents to offer: the scenario's varying calendar rows,
    its latent factors, and the other persons' varying lifespans -- the
    nodes the loader accepts as a parent."""
    with _intent_db(data_dir, scenario) as (con, _name):
        factors = [name for (name,) in con.execute(
            "SELECT name FROM calendar_event WHERE kind = ? AND name IS NOT NULL",
            (registries.LATENT_FACTOR_KIND,),
        )]
        nodes = varying_names(con)
    own = registries.LIFESPAN_NODE_PREFIX + person
    return sorted({*nodes, *factors} - {own})


@lifespan_group_command.command("set")
@click.argument("person", required=False, type=completion.PERSON)
@click.argument("age", required=False)
@click.option("--vary", default=None,
              help=f"How the age varies across ensemble members: {_NO_VARY}, "
                   f"{', '.join(registries.LIFESPAN_DISTRIBUTIONS)}.")
@click.option("--low", default=None, help="Lowest age a member draws.")
@click.option("--high", default=None, help="Highest age a member draws.")
@click.option("--mode", default=None,
              help="Most likely age, for triangular ('' for the age at death).")
@click.option("--correlate-with", type=completion.CORRELATE_WITH, default=None,
              help="Correlation parent: lifespan:<person>, a varying calendar row "
                   "or a latent factor ('' for none).")
@click.option("--correlation", default=None, help="Correlation with that parent, -1..1.")
@click.option("--life-table", type=completion.LIFE_TABLE, default=None,
              help="The aux life_table__<name>.csv to read the age against, by <name> "
                   "('' for the default life_table.csv).")
@_LISTING_SCENARIO_OPTION
@_LISTING_DATA_DIR_OPTION
def lifespan_set(
    person: str | None, age: str | None, vary: str | None, low: str | None, high: str | None,
    mode: str | None, correlate_with: str | None, correlation: str | None,
    life_table: str | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Set PERSON's AGE at death in years (e.g. 84.5), and how it varies.

    The final month, the last month the person's rules fire, is the birth
    month plus the age in months. In an ensemble each member draws an age
    from --vary (uniform or triangular between --low and --high, peaking at
    --mode); the base case, member 0, is AGE. With --vary life_table the age
    is drawn from the aux life table for the person's sex, shifted so its
    median is AGE (no --low, --high or --mode); the shift and the
    percentile ages it gives are printed. --life-table reads another
    population's table, aux/life_table__<name>.csv, for this person in this
    scenario; AGE is then the median on that table.

    With no AGE and no options, asks for each field with its current value
    as the default; otherwise just the fields given change. Written to this
    scenario's own lifespan.csv; one it inherits is overridden here, and the
    parent keeps its own.

    Without PERSON, it is asked for.
    """
    data = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data, scenario)
    path = scenario_dir / "lifespan.csv"
    if person is None:
        person = _ask_name("person", _read_persons(data / "persons.csv")["id"])
    person = person.strip()
    persons = set(_read_persons(data / "persons.csv")["id"])
    if person not in persons:
        raise click.ClickException(f"unknown person {person!r}; persons: {sorted(persons)}")
    before = _file_bytes(path)
    found = _overlay_rows(scenario_dir, "lifespan.csv", key="person").get(person)
    current = {k: v for k, v in (found.row if found else {}).items() if k != "final_month"}
    given = (age, vary, low, high, mode, correlate_with, correlation, life_table)
    guided = all(v is None for v in given)
    fields = _lifespan_fields(
        current, _lifespan_parents(data, scenario_name, person) if guided else [],
        guided=guided,
        age=age, vary=vary, low=low, high=high, mode=mode,
        correlate_with=correlate_with, correlation=correlation, life_table=life_table,
        tables=names.series_names(data, scenario_name, "life_table") if guided else (),
    )
    row = {"person": person, **fields}
    df = _own_lifespans(path)
    with _scenario_edit(data, scenario_name) as edit:
        _require_unchanged(edit, path, before)
        _append_csv_row(
            edit.path(path), registries.LIFESPAN_CSV_HEADER, row,
            replace=None if found is None or found.index is None else df.index[found.index],
        )
    if found is not None and found.index is None:
        click.echo(f"set {person}'s lifespan to {_lifespan_summary(row)} in {path}, overriding "
                   f"{_lifespan_summary(found.row)} from {found.inherited_from!r}")
    else:
        click.echo(f"set {person}'s lifespan to {_lifespan_summary(row)} in {path}")
    if row["vary_distribution"] == registries.LIFE_TABLE_DISTRIBUTION:
        click.echo(_life_table_summary(data, scenario_name, person))


def _life_table_summary(data_dir: Path, scenario: str, person: str) -> str:
    """What a life_table lifespan means (#222): its solved set-forward, the
    table rating that would give the same median, and its percentile ages."""
    with _intent_db(data_dir, scenario) as (con, _name):
        age, set_forward = con.execute(
            "SELECT age_at_death, life_table_set_forward FROM person_lifespan WHERE person = ?",
            (person,),
        ).fetchone()
        dist = life_table_age(con, person, set_forward)
    rating = equivalent_multiplier(dist.table, dist.start, round(age * 12))
    ages = ", ".join(f"{round(q * 100)}th {dist.quantile(q):.1f}"
                     for q in (0.1, 0.25, 0.5, 0.75, 0.9))
    lines = [f"  life table read {abs(set_forward):.1f} years "
             f"{'older' if set_forward >= 0 else 'younger'} (set-forward {set_forward:+.1f})"]
    if rating is not None:
        lines.append(f"  the same median as about {rating:.0%} of the table's death rates")
    lines.append(f"  age at death by percentile: {ages}")
    return "\n".join(lines)


@lifespan_group_command.command("clear")
@click.argument("person", required=False, type=completion.PERSON)
@_LISTING_SCENARIO_OPTION
@_LISTING_DATA_DIR_OPTION
def lifespan_clear(person: str | None, scenario: str | None, data_dir: Path | None) -> None:
    """Remove PERSON's row from this scenario's lifespan.csv.

    A lifespan the scenario inherits can't be cleared here, since a row
    can only override a lifespan, not remove it: clear it where it is
    set, or set another age here.

    Without PERSON, one with a lifespan set in this scenario is asked for.
    """
    data = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data, scenario)
    path = scenario_dir / "lifespan.csv"
    if person is None:
        own = _overlay_rows(scenario_dir, "lifespan.csv", key="person")
        person = _ask_name("person", [p for p, row in own.items() if row.index is not None],
                           prompt="person whose lifespan to clear",
                           plural="persons with a lifespan set here")
    person = person.strip()
    before = _file_bytes(path)
    found = _overlay_rows(scenario_dir, "lifespan.csv", key="person").get(person)
    if found is None:
        raise click.ClickException(f"{person!r} has no lifespan in scenario {scenario_name!r}")
    if found.index is None:
        raise click.ClickException(
            f"{person}'s lifespan ({_lifespan_summary(found.row)}) is inherited from "
            f"{found.inherited_from!r}; clear it there, or set another age here "
            "(sim person lifespan set)"
        )
    df = _own_lifespans(path)
    remaining = df.drop(index=df.index[found.index])
    with _scenario_edit(data, scenario_name) as edit:
        _require_unchanged(edit, path, before)
        if remaining.empty:
            edit.path(path).unlink()
        else:
            remaining.to_csv(edit.path(path), index=False)
    if found.inherited_from is not None:
        after = _overlay_rows(scenario_dir, "lifespan.csv", key="person").get(person)
        if after is not None:
            click.echo(f"cleared {person}'s lifespan in {path}; it now inherits "
                       f"{_lifespan_summary(after.row)} from {after.inherited_from!r}")
            return
    click.echo(f"cleared {person}'s lifespan in {path}")


@sim_group_command.group(name="ytd")
def ytd_group_command() -> None:
    """See and change the scenario's year-to-date catch-up rows (ADR-003 §4.10)."""


@ytd_group_command.command("list")
@_LISTING_SCENARIO_OPTION
@_LISTING_DATA_DIR_OPTION
def ytd_list(scenario: str | None, data_dir: Path | None) -> None:
    """List year_to_date.csv's rows, then the scheduled rows carrying a
    ytd_amount or ytd_preroll.

    Amounts are as written: dollars, an @itemization, or "preroll" for a
    scheduled row whose first year is pre-rolled. Year is year_to_date.csv's
    year, or a scheduled row's tax_year.
    """
    from rich.markup import escape
    from rich.table import Table

    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, scenario)
    rows = setup_listings.ytd_listing(ref.path)
    if not rows:
        click.echo(f"no year-to-date rows in {ref.name}")
        return
    table = Table(title=f"year to date ({ref.name})", header_style="bold")
    for column in ("Name", "Source", "From", "To", "Amount", "Tax", "Year",
                   "Description", "Defined in"):
        table.add_column(column, justify="right" if column == "Amount" else "left")
    for row in rows:
        table.add_row(*(escape(cell) for cell in (
            row.name, row.source, row.from_account, row.to_account, row.amount,
            row.tax_character, row.year, row.description, row.defined_in,
        )))
    _print_wide(table)


# ---------- ytd add|edit|delete: one year_to_date.csv row (#193) ----------

_YTD_CHECKERS: dict[str, Callable[[str], object]] = {
    "money": _check_money,
    "year": _check_tax_year,
}

# The completing type of each option of ``ytd_guide.ROW``: a row's sides are
# accounts, never chains (ADR-003 §4.10).
_YTD_OPTION_TYPES: dict[str, click.ParamType] = {
    "from_account": completion.ACCOUNT,
    "to_account": completion.ACCOUNT,
    "from_lot": completion.LotName("from_account"),
    "to_lot": completion.LotName("to_account"),
    "tax_character": click.Choice(sorted(registries.TAX_CHARACTERS)),
}


def _ytd_guide() -> calendar_guide.Guide:
    return calendar_guide.Guide(
        ask=lambda text, choices, default: _prompt_with_completion(
            text, choices=choices, default=default,
        ),
        checkers=_YTD_CHECKERS,
    )


def _ytd_context(data_dir: Path, scenario: str) -> calendar_guide.GuideContext:
    """The scenario's accounts and their lots, as completions for a
    year-to-date row's prompts."""
    with _intent_db(data_dir, scenario) as (con, _name):
        return calendar_guide.GuideContext(
            accounts=sorted(r[0] for r in con.execute("SELECT name FROM account")),
            lots=_lot_info(con),
        )


def _ytd_row(
    params: dict[str, object], current: dict[str, str], context: calendar_guide.GuideContext,
) -> dict[str, str]:
    """``current`` with ``params``' answers: a field the answers leave out is
    blank, except a lot the guide didn't ask about, which keeps its cell
    while its account does (a spelled-out ``default``) and is blank once the
    account changes."""
    answers = {column: str(params.get(column, "") or "").strip() for column in ytd_guide.ROW_COLUMNS}
    row = dict(current)
    for field_ in ytd_guide.ROW.fields:
        column = field_.param
        if field_.applies(context, answers) or column not in ("from_lot", "to_lot"):
            row[column] = answers[column]
        else:
            account = column.replace("_lot", "_account")
            if answers[account] != current[account]:
                row[column] = ""
    row["amount"] = _money_cell(row["amount"]) if row["amount"] else ""
    return row


def _check_ytd_row(row: dict[str, str]) -> None:
    """What the loader would refuse of one row on its own, said before
    anything is staged."""
    for field_ in ytd_guide.ROW.fields:
        if field_.is_required(row) and field_.param not in ("from_lot", "to_lot") and not row[field_.param]:
            raise click.ClickException(f"{field_.name} is required")
    if row["tax_character"] not in registries.TAX_CHARACTERS:
        raise click.ClickException(f"unknown tax character: {row['tax_character']!r}")
    if row["year"]:
        _check_tax_year(row["year"])


def _read_ytd_rows(path: Path) -> list[dict[str, str]]:
    """A scenario's own year_to_date.csv rows, every column present."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [
        {column: str(row.get(column, "")) for column in registries.YEAR_TO_DATE_CSV_HEADER}
        for row in frame.to_dict("records")
    ]


def _widen_header(staged: Path, scenario_dir: Path, filename: str, header: tuple[str, ...]) -> None:
    """Give a staged CSV keyed by ``name`` every column of ``header``, so a
    row can be written by name: the loader takes a header that leaves
    optional columns out.

    A column a scenario's file leaves out is inherited, row by row, so each
    existing row gets the value it inherits there (from the real
    ``scenario_dir``'s lineage), blank when nothing above has one."""
    if not staged.exists() or staged.stat().st_size == 0:
        return
    frame = pd.read_csv(staged, dtype=str, keep_default_na=False)
    missing = [c for c in header if c not in frame.columns]
    if not missing:
        return
    merged = _overlay_rows(scenario_dir, filename)
    for column in missing:
        frame[column] = [
            found.row.get(column, "") if (found := merged.get(str(name).strip())) else ""
            for name in frame["name"]
        ]
    frame.to_csv(staged, index=False)


def _widen_ytd_header(staged: Path, scenario_dir: Path) -> None:
    _widen_header(staged, scenario_dir, "year_to_date.csv", registries.YEAR_TO_DATE_CSV_HEADER)


class _PickedRow(NamedTuple):
    found: _Overlaid  # the row as the load order leaves it
    own: dict[str, str] | None  # its row in this scenario's own file, as read then


def _pick_ytd(scenario_dir: Path, scenario_name: str, name: str) -> _PickedRow:
    found = _overlay_rows(scenario_dir, "year_to_date.csv").get(name.strip())
    if found is None:
        raise click.ClickException(
            f"no year-to-date row {name!r} in scenario {scenario_name!r} "
            "(a scheduled rule's ytd_amount or ytd_preroll is changed with `sim scheduled edit`)"
        )
    own = None
    if found.index is not None:
        own = _read_ytd_rows(scenario_dir / "year_to_date.csv")[found.index]
    return _PickedRow(found, own)


def _write_ytd(
    data_dir: Path, scenario_name: str, path: Path, picked: _PickedRow, row: dict[str, str] | None,
) -> None:
    """Write ``row`` over the scenario's own row -- or append it as an
    override when the row is inherited; ``None`` removes the own row -- as
    one staged edit."""
    index = picked.found.index
    with _scenario_edit(data_dir, scenario_name) as edit:
        staged = edit.path(path)
        if index is not None:
            # Read inside the staged edit, before widening fills in inherited
            # cells: the prompts may have taken a while, and the index is only
            # good if the file still has the row there, as it was read.
            existing = _read_ytd_rows(staged)
            if index >= len(existing) or existing[index] != picked.own:
                raise click.ClickException(
                    f"{path} changed while this row was being edited. Nothing was written; start again."
                )
        _widen_ytd_header(staged, path.parent)
        if index is None:
            if row is not None:
                _append_csv_row(staged, registries.YEAR_TO_DATE_CSV_HEADER, row)
            return
        if row is None:
            frame = pd.read_csv(staged, dtype=str, keep_default_na=False)
            frame.drop(index=frame.index[index]).to_csv(staged, index=False)
        else:
            _append_csv_row(staged, registries.YEAR_TO_DATE_CSV_HEADER, row, replace=index)


def _ytd_summary(row: dict[str, str]) -> str:
    sides = [
        "/".join(filter(None, (row[f"{side}_account"], row[f"{side}_lot"])))
        for side in ("from", "to")
    ]
    year = f", {row['year']}" if row["year"] else ""
    return f"{sides[0]} -> {sides[1]} {row['amount']} ({row['tax_character']}{year})"


@ytd_group_command.command("add")
@click.argument("name", required=False)
@click.argument("from_account", required=False, type=completion.ACCOUNT)
@click.argument("to_account", required=False, type=completion.ACCOUNT)
@click.argument("amount", required=False)
@click.option("--from-lot", type=completion.LotName("from_account"), default=None,
              help="Lot of the from account; asked for when a blank one would not say which.")
@click.option("--to-lot", type=completion.LotName("to_account"), default=None,
              help="Lot of the to account; asked for when a blank one would not say which.")
@click.option("--tax-character", type=click.Choice(sorted(registries.TAX_CHARACTERS)),
              default=None, help="How the amount is taxed; asked for when left out.")
@click.option("--year", default=None,
              help="The tax year of a fact from a whole prior year (default: the "
                   "scenario's first year).")
@click.option("--description", default="")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def ytd_add(
    name: str | None, from_account: str | None, to_account: str | None, amount: str | None,
    from_lot: str | None, to_lot: str | None, tax_character: str | None, year: str | None,
    description: str, scenario: str | None, data_dir: Path | None,
) -> None:
    """Append a row to <scenario>/year_to_date.csv: money that moved, or a
    tax amount, from before the scenario starts (ADR-003 §4.10).

    AMOUNT is dollars or an @itemization. A negative one (a loss carried
    forward) goes after `--`, so it isn't read as an option:
    `sim ytd add loss_2025 Income_CapitalGain Income_CapitalGain --tax-character ltcg --year 2025 -- -1500`.
    Whatever is left out and needed is asked for. `sim ytd edit` changes a
    row later.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    context = _ytd_context(data_dir_p, scenario_name)
    taken = set(names.ytd_names(data_dir_p, scenario_name))
    if name is not None:
        name = name.strip()  # the loader strips every cell
    if not name:
        while (name := _ask_new_name("name of the new row")) in taken:
            click.echo(f"  another row is already named {name!r}")
    if name in taken:
        raise click.ClickException(
            f"year-to-date row {name!r} already exists; `sim ytd edit {name}` changes it"
        )
    answers: calendar_guide.Answers = {
        "from_account": from_account or "", "from_lot": from_lot or "",
        "to_account": to_account or "", "to_lot": to_lot or "",
        "amount": amount or "", "tax_character": tax_character or "",
        "year": year or "", "description": description,
    }
    for column, checker in (("amount", _check_money), ("year", _check_tax_year)):
        if answers[column]:
            checker(answers[column])
    guide = _ytd_guide()
    for field_ in ytd_guide.ROW.fields:
        if (field_.applies(context, answers) and field_.is_required(answers)
                and not answers[field_.param]):
            answers[field_.param] = guide.ask_field(ytd_guide.ROW, field_, context, answers)
    row = _ytd_row(dict(answers), {"name": name, **ytd_guide.answers_of(answers)}, context)
    _check_ytd_row(row)
    path = scenario_dir / "year_to_date.csv"
    with _scenario_edit(data_dir_p, scenario_name) as edit:
        staged = edit.path(path)
        _widen_ytd_header(staged, scenario_dir)
        _append_csv_row(staged, registries.YEAR_TO_DATE_CSV_HEADER, row)
    click.echo(f"appended year-to-date row '{name}' {_ytd_summary(row)} to {path}")


def _ytd_options[F: Callable[..., object]](f: F) -> F:
    """One option per field of ``ytd_guide.ROW``. Each defaults to ``None``
    (not given); ``""`` clears a field."""
    for field_ in reversed(ytd_guide.ROW.fields):
        if field_.option is not None:
            f = click.option(
                field_.option, field_.param, default=None, help=field_.help or field_.label,
                type=_YTD_OPTION_TYPES.get(field_.param),
            )(f)
    return f


@ytd_group_command.command("edit")
@click.argument("name", required=False, type=completion.YTD_ROW)
@_ytd_options
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.pass_context
def ytd_edit(
    ctx: click.Context, name: str | None, scenario: str | None, data_dir: Path | None,
    **values: str | None,
) -> None:
    """Change a year-to-date row.

    Options set those fields and write at once ("" clears one). With none,
    the row's fields are shown; name one to change it, as often as needed,
    and write when done.

    A row the scenario inherits is changed by an override: the whole row,
    edited, appended to this scenario's year_to_date.csv; the parent keeps
    its own.

    Without NAME, it is asked for.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "year_to_date.csv"
    if name is None:
        name = _ask_name("year-to-date row", names.ytd_names(data_dir_p, scenario_name),
                         prompt="row to edit")
    picked = _pick_ytd(scenario_dir, scenario_name, name)
    chosen = picked.found
    current = {column: chosen.row.get(column, "") for column in registries.YEAR_TO_DATE_CSV_HEADER}
    context = _ytd_context(data_dir_p, scenario_name)
    row_name = current["name"]

    def submit(params: dict[str, object]) -> None:
        row = _ytd_row(params, current, context)
        _check_ytd_row(row)
        if all(row[column].strip() == current[column].strip()
               for column in registries.YEAR_TO_DATE_CSV_HEADER):
            click.echo("no change; nothing was written")
            return
        _write_ytd(data_dir_p, scenario_name, path, picked, row)
        if chosen.index is None:
            click.echo(
                f"wrote an override of year-to-date row '{row_name}' "
                f"(inherited from {chosen.inherited_from!r}) to {path}"
            )
        else:
            click.echo(f"updated year-to-date row '{row_name}' in {path}")

    answers = ytd_guide.answers_of(current)
    given = {param: value.strip() for param, value in values.items() if value is not None}
    if given:
        for column, checker in (("amount", _check_money), ("year", _check_tax_year)):
            if given.get(column):
                checker(given[column])
        for side in ("from", "to"):
            account, lot = f"{side}_account", f"{side}_lot"
            if account in given and given[account] != current[account] and lot not in given:
                # The old lot is another account's; one of the same name
                # here would be a different holding.
                given[lot] = ""
                if calendar_guide.lot_needed(context.lots.get(given[account])):
                    raise click.ClickException(
                        f"--{side}-lot is needed for {given[account]}: one of "
                        f"{', '.join(context.lots[given[account]][1])}"
                    )
        submit({**answers, **given})
        return
    _echo_guided_scenario(scenario_name, path, defaulted=scenario is None)
    click.echo(f"Editing year-to-date row '{row_name}'")
    if chosen.index is None:
        click.echo(
            f"  It comes from scenario {chosen.inherited_from!r}: writing puts an override "
            "of the whole row in this scenario."
        )
    guide = _ytd_guide()
    guide.complete(ytd_guide.ROW, context, answers)
    if not guide.review(ytd_guide.ROW, context, answers, submit, show_command=False):
        ctx.exit(1)


@ytd_group_command.command("delete")
@click.argument("name", required=False, type=completion.YTD_ROW)
@click.option("--yes", is_flag=True, help="Don't ask first.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def ytd_delete(name: str | None, yes: bool, scenario: str | None, data_dir: Path | None) -> None:
    """Delete a year-to-date row.

    A row of this scenario's own is removed. Deleting this scenario's
    override of an inherited row brings back the inherited row.

    A row the scenario inherits can't be removed from here, since an
    overlay can't remove a row (ADR-003 §4.5). Instead its amount is set to
    zero by an override, so it posts nothing in this scenario.

    Without NAME, it is asked for.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "year_to_date.csv"
    if name is None:
        name = _ask_name("year-to-date row", names.ytd_names(data_dir_p, scenario_name),
                         prompt="row to delete")
    picked = _pick_ytd(scenario_dir, scenario_name, name)
    chosen = picked.found
    row_name = chosen.row["name"]

    if chosen.index is not None:
        if chosen.inherited_from is not None:
            prompt = (f"delete this scenario's override of '{row_name}', so the row from "
                      f"{chosen.inherited_from!r} applies again?")
        else:
            prompt = f"delete year-to-date row '{row_name}'?"
        if not yes and not click.confirm(prompt, default=False):
            click.echo("aborted")
            return
        _write_ytd(data_dir_p, scenario_name, path, picked, None)
        if chosen.inherited_from is not None:
            click.echo(
                f"deleted this scenario's override of year-to-date row '{row_name}' from {path}; "
                f"it is inherited from {chosen.inherited_from!r} again"
            )
        else:
            click.echo(f"deleted year-to-date row '{row_name}' from {path}")
        return

    current = {column: chosen.row.get(column, "") for column in registries.YEAR_TO_DATE_CSV_HEADER}
    if not current["amount"].startswith("@") and _parse_amount_cents(current["amount"]) == 0:
        click.echo(f"year-to-date row '{row_name}' is already zero; nothing was written")
        return
    click.echo(
        f"year-to-date row '{row_name}' comes from scenario {chosen.inherited_from!r}, and "
        f"{scenario_name!r} can't remove what it inherits. It can zero it instead: an "
        "override with amount 0, so it posts nothing here."
    )
    if not yes and not click.confirm("write that override?", default=False):
        click.echo("aborted")
        return
    _write_ytd(data_dir_p, scenario_name, path, picked, {**current, "amount": "0.00"})
    click.echo(f"wrote an override zeroing year-to-date row '{row_name}' to {path}")


# ---------- guard-rail list|add|edit|delete: guard_rails.csv (#305) ----------


@sim_group_command.group(name="guard-rail")
def guard_rail_group_command() -> None:
    """See and change the scenario's guard rails (ADR-003 §4.11).

    A guard rail is a line the scenario's spending, or its withdrawals from
    the portfolio, should stay on one side of: the 4% rule, Guyton-Klinger's
    rails, a budget. Rails are a lens on a run's results, not an input to
    it, so changing one reruns nothing; `sim report guard-rails` shows where
    the latest run stands.
    """


@guard_rail_group_command.command("list")
@_LISTING_SCENARIO_OPTION
@_LISTING_DATA_DIR_OPTION
def guard_rail_list(scenario: str | None, data_dir: Path | None) -> None:
    """List the scenario's guard rails, its own and inherited."""
    from rich.markup import escape
    from rich.table import Table

    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, scenario)
    rows, issues = guard_rails.read_rows(ref.scenarios_dir, ref.name)
    rails, more = guard_rails.parse_rows(rows)
    for issue in [*issues, *more]:
        click.echo(f"  error: {issue}", err=True)
    if not rails:
        click.echo(f"no guard rails in {ref.name}")
        return
    table = Table(title=f"guard rails ({ref.name})", header_style="bold")
    for column in ("Name", "Rail", "Sense", "Start", "End", "Description", "Defined in"):
        table.add_column(column)
    for rail in rails:
        table.add_row(*(escape(cell) for cell in (
            rail.name, rail.describe(), rail.sense, rail.start[:7] if rail.start[:1].isdigit()
            else rail.start, rail.end[:7] if rail.end[:1].isdigit() else rail.end,
            rail.description, rail.defined_in,
        )))
    _print_wide(table)


def _check_tag_filter(text: str) -> str:
    try:
        guard_rails.TagFilter.parse(text)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    return text.strip()


def _check_percent_threshold(text: str) -> int:
    try:
        return units.percent_to_micros(text.strip())
    except ValueError as exc:
        raise click.ClickException(f"{exc}; this basis is a percent of the portfolio") from None


def _check_dollar_threshold(text: str) -> int:
    if text.strip().endswith("%"):
        raise click.ClickException(
            f"threshold {text.strip()!r} is a percent: only basis portfolio or initial_portfolio "
            "takes one; the others take dollars a year")
    try:
        return units.dollars_to_cents(text.strip())
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


# By field type; a threshold's is "percent" or "dollars" by its basis.
_GUARD_RAIL_CHECKERS: dict[str, Callable[[str], object]] = {
    "tag_filter": _check_tag_filter,
    "percent": _check_percent_threshold,
    "dollars": _check_dollar_threshold,
    "date": _validate_calendar_date,
}


def _check_guard_rail_options(given: dict[str, str], answers: calendar_guide.Answers) -> None:
    """Each option given checked as its field's type, which may depend on
    the rail's other fields (``answers``, with the options in)."""
    for field_ in guard_rail_guide.ROW.fields:
        value = given.get(field_.param)
        type_ = calendar_guide.value_type(guard_rail_guide.ROW, field_, answers)
        if value and (checker := _GUARD_RAIL_CHECKERS.get(type_)) is not None:
            checker(value)

_GUARD_RAIL_OPTION_TYPES: dict[str, click.ParamType] = {
    "measure": click.Choice(registries.GUARD_RAIL_MEASURES),
    "side": click.Choice(registries.GUARD_RAIL_SIDES),
    "basis": click.Choice(registries.GUARD_RAIL_BASES),
    "sense": click.Choice(registries.GUARD_RAIL_SENSES),
    "select": completion.TAG_FILTER,
    "portfolio": completion.TAG_FILTER,
    "start": completion.CALENDAR_DATE,
    "end": completion.CALENDAR_DATE,
}


def _guard_rail_guide() -> calendar_guide.Guide:
    return calendar_guide.Guide(
        ask=lambda text, choices, default: _prompt_with_completion(
            text, choices=choices, default=default,
        ),
        checkers=_GUARD_RAIL_CHECKERS,
    )


def _guard_rail_context(data_dir: Path, scenario: str) -> calendar_guide.GuideContext:
    """The scenario's tags and date markers, as completions."""
    return calendar_guide.GuideContext(
        markers=names.date_marker_names(data_dir, scenario),
        tags=names.tag_tokens(data_dir, scenario),
    )


def _guard_rail_row(params: dict[str, object], current: dict[str, str],
                    context: calendar_guide.GuideContext) -> dict[str, str]:
    """``current`` with ``params``' answers; a field that no longer applies
    (``select`` once the measure is withdrawals) is blank."""
    answers = {column: str(params.get(column, "") or "").strip()
               for column in guard_rail_guide.ROW_COLUMNS}
    row = dict(current)
    for field_ in guard_rail_guide.ROW.fields:
        row[field_.param] = answers[field_.param] if field_.applies(context, answers) else ""
    if row["threshold"] and not row["threshold"].endswith("%"):
        row["threshold"] = _money_cell(row["threshold"])
    return row


def _check_guard_rail_row(row: dict[str, str]) -> None:
    """What the loader would refuse of one rail on its own, said before
    anything is staged."""
    _rail, problems = guard_rails.parse_cells(row)
    if problems:
        raise click.ClickException("; ".join(problems))


def _read_guard_rail_rows(path: Path) -> list[dict[str, str]]:
    """A scenario's own guard_rails.csv rows, every column present."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [{column: str(row.get(column, "")) for column in registries.GUARD_RAILS_CSV_HEADER}
            for row in frame.to_dict("records")]


def _pick_guard_rail(scenario_dir: Path, scenario_name: str, name: str) -> _PickedRow:
    found = _overlay_rows(scenario_dir, guard_rails.GUARD_RAILS_FILE).get(name.strip())
    if found is None:
        raise click.ClickException(f"no guard rail {name!r} in scenario {scenario_name!r}")
    own = None
    if found.index is not None:
        own = _read_guard_rail_rows(scenario_dir / guard_rails.GUARD_RAILS_FILE)[found.index]
    return _PickedRow(found, own)


def _write_guard_rail(data_dir: Path, scenario_name: str, path: Path, picked: _PickedRow | None,
                      row: dict[str, str] | None) -> None:
    """Write ``row`` over the scenario's own rail, or append it (a new rail,
    or an override of an inherited one); ``None`` removes the own row. One
    staged edit, kept only if every affected scenario still loads."""
    header = registries.GUARD_RAILS_CSV_HEADER
    index = picked.found.index if picked is not None else None
    with _scenario_edit(data_dir, scenario_name) as edit:
        staged = edit.path(path)
        if index is not None:
            existing = _read_guard_rail_rows(staged)
            if picked is not None and (index >= len(existing) or existing[index] != picked.own):
                raise click.ClickException(
                    f"{path} changed while this rail was being edited. Nothing was written; start again."
                )
        _widen_header(staged, path.parent, path.name, header)
        if index is None:
            if row is not None:
                _append_csv_row(staged, header, row)
            return
        if row is None:
            frame = pd.read_csv(staged, dtype=str, keep_default_na=False)
            frame.drop(index=frame.index[index]).to_csv(staged, index=False)
        else:
            _append_csv_row(staged, header, row, replace=index)


def _guard_rail_options[F: Callable[..., object]](f: F) -> F:
    """One option per field of ``guard_rail_guide.ROW``. Each defaults to
    ``None`` (not given); ``""`` clears a field."""
    for field_ in reversed(guard_rail_guide.ROW.fields):
        if field_.option is not None:
            f = click.option(
                field_.option, field_.param, default=None, help=field_.help or field_.label,
                type=_GUARD_RAIL_OPTION_TYPES.get(field_.param),
            )(f)
    return f


def _rail_summary(row: dict[str, str]) -> str:
    rail, _problems = guard_rails.parse_cells(row)
    return rail.describe() if rail is not None else ""


@guard_rail_group_command.command("add")
@click.argument("name", required=False)
@_guard_rail_options
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.pass_context
def guard_rail_add(
    ctx: click.Context, name: str | None, scenario: str | None, data_dir: Path | None,
    **values: str | None,
) -> None:
    """Add a guard rail to <scenario>/guard_rails.csv.

    With no options, every field is asked for, then shown to write or
    change. With options, only what they leave out and a rail needs
    (measure, side, basis, threshold) is asked for, e.g. the 4% rule:
    `sim guard-rail add four_percent --measure withdrawals --side above
    --basis initial_portfolio --threshold 4%`.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / guard_rails.GUARD_RAILS_FILE
    context = _guard_rail_context(data_dir_p, scenario_name)
    taken = set(names.guard_rail_names(data_dir_p, scenario_name))
    name = (name or "").strip()
    if not name:
        while (name := _ask_new_name("name of the new rail")) in taken:
            click.echo(f"  another rail is already named {name!r}")
    if name in taken:
        raise click.ClickException(
            f"guard rail {name!r} already exists; `sim guard-rail edit {name}` changes it")
    given = {param: value.strip() for param, value in values.items() if value is not None}
    answers = guard_rail_guide.answers_of(given)
    _check_guard_rail_options(given, answers)
    empty = {column: "" for column in registries.GUARD_RAILS_CSV_HEADER}

    def submit(params: dict[str, object]) -> None:
        row = _guard_rail_row(params, {**empty, "name": name}, context)
        _check_guard_rail_row(row)
        _write_guard_rail(data_dir_p, scenario_name, path, None, row)
        click.echo(f"appended guard rail '{name}' ({_rail_summary(row)}) to {path}")

    guide = _guard_rail_guide()
    if given:
        for field_ in guard_rail_guide.ROW.fields:
            if (field_.applies(context, answers) and field_.is_required(answers)
                    and not answers[field_.param]):
                answers[field_.param] = guide.ask_field(guard_rail_guide.ROW, field_, context, answers)
        submit(dict(answers))
        return
    _echo_guided_scenario(scenario_name, path, defaulted=scenario is None)
    click.echo(f"Adding guard rail '{name}'")
    for param in list(answers):
        del answers[param]  # every field is asked, optional ones too
    guide.complete(guard_rail_guide.ROW, context, answers)
    if not guide.review(guard_rail_guide.ROW, context, {**answers}, submit,
                        scenario=None if scenario is None else scenario_name):
        ctx.exit(1)


@guard_rail_group_command.command("edit")
@click.argument("name", required=False, type=completion.GUARD_RAIL)
@_guard_rail_options
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.pass_context
def guard_rail_edit(
    ctx: click.Context, name: str | None, scenario: str | None, data_dir: Path | None,
    **values: str | None,
) -> None:
    """Change a guard rail.

    Options set those fields and write at once ("" clears one). With none,
    the rail's fields are shown; name one to change it, as often as needed,
    and write when done.

    A rail the scenario inherits is changed by an override: the whole row,
    edited, appended to this scenario's guard_rails.csv; the parent keeps
    its own.

    Without NAME, it is asked for.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / guard_rails.GUARD_RAILS_FILE
    if name is None:
        name = _ask_name("guard rail", names.guard_rail_names(data_dir_p, scenario_name),
                         prompt="rail to edit")
    picked = _pick_guard_rail(scenario_dir, scenario_name, name)
    chosen = picked.found
    current = {column: chosen.row.get(column, "") for column in registries.GUARD_RAILS_CSV_HEADER}
    context = _guard_rail_context(data_dir_p, scenario_name)
    rail = current["name"]

    def submit(params: dict[str, object]) -> None:
        row = _guard_rail_row(params, current, context)
        _check_guard_rail_row(row)
        if all(row[c].strip() == current[c].strip() for c in registries.GUARD_RAILS_CSV_HEADER):
            click.echo("no change; nothing was written")
            return
        _write_guard_rail(data_dir_p, scenario_name, path, picked, row)
        if chosen.index is None:
            click.echo(f"wrote an override of guard rail '{rail}' "
                       f"(inherited from {chosen.inherited_from!r}) to {path}")
        else:
            click.echo(f"updated guard rail '{rail}' in {path}")

    answers = guard_rail_guide.answers_of(current)
    given = {param: value.strip() for param, value in values.items() if value is not None}
    if given:
        _check_guard_rail_options(given, {**answers, **given})
        submit({**answers, **given})
        return
    _echo_guided_scenario(scenario_name, path, defaulted=scenario is None)
    click.echo(f"Editing guard rail '{rail}'")
    if chosen.index is None:
        click.echo(f"  It comes from scenario {chosen.inherited_from!r}: writing puts an override "
                   "of the whole rail in this scenario.")
    guide = _guard_rail_guide()
    guide.complete(guard_rail_guide.ROW, context, answers)
    if not guide.review(guard_rail_guide.ROW, context, answers, submit, show_command=False):
        ctx.exit(1)


@guard_rail_group_command.command("delete")
@click.argument("name", required=False, type=completion.GUARD_RAIL)
@click.option("--yes", is_flag=True, help="Don't ask first.")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def guard_rail_delete(name: str | None, yes: bool, scenario: str | None, data_dir: Path | None) -> None:
    """Delete a guard rail.

    A rail of this scenario's own is removed. Deleting this scenario's
    override of an inherited rail brings back the inherited one.

    A rail the scenario inherits can't be removed from here, since an
    overlay can't remove a row (ADR-003 §4.5). Instead it is ended: an
    override whose end is the month before the scenario starts, so it
    covers none of its years.

    Without NAME, it is asked for.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / guard_rails.GUARD_RAILS_FILE
    if name is None:
        name = _ask_name("guard rail", names.guard_rail_names(data_dir_p, scenario_name),
                         prompt="rail to delete")
    picked = _pick_guard_rail(scenario_dir, scenario_name, name)
    chosen = picked.found
    rail = chosen.row["name"]

    if chosen.index is not None:
        if chosen.inherited_from is not None:
            prompt = (f"delete this scenario's override of '{rail}', so the rail from "
                      f"{chosen.inherited_from!r} applies again?")
        else:
            prompt = f"delete guard rail '{rail}'?"
        if not yes and not click.confirm(prompt, default=False):
            click.echo("aborted")
            return
        _write_guard_rail(data_dir_p, scenario_name, path, picked, None)
        if chosen.inherited_from is not None:
            click.echo(f"deleted this scenario's override of guard rail '{rail}' from {path}; "
                       f"it is inherited from {chosen.inherited_from!r} again")
        else:
            click.echo(f"deleted guard rail '{rail}' from {path}")
        return

    with _intent_db(data_dir_p, scenario_name) as (con, _name):
        start = con.execute("SELECT start FROM scenario").fetchone()[0]
    end = _month_before(start)
    current = {column: chosen.row.get(column, "") for column in registries.GUARD_RAILS_CSV_HEADER}
    if is_month_or_date(current["end"]) and month_end_iso(current["end"]) < start:
        click.echo(f"guard rail '{rail}' already ends before the scenario starts; nothing was written")
        return
    click.echo(
        f"guard rail '{rail}' comes from scenario {chosen.inherited_from!r}, and "
        f"{scenario_name!r} can't remove what it inherits. It can end it instead: an "
        f"override with end {end}, the month before the scenario starts, so it covers nothing."
    )
    if not yes and not click.confirm("write that override?", default=False):
        click.echo("aborted")
        return
    _write_guard_rail(data_dir_p, scenario_name, path, picked, {**current, "end": end})
    click.echo(f"wrote an override ending guard rail '{rail}' in {end} to {path}")


@sim_group_command.group(name="itemization")
def itemization_group_command() -> None:
    """See the itemizations an @name money cell sums (ADR-003 §4.1c).

    The files themselves are edited as spreadsheets.
    """


def _fmt_total(item: setup_listings.Itemization) -> str:
    if item.total_cents is None:
        return "error"
    sign = "-" if item.total_cents < 0 else ""
    dollars, cents = divmod(abs(item.total_cents), 100)
    return f"{sign}${dollars:,}.{cents:02d}"


@itemization_group_command.command("list")
@_LISTING_SCENARIO_OPTION
@_LISTING_DATA_DIR_OPTION
def itemization_list(scenario: str | None, data_dir: Path | None) -> None:
    """List the itemizations the scenario can reference, with their totals.

    A scenario's own itemizations/ hides an ancestor's file of the same name,
    and an ancestor's hides the shared one, as the loader looks them up.
    Refs counts the @name cells referencing each (`sim itemization show`
    lists them).
    """
    from rich.markup import escape
    from rich.table import Table

    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, scenario)
    items = setup_listings.itemizations(data, ref.path)
    if not items:
        click.echo(f"no itemizations for {ref.name}")
        return
    references = setup_listings.itemization_references(data, ref.path)
    table = Table(title=f"itemizations ({ref.name})", header_style="bold")
    for column, justify in (("Name", "left"), ("Rows", "right"), ("Total", "right"),
                            ("Refs", "right"), ("Defined in", "left")):
        table.add_column(column, justify=justify)
    for item in items:
        table.add_row(
            escape(item.name), str(len(item.rows)), escape(_fmt_total(item)),
            str(len(references.get(item.name, []))), escape(item.defined_in),
        )
    _print_wide(table)
    for item in items:
        if item.error:
            click.echo(f"{item.name}: {item.error}", err=True)


@itemization_group_command.command("show")
@click.argument("name", required=False, type=completion.ITEMIZATION)
@_LISTING_SCENARIO_OPTION
@_LISTING_DATA_DIR_OPTION
def itemization_show(name: str | None, scenario: str | None, data_dir: Path | None) -> None:
    """Show itemization NAME's rows, its total, and what references it.

    Without NAME, it is asked for.
    """
    from rich.markup import escape
    from rich.table import Table

    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, scenario)
    found = setup_listings.itemizations(data, ref.path)
    if name is None:
        name = _ask_name("itemization", [i.name for i in found])
    name = name.removeprefix("@")
    item = next((i for i in found if i.name == name), None)
    if item is None:
        raise click.ClickException(f"no itemization {name!r} for {ref.name}")
    click.echo(f"@{item.name} ({item.defined_in}): {item.path}")
    table = Table(header_style="bold")
    columns = list(item.rows[0]) if item.rows else ["Value"]
    for column in columns:
        table.add_column(column, justify="right" if column == "Value" else "left")
    for row in item.rows:
        table.add_row(*(escape(row.get(column, "")) for column in columns))
    _print_wide(table)
    if item.error:
        click.echo(f"total: cannot be computed: {item.error}")
    else:
        click.echo(f"total: {_fmt_total(item)}")
    references = setup_listings.itemization_references(data, ref.path).get(name, [])
    if not references:
        click.echo("referenced by: nothing")
        return
    click.echo("referenced by:")
    for found in references:
        try:
            shown = found.path.relative_to(data)
        except ValueError:
            shown = found.path
        click.echo(f"  {shown} line {found.line}, {found.column}")


@scenario_group_command.command("show")
@click.argument("name", type=completion.SCENARIO, required=False)
@_LISTING_DATA_DIR_OPTION
@click.option("--runs-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Where run files are (default: state/runs beside the data repo).")
def scenario_show(name: str | None, data_dir: Path | None, runs_dir: Path | None) -> None:
    """Show scenario NAME (default: the current one): its description,
    lineage, dates, seed, ensemble size, reserve, and latest run.

    A value marked (inherited) comes from an ancestor in the load order.
    """
    data = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(data, name)
    shown = setup_listings.scenario_show(data, ref.path, _resolve_runs_dir(runs_dir, data))

    def mark(value: object, inherited: bool) -> str:
        return f"{value} (inherited)" if inherited else str(value)

    click.echo(f"scenario: {shown.name}")
    click.echo(f"  folder: {shown.path}")
    if shown.parent is None:
        click.echo("  parent: none (a root scenario)")
    else:
        click.echo(f"  parent: {shown.parent}")
    if shown.mixins:
        click.echo(f"  mix-ins: {', '.join(shown.mixins)}")
    click.echo(f"  load order: {' -> '.join(shown.load_order)}")
    for label, month in (("start", shown.start), ("end", shown.end)):
        click.echo(f"  {label}: {mark(month.month, month.inherited) if month else '(not set)'}")
    click.echo(f"  seed: {mark(shown.seed.value, shown.seed.inherited) if shown.seed else '(not set)'}")
    size = shown.ensemble_size
    click.echo(f"  ensemble size: {mark(size.value, size.inherited) if size else '0 (not an ensemble)'}")
    reserve = shown.reserve
    click.echo(f"  reserve: {mark(*reserve) if reserve else '(not set)'}")
    if shown.latest_run is None:
        click.echo("  latest run: none")
    else:
        path, when = shown.latest_run
        click.echo(f"  latest run: {path.name} ({when:%Y-%m-%d %H:%M})")
    if shown.latest_batch is not None:
        path, when = shown.latest_batch
        click.echo(f"  latest ensemble batch: {path.name} ({when:%Y-%m-%d %H:%M})")
    if shown.description:
        click.echo()
        click.echo(readme.relevel_heading(shown.description, 1))


# ---------- sim invest (#217) ----------


def _ask(text: str, choices: list[str] | None = None, default: str | None = None) -> str:
    if choices:
        click.echo(f"  choices: {', '.join(choices)}")
    return _prompt_with_completion(text, choices=choices, default=default).strip()


@sim_group_command.command("invest")
@click.argument("source", required=False, type=completion.ENDPOINT)
@click.argument("account", required=False, type=completion.ACCOUNT_OR_NEW)
@click.argument("lot", required=False)
@click.option("--source-lot", type=completion.LotName("source"), default=None,
              help="SOURCE's lot, when SOURCE is an account with lots.")
@click.option("--amount", default=None, help="Dollars to invest.")
@click.option("--date", "when", default=None, metavar="YYYY-MM",
              help="When (default: the scenario's first month).")
@click.option("--symbol", type=completion.SYMBOL, default=None,
              help="What the lot holds: a known symbol, or a new one.")
@click.option("--symbol-name", default=None, help="A new symbol's name, for securities.csv.")
@click.option("--weight", "weight_specs", multiple=True, metavar="CLASS=PERCENT",
              help="A new symbol's share in one asset class (repeatable), as for `sim symbol add`.")
@click.option("--fetch/--no-fetch", default=True, show_default=True,
              help="Fetch a new symbol's prices.")
@click.option("--subkind", "lot_subkind", default=None,
              type=click.Choice(sorted(registries.RETIREMENT_LOT_SUBKINDS)),
              help="The lot's subkind, in a retirement account: Roth or traditional, IRA or 401k.")
@click.option("--account-subkind", default=None,
              type=click.Choice(sorted(registries.ACCOUNT_SUBKINDS["asset"])),
              help="A new ACCOUNT's subkind.")
@click.option("--owner", type=completion.OWNER, default=None, help="A new ACCOUNT's owner (default: joint).")
@click.option("--scenario", type=completion.SCENARIO, default=None,
              help="Scenario folder name (default: the current scenario).")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None)
def invest_command(
    source: str | None, account: str | None, lot: str | None, source_lot: str | None,
    amount: str | None, when: str | None, symbol: str | None, symbol_name: str | None,
    weight_specs: tuple[str, ...], fetch: bool, lot_subkind: str | None,
    account_subkind: str | None, owner: str | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Invest AMOUNT from SOURCE in a new LOT of ACCOUNT, on a date.

    Writes, as one edit, the new lot (holding --symbol, acquired on the date),
    the account if ACCOUNT is new, and a calendar one_off_transfer from SOURCE
    into the lot. A new symbol is added first, as `sim symbol add` would
    (--symbol-name, --weight, and its prices unless --no-fetch). SOURCE is an
    account or a source chain. Whatever is left out is asked for, and the
    equivalent command is printed.

    For a holding the scenario starts with, use `sim account lot add` instead.
    """
    data = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data, scenario)
    guided = None in (source, account, lot, amount)
    with _intent_db(data, scenario_name) as (con, _name):
        kinds = dict(con.execute("SELECT name, kind FROM account").fetchall())
        subkinds = dict(con.execute("SELECT name, subkind FROM account").fetchall())
        lot_info = _lot_info(con)
        chains = _interior_chains(con, "source")
        known_symbols = [r[0] for r in con.execute("SELECT symbol FROM security ORDER BY symbol")]
        growing = {r[0] for r in con.execute(
            "SELECT symbol FROM security_allocation UNION SELECT series FROM aux_table WHERE kind = 'rate'"
        ) if r[0]}
        start = con.execute("SELECT start FROM scenario").fetchone()[0]
        persons = [r[0] for r in con.execute("SELECT id FROM person ORDER BY id")]
        people = [r[0] for r in con.execute(  # who may own a retirement account (#217)
            "SELECT id FROM person WHERE trim(coalesce(role, '')) IN "
            f"({', '.join('?' * len(registries.PERSON_ROLES))}) ORDER BY id",
            sorted(registries.PERSON_ROLES))]
    assets = sorted(n for n, k in kinds.items() if k == "asset" and n in lot_info)
    if guided:
        _echo_guided_scenario(scenario_name, scenario_dir / "calendar.csv", defaulted=scenario is None)

    if source is None:
        source = _ask("invest from (account or source chain)", [*sorted(lot_info), *chains])
    if source not in lot_info and source not in chains:
        raise click.ClickException(f"unknown source {source!r}: not an account or a source chain")
    if source in lot_info and calendar_guide.lot_needed(lot_info[source]):
        if source_lot is None:
            labels = lot_info[source][1]
            source_lot = labels[0] if len(labels) == 1 else _ask(f"lot of {source}", labels)
        if source_lot not in lot_info[source][1]:
            raise click.ClickException(f"{source!r} has no lot {source_lot!r}")
    elif source_lot:
        raise click.ClickException(f"{source!r} takes no --source-lot")

    if account is None:
        account = _ask("invest in (account; a new name adds one)", assets)
    new_account = account not in kinds
    if new_account:
        if account_subkind is None:
            account_subkind = _ask("new account's subkind", sorted(registries.ACCOUNT_SUBKINDS["asset"]))
        if account_subkind not in registries.ACCOUNT_SUBKINDS["asset"]:
            raise click.ClickException(f"unknown asset subkind {account_subkind!r}")
        # A retirement account belongs to one person, never `joint` (#217).
        owners = people if account_subkind == registries.RETIREMENT_SUBKIND else [*persons, "joint"]
        if owner is None:
            if owners is people and not guided:
                raise click.ClickException(f"a retirement account needs --owner ({', '.join(people)})")
            owner = (_ask("owner", owners, default=None if owners is people else "joint")
                     if guided else "joint")
        if owner not in owners:
            raise click.ClickException(f"owner {owner!r} can't own this account; allowed: {owners}")
    else:
        if kinds[account] != "asset":
            raise click.ClickException(f"{account!r} is not an asset account")
        if account_subkind or owner:
            raise click.ClickException("--account-subkind and --owner are only for a new account")
        account_subkind = subkinds[account]
    investment = account_subkind in INVESTMENT_ASSET_SUBKINDS
    taken = [] if new_account else lot_info.get(account, (False, []))[1]

    if lot is None:
        lot = _ask("new lot", default=symbol)
    lot = lot.strip()
    if not lot or ":" in lot:
        raise click.ClickException("a lot label is required, without ':'")
    if lot in taken:
        raise click.ClickException(f"{account!r} already has a lot {lot!r}; a new investment is a new lot")
    retirement = account_subkind == registries.RETIREMENT_SUBKIND
    if retirement and lot_subkind is None:
        if not guided:
            raise click.ClickException("a retirement account's lot needs --subkind")
        lot_subkind = _ask("lot subkind", sorted(registries.RETIREMENT_LOT_SUBKINDS))
    if lot_subkind and lot_subkind not in registries.RETIREMENT_LOT_SUBKINDS:
        raise click.ClickException(f"unknown lot subkind {lot_subkind!r}")
    if lot_subkind and not retirement:
        raise click.ClickException("--subkind is only for a lot of a retirement account")

    if symbol is None and guided:
        symbol = _ask("symbol (optional; a new one is added)", known_symbols, default="") or None
    symbol = symbol.strip().upper() if symbol else None
    new_symbol = symbol is not None and symbol not in known_symbols \
        and symbol not in registries.SYMBOL_ASSET_CLASS
    if new_symbol and guided and symbol_name is None and not weight_specs:
        symbol_name = click.prompt("its name (optional)", default="", show_default=False).strip() or None
        text = click.prompt("its weights, e.g. stock_us=60% bond=40% (optional)",
                            default="", show_default=False).strip()
        weight_specs = tuple(text.split())
    try:
        weights = symbols.parse_weights(list(weight_specs))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    if (symbol_name or weights) and not new_symbol:
        raise click.ClickException("--symbol-name and --weight are only for a new symbol")

    if amount is None:
        amount = _ask("amount in dollars")
    cents = _parse_amount_cents(amount)
    if cents <= 0:
        raise click.ClickException("the amount must be more than zero")
    if when is None:
        when = _ask("date (YYYY-MM)", default=start[:7]) if guided else start[:7]
    when = _validate_month_date(when)
    if when[:7] < start[:7]:
        # The engine posts calendar events only in simulated months, so an
        # earlier purchase would leave the lot empty; it is an opening holding.
        raise click.ClickException(
            f"{when} is before the scenario starts ({start[:7]}); for a holding it starts "
            "with, use `sim account lot add`")

    if new_symbol:
        assert symbol is not None
        try:
            added = symbols.add_symbol(data, symbol, name=symbol_name, weights=weights or None)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(f"added symbol {added.symbol}" + ("" if added.weighted else
                   "; no allocation yet, so it shows as unclassified"))
        if fetch and _fetch_prices(data, [added.symbol], None, "yahoo"):
            # As for `sim symbol add`: stop with the symbol added, before the investment.
            raise click.ClickException(
                f"{added.symbol}: prices not fetched, so nothing else was written; fetch them "
                f"with `sim prices fetch {added.symbol}`, then run sim invest again")

    dollars = _money_cell(amount)
    grows = symbol is not None and (symbol in growing or bool(weights))
    # An existing account with no growth character has no lot that grows yet,
    # so this lot sets it, as a new account's first lot does.
    found = None if new_account else _overlay_rows(scenario_dir, "accounts.csv").get(account)
    set_character = grows and (new_account or (found is not None and not found.row.get("tax_character")))
    growth = ""
    if set_character:
        growth = (
            ("tax_free" if registries.RETIREMENT_LOT_SUBKINDS.get(lot_subkind or "") == "tax_free"
             else "deferred") if retirement else lot_guide.GROWTH_CHARACTER.get(str(account_subkind), "gains")
        )
    with _scenario_edit(data, scenario_name) as edit:
        if set_character and found is not None:
            # A whole-row override when the account is inherited (#202).
            _append_csv_row(edit.path(scenario_dir / "accounts.csv"), registries.ACCOUNTS_CSV_HEADER,
                            {**found.row, "tax_character": growth}, replace=found.index)
        if new_account:
            _append_csv_row(edit.path(scenario_dir / "accounts.csv"), registries.ACCOUNTS_CSV_HEADER, {
                "name": account, "kind": "asset", "subkind": account_subkind, "owner": owner or "joint",
                "tax_treatment": _default_tax_treatment("asset", str(account_subkind)),
                "tax_character": growth, "opened": when, "meta_json": "{}",
            })
        _append_csv_row(edit.path(scenario_dir / "account_lots.csv"), registries.ACCOUNT_LOTS_CSV_HEADER, {
            "account_name": account,
            "lot": lot if investment or lot != registries.DEFAULT_LOT else "",
            "subkind": lot_subkind or "", "symbol": symbol or "",
            # The purchase posts at month end, so the lot is acquired then too.
            "acquired": month_end_iso(when),
            "opening_balance": "0.00",
        })
        _append_csv_row(edit.path(scenario_dir / "calendar.csv"), registries.CALENDAR_HEADER, {
            "date": when, "kind": "one_off_transfer",
            "account_1": source, "lot_1": source_lot or "", "account_2": account, "lot_2": lot,
            "amount": dollars, "description": f"invest in {account} ({lot})",
        })
    if set_character and not new_account:
        click.echo(f"set {account!r}'s tax_character to {growth!r}, so the new lot grows")
    click.echo(f"{'added account ' + repr(account) + ', ' if new_account else ''}"
               f"added lot {lot!r} of {account!r} and a {when} transfer of {dollars} "
               f"from {source} in scenario {scenario_name}")
    if guided:
        words = ["sim", "invest", source, account, lot, "--amount", dollars, "--date", when]
        for option, value in (("--source-lot", source_lot), ("--symbol", symbol),
                              ("--symbol-name", symbol_name), ("--subkind", lot_subkind)):
            if value:
                words += [option, value]
        for spec in weight_specs:
            words += ["--weight", spec]
        if new_account:
            words += ["--account-subkind", str(account_subkind), "--owner", str(owner)]
        if not fetch:
            words.append("--no-fetch")
        click.echo("Equivalent command:")
        click.echo("  " + shlex.join(words))


# ---------- Silent alias: accounts -> account ----------

# Create silent alias for backwards compatibility
@sim_group_command.group(name="accounts", hidden=True)
def accounts_group_command() -> None:
    """Alias for 'account' (silent)."""
    pass


# Dynamically copy all commands from account_group to accounts_group
for cmd_name, cmd in account_group_command.commands.items():
    accounts_group_command.add_command(cmd, name=cmd_name)


def _walk_commands(command: click.Command, seen: set[int]) -> Iterable[click.Command]:
    """``command`` and every command under it, once each: `sim accounts`
    shares `sim account`'s."""
    if id(command) in seen:
        return
    seen.add(id(command))
    yield command
    if isinstance(command, click.Group):
        for sub in command.commands.values():
            yield from _walk_commands(sub, seen)


for _command in _walk_commands(sim_group_command, set()):
    _wrap_to_ask_again(_command)


@sim_group_command.group(name="builder")
def builder_group() -> None:
    """Background scenario builds (#300).

    Once a workspace opts in (``sim builder install``, which
    ``scripts/setup.sh`` runs), commits, checkouts, merges and rebases in
    either repo start or poke a background builder that rebuilds every
    scenario whose results could have changed. Opening a scenario's run waits
    for a pending build of it. It builds only when the code is committed and
    matches the workspace's local branch (main, unless ``install BRANCH``
    named another). ANALYSIS_BUILDER=off turns it off.
    """


def _builder_data_dir(data_dir: Path | None) -> Path:
    return _resolve_data_dir(data_dir).resolve()


_BUILDER_DATA_DIR = click.option(
    "--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)",
)


@builder_group.command(name="install")
@click.argument("branch", required=False)
@_BUILDER_DATA_DIR
def builder_install(branch: str | None, data_dir: Path | None) -> None:
    """Opt this workspace pair in: check the code worktree and the data repo
    name each other, record the pair, and install the data repo's hooks.

    BRANCH is the local branch whose code it builds from (default: the one
    recorded before, else main). Every shell, notebook and builder of the
    workspace reads it from there, so they all agree; run this again to
    change it.
    """
    from analysis.sim import builder

    try:
        done = builder.install(_builder_data_dir(data_dir), branch)
    except ValueError as exc:
        raise click.ClickException(f"not a workspace pair: {exc}") from None
    for line in done:
        click.echo(line)
    click.echo(builder.notify(_builder_data_dir(data_dir)))


@builder_group.command(name="uninstall")
@_BUILDER_DATA_DIR
def builder_uninstall(data_dir: Path | None) -> None:
    """Opt this workspace out and stop its builder."""
    from analysis.sim import builder

    for line in builder.uninstall(_builder_data_dir(data_dir)):
        click.echo(line)


@builder_group.command(name="notify")
@_BUILDER_DATA_DIR
@click.option("--quiet", is_flag=True, default=False, help="Print nothing (for hooks).")
def builder_notify(data_dir: Path | None, quiet: bool) -> None:
    """Ask the builder to look for work, starting it if it isn't running.
    The git hooks call this."""
    from analysis.sim import builder

    line = builder.notify(_builder_data_dir(data_dir))
    if not quiet:
        click.echo(line)


@builder_group.command(name="start")
@_BUILDER_DATA_DIR
def builder_start(data_dir: Path | None) -> None:
    """Start the builder now (same as ``notify``)."""
    from analysis.sim import builder

    click.echo(builder.notify(_builder_data_dir(data_dir)))


@builder_group.command(name="stop")
@_BUILDER_DATA_DIR
def builder_stop(data_dir: Path | None) -> None:
    """Stop the builder, cancelling the build in progress. The next commit
    or opened run starts it again."""
    import signal as signals

    from analysis.sim import builder

    paths = builder.BuilderPaths.for_data(_builder_data_dir(data_dir))
    pid = builder.read_status(paths).get("pid")
    if not builder.daemon_alive(paths) or not isinstance(pid, int):
        click.echo("the builder isn't running")
        return
    os.kill(pid, signals.SIGTERM)
    click.echo(f"stopping the builder (pid {pid})")


@builder_group.command(name="run", hidden=True)
@_BUILDER_DATA_DIR
def builder_run(data_dir: Path | None) -> None:
    """Run the builder in the foreground (what ``start`` launches)."""
    from analysis.sim import builder

    raise SystemExit(builder.run_daemon(
        _builder_data_dir(data_dir), echo=lambda text: click.echo(text),
    ))


@builder_group.command(name="status")
@_BUILDER_DATA_DIR
def builder_status(data_dir: Path | None) -> None:
    """What the builder is doing: running or not, why it's off, the build in
    progress, the queue, what waits on dirty data, and recent failures."""
    from analysis.sim import builder

    resolved = _builder_data_dir(data_dir)
    paths = builder.BuilderPaths.for_data(resolved)
    if builder.read_config(paths) is None:
        click.echo("background builds aren't installed here (sim builder install)")
        return
    if builder.switched_off():
        click.echo("background builds are off here (ANALYSIS_BUILDER=off)")
    config = builder.read_config(paths) or {}
    code = config.get("code_dir")
    trees, reason = builder.main_check(builder.config_branch(config), Path(code) if code else None)
    if trees is None:
        click.echo(f"disabled: {reason}")
    status = builder.read_status(paths)
    alive = builder.daemon_alive(paths)
    state = f"{status.get('state', 'running')}, pid {status.get('pid')}" if alive else "not running"
    click.echo(f"builder: {state}")
    if alive:
        current = status.get("current")
        if current:
            started = datetime.fromtimestamp(current["started"], UTC).strftime("%H:%M:%SZ")
            click.echo(f"building: {current['scenario']} (since {started})")
        queue = status.get("queue") or []
        click.echo(f"queue: {', '.join(queue) if queue else '(empty)'}")
    for name, why in sorted((status.get("waiting") or {}).items()):
        click.echo(f"waiting: {name} ({why})")
    for name, failed in sorted((status.get("failed") or {}).items()):
        click.echo(f"failed: {name}: {failed.get('message')}")
    click.echo(f"log: {paths.log_dir} (sim builder log)")


@builder_group.command(name="log")
@_BUILDER_DATA_DIR
@click.option("-n", "count", type=click.IntRange(min=1), default=None,
              help="The newest N records, however old (with --since, the newest N within it).")
@click.option("--since", default=None, metavar="DURATION",
              help="Records from this long ago on, e.g. '3 days' (minutes, hours, days or "
                   "weeks). Default: 1 hour, unless -n is given.")
@click.option("-a", "--all", "show_all", is_flag=True, default=False, help="Every record kept.")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show each build's output (the indented lines).")
@click.option("-f", "--follow", is_flag=True, default=False,
              help="Oldest first, then print what is added until Ctrl-C.")
def builder_log(
    data_dir: Path | None, count: int | None, since: str | None,
    show_all: bool, verbose: bool, follow: bool,
) -> None:
    """The builder's log, newest record first.

    A record is one event (a build starting, finishing, failing or being
    cancelled; the builder starting or stopping) and the build output after
    it, which only -v shows; a failed build shows the end of its output
    anyway. One file is kept a day, pruned by .retention.yaml's logs: rules.
    """
    from analysis.sim import builder, builder_log as blog
    from analysis.sim.retention import RetentionError, parse_duration

    if show_all and (count is not None or since is not None):
        raise click.UsageError("--all shows every record; leave out -n and --since")
    window = None
    if not show_all and (since is not None or count is None):
        try:
            window = parse_duration(since if since is not None else "1 hour")
        except RetentionError as exc:
            raise click.BadParameter(str(exc), param_hint="--since") from None
    paths = builder.BuilderPaths.for_data(_builder_data_dir(data_dir))
    records = blog.read_records(paths.dir)
    tails = {} if verbose else blog.failure_tails(records)
    chosen = list(enumerate(records))
    if window is not None:
        cutoff = blog.since_cutoff(window)
        chosen = [(i, r) for i, r in chosen if r.at is not None and r.at >= cutoff]
    if count is not None:
        chosen = chosen[-count:]
    if not records and not follow:
        click.echo(f"(no log yet: {paths.log_dir})")
        return
    if not chosen and not follow:
        older = "-n N or -a" if window is not None else "-a"
        click.echo(f"(nothing in the log since {since or '1 hour'} ago; {older} shows older records)")
        return
    for i, record in (chosen if follow else reversed(chosen)):
        for line in blog.render(record, verbose, tails.get(i)):
            click.echo(line)
    if follow:
        try:
            blog.follow(paths.dir, click.echo, verbose=verbose)
        except KeyboardInterrupt:
            pass
