"""``analysis sim`` subcommand group."""

from __future__ import annotations

import csv as _csv
import json
import math
import secrets
import shlex
import shutil
from collections.abc import Callable, Generator, Mapping, Set
from contextlib import ExitStack, contextmanager
import os
import sqlite3
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple

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
    allocation, calendar_guide, chain_edit, chain_map, lot_guide, prices, registries, sensitivity,
    symbols, units,
)
from analysis.sim.accounts import load_accounts_view, render_accounts_table
from analysis.sim.connection import (
    open_run, open_scenario, year_end_values, Connection, RunConnection,
)
from analysis.sim.holdings import (
    account_holdings,
    render_holders_table,
    render_holdings_table,
    symbol_holders,
)
from analysis.sim.distributions import (
    DistributionError,
    TruncatedNormal,
    parse_quantiles,
    resolve_truncated_normal_bounds,
)
from analysis.sim.engine import run_scenario
from analysis.sim.ensemble import (
    EnsembleError,
    factor_reach,
    latest_ensemble_batch,
    load_ensemble,
    member_run_statuses,
    comparison_deltas,
    member_pairs,
    members_balance_by_month,
    oat_plan,
    recipe_reserve,
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
    calendar_row_key,
    list_scenario_names,
    load_scenario,
    open_intent_db,
    find_state_dir,
    read_current_scenario,
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
from analysis.sim.dates import MONTH_FORMAT_HINT, is_full_date, is_month_or_date, month_end_iso, parse_day
from analysis.sim.lineage import load_order
from analysis.frames import named_rows
from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS, DollarMode, ItemType


def _resolve_state_dir(
    directory: Path | None, default: Path, data_dir: Path | None = None,
) -> Path:
    """``directory`` if given, else ``default`` (``intent`` or ``runs``) under the
    state dir beside the data repo (``data_dir``, else ``$ANALYSIS_DATA_DIR``)."""
    if directory is not None:
        return directory
    return find_state_dir(data_dir, DEFAULT_STATE_DIR) / default


@click.group(name="sim")
def sim_group_command() -> None:
    """Scenario-based finance simulator."""


@sim_group_command.command()
@click.argument("scenario_name", required=False)
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
    """
    resolved = _resolve_data_dir(data_dir)
    explicit = scenario_name is not None
    resolved_intent_dir = _resolve_state_dir(intent_dir, DEFAULT_INTENT_DIR, resolved)
    ref = _resolve_scenario(resolved, scenario_name, intent_dir=resolved_intent_dir)
    try:
        result = load_scenario(
            resolved, ref.name, intent_dir=resolved_intent_dir,
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
    if explicit:
        write_current_scenario(resolved, ref.name)
    click.echo(f"loaded {result.intent_path}")
    click.echo(f"  current scenario: {ref.name}")
    for table, n in result.counts.items():
        click.echo(f"  {table}: {n}")

    if run_after_load:
        ctx.invoke(
            run,
            scenario_name=ref.name,
            data_dir=data_dir,
            intent_dir=intent_dir,
            runs_dir=runs_dir,
            run_by=run_by,
            allow_dirty_data=allow_dirty_data,
            allow_dirty_code=allow_dirty_code,
        )


@sim_group_command.command()
@click.argument("scenario_name", required=False)
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
)
@click.option(
    "--all", "check_all", is_flag=True, default=False,
    help="Check every scenario in the data repo, continuing past failures.",
)
def check(scenario_name: str | None, data_dir: Path | None, check_all: bool) -> None:
    """Validate the scenario's CSVs without producing a persistent intent file."""
    if check_all and scenario_name is not None:
        raise click.UsageError("give a scenario name or --all, not both")
    resolved = _resolve_data_dir(data_dir)
    if not check_all:
        ref = _resolve_scenario(resolved, scenario_name)
        _check_scenario(resolved, ref.name)
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
            _check_scenario(resolved, name)
        except click.ClickException:
            failed.append(name)  # its issues are already printed
    click.echo()
    if failed:
        # Non-zero exit so the gate loop this replaces still fails a push.
        raise click.ClickException(
            f"{len(failed)} of {len(names)} scenario(s) failed: {', '.join(failed)}"
        )
    click.echo(f"all {len(names)} scenario(s) ok")


def _check_scenario(data_dir: Path, name: str) -> None:
    """Load one scenario into a throwaway intent file, printing its issues and
    warnings; a failed load raises ClickException."""
    with tempfile.TemporaryDirectory() as td:
        try:
            result = load_scenario(
                data_dir, name, intent_dir=Path(td), compute_stamps=False,
            )
        except LoadError as exc:
            for issue in exc.issues:
                click.echo(f"  error: {issue}", err=True)
            raise click.ClickException(f"{len(exc.issues)} issue(s)") from None
    _echo_load_warnings(result)
    click.echo("ok")


def _echo_load_warnings(result: LoadResult) -> None:
    """Print a load's non-fatal warnings (ADR-008: e.g. an unused latent
    factor) to stderr, the way errors are printed, without failing."""
    for warning in result.warnings:
        click.echo(f"WARN: {warning}", err=True)  # ADR-004 §6's format


def _resolve_scenario(
    data_dir: Path, scenario: str | None, *, intent_dir: Path | None = None,
) -> ScenarioRef:
    """Scenario resolution shared with the notebooks (see loader)."""
    try:
        return sc.resolve_scenario_name(data_dir, scenario, intent_dir=intent_dir)
    except LoadError as exc:
        raise click.ClickException("; ".join(exc.issues)) from None


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
@click.argument("scenario_name", required=False)
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
    (ADR-006), this generates and runs an ensemble instead of a single run;
    with ``--sensitivity``, a one-at-a-time sensitivity batch (ADR-007).
    """
    quantiles, only_names, jobs = _batch_options(
        sensitivity=sensitivity, ensemble_size=ensemble_size, only=only,
        quantiles_text=quantiles_text, jobs=jobs,
    )
    explicit = scenario_name is not None
    data_dir_available = data_dir is not None or bool(os.environ.get("ANALYSIS_DATA_DIR"))
    resolved_data_dir = _resolve_data_dir(data_dir) if data_dir_available else None
    if scenario_name is None:
        resolved_data_dir = resolved_data_dir or _resolve_data_dir(data_dir)
        scenario_name = _resolve_scenario(resolved_data_dir, None).name
    resolved_intent_dir = _resolve_state_dir(intent_dir, DEFAULT_INTENT_DIR, resolved_data_dir)
    resolved_runs_dir = _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR, resolved_data_dir)
    intent_path = resolved_intent_dir / f"{scenario_name}.sqlite3"
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
    if explicit and resolved_data_dir is not None:
        write_current_scenario(resolved_data_dir, scenario_name)

    resolved_by = run_by or os.environ.get("USER") or "unknown"

    if sensitivity:
        if not _has_unresolved_vary(intent_path):
            raise click.ClickException(
                f"{scenario_name}: nothing to perturb -- no calendar row has "
                "vary_distribution set (ADR-007)"
            )
        _run_sensitivity(
            scenario_name, intent_path,
            resolved_intent_dir, resolved_runs_dir,
            quantiles=quantiles, only=only_names, run_by=resolved_by,
            allow_dirty_data=allow_dirty_data, allow_dirty_code=allow_dirty_code, jobs=jobs,
        )
        return

    if _has_unresolved_vary(intent_path):
        _run_ensemble(
            scenario_name, intent_path,
            resolved_intent_dir, resolved_runs_dir,
            ensemble_size=ensemble_size, run_by=resolved_by,
            allow_dirty_data=allow_dirty_data, allow_dirty_code=allow_dirty_code, jobs=jobs,
        )
        return

    data_version = _read_data_version(intent_path)
    short = data_version[:7] if data_version else "nodata"
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    run_path = resolved_runs_dir / f"{stamp}_{scenario_name}_{short}.sqlite3"

    try:
        out = run_scenario(
            intent_path, run_path,
            run_by=resolved_by,
            allow_dirty_data=allow_dirty_data,
            allow_dirty_code=allow_dirty_code,
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
    """ADR-006: whether ``intent_path`` still has any calendar_event row with
    `vary_distribution` set, i.e. is a recipe that hasn't been resolved."""
    con = sqlite3.connect(intent_path)
    try:
        row = con.execute(
            "SELECT 1 FROM calendar_event WHERE vary_distribution IS NOT NULL LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return row is not None


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
        meta_size = json.loads(meta_json or "{}").get("ensemble_size")
        if meta_size is None:
            raise click.ClickException(
                f"{scenario_name}: ensemble recipe requires --ensemble-size or "
                "scenario.meta_json.ensemble_size"
            )
        member_count = int(meta_size)
        source = "scenario.meta_json.ensemble_size"
    if member_count <= 0:
        raise click.ClickException(
            f"{scenario_name}: {source} must be a positive integer (got {member_count})"
        )

    batch, member_intent_dir, member_runs_dir = _reserve_batch(scenario_name, intent_dir, runs_dir)
    # Provisional, so an interrupted batch is still recognizable; rewritten
    # with the real failures below.
    write_batch_manifest(member_runs_dir, member_count=member_count, failures=[])
    failures = _run_members(
        recipe_intent_path, member_intent_dir, member_runs_dir,
        [(i, None) for i in range(member_count)],
        seed=seed, recipe_tree_id=recipe_tree_id, mode="monte_carlo",
        run_by=run_by, allow_dirty_data=allow_dirty_data, allow_dirty_code=allow_dirty_code,
        jobs=jobs,
    )
    # A member whose resolve_member call itself raised never got a run file
    # at all -- member_run_statuses can only see files that exist, so this
    # batch-level record is the only place a failure like that (or every
    # member failing, leaving no member_*.sqlite3 at all) is still visible
    # to a later `sim ensemble summary`/notebook widget.
    write_batch_manifest(member_runs_dir, member_count=member_count, failures=failures)
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
    _report_batch(f"sensitivity {scenario_name} ({batch})", len(plan), failures)


def _recipe_identity(recipe_intent_path: Path) -> tuple[int | None, str | None, str]:
    """(scenario.seed, scenario.meta_json, intent_manifest.scenario_tree_id)."""
    con = sqlite3.connect(recipe_intent_path)
    try:
        return con.execute(
            "SELECT scenario.seed, scenario.meta_json, intent_manifest.scenario_tree_id "
            "FROM scenario, intent_manifest"
        ).fetchone()
    finally:
        con.close()


def _unresolved_vary_names(intent_path: Path) -> list[str]:
    con = sqlite3.connect(intent_path)
    try:
        return [r[0] for r in con.execute(
            "SELECT name FROM calendar_event WHERE vary_distribution IS NOT NULL ORDER BY name"
        )]
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
) -> list[tuple[int, Path, str]]:
    """Resolve and run each ``(member index, overrides)`` spec; return the
    failures as ``(index, member intent path, error)``, sorted by index. A
    failure is reported and skipped, never retried (ADR-006 §7).

    With ``jobs > 1`` members run in a process pool (ADR-007 §8): they share
    no files, so the only ordering that changes is the progress lines, which
    are printed as members finish.
    """
    tasks = [
        (
            recipe_intent_path, member_intent_dir / f"member_{i:04d}.sqlite3",
            member_runs_dir / f"member_{i:04d}.sqlite3", i, overrides,
            seed, len(specs), recipe_tree_id, mode, run_by, allow_dirty_data, allow_dirty_code,
        )
        for i, overrides in specs
    ]
    failures: list[tuple[int, Path, str]] = []

    def _report(i: int, overrides: dict[str, float] | None, intent_path: Path,
                run_path: Path, error: str | None) -> None:
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
                    error = future.result()
                except Exception as exc:  # the worker process itself died
                    error = f"worker failed: {exc}"
                _report(task[3], task[4], task[1], task[2], error)
    return sorted(failures)


def _run_one_member(
    recipe_intent_path: Path, member_intent_path: Path, member_run_path: Path,
    member_index: int, overrides: dict[str, float] | None, seed: int | None, member_count: int,
    recipe_tree_id: str, mode: str, run_by: str, allow_dirty_data: bool, allow_dirty_code: bool,
) -> str | None:
    """Resolve and run one member; the error text on failure, else ``None``.
    Module-level (not a closure) so a process pool can pickle it."""
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
        return str(exc)
    return None


def _report_batch(label: str, member_count: int, failures: list[tuple[int, Path, str]]) -> None:
    succeeded = member_count - len(failures)
    click.echo(f"{label}: {succeeded}/{member_count} member(s) succeeded")
    if failures:
        click.echo(f"  {len(failures)} failure(s):")
        for i, path, _error in failures:
            click.echo(f"    member {i}: {path}")


@sim_group_command.group(name="ensemble")
def ensemble_group_command() -> None:
    """Inspect or manage an ensemble recipe's generated members (ADR-006)."""


@ensemble_group_command.command("create")
@click.argument("name")
@click.argument("parent")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default=None, help="Default: 'Derived from <parent>'.")
@click.option(
    "--seed", type=int, default=None,
    help="Ensemble master seed (default: a random 31-bit int, printed on creation).",
)
@click.option(
    "--size", "ensemble_size", type=int, default=registries.DEFAULT_ENSEMBLE_SIZE,
    show_default=True,
    help="Default member count (scenario.meta_json.ensemble_size), so "
         "'sim run'/'sim all' can generate and run this recipe without "
         "--ensemble-size on every invocation; 'sim run --ensemble-size' "
         "can still override it per invocation. See docs/guides/ensembles.md "
         "for what size is actually appropriate for this recipe.",
)
def ensemble_create(
    name: str,
    parent: str,
    data_dir: Path | None,
    description: str | None,
    seed: int | None,
    ensemble_size: int,
) -> None:
    """Create ensemble recipe NAME deriving from PARENT scenario (ADR-006).

    A recipe is an ordinary scenario until a calendar.csv row is marked
    varying (``sim calendar add <kind> ... --vary-distribution ...``);
    ``sim run NAME`` then generates and runs every member instead of a
    single run, using --size's recorded member count unless overridden by
    ``sim run NAME --ensemble-size N``.
    """
    if ensemble_size <= 0:
        raise click.ClickException(f"--size must be a positive integer (got {ensemble_size})")
    data_dir_p = _resolve_data_dir(data_dir)
    seed = seed if seed is not None else secrets.randbelow(2**31)
    # Preflights PARENT, then creates and validates the recipe in a staged copy.
    with _scenario_edit(data_dir_p, parent) as edit:
        try:
            staged = sc.create_scenario(edit.data_dir, name, parent, description=description)
        except Exception as exc:
            raise click.ClickException(f"failed to create scenario {name}: {exc}") from None

        scenario_csv = staged / "scenario.csv"
        df = pd.read_csv(scenario_csv, dtype=str, keep_default_na=False)
        meta = json.loads(df.loc[0, "meta_json"] or "{}")
        meta["ensemble_size"] = ensemble_size
        df.loc[0, "seed"] = str(seed)
        df.loc[0, "meta_json"] = json.dumps(meta, separators=(",", ":"))
        df.to_csv(scenario_csv, index=False)
    target = data_dir_p / "scenarios" / name

    parent_ref = _resolve_scenario(data_dir_p, parent)
    write_current_scenario(data_dir_p, name)
    click.echo(f"created ensemble recipe {target}")
    click.echo(f"  inherits: {parent_ref.name}")
    click.echo(f"  seed: {seed}")
    click.echo(f"  ensemble_size: {ensemble_size}")
    click.echo(f"  current scenario: {name}")
    click.echo(
        "  next: sim calendar add <kind> ... --vary-distribution ... to mark rows as "
        f"varying, then sim run {name} --ensemble-size N"
    )


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
    "--account", default=None,
    help="Report one account's balance instead of net worth.",
)
_ENSEMBLE_RESERVE_OPTION = click.option(
    "--reserve", default=None,
    help="Reserve in CPI-constant dollars (e.g. 250000) for what's reported, "
         "overriding the recipe's scenario.meta_json reserve (which applies to "
         "net worth only).",
)


