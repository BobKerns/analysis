"""``analysis sim`` subcommand group."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import click

from analysis.sim.engine import run_scenario
from analysis.sim.loader import DataDirtyRefused, LoadError, load_scenario

DEFAULT_INTENT_DIR = Path("state/intent")
DEFAULT_RUNS_DIR = Path("state/runs")


@click.group(name="sim")
def sim_group_command() -> None:
    """Scenario-based finance simulator."""


@sim_group_command.command()
@click.argument("scenario_name")
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Data repo (default: $ANALYSIS_DATA_DIR)",
)
@click.option(
    "--intent-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_INTENT_DIR,
    show_default=True,
)
@click.option(
    "--allow-dirty-data",
    is_flag=True,
    default=False,
    help="Allow loading when the data repo has uncommitted changes in the scenario's scoped paths.",
)
def load(
    scenario_name: str,
    data_dir: Path | None,
    intent_dir: Path,
    allow_dirty_data: bool,
) -> None:
    """Build state/intent/<scenario>.sqlite3 from the data repo."""
    resolved = _resolve_data_dir(data_dir)
    if not allow_dirty_data and os.environ.get("ANALYSIS_DATA_MODE") == "prototype":
        allow_dirty_data = True
        click.echo("info: ANALYSIS_DATA_MODE=prototype; allowing dirty data", err=True)
    try:
        result = load_scenario(
            resolved, scenario_name, intent_dir=intent_dir,
            allow_dirty_data=allow_dirty_data,
        )
    except LoadError as exc:
        for issue in exc.issues:
            click.echo(f"  error: {issue}", err=True)
        raise click.ClickException(f"{len(exc.issues)} issue(s)") from None
    except DataDirtyRefused as exc:
        click.echo(str(exc), err=True)
        raise click.ClickException("data dirty; refusing to load") from None
    click.echo(f"loaded {result.intent_path}")
    for table, n in result.counts.items():
        click.echo(f"  {table}: {n}")


@sim_group_command.command()
@click.argument("scenario_name")
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
)
def check(scenario_name: str, data_dir: Path | None) -> None:
    """Validate the scenario's CSVs without producing a persistent intent file."""
    resolved = _resolve_data_dir(data_dir)
    with tempfile.TemporaryDirectory() as td:
        try:
            load_scenario(
                resolved, scenario_name, intent_dir=Path(td), compute_stamps=False,
            )
        except LoadError as exc:
            for issue in exc.issues:
                click.echo(f"  error: {issue}", err=True)
            raise click.ClickException(f"{len(exc.issues)} issue(s)") from None
    click.echo("ok")


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
@click.argument("scenario_name")
@click.option(
    "--intent-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=DEFAULT_INTENT_DIR,
    show_default=True,
)
@click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_RUNS_DIR,
    show_default=True,
)
@click.option(
    "--data-mode",
    default=None,
    help="live | prototype (default: $ANALYSIS_DATA_MODE or 'live')",
)
@click.option(
    "--run-by",
    default=None,
    help="Identifier recorded in simulation_run.run_by (default: $USER)",
)
@click.option("--allow-dirty-data", is_flag=True, default=False)
@click.option("--allow-dirty-code", is_flag=True, default=False)
def run(
    scenario_name: str,
    intent_dir: Path,
    runs_dir: Path,
    data_mode: str | None,
    run_by: str | None,
    allow_dirty_data: bool,
    allow_dirty_code: bool,
) -> None:
    """Execute <scenario> against its intent file and write a new run file."""
    intent_path = intent_dir / f"{scenario_name}.sqlite3"
    if not intent_path.exists():
        raise click.ClickException(
            f"intent file not found: {intent_path}\n"
            f"  run: analysis sim load {scenario_name}"
        )

    resolved_mode = data_mode or os.environ.get("ANALYSIS_DATA_MODE") or "live"
    resolved_by = run_by or os.environ.get("USER") or "unknown"
    if not allow_dirty_data and resolved_mode == "prototype":
        allow_dirty_data = True

    data_version = _read_data_version(intent_path)
    short = data_version[:7] if data_version else "nodata"
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    run_path = runs_dir / f"{stamp}_{scenario_name}_{short}.sqlite3"

    try:
        out = run_scenario(
            intent_path, run_path,
            run_by=resolved_by, data_mode=resolved_mode,
            allow_dirty_data=allow_dirty_data,
            allow_dirty_code=allow_dirty_code,
        )
    except DataDirtyRefused as exc:
        click.echo(str(exc), err=True)
        raise click.ClickException("data dirty; refusing to run") from None
    click.echo(f"run {out}")


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
    default=DEFAULT_RUNS_DIR,
    show_default=True,
)
def list_runs(runs_dir: Path) -> None:
    """List run files in --runs-dir with status, scenario, who, when."""
    if not runs_dir.exists():
        click.echo(f"(no runs dir: {runs_dir})")
        return
    files = sorted(runs_dir.glob("*.sqlite3"))
    if not files:
        click.echo(f"(no run files in {runs_dir})")
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
            "code_version, code_dirty, data_version, data_dirty, data_mode, "
            "run_by, host, python_version "
            "FROM simulation_run WHERE id = 1"
        ).fetchone()
        if run is None:
            raise click.ClickException(f"no simulation_run row in {runfile}")
        (name, status, started, finished, failure, cv, cd, dv, dd, mode,
         by, host, py) = run
        click.echo(f"file:           {runfile}")
        click.echo(f"scenario:       {name}")
        click.echo(f"status:         {status}" + (f"  ({failure})" if failure else ""))
        click.echo(f"started:        {started}")
        click.echo(f"finished:       {finished or '-'}")
        click.echo(f"code:           {cv[:12]}{' (dirty)' if cd else ''}")
        click.echo(f"data:           {dv[:12]}{' (dirty)' if dd else ''}  mode={mode}")
        click.echo(f"run_by:         {by}@{host}  python {py}")

        steps = con.execute("SELECT COUNT(*) FROM simulation_step").fetchone()[0]
        states = con.execute("SELECT COUNT(*) FROM account_state").fetchone()[0]
        xfers = con.execute("SELECT COUNT(*) FROM transfer_event").fetchone()[0]
        click.echo(
            f"counts:         steps={steps}  account_states={states}  transfers={xfers}"
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


def _fmt_cents(cents: int | None) -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else " "
    n = abs(int(cents))
    dollars, rem = divmod(n, 100)
    return f"{sign}${dollars:>14,}.{rem:02d}"