def _resolve_runs_dir(runs_dir: Path | None, data_dir: Path | None) -> Path:
    data_dir_available = data_dir is not None or bool(os.environ.get("ANALYSIS_DATA_DIR"))
    resolved_data_dir = _resolve_data_dir(data_dir) if data_dir_available else None
    return _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR, resolved_data_dir)


def _reserve_dollars(
    reserve: str | None, connections: dict[str, RunConnection], account: str | None,
) -> float | None:
    """``--reserve``, applied to whatever is reported; without it, the
    recipe's own reserve, which is a net-worth floor and so applies to net
    worth only."""
    if reserve is None:
        return recipe_reserve(connections) if account is None else None
    try:
        return units.dollars_to_cents(reserve) / 100
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--reserve") from exc


def _member_frame(
    connections: dict[str, RunConnection], account: str | None, mode: DollarMode,
) -> pd.DataFrame:
    try:
        return members_balance_by_month(connections, account, mode)
    except EnsembleError as exc:
        raise click.ClickException(str(exc)) from exc


@ensemble_group_command.command("summary")
@click.argument("scenario_name")
@_ENSEMBLE_RUNS_DIR_OPTION
@_ENSEMBLE_DATA_DIR_OPTION
@click.option(
    "--batch", default=None,
    help="Which generated batch (a 'sim run' timestamp) to summarize; default: the latest.",
)
@_ENSEMBLE_ACCOUNT_OPTION
@_ENSEMBLE_RESERVE_OPTION
def ensemble_summary(
    scenario_name: str, runs_dir: Path | None, data_dir: Path | None, batch: str | None,
    account: str | None, reserve: str | None,
) -> None:
    """Mean/median/SD, 5th/20th/.../95th percentiles and the mean of the
    worst 5% and 20% (CVaR) of year-end net worth (or --account's balance)
    across SCENARIO_NAME's members, and the share below the reserve when
    one is set."""
    batch_dir = _ensemble_batch_dir(_resolve_runs_dir(runs_dir, data_dir), scenario_name, batch)
    statuses = member_run_statuses(batch_dir)
    failed = sorted(stem for stem, status in statuses.items() if status != "succeeded")
    connections = load_ensemble(batch_dir)
    try:
        frame = _member_frame(connections, account, DollarMode.NOMINAL)
        reserve_dollars = _reserve_dollars(reserve, connections, account)
        cpi_frame = (
            None if reserve_dollars is None
            else _member_frame(connections, account, DollarMode.CPI_CONSTANT)
        )
        table = year_end_values(pd.concat(
            [summary_with_percentiles(frame), risk_summary(frame, reserve_dollars, cpi_frame)],
            axis=1,
        ))
        click.echo(
            f"ensemble {scenario_name} ({batch_dir.name}): {len(connections)} succeeded "
            f"member(s), {len(failed)} failed"
            + (f"; account {account}" if account else "")
            + ("" if reserve_dollars is None
               else f"; reserve ${reserve_dollars:,.0f} (CPI-constant)")
        )
        _echo_year_table(table)
        if failed:
            click.echo(f"  {len(failed)} failed member(s), excluded above:")
            for stem in failed:
                click.echo(f"    {stem}: {statuses[stem]}")
    finally:
        _close_all(connections)


@ensemble_group_command.command("compare")
@click.argument("baseline")
@click.argument("variant")
@_ENSEMBLE_RUNS_DIR_OPTION
@_ENSEMBLE_DATA_DIR_OPTION
@_ENSEMBLE_ACCOUNT_OPTION
@_ENSEMBLE_RESERVE_OPTION
@click.option(
    "--dollars", type=click.Choice([m.value for m in DollarMode]),
    default=DollarMode.CPI_CONSTANT.value, show_default=True,
    help="Dollars the percentiles and tail means are reported in.",
)
def ensemble_compare(
    baseline: str, variant: str, runs_dir: Path | None, data_dir: Path | None,
    account: str | None, reserve: str | None, dollars: str,
) -> None:
    """Compare VARIANT's ensemble with BASELINE's on risk (#163), each from
    its latest batch: by year end, each percentile breakpoint's delta, the
    delta in the mean of the worst 5% and 20% of members (CVaR), and, when
    the two recipes share a seed, the share of paired futures where VARIANT
    comes out ahead. Then each recipe's tail means and, when a reserve is
    set, the share of members below it."""
    resolved_runs_dir = _resolve_runs_dir(runs_dir, data_dir)
    mode = DollarMode(dollars)
    sides: dict[str, dict[str, RunConnection]] = {}
    try:
        for name in (baseline, variant):
            sides[name] = load_ensemble(_ensemble_batch_dir(resolved_runs_dir, name, None))
        base_conns, variant_conns = sides[baseline], sides[variant]
        # An account only one recipe has gets that recipe's own tail table
        # below, with no deltas: there is nothing to subtract it from.
        frames: dict[str, pd.DataFrame] = {}
        for name, conns in sides.items():
            try:
                frames[name] = members_balance_by_month(conns, account, mode)
            except EnsembleError:
                pass
        if not frames:
            raise click.ClickException(f"no account {account!r} in {baseline} or {variant}")
        reserve_dollars = _reserve_dollars(reserve, base_conns, account)
        cpi_frames = {
            name: (
                frames[name] if mode == DollarMode.CPI_CONSTANT
                else members_balance_by_month(sides[name], account, DollarMode.CPI_CONSTANT)
            )
            for name in frames
        } if reserve_dollars is not None else {}
        mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
        subject = f"account {account}" if account else "net worth"
        click.echo(
            f"{variant} vs {baseline}: {subject}, {mode_label} dollars; "
            f"{len(base_conns)} and {len(variant_conns)} member(s)"
        )
        if len(frames) == 2:
            pairs, unpaired = member_pairs(base_conns, variant_conns)
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
            click.echo(f"  only one side: {missing} has no account {account}")

        reserve_note = (
            "" if reserve_dollars is None
            else f"; reserve ${reserve_dollars:,.0f} (CPI-constant)"
        )
        for name, frame in frames.items():
            click.echo(f"{name} tail, year end{reserve_note}:")
            _echo_year_table(year_end_values(
                risk_summary(frame, reserve_dollars, cpi_frames.get(name))
            ))
    finally:
        for conns in sides.values():
            _close_all(conns)


@ensemble_group_command.command("sensitivity")
@click.argument("scenario_name")
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
    scenario_name: str, runs_dir: Path | None, data_dir: Path | None, batch: str | None,
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
    """
    data_dir_available = data_dir is not None or bool(os.environ.get("ANALYSIS_DATA_DIR"))
    resolved_data_dir = _resolve_data_dir(data_dir) if data_dir_available else None
    resolved_runs_dir = _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR, resolved_data_dir)
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
@click.pass_context
def all_scenarios(
    ctx: click.Context,
    data_dir: Path | None,
    intent_dir: Path | None,
    runs_dir: Path | None,
    allow_dirty_data: bool,
    allow_dirty_code: bool,
    run_by: str | None,
) -> None:
    """Load and run every scenario in the data repo.

    Reports failures per scenario and keeps going; does not stop or fail the
    command itself.
    """
    resolved = _resolve_data_dir(data_dir)
    try:
        names = list_scenario_names(resolved)
    except LoadError as exc:
        raise click.ClickException("; ".join(exc.issues)) from None
    if not names:
        raise click.ClickException(f"no scenarios found under {resolved / 'scenarios'}")

    failures: list[tuple[str, str]] = []
    for name in names:
        click.echo(f"=== {name} ===")
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
    if failures:
        click.echo(f"{len(failures)} of {len(names)} scenario(s) failed:")
        for name, message in failures:
            click.echo(f"  {name}: {message}")
    else:
        click.echo(f"all {len(names)} scenario(s) succeeded")


def _read_data_version(intent_path: Path) -> str | None:
    con = sqlite3.connect(intent_path)
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
            con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
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


@sim_group_command.command()
@click.argument("runfile", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def explain(runfile: Path) -> None:
    """Dump scenario name, versions, year-end net worth, counts. Sanity dump."""
    con = sqlite3.connect(f"file:{runfile}?mode=ro", uri=True)
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
            "FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id "
            "JOIN lot_state ls ON ls.lot_id = m.id AND ls.step_id = "
            "  (SELECT id FROM simulation_step ORDER BY step_index DESC LIMIT 1) "
            "WHERE a.subkind IN ({}) OR a.id IN "
            "  (SELECT account_id FROM account_model_member "
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
        click.echo("year-end net worth:")
        rows = con.execute(
            "WITH year_end AS ("
            "  SELECT substr(month,1,4) AS year, MAX(month) AS m "
            "  FROM simulation_step GROUP BY substr(month,1,4)"
            ") "
            "SELECT y.year, nw.net_worth "
            "FROM year_end y JOIN net_worth nw ON nw.month = y.m "
            "ORDER BY y.year"
        ).fetchall()
        for year, nw in rows:
            click.echo(f"  {year}  {_fmt_cents(nw)}")
    finally:
        con.close()


@sim_group_command.command("items")
@click.option("--account", required=True, help="Account name in the selected run.")
@click.option("--lot", default=None,
              help="Only events touching this lot of the account (default: every lot).")
@click.option("--start", required=True, help="First month (YYYY-MM or YYYY-MM-DD).")
@click.option("--end", default=None, help="Last month (YYYY-MM or YYYY-MM-DD); overrides --months.")
@click.option("--months", default=1, show_default=True, type=click.IntRange(min=1),
              help="Number of months to include when --end is omitted.")
@click.option("--runfile", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="Run file (default: selected scenario's latest run).")
@click.option("--scenario", default=None,
              help="Scenario name used to find the latest run.")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR).")
@click.option("--runs-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory to search for the latest run.")
def items(
    account: str,
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

    con = sqlite3.connect(f"file:{runfile}?mode=ro", uri=True)
    try:
        account_row = con.execute("SELECT id FROM account WHERE name = ?", (account,)).fetchone()
        if account_row is None:
            raise click.ClickException(f"unknown account in run: {account!r}")
        account_id = account_row[0]
        lot_id = None
        if lot is not None:
            lot_row = con.execute(
                "SELECT id FROM account_model_member WHERE account_id = ? AND lot = ?",
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
            "JOIN account_model_member fm ON fm.id = te.from_lot_id "
            "JOIN account_model_member tm ON tm.id = te.to_lot_id "
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


def _read_account_names(data_dir: Path) -> list[str]:
    """Cheap CSV-only reader used for shell tab-completion (no scenario context).

    Validation and listing commands go through ``open_intent_db`` instead so
    the loader stays the single CSV reader.
    """
    p = data_dir / "accounts.csv"
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return [r["name"] for r in _csv.DictReader(fh) if r.get("name")]


def _complete_account(ctx, param, incomplete):  # noqa: ARG001
    env = os.environ.get("ANALYSIS_DATA_DIR")
    if not env:
        return []
    try:
        names = _read_account_names(Path(env))
    except OSError:
        return []
    return [n for n in names if n.lower().startswith(incomplete.lower())]


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


# calendar.csv columns a file may legitimately lack (ADR-006): optional,
# nullable additions to an already-established header. Any other missing
# column, in any CSV kind, is schema drift to report, not paper over --
# an accounts.csv missing e.g. `kind` is a malformed file, not an old one.
_BACKFILLABLE_CALENDAR_COLUMNS = frozenset({
    "name", "vary_target", "vary_distribution", "vary_low", "vary_high",
    "vary_median", "vary_sd", "vary_skew", "vary_tailweight", "vary_mode", "vary_ramp_months",
    "vary_correlate_with", "vary_correlation", "vary_tail_dependence",
    "vary_process", "vary_persistence", "vary_innovation_sd",
})


def _append_csv_row(path: Path, header: tuple[str, ...],
                    row: dict[str, str], *, replace: int | None = None) -> None:
    """Append a row to a CSV, aligning columns by name via pandas. With
    ``replace``, overwrite that data row (0-based) instead.

    If the file exists, its header must contain every column of ``header``
    except the backfillable ones (currently just calendar.csv's ADR-006
    additions), which are added blank on every existing row instead; this
    still catches real schema drift rather than silently writing misaligned
    columns.
    """
    cols = list(header)
    if path.exists() and path.stat().st_size > 0:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        existing = list(df.columns)
        missing = [c for c in header if c not in existing]
        not_backfillable = [c for c in missing if c not in _BACKFILLABLE_CALENDAR_COLUMNS]
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
def _scenario_edit(data_dir: Path, scenario: str | None) -> Generator[StagedEdit]:
    """``staged_edit`` for a CLI command: edits land only if every affected
    scenario still loads (issue #44). Issues go to stderr as ``error:`` lines,
    like ``sim check``, and the command fails with nothing written."""
    try:
        with staged_edit(data_dir, scenario) as edit:
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
        for issue in exc.issues:
            click.echo(f"  error: {issue}", err=True)
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


@contextmanager
def _intent_db(data_dir: Path, scenario: str | None) -> Generator[tuple[sqlite3.Connection, str]]:
    """``open_intent_db`` for an edit command's up-front checks: a scenario
    that doesn't load says so plainly, rather than as a traceback or as if the
    row being added were at fault."""
    with ExitStack() as stack:
        try:
            con, name = stack.enter_context(open_intent_db(data_dir, scenario))
        except LoadError as exc:
            for issue in exc.issues:
                click.echo(f"  error: {issue}", err=True)
            raise click.ClickException(
                f"scenario {scenario or 'current'!r} already fails to load "
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


def _write_calendar_row(
    path: Path, row: dict[str, str], *, data_dir: Path, scenario_name: str,
) -> str:
    """Append ``row`` to the scenario's calendar.csv -- or, under ``sim
    calendar edit``, replace the row being edited -- if the scenario still
    loads with it. Returns "appended" or "updated".

    An edited row may not end up alike in every key column to another row of
    the same file: the loader would treat the two as one event, so the edit
    would quietly merge them. (Appending such a row stays allowed: it is how
    an existing row is given a name and a variation, ADR-006.)
    """
    context = click.get_current_context(silent=True)
    replacing = context.meta.get(_CALENDAR_REPLACE_ROW) if context is not None else None
    with _scenario_edit(data_dir, scenario_name) as edit:
        staged = edit.path(path)
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
        row = dict(row)
        # Keep what the kind's command has no way to express.
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
        _append_csv_row(staged, registries.CALENDAR_HEADER, row, replace=index)
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
        "SELECT a.name, a.subkind, m.lot FROM account_model_member m "
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
        "--vary-innovation-sd", default=None,
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
    vary_process: str | None, vary_persistence: str | None, vary_innovation_sd: str | None,
    *, vary_target: str, vary_ramp_months: int | None,
    amount_unit: str | None,
) -> dict[str, str]:
    """``vary_process``/``vary_persistence``/``vary_innovation_sd`` cells
    (ADR-006 §4a), checked as far as the row alone allows; the level's
    bound and a tree's nu are the validating load's job."""
    if vary_process is None:
        if vary_persistence is not None or vary_innovation_sd is not None:
            raise click.ClickException(
                "--vary-persistence and --vary-innovation-sd go with --vary-process")
        return {}
    if vary_target != "amount" or amount_unit is None:
        raise click.ClickException("--vary-process needs --vary-target amount")
    if vary_ramp_months is not None:
        raise click.ClickException("--vary-process and --vary-ramp-months don't combine")
    if vary_persistence is None or vary_innovation_sd is None:
        raise click.ClickException(
            "--vary-process requires --vary-persistence and --vary-innovation-sd")
    try:
        phi = float(vary_persistence)
    except ValueError:
        raise click.ClickException(
            f"--vary-persistence must be a number (got {vary_persistence!r})") from None
    if not 0.0 <= phi < 1.0:
        raise click.ClickException(
            f"--vary-persistence must be at least 0 and below 1 (got {vary_persistence})")
    sd = _AMOUNT_PARSERS[amount_unit](vary_innovation_sd)
    if sd <= 0:
        raise click.ClickException(
            f"--vary-innovation-sd must be positive (got {vary_innovation_sd})")
    return {"vary_process": vary_process, "vary_persistence": vary_persistence,
            "vary_innovation_sd": units.format_stored(amount_unit, sd)}


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
        "--vary-correlate-with", default=None,
        help="Another varying row's or a latent factor's name: this row's parent in a "
             "correlation tree (ADR-008). Requires --vary-correlation.",
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
    vary_correlate_with: str | None,
    vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
    amount_unit: str | None,
    vary_process: str | None = None,
    vary_persistence: str | None = None,
    vary_innovation_sd: str | None = None,
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
            vary_skew, vary_tailweight, vary_mode, vary_ramp_months is not None,
            vary_correlate_with, vary_correlation, vary_tail_dependence,
            vary_process, vary_persistence, vary_innovation_sd,
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
            return _validate_month_date(raw)
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
            vary_process, vary_persistence, vary_innovation_sd, vary_target=vary_target,
            vary_ramp_months=vary_ramp_months, amount_unit=amount_unit,
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
        date = _prompt_with_completion("date (YYYY-MM or YYYY-MM-DD)")
    date = _validate_month_date(date)

    from_account, from_lot, to_account, to_lot = _pick_endpoints(
        endpoints, from_account or None, from_lot, to_account or None, to_lot,
    )

    if amount is None:
        amount = _prompt_with_completion("amount (dollars, e.g. 1234.56)")
    cents = _parse_amount_cents(amount)
    if cents <= 0:
        raise click.ClickException("amount must be positive")

    if tax_character not in registries.TAX_CHARACTERS:
        raise click.ClickException(
            f"unknown tax_character: {tax_character!r}; "
            f"allowed: {sorted(registries.TAX_CHARACTERS)}"
        )

    vary_fields = _vary_row_fields(
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
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
@click.argument("date", required=False)
@click.argument("from_account", required=False, shell_complete=_complete_account)
@click.argument("to_account", required=False, shell_complete=_complete_account)
@click.argument("amount", required=False)
@click.option("--from-lot", default=None,
              help="Lot of the from account; asked for when a blank one would not say which.")
@click.option("--to-lot", default=None,
              help="Lot of the to account; asked for when a blank one would not say which.")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--tax-character", default="none", show_default=True,
              help="ADR-003 tax_character (e.g. ordinary, ltcg, none).")
@click.option("--description", default="", help="Free-text description.")
@click.option("--series", default="", help="Optional series tag.")
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
    registries.LATENT_FACTOR_KIND: "latent-factor",
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
        "series": row["series"],
        "rate_percent": row["amount"],
        "scheduled_name": row["scheduled_name"],
        "amount": row["amount"],
        "param_date": row["param_date"],
        "from_account": row["account_1"], "from_lot": row["lot_1"],
        "to_account": row["account_2"], "to_lot": row["lot_2"],
        "tax_character": row["tax_character"] or "none",
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
        "vary_mode": row["vary_mode"],
        "vary_skew": row["vary_skew"], "vary_tailweight": row["vary_tailweight"],
        "vary_ramp_months": row["vary_ramp_months"],
        "vary_process": row["vary_process"],
        "vary_persistence": row["vary_persistence"],
        "vary_innovation_sd": row["vary_innovation_sd"],
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
                "rate": sorted({
                    *column(con, aux, "rate"),
                    *column(con, "SELECT series FROM account_model_member"),
                }),
                "inflation": sorted({
                    *column(con, aux, "inflation"),
                    *column(con, "SELECT series FROM scheduled_transaction"),
                } - {"fixed"}),
            },
            row_names=[
                name for name in column(con, "SELECT name FROM calendar_event")
                if name != own_name
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
            "date": _validate_month_date,
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
@click.option("--scenario", default=None,
              help="For the guided mode: scenario folder name "
                   "(default: the only scenario in data-dir).")
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
    it asks anything: a defaulted scenario may not be the one meant."""
    how = " (the default; use --scenario for another)" if defaulted else ""
    click.echo(f"Scenario: {name}{how}")
    click.echo(f"  {path}")


def _select_calendar_row(rows: list[dict[str, str]], wanted: str | None) -> int:
    """Narrow ``rows`` to one, by its number, its name, or text it contains;
    asks (again) while that leaves more or fewer than one."""
    labels = [_calendar_row_label(row) for row in rows]
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


@calendar_group_command.command("edit")
@click.argument("row", required=False)
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.pass_context
def calendar_edit(
    ctx: click.Context, row: str | None, scenario: str | None, data_dir: Path | None,
) -> None:
    """Change one row of the scenario's own calendar.csv.

    ROW is the row's number, its name, or text that picks it out; without it,
    or if it matches several rows, the rows are listed to choose from. The
    row's fields are then shown: name one to change it, as often as needed,
    and write when done. The row keeps its kind and its place in the file.

    A change that would make the row a copy of another row, or reuse another
    row's name, is refused. Rows inherited from a parent scenario are edited
    in that scenario.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "calendar.csv"
    _echo_guided_scenario(scenario_name, path, defaulted=scenario is None)
    rows = _read_calendar_rows(path)
    if not rows:
        raise click.ClickException(
            f"scenario {scenario_name!r} has no calendar rows of its own to edit "
            "(rows it inherits are edited in the parent scenario)"
        )
    index = _select_calendar_row(rows, row)
    converted = _calendar_row_answers(rows[index])
    if converted is None:
        raise click.ClickException(
            f"row {index + 1} is a {rows[index]['kind']!r} row, which this command "
            f"cannot edit; change it in {path}"
        )
    spec, answers = converted
    click.echo(f"Editing row {index + 1}: {_calendar_row_label(rows[index])}")
    context = _guide_context(data_dir_p, scenario_name, rows=rows, editing=index)
    guide = _calendar_guide()
    guide.complete(spec, context, answers)

    def submit(params: dict[str, object]) -> None:
        ctx.meta[_CALENDAR_REPLACE_ROW] = (index, rows[index])
        try:
            # The scenario announced above, by name: a defaulted scenario is
            # not resolved again, in case the default changed meanwhile.
            ctx.invoke(
                calendar_add_group_command.commands[spec.command],
                scenario=scenario_name, data_dir=data_dir_p, **params,
            )
        finally:
            del ctx.meta[_CALENDAR_REPLACE_ROW]

    if not guide.review(spec, context, answers, submit, show_command=False):
        ctx.exit(1)


def _scheduled_names(data_dir: Path, scenario: str | None) -> set[str]:
    with _intent_db(data_dir, scenario) as (con, _name):
        return {r[0] for r in con.execute("SELECT name FROM scheduled_transaction")}


@calendar_add_group_command.command("set-rate")
@click.argument("date", required=False)
@click.argument("series", required=False)
@click.argument("rate_percent", required=False)
@click.option(
    "--aux-kind", default="rate", show_default=True,
    help="aux_table kind this rate belongs to. The default 'rate' is an asset's own "
         "growth series (accounts.csv's own series column); use 'inflation' to vary an "
         "inflation/COLA series such as cpi (scheduled.csv rows reference it by series name).",
)
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default="", help="Free-text description.")
@click.option("--tags", default="",
              help="Space-separated '<role>:<name>' tokens (e.g. 'expense:discretionary').")
@_vary_row_options
@_vary_process_options
def calendar_add_set_rate(
    date: str | None, series: str | None, rate_percent: str | None, aux_kind: str,
    scenario: str | None, data_dir: Path | None, description: str, tags: str,
    row_name: str | None, vary_target: str | None, vary_distribution: str | None,
    vary_low: str | None, vary_high: str | None, vary_median: str | None, vary_sd: str | None,
    vary_skew: str | None, vary_tailweight: str | None, vary_mode: str | None,
    vary_correlate_with: str | None, vary_correlation: str | None,
    vary_tail_dependence: str | None,
    vary_ramp_months: int | None,
    vary_process: str | None, vary_persistence: str | None, vary_innovation_sd: str | None,
) -> None:
    """Append a set_rate event: SERIES starts earning RATE_PERCENT (e.g. 4.5) from DATE.

    RATE_PERCENT is a schema placeholder, ignored, when --vary-target=amount.
    Arguments left out are asked for.
    """
    try:
        registries.parse_tags(tags)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    given = _ask_missing_arguments(
        "set-rate", data_dir, scenario,
        date=date, series=series, rate_percent=rate_percent, aux_kind=aux_kind,
    )
    date, series, rate_percent = given["date"], given["series"], given["rate_percent"]
    date = _validate_month_date(date)
    micros = _percent_to_rate_micros(rate_percent)
    vary_fields = _vary_row_fields(
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence,
        vary_ramp_months=vary_ramp_months, amount_unit="percent",
        vary_process=vary_process, vary_persistence=vary_persistence,
        vary_innovation_sd=vary_innovation_sd,
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


@calendar_add_group_command.command("set-amount")
@click.argument("date", required=False)
@click.argument("scheduled_name", required=False)
@click.argument("amount", required=False)
@click.option("--param-date", default="",
              help="New base date for the rule (YYYY-MM or YYYY-MM-DD), optional.")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
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

    date = _validate_month_date(date)
    if param_date:
        param_date = _validate_month_date(param_date)
    cents = _parse_amount_cents(amount)
    vary_fields = _vary_row_fields(
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
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
@click.argument("date", required=False)
@click.argument("scheduled_name", required=False)
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
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

    date = _validate_month_date(date)
    vary_fields = _vary_row_fields(
        row_name=row_name, vary_target=vary_target, vary_distribution=vary_distribution,
        vary_low=vary_low, vary_high=vary_high, vary_median=vary_median, vary_sd=vary_sd,
        vary_skew=vary_skew, vary_tailweight=vary_tailweight, vary_mode=vary_mode,
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
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default="", help="What this factor stands for.")
@click.option("--vary-correlate-with", default=None,
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

    A factor with no dependents yet loads with a warning. NAME is asked for
    if left out.
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


@calendar_add_group_command.command("transfer")
@click.argument("date", required=False)
@click.argument("from_account", required=False, shell_complete=_complete_account)
@click.argument("to_account", required=False, shell_complete=_complete_account)
@click.argument("amount", required=False)
@click.option("--from-lot", default=None,
              help="Lot of the from account; asked for when a blank one would not say which.")
@click.option("--to-lot", default=None,
              help="Lot of the to account; asked for when a blank one would not say which.")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--tax-character", default="none", show_default=True,
              help="ADR-003 tax_character (e.g. ordinary, ltcg, none).")
@click.option("--description", default="", help="Free-text description.")
@click.option("--series", default="", help="Optional series tag.")
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
        vary_correlate_with=vary_correlate_with, vary_correlation=vary_correlation,
        vary_tail_dependence=vary_tail_dependence,
        vary_ramp_months=vary_ramp_months,
    )


@calendar_add_group_command.command("transfer-balance")
@click.argument("date", required=False)
@click.argument("from_account", required=False, shell_complete=_complete_account)
@click.argument("to_account", required=False, shell_complete=_complete_account)
@click.option("--from-lot", default=None,
              help="Lot of the from account; asked for when a blank one would not say which.")
@click.option("--to-lot", default=None,
              help="Lot of the to account; asked for when a blank one would not say which.")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--tax-character", default="none", show_default=True,
              help="ADR-003 tax_character; none for a direct rollover.")
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
    if tax_character not in registries.TAX_CHARACTERS:
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
        date = _prompt_with_completion("date (YYYY-MM or YYYY-MM-DD)")
    date = _validate_month_date(date)
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


# ---------- scheduled ----------


@sim_group_command.group(name="scheduled")
def scheduled_group_command() -> None:
    """Inspect or modify recurring scheduled transactions."""


@scheduled_group_command.command("add")
@click.argument("date", required=False)
@click.argument("from_account", required=False, shell_complete=_complete_account)
@click.argument("to_account", required=False, shell_complete=_complete_account)
@click.argument("amount", required=False)
@click.option("--from-lot", default=None,
              help="Lot of the from account; asked for when a blank one would not say which.")
@click.option("--to-lot", default=None,
              help="Lot of the to account; asked for when a blank one would not say which.")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--name", "row_name", default=None,
              help="scheduled.csv name (default: derived from accounts and date).")
@click.option("--cadence", type=click.Choice(sorted(registries.CADENCES)),
              default="monthly", show_default=True)
@click.option("--month-of-year", type=click.IntRange(1, 12), default=None,
              help="Required for annual cadence; defaults to start month.")
@click.option("--end", "end_date", default="",
              help="Optional end date (YYYY-MM or YYYY-MM-DD).")
@click.option("--tax-character", default="none", show_default=True)
@click.option("--series", default="fixed", show_default=True,
              help="'fixed' for no inflation, else an inflation series (e.g. cpi).")
@click.option("--priority", type=int, default=200, show_default=True)
@click.option("--description", default="")
@click.option("--tags", default="",
              help="Space-separated '<role>:<name>' tokens (e.g. 'expense:discretionary household:rwk').")
def scheduled_add(
    date: str | None,
    from_account: str | None,
    to_account: str | None,
    amount: str | None,
    from_lot: str | None,
    to_lot: str | None,
    scenario: str | None,
    data_dir: Path | None,
    row_name: str | None,
    cadence: str,
    month_of_year: int | None,
    end_date: str,
    tax_character: str,
    series: str,
    priority: int,
    description: str,
    tags: str,
) -> None:
    """Append a recurring fixed-amount transfer to <scenario>/scheduled.csv."""
    try:
        registries.parse_tags(tags)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    with _intent_db(data_dir_p, scenario) as (con, _name):
        endpoints = _endpoints(con)
        existing_names = {
            r[0] for r in con.execute("SELECT name FROM scheduled_transaction")
        }

    if not date:
        date = _prompt_with_completion("start date (YYYY-MM or YYYY-MM-DD)")
    date = _validate_month_date(date)
    if cadence == "biweekly" and not is_full_date(date):
        raise click.ClickException(
            f"a biweekly start needs a full YYYY-MM-DD, its first payday: {date!r}"
        )
    if end_date:
        end_date = _validate_month_date(end_date)

    from_account, from_lot, to_account, to_lot = _pick_endpoints(
        endpoints, from_account or None, from_lot, to_account or None, to_lot,
    )

    if amount is None:
        amount = _prompt_with_completion("amount (dollars, e.g. 1234.56)")
    cents = _parse_amount_cents(amount)
    if cents <= 0:
        raise click.ClickException("amount must be positive")
    amount_dollars = f"{cents // 100}.{cents % 100:02d}"

    if cadence == "annual" and month_of_year is None:
        month_of_year = int(date[5:7])

    if tax_character not in registries.TAX_CHARACTERS:
        raise click.ClickException(
            f"unknown tax_character: {tax_character!r}; "
            f"allowed: {sorted(registries.TAX_CHARACTERS)}"
        )

    if not row_name:
        slug_from = "_".join(filter(None, (from_account, from_lot))).lower().replace(" ", "_")
        slug_to = "_".join(filter(None, (to_account, to_lot))).lower().replace(" ", "_")
        row_name = f"xfer_{slug_from}_to_{slug_to}_{date}"
    if row_name in existing_names:
        raise click.ClickException(
            f"scheduled transaction already exists: {row_name!r}"
        )

    row = {
        "name": row_name,
        "from_account": from_account,
        "from_lot": from_lot,
        "to_account": to_account,
        "to_lot": to_lot,
        "cadence": cadence,
        "month_of_year": str(month_of_year) if month_of_year else "",
        "amount_rule_kind": "fixed_amount",
        "series": series,
        "amount": amount_dollars,
        "base_date": date,
        "tax_character": tax_character,
        "start": date,
        "end": end_date,
        "priority": str(priority),
        "description": description,
        "meta_json": "{}",
        "params_json": "{}",
        "tags": tags,
    }
    path = scenario_dir / "scheduled.csv"
    _append_and_validate(path, registries.SCHEDULED_CSV_HEADER, row,
                         data_dir=data_dir_p, scenario_name=scenario_name)
    click.echo(
        f"appended scheduled row '{row_name}' ({cadence}) "
        f"{from_account} -> {to_account} {_fmt_cents(cents).strip()} to {path}"
    )


@scheduled_group_command.command("import-baseline")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
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
@click.option("--from-account", required=True,
              help="Scenario account that funds all generated expense rows.")
@click.option("--from-lot", default="",
              help="Lot of --from-account (required for investment accounts).")
@click.option("--start", default=None,
              help="First scheduled month YYYY-MM or YYYY-MM-DD (default: scenario start).")
@click.option("--series", default="cpi", show_default=True,
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
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def scheduled_list(scenario: str | None, data_dir: Path | None) -> None:
    """List recurring scheduled transactions."""
    from rich.console import Console
    from rich.table import Table

    from analysis.sim.notebook_ui.scenario_view import scheduled_view

    data_dir_p = _resolve_data_dir(data_dir)
    with open_scenario(data_dir_p, scenario) as sc:
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
@click.argument("name")
@click.argument("tag_tokens", nargs=-1, required=True)
@click.option("--scenario", default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def scheduled_tag_add(
    name: str,
    tag_tokens: tuple[str, ...],
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Add one or more '<role>:<name>' tags to a scheduled transaction."""
    _scenario_write(data_dir, scenario, "scheduled", name, tag_tokens, "add")


@scheduled_tag_group.command("rm")
@click.argument("name")
@click.argument("tag_tokens", nargs=-1, required=True)
@click.option("--scenario", default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def scheduled_tag_rm(
    name: str,
    tag_tokens: tuple[str, ...],
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Remove one or more '<role>:<name>' tags from a scheduled transaction."""
    _scenario_write(data_dir, scenario, "scheduled", name, tag_tokens, "rm")


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
@click.option("--owner", default=None,
              help="Person id from persons.csv (default: joint).")
@click.option("--tax-treatment", default=None,
              type=click.Choice(sorted(registries.TAX_TREATMENTS)),
              help="Default inferred from kind.")
@click.option("--series", default="",
              help="Aux rate series; gives an asset account monthly growth.")
@click.option("--tax-character", default="",
              type=click.Choice(["", *sorted(registries.GROWTH_COUNTERPARTY_ACCOUNTS)]),
              help="Character of the growth flow; required with --series.")
@click.option("--opened", "opened", default="",
              help="Open date (YYYY-MM or YYYY-MM-DD).")
@click.option("--closed", "closed", default="",
              help="Close date (YYYY-MM or YYYY-MM-DD), if any.")
@click.option("--opening-balance", default=None,
              help="Initial balance in dollars; appended to the scenario's "
                   "account_model file.")
@click.option("--scenario", default=None,
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
    scenario: str | None,
    data_dir: Path | None,
    tags: str,
) -> None:
    """Append an account to accounts.csv (and opening balance to the model)."""
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

    if tax_treatment is None:
        tax_treatment = _default_tax_treatment(kind, subkind)
    if tax_treatment not in registries.TAX_TREATMENTS:
        raise click.ClickException(
            f"unknown tax_treatment: {tax_treatment!r}; "
            f"allowed: {sorted(registries.TAX_TREATMENTS)}"
        )

    if opened:
        opened = _validate_month_date(opened)
    if closed:
        closed = _validate_month_date(closed)

    if series and not tax_character:
        raise click.ClickException(
            "--series requires --tax-character "
            f"({', '.join(sorted(registries.GROWTH_COUNTERPARTY_ACCOUNTS))})"
        )
    if series and kind != "asset":
        raise click.ClickException("--series is only supported for asset accounts")

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

    # Both rows go in as one edit, after every prompt: the account and its
    # opening balance are validated together and land together or not at all.
    accounts_path = data_dir_p / "scenarios" / scen_name / "accounts.csv"
    values_path = data_dir_p / "scenarios" / scen_name / "account_values.csv"
    with _scenario_edit(data_dir_p, scen_name) as edit:
        _append_csv_row(edit.path(accounts_path), registries.ACCOUNTS_CSV_HEADER, row)
        if cents is not None:
            _append_csv_row(
                edit.path(values_path),
                registries.ACCOUNT_VALUES_CSV_HEADER,
                {
                    "account_name": name,
                    "lot": registries.DEFAULT_LOT if subkind in INVESTMENT_ASSET_SUBKINDS else "",
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


def _rename_account_row_values(path: Path, field_names: tuple[str, ...], old_name: str, new_name: str) -> bool:
    """Rename a CSV field value across one scenario file if the column exists."""
    if not path.exists():
        return False
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    changed = False
    for field in field_names:
        if field not in df.columns:
            continue
        before = df[field].copy()
        df[field] = df[field].map(lambda v: new_name if v == old_name else v)
        changed = changed or not before.equals(df[field])
    if changed:
        df.to_csv(path, index=False)
    return changed


def _rename_account_in_scenario(data_dir: Path, scenario: str | None, old_name: str, new_name: str) -> None:
    """Rename an account and update references across scenario CSV files."""
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir, scenario)
    with _scenario_edit(data_dir, scenario_name) as edit:
        existing = {r[0] for r in edit.con.execute("SELECT name FROM account")}
        if old_name not in existing:
            raise click.ClickException(f"unknown account: {old_name!r}")
        if new_name in existing and old_name != new_name:
            raise click.ClickException(f"account already exists: {new_name!r}")
        if not _rename_account_files(edit.scenario_dir, old_name, new_name):
            raise click.ClickException(f"account not found in scenario files: {old_name!r}")

    click.echo(f"renamed account '{old_name}' to '{new_name}' in {scenario_dir}")


def _rename_account_files(scenario_dir: Path, old_name: str, new_name: str) -> bool:
    """Rename an account's references across one scenario folder's CSVs;
    whether anything changed."""
    changed = False
    accounts_path = scenario_dir / "accounts.csv"
    changed |= _rename_account_row_values(accounts_path, ("name",), old_name, new_name)
    changed |= _rename_account_row_values(
        scenario_dir / "account_values.csv",
        ("account_name", "refill_chain", "overflow_chain"), old_name, new_name,
    )
    changed |= _rename_account_row_values(scenario_dir / "scheduled.csv",
                                          ("from_account", "to_account", "interest_account"),
                                          old_name, new_name)
    changed |= _rename_account_row_values(scenario_dir / "calendar.csv", ("account_1", "account_2"), old_name, new_name)
    for chain_path in chain_edit.chain_files(scenario_dir):
        changed |= _rename_account_row_values(chain_path, ("account_name",), old_name, new_name)
    changed |= _rename_account_row_values(scenario_dir / "quicken_account_map.csv",
                                          ("account_name",),
                                          old_name, new_name)
    changed |= _rename_account_row_values(scenario_dir / "year_to_date.csv",
                                          ("from_account", "to_account"),
                                          old_name, new_name)
    return changed


_LOT_CHAIN_FIELDS = ("refill_chain", "overflow_chain")

# A lot's chain column, and the columns that mean nothing without it:
# clearing the chain clears them too, or the loader rejects the row.
_CHAIN_DEPENDENT_FIELDS: dict[str, tuple[str, ...]] = {
    "refill_chain": ("refill_lot", "refill_target", "refill_amount"),
    "overflow_chain": ("overflow_lot",),
}

_ACCOUNT_REFERENCE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("account_values.csv", ("account_name",)),
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
        "UNION ALL SELECT 1 FROM account_model_member m "
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
    values_path = scenario_dir / "account_values.csv"
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
                    f"  account_values.csv row {idx + 2}: {', '.join(matched_fields)}"
                )

    paths = [
        (scenario_dir / filename, fields)
        for filename, fields in _ACCOUNT_REFERENCE_FIELDS
    ]
    paths.extend((path, ("account_name",)) for path in chain_edit.chain_files(scenario_dir))

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
    paths.extend((path, ("account_name",)) for path in chain_edit.chain_files(scenario_dir))

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

    values_path = scenario_dir / "account_values.csv"
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
@click.argument("name")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def delete_account(name: str, scenario: str | None, data_dir: Path | None) -> None:
    """Delete an account and any direct CSV rows that reference it."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    accounts_path = scenario_dir / "accounts.csv"
    if not accounts_path.exists():
        raise click.ClickException(f"accounts.csv not found: {accounts_path}")

    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    if "name" not in df.columns or not (df["name"] == name).any():
        raise click.ClickException(f"unknown account: {name!r}")

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
@click.argument("name")
@click.option("--new-name", default=None, help="Update the account name.")
@click.option("--kind", default=None, help="New account kind.")
@click.option("--subkind", default=None, help="New account subkind.")
@click.option("--owner", default=None, help="New owner (person id or 'joint').")
@click.option("--tax-treatment", default=None, help="New tax treatment.")
@click.option("--opened", default=None, help="Open date (YYYY-MM or YYYY-MM-DD).")
@click.option("--closed", default=None, help="Close date (YYYY-MM or YYYY-MM-DD).")
@click.option("--meta-json", default=None, help="JSON payload for the account metadata.")
@click.option("--tags", default=None, help="Space-separated '<role>:<name>' tokens.")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def edit_account(
    name: str,
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
    """Edit an account's fields, prompting with current values as defaults when needed."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    accounts_path = scenario_dir / "accounts.csv"
    if not accounts_path.exists():
        raise click.ClickException(f"accounts.csv not found: {accounts_path}")

    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    if "name" not in df.columns:
        raise click.ClickException(f"accounts.csv missing 'name' column: {accounts_path}")
    matches = df.index[df["name"] == name].tolist()
    if not matches:
        raise click.ClickException(f"unknown account: {name!r}")
    idx = matches[0]
    current = df.loc[idx].to_dict()

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
        tax_treatment = click.prompt(
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

    df.loc[idx, [
        "name", "kind", "subkind", "owner", "tax_treatment",
        "opened", "closed", "meta_json", "tags",
    ]] = [
        new_name, kind, subkind, owner, tax_treatment,
        opened, closed, meta_json, tags,
    ]
    if new_name != name and new_name in existing:
        raise click.ClickException(f"account already exists: {new_name!r}")
    with _scenario_edit(data_dir_p, scenario_name) as edit:
        df.to_csv(edit.path(accounts_path), index=False)
        if new_name != name:
            # accounts.csv already carries new_name; this renames the references.
            _rename_account_files(edit.scenario_dir, name, new_name)

    click.echo(f"updated account '{new_name}' in {accounts_path}")


@account_group_command.command("rename")
@click.argument("old_name")
@click.argument("new_name")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def rename_account(
    old_name: str,
    new_name: str,
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Rename an account and update references across the scenario CSVs."""
    old_name = old_name.strip()
    new_name = new_name.strip()
    if not old_name:
        raise click.ClickException("old account name required")
    if not new_name:
        raise click.ClickException("new account name required")

    data_dir_p = _resolve_data_dir(data_dir)
    _rename_account_in_scenario(data_dir_p, scenario, old_name, new_name)


def _default_tax_treatment(kind: str, subkind: str) -> str:
    match (kind, subkind):
        case ("asset", "traditional_ira" | "traditional_401k" | "hsa"):
            return "tax_deferred"
        case ("asset", "roth_ira" | "roth_401k"):
            return "tax_free"
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
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--as-of", default=None,
              help="Resolve rates effective on or before this date (YYYY-MM or YYYY-MM-DD).")
@click.option("--kind", default=None,
              help="Filter to accounts of this kind (e.g. asset, liability).")
@click.option("--owner", default=None,
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
    with open_scenario(data_dir_p, scenario) as sc:
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
        click.option("--scenario", default=None,
                     help="Scenario folder name (default: the only scenario in data-dir)."),
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
@click.argument("account")
@_holdings_options
def account_holdings_command(
    account: str, scenario: str | None, data_dir: Path | None, month: str | None,
    opening: bool, runfile: Path | None,
) -> None:
    """Show the lots in ACCOUNT: symbol, quantity, balance, basis and unrealized gain."""
    from rich.console import Console

    with _holdings_source(scenario, data_dir, month, opening, runfile) as (source, described):
        try:
            frame = account_holdings(source, account, month=month)
        except (KeyError, ValueError) as exc:
            raise click.ClickException(exc.args[0]) from None
    title = f"holdings of {account} ({described}; {frame['as_of'].iloc[0]})"
    Console().print(render_holdings_table(frame, title=title))


@account_group_command.command("holders")
@click.argument("symbol")
@_holdings_options
def account_holders_command(
    symbol: str, scenario: str | None, data_dir: Path | None, month: str | None,
    opening: bool, runfile: Path | None,
) -> None:
    """Show every account holding SYMBOL, lot by lot, with totals."""
    from rich.console import Console

    with _holdings_source(scenario, data_dir, month, opening, runfile) as (source, described):
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
    from rich.console import Console
    from rich.measure import Measurement

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
    rendered = allocation.render_allocation_table(table, title=title)
    console = Console()
    # Never crop amounts to fit the terminal: a wide --by split prints at its full
    # width (the terminal wraps it; `| less -S` scrolls it) rather than losing columns.
    needed = Measurement.get(console, console.options.update(max_width=10_000), rendered).maximum
    if needed > console.width:
        console = Console(width=needed)
    console.print(rendered)
    if allocation.UNCLASSIFIED in table.index:
        click.echo("unclassified: lots whose symbol has no rows in allocations.csv"
                   " (user guide Part 4.11: Add a stock or fund)", err=True)


@account_group_command.group(name="tag")
def account_tag_group() -> None:
    """Add or remove tags on an account."""


@account_tag_group.command("add")
@click.argument("name")
@click.argument("tag_tokens", nargs=-1, required=True)
@click.option("--scenario", default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def account_tag_add(
    name: str,
    tag_tokens: tuple[str, ...],
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Add one or more '<role>:<name>' tags to an account."""
    _scenario_write(data_dir, scenario, "account", name, tag_tokens, "add")


@account_tag_group.command("rm")
@click.argument("name")
@click.argument("tag_tokens", nargs=-1, required=True)
@click.option("--scenario", default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def account_tag_rm(
    name: str,
    tag_tokens: tuple[str, ...],
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Remove one or more '<role>:<name>' tags from an account."""
    _scenario_write(data_dir, scenario, "account", name, tag_tokens, "rm")


# ---------- account lot: one account_values.csv row (#124) ----------

# Per file, the (account, lot) column pairs that name a lot. A blank lot names
# the account's `default` lot, the only one a blank may name (ADR-003 §7).
_LOT_REFERENCE_FIELDS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("scheduled.csv", (("from_account", "from_lot"), ("to_account", "to_lot"))),
    ("calendar.csv", (("account_1", "lot_1"), ("account_2", "lot_2"))),
    ("year_to_date.csv", (("from_account", "from_lot"), ("to_account", "to_lot"))),
)

# account_values.csv's own references: another lot's refill or overflow chain.
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
    """A scenario's own account_values.csv rows, every column present."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame = frame.reindex(columns=registries.ACCOUNT_VALUES_CSV_HEADER, fill_value="")
    return [{str(k): str(v) for k, v in record.items()} for record in frame.to_dict("records")]


def _scenario_lots(scenario_dir: Path, name: str) -> list[_Lot]:
    """Every lot of scenario ``name``, overlaid down its load order as the
    loader overlays them, in the order they first appear."""
    lots: dict[tuple[str, str], _Lot] = {}
    for scenario_name in load_order(scenario_dir.parent, name):
        rows = _read_values_rows(scenario_dir.parent / scenario_name / "account_values.csv")
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
    paths.extend((path, (("account_name", "lot"),)) for path in chain_edit.chain_files(scenario_dir))
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
    display lines; with ``own_row``, its own account_values.csv row too (an
    inheriting scenario's override of it)."""
    references: list[str] = []
    values = _read_values_rows(scenario_dir / "account_values.csv")
    for index, row in enumerate(values):
        if _names_lot(row, "account_name", "lot", account, lot):
            if own_row:
                references.append(f"  account_values.csv row {index + 2}: an override of the lot")
            continue
        matched = [f"{a}/{b}" for a, b in _LOT_CHAIN_LOT_FIELDS if _names_lot(row, a, b, account, lot)]
        if matched:
            references.append(f"  account_values.csv row {index + 2}: {', '.join(matched)}")
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
    values_path = scenario_dir / "account_values.csv"
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
    """A money answer as account_values.csv holds it: dollars and cents, or
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
                "UNION SELECT series FROM account_model_member"
            ) if r[0]
        })
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
        )


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
    """The account_values.csv row for a lot's answers. An unchanged cell is
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
    delete_references: tuple[str, str] | None = None,
) -> None:
    """Write ``row`` into the scenario's own account_values.csv -- appended,
    or in place of ``replace``'s row; ``None`` removes that row instead --
    and rewrite the references ``rename`` (old, new lot) or
    ``delete_references`` (account, lot) asks for, as one staged edit."""
    with _scenario_edit(data_dir, scenario_name) as edit:
        staged = edit.path(path)
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
                _append_csv_row(staged, registries.ACCOUNT_VALUES_CSV_HEADER, row, replace=replace.index)
        elif row is not None:
            _append_csv_row(staged, registries.ACCOUNT_VALUES_CSV_HEADER, row)
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
            f = click.option(field_.option, default=None, help=field_.help or field_.label)(f)
    return f


def _is_investment(context: calendar_guide.GuideContext, account: str) -> bool:
    return context.subkinds.get(account) in INVESTMENT_ASSET_SUBKINDS


@account_group_command.group(name="lot")
def lot_group_command() -> None:
    """Add, change or delete an account's lots (account_values.csv rows, ADR-005)."""


@lot_group_command.command("list")
@click.argument("account", required=False, shell_complete=_complete_account)
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
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
            # The loader gives an account with no account_values.csv rows an
            # empty default lot; say so rather than list nothing.
            click.echo(
                f"{account!r} has no lots in account_values.csv, so it loads with an "
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
@click.argument("account", required=False, shell_complete=_complete_account)
@click.argument("lot", required=False)
@_lot_value_options
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.pass_context
def lot_add(
    ctx: click.Context, account: str | None, lot: str | None,
    scenario: str | None, data_dir: Path | None, **values: str | None,
) -> None:
    """Add a lot to an account: one row of the scenario's account_values.csv.

    With ACCOUNT and LOT, writes the lot from the options. Without them, asks
    for what applies to the account a question at a time (a cost basis only
    for a taxable account, balance management only if wanted), then shows the
    lot and the equivalent command, and writes it once you confirm.

    A lot a parent scenario has that this one, or a scenario between, deleted
    (zeroed) may be added again; it replaces or overrides the zeroed one.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "account_values.csv"
    lots = _scenario_lots(scenario_dir, scenario_name)
    context = _lot_guide_context(data_dir_p, scenario_name, lots)

    def submit(params: dict[str, object]) -> None:
        _check_lot_params(context, params)
        key = (str(params["account"]).strip(), str(params["lot"]).strip())
        zeroed = next((one for one in lots if one.deleted and (one.account, one.lot) == key), None)
        row = _lot_row(params, None, investment=_is_investment(context, key[0]))
        # The scenario resolved above, by name: a defaulted scenario is not
        # resolved again, in case the default changed during the prompts.
        _write_lot(data_dir_p, scenario_name, path, row, replace=zeroed)
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
@click.argument("account", required=False, shell_complete=_complete_account)
@click.argument("lot", required=False)
@click.option("--new-lot", default=None,
              help="Rename the lot; this scenario's references to it follow.")
@_lot_value_options
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
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
    renaming one is refused, since the parent's lot would remain.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scenario_name = _resolve_scenario_dir(data_dir_p, scenario)
    path = scenario_dir / "account_values.csv"
    lots = _scenario_lots(scenario_dir, scenario_name)
    chosen = _select_lot(lots, account, lot)
    context = _lot_guide_context(data_dir_p, scenario_name, lots, editing=chosen)

    def submit(params: dict[str, object]) -> None:
        _check_lot_params(context, params, editing=chosen)
        new = str(params["lot"]).strip()
        renamed = new != chosen.lot
        if renamed and chosen.inherited_from:
            raise click.ClickException(
                f"lot {chosen.lot!r} of {chosen.account!r} comes from scenario "
                f"{chosen.inherited_from!r}; rename it there"
            )
        row = _lot_row(params, chosen.row, investment=_is_investment(context, chosen.account))
        if row == chosen.row and chosen.index is not None:
            click.echo("no change; nothing was written")
            return
        _write_lot(
            data_dir_p, scenario_name, path, row, replace=chosen,
            rename=(chosen.lot, new) if renamed else None,
        )
        if chosen.index is None:
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
    if chosen.index is None:
        click.echo(
            f"  It comes from scenario {chosen.inherited_from!r}: writing puts an override "
            "of the whole lot in this scenario."
        )
    guide = _lot_guide()
    guide.complete(lot_guide.LOT, context, answers)
    if not guide.review(lot_guide.LOT, context, answers, submit, show_command=False):
        ctx.exit(1)


@lot_group_command.command("delete")
@click.argument("account", required=False, shell_complete=_complete_account)
@click.argument("lot", required=False)
@click.option("--yes", is_flag=True, help="Don't ask before deleting the rows that reference the lot.")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
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
    path = scenario_dir / "account_values.csv"
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
        for line in _lot_reference_rows(scenario_dir.parent / name, chosen.account, chosen.lot)
    ]
    if references:
        click.echo("still named by:")
        click.echo("\n".join(references))


@account_group_command.command("check")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def check_account_values(
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Check for account_values alignment issues (missing, duplicate, unknown accounts)."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scen_name = _resolve_scenario_dir(data_dir_p, scenario)

    accounts_path = scenario_dir / "accounts.csv"
    values_path = scenario_dir / "account_values.csv"

    if not accounts_path.exists():
        raise click.ClickException(f"accounts.csv not found: {accounts_path}")
    if not values_path.exists():
        raise click.ClickException(f"account_values.csv not found: {values_path}")

    # Read accounts
    account_names = set()
    with accounts_path.open(newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        if reader.fieldnames is None or "name" not in reader.fieldnames:
            raise click.ClickException("accounts.csv missing 'name' column")
        for row in reader:
            if row.get("name"):
                account_names.add(row["name"])

    # Read account_values
    values_accounts = {}
    seen_lots: set[tuple[str, str]] = set()
    with values_path.open(newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        if reader.fieldnames is None or "account_name" not in reader.fieldnames:
            raise click.ClickException("account_values.csv missing 'account_name' column")
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
        click.echo(f"missing account_values rows for: {', '.join(sorted(missing))}", err=True)
    if not missing and not any(v for k, v in values_accounts.items() if k not in account_names):
        click.echo("ok")


@account_group_command.command("sync")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def sync_account_values(
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Sync account_values.csv: create missing rows for accounts (with zero opening_balance)."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, scen_name = _resolve_scenario_dir(data_dir_p, scenario)

    accounts_path = scenario_dir / "accounts.csv"
    values_path = scenario_dir / "account_values.csv"

    if not accounts_path.exists():
        raise click.ClickException(f"accounts.csv not found: {accounts_path}")
    if not values_path.exists():
        raise click.ClickException(f"account_values.csv not found: {values_path}")

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

    # Read existing account_values
    values_accounts = set()
    values_rows = []
    with values_path.open(newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        if reader.fieldnames is None or "account_name" not in reader.fieldnames:
            raise click.ClickException("account_values.csv missing 'account_name' column")
        values_header = list(reader.fieldnames)
        for row in reader:
            values_rows.append(row)
            if row.get("account_name"):
                values_accounts.add(row["account_name"])

    # Find missing
    missing = [name for name in account_names if name not in values_accounts]
    if not missing:
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
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
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
    """Update account_values.csv from a Quicken account-tree report export.

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
    values_path = scenario_dir / "account_values.csv"
    if not accounts_path.exists():
        raise click.ClickException(f"accounts.csv not found: {accounts_path}")
    if not values_path.exists():
        raise click.ClickException(f"account_values.csv not found: {values_path}")

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
        raise click.ClickException(f"account_values.csv missing 'account_name' column: {values_path}")
    known = set(df["account_name"])

    changed = 0
    for name, cents in updates.items():
        if name not in known:
            raise click.ClickException(
                f"mapped account not found in account_values.csv: {name!r}"
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
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def list_transfers(scenario: str | None, data_dir: Path | None) -> None:
    """List one-time transfers: calendar one_off_transfer and transfer_balance
    events, and scheduled rows with cadence one_off."""
    from rich.console import Console
    from rich.table import Table

    data_dir_p = _resolve_data_dir(data_dir)
    with open_intent_db(data_dir_p, scenario) as (con, scen_name):
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
            "LEFT JOIN account_model_member m1 ON m1.id = c1.lot_id "
            "LEFT JOIN account a1 ON a1.id = c1.account_id "
            "LEFT JOIN chain c2 ON c2.name = ce.account_2 "
            "LEFT JOIN account_model_member m2 ON m2.id = c2.lot_id "
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
            "LEFT JOIN account_model_member m1 ON m1.id = c1.lot_id "
            "LEFT JOIN account a1 ON a1.id = c1.account_id "
            "JOIN chain c2 ON c2.id = s.to_account "
            "LEFT JOIN account_model_member m2 ON m2.id = c2.lot_id "
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
    item_name: str,
    tag_tokens: tuple[str, ...],
    op: str,  # 'add' | 'rm'
) -> None:
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
@click.argument("symbols", nargs=-1)
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
@click.argument("symbols", nargs=-1)
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
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)")
@click.option("--tolerance", type=float, default=1.0, show_default=True,
              help="Percent difference between a stated balance and quantity x price that is flagged.")
def prices_check_command(scenario: str | None, data_dir: Path | None, tolerance: float) -> None:
    """Compare each lot's stated balance with quantity x price.

    For lots with a symbol and a quantity, shows the balance in account_values.csv
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
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
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
@click.argument("names", nargs=-1)
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
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
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
    if graph_file is not None and graph_file.suffix.lower() not in (".png", ".svg"):
        raise click.UsageError(f"--graph-file {graph_file} must end in .png or .svg")
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
    fmt = "svg" if graph_file is not None and graph_file.suffix.lower() == ".svg" else "png"
    # As wide as the terminal, short of its last column, which some
    # terminals wrap at.
    fit_width = (shutil.get_terminal_size().columns - 1) * cell[0] if cell else None
    try:
        image = chain_graph.render(source, fmt, scale=chain_graph.terminal_scale(cell),
                                   fit_width=fit_width)
    except chain_graph.GraphError as error:
        raise click.ClickException(str(error)) from error
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
            with tempfile.NamedTemporaryFile(prefix="chain-", suffix=".png", delete=False) as f:
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
}


def _step_options(f):
    for option in ("cadence", "series", "maximum", "percent", "lot"):
        f = click.option(f"--{option}", default=None, help=_STEP_OPTIONS_HELP[option])(f)
    return f


def _scenario_options(f):
    f = click.option("--data-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
                     default=None, help="Data repo (default: $ANALYSIS_DATA_DIR)")(f)
    return click.option("--scenario", default=None,
                        help="Scenario folder name (default: the only scenario in data-dir).")(f)


def _step_row(
    chain: str, account: str | None, lot: str | None, percent: str | None, maximum: str | None,
    series: str | None, cadence: str | None, endpoints: _Endpoints, role: str,
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
           "series": series or "", "cadence": cadence or ""}
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
    for column in ("lot", "percent", "maximum", "series", "cadence"):
        if row.get(column):
            words += [f"--{column}", row[column]]
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
        source = chain_edit.chain_file(scenarios / owner, name)
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
@click.argument("account", required=False, shell_complete=_complete_account)
@click.option("--role", type=click.Choice(registries.CHAIN_ROLES), default=None,
              help="source: money is drawn from it; destination: money is paid into it (asked if left out).")
@_step_options
@_scenario_options
def chain_add_command(
    name: str | None, account: str | None, role: str | None,
    lot: str | None, percent: str | None, maximum: str | None, series: str | None, cadence: str | None,
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
            steps = [_step_row(name, account, lot, percent, maximum, series, cadence, endpoints, role)]
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
@click.argument("name", required=False)
@click.argument("account", required=False, shell_complete=_complete_account)
@click.option("--position", type=int, default=None,
              help="Step number the new step takes, moving later steps down (default: last).")
@_step_options
@_scenario_options
def chain_step_add_command(
    name: str | None, account: str | None, position: int | None,
    lot: str | None, percent: str | None, maximum: str | None, series: str | None, cadence: str | None,
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
                        endpoints.roles[name])
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
@click.argument("name", required=False)
@click.argument("number", type=int, required=False)
@_step_options
@_scenario_options
def chain_step_edit_command(
    name: str | None, number: int | None,
    lot: str | None, percent: str | None, maximum: str | None, series: str | None, cadence: str | None,
    scenario: str | None, data_dir: Path | None,
) -> None:
    """Change step NUMBER of chain NAME. Each option given replaces that
    column; give it as '' to clear it (a percent cleared is 100%).

    Without NUMBER, shows the steps and asks which. Without any option, asks
    for each column in turn, starting from its current value, and prints the
    equivalent command.
    """
    given = {k: v for k, v in
             {"lot": lot, "percent": percent, "maximum": maximum, "series": series, "cadence": cadence}.items()
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
            endpoints, endpoints.roles[name],
        )
        after = steps[index]
    click.echo(f"{name}: step {number} is {_step_summary(after)}")
    if not given:
        changed = [c for c in ("lot", "percent", "maximum", "series", "cadence")
                   if before.get(c, "") != after.get(c, "")]
        if changed:
            words = ["sim", "chain", "step", "edit", name, str(number)]
            for column in changed:
                words += [f"--{column}", after.get(column, "")]
            _echo_equivalent(words)
        else:
            click.echo("  (nothing changed)")


@chain_step_group.command("rm")
@click.argument("name", required=False)
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
@click.argument("name", required=False)
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
@click.argument("old", required=False)
@click.argument("new", required=False)
@_scenario_options
def chain_rename_command(old: str | None, new: str | None, scenario: str | None, data_dir: Path | None) -> None:
    """Rename chain OLD to NEW, and every reference to it: rules, calendar
    transfers, lots' refill and overflow, and other chains' steps, here and in
    every scenario that inherits from this one. Either name left out is
    asked for."""
    data = _resolve_data_dir(data_dir)
    with _scenario_edit(data, scenario) as edit:
        if old is None:
            old = _ask_chain(edit.con, "chain to rename")
        scenarios = edit.data_dir / "scenarios"
        owner = chain_edit.defining_scenario(scenarios, edit.name, old)
        if owner is None:
            raise click.ClickException(_no_chain(edit.con, old, edit.name))
        first = next(each for each in load_order(scenarios, edit.name)
                     if chain_edit.chain_file(scenarios / each, old) is not None)
        if first != edit.name:
            # Renaming only a copy would leave the parent's chain in place
            # under the old name, so a rename starts where the chain does.
            raise click.ClickException(
                f"{old} comes from {first}; rename it there (--scenario {first}), "
                "and the scenarios built on it follow")
        taken = {r[0] for r in edit.con.execute("SELECT name FROM chain UNION SELECT name FROM account")}
        if new is None:
            new = _ask_new_chain_name(taken, f"new name for {old} (one word, no spaces)")
        elif problem := chain_edit.name_problem(new):
            raise click.ClickException(problem)
        elif new in taken:
            raise click.ClickException(f"{new!r} is already a chain or an account's name")
        affected = (edit.name, *chain_edit.descendants(scenarios, edit.name))
        # A scenario here that gets OLD from a mix-in outside this lineage
        # keeps that chain, so renaming its references would point them at
        # the wrong one.
        if shadowed := {each: source for each in affected
                        if (source := chain_edit.defining_scenario(scenarios, each, old)) not in affected}:
            raise click.ClickException(
                "; ".join(f"{each} gets {old} from {source}" for each, source in shadowed.items())
                + f", which a rename in {edit.name} doesn't reach; rename it there first")
        # A scenario built on this one may already have a chain called NEW,
        # its own or one from a mix-in, which the rename would overwrite or
        # shadow; check before touching anything.
        clashes = {each: source for each in affected
                   if (source := chain_edit.defining_scenario(scenarios, each, new)) is not None}
        if clashes:
            raise click.ClickException(
                f"a chain {new!r} already applies in "
                + ", ".join(each if source == each else f"{each} (from {source})"
                            for each, source in clashes.items())
                + "; rename or delete it first")
        changed = []
        for each in affected:
            changed += [f"{each}: {c}" for c in chain_edit.rename_references(scenarios / each, old, new)]
    click.echo(f"renamed chain {old} to {new}")
    for line in changed:
        click.echo(f"  {line}")


@chain_group_command.command("delete")
@click.argument("name", required=False)
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
    scenario's own prices/ or its account_values.csv names it. A missing
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
              help="Show a table with each scenario's parents, dates and description.")
def scenario_list(data_dir: Path | None, verbose: bool) -> None:
    """List the data repo's scenarios.

    Plain output is just the names, one per line, for scripts. --verbose adds
    parents, start and end months, and description, and marks the current
    scenario with *. A date in [brackets] is inherited from an ancestor.
    """
    data_dir_p = _resolve_data_dir(data_dir)
    try:
        if not verbose:
            for name in list_scenario_names(data_dir_p):
                click.echo(name)
            return
        summaries = sc.scenario_summaries(data_dir_p)
    except LoadError as exc:
        raise click.ClickException("; ".join(exc.issues)) from None
    if not summaries:
        click.echo(f"(no scenarios in {data_dir_p / 'scenarios'})")
        return

    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    def date_cell(d: sc.ScenarioDate | None) -> str:
        if d is None:
            return ""
        return escape(f"[{d.month}]") if d.inherited else escape(d.month)

    current = read_current_scenario(data_dir_p)
    table = Table(title=f"scenarios ({data_dir_p.name})",
                  caption=escape("[dates] are inherited"))
    table.add_column("", no_wrap=True)
    table.add_column("scenario", no_wrap=True)
    table.add_column("parents")
    table.add_column("start", no_wrap=True)
    table.add_column("end", no_wrap=True)
    table.add_column("description")
    for s in summaries:
        table.add_row(
            "*" if s.name == current else "",
            s.name, " + ".join(s.parents), date_cell(s.start), date_cell(s.end),
            escape(s.description),
        )
    Console().print(table)


@scenario_group_command.command("create")
@click.argument("name")
@click.argument("parent")
@click.argument("mixins", nargs=-1)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default=None,
              help="Default: 'Derived from <parent> + <mixin>...'.")
def scenario_create(
    name: str,
    parent: str,
    mixins: tuple[str, ...],
    data_dir: Path | None,
    description: str | None,
) -> None:
    """Create scenario NAME deriving from PARENT, and make it current.

    The new scenario inherits everything from PARENT; only the rows you add to
    its (empty) overlay CSVs differ. Each MIXIN is then applied on top of
    PARENT, in order, down to the first ancestor already inherited -- usually
    just the mix-in itself, stopping at the shared base. See ADR-003 §4.5 for
    merge semantics.
    """

    skeletons: dict[str, tuple[str, ...]] = {
        "accounts.csv": registries.ACCOUNTS_CSV_HEADER,
        "account_values.csv": registries.ACCOUNT_VALUES_CSV_HEADER,
        "scheduled.csv": registries.SCHEDULED_CSV_HEADER,
        "calendar.csv": registries.CALENDAR_HEADER,
    }
    try:
        data_dir_p = _resolve_data_dir(data_dir)
        # Preflights PARENT, then creates and validates NAME in a staged copy.
        with _scenario_edit(data_dir_p, parent) as edit:
            sc.create_scenario(
                edit.data_dir, name, parent, description=description, mixins=mixins,
            )
        target = data_dir_p / "scenarios" / name

        inherits = [_resolve_scenario(data_dir_p, p).name for p in (parent, *mixins)]
        write_current_scenario(data_dir_p, name)
        click.echo(f"created {target}")
        click.echo(f"  inherits: {' + '.join(inherits)}")
        for filename in ("scenario.csv", *skeletons):
            click.echo(f"  {filename}")
        click.echo("  chains/source/.gitignore")
        click.echo("  chains/source/_example.csv")
        click.echo("  chains/destination/.gitignore")
        click.echo("  series/")
        click.echo("  series/README.md")
        click.echo(f"  current scenario: {name}")
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        raise click.ClickException(
            f"failed to create scenario {name}"
        ) from None


@sim_group_command.group(name="tags", invoke_without_command=True)
@click.pass_context
@click.option("--scenario", default=None)
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
    with open_scenario(data_dir_p, scenario) as sc:
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
@click.argument("tag_token")
@click.option("--scenario", default=None)
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def tags_items(
    tag_token: str,
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """List items (accounts, scheduled, calendar events) carrying the given tag.

    ``<role>:*`` lists everything in a role (e.g. ``estimate:*``).
    """
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
    data_dir_p = _resolve_data_dir(data_dir)
    with open_scenario(data_dir_p, scenario) as sc:
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


# ---------- Silent alias: accounts -> account ----------

# Create silent alias for backwards compatibility
@sim_group_command.group(name="accounts", hidden=True)
def accounts_group_command() -> None:
    """Alias for 'account' (silent)."""
    pass


# Dynamically copy all commands from account_group to accounts_group
for cmd_name, cmd in account_group_command.commands.items():
    accounts_group_command.add_command(cmd, name=cmd_name)
