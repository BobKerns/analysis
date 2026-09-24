"""``analysis sim`` subcommand group."""

from __future__ import annotations

import csv as _csv
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import click
import pandas as pd

from analysis.amazon.notebook import load_monthly_summaries
from analysis.expenses.baseline import (
    build_expense_baseline,
    build_scheduled_baseline_rows,
    load_category_map,
    load_expense_account_map,
    replace_generated_scheduled_rows,
)
from analysis.sim import registries
from analysis.sim.accounts import load_accounts_view, render_accounts_table
from analysis.sim.connection import open_scenario
from analysis.sim.engine import run_scenario
from analysis.sim.loader import (
    DEFAULT_INTENT_DIR,
    DEFAULT_RUNS_DIR,
    DEFAULT_STATE_DIR,
    DataDirtyRefused,
    LoadError,
    ScenarioRef,
    list_scenario_names,
    load_scenario,
    open_intent_db,
    find_state_dir,
    write_current_scenario,
)
from analysis.sim.quicken import (
    format_dollars,
    is_investment_asset,
    load_account_map,
    parse_net_worth_export,
)
import analysis.sim.scenario as sc
from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS


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
def check(scenario_name: str | None, data_dir: Path | None) -> None:
    """Validate the scenario's CSVs without producing a persistent intent file."""
    resolved = _resolve_data_dir(data_dir)
    ref = _resolve_scenario(resolved, scenario_name)
    with tempfile.TemporaryDirectory() as td:
        try:
            load_scenario(
                resolved, ref.name, intent_dir=Path(td), compute_stamps=False,
            )
        except LoadError as exc:
            for issue in exc.issues:
                click.echo(f"  error: {issue}", err=True)
            raise click.ClickException(f"{len(exc.issues)} issue(s)") from None
    click.echo("ok")


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
def run(
    scenario_name: str | None,
    data_dir: Path | None,
    intent_dir: Path | None,
    runs_dir: Path | None,
    run_by: str | None,
    allow_dirty_data: bool,
    allow_dirty_code: bool,
) -> None:
    """Execute <scenario> against its intent file and write a new run file.

    Naming a scenario also records it as the data repo's current scenario,
    same as ``sim load <scenario>``, but only when a data repo is resolvable
    (--data-dir or $ANALYSIS_DATA_DIR); this command otherwise only needs the
    already-built intent file, not the data repo.
    """
    explicit = scenario_name is not None
    data_dir_available = data_dir is not None or bool(os.environ.get("ANALYSIS_DATA_DIR"))
    resolved_data_dir = _resolve_data_dir(data_dir) if data_dir_available else None
    if scenario_name is None:
        resolved_data_dir = resolved_data_dir or _resolve_data_dir(data_dir)
        scenario_name = _resolve_scenario(resolved_data_dir, None).name
    resolved_intent_dir = _resolve_state_dir(intent_dir, DEFAULT_INTENT_DIR, resolved_data_dir)
    resolved_runs_dir = _resolve_state_dir(runs_dir, DEFAULT_RUNS_DIR, resolved_data_dir)
    intent_path = resolved_intent_dir / f"{scenario_name}.sqlite3"
    if not intent_path.exists():
        raise click.ClickException(
            f"intent file not found: {intent_path}\n"
            f"  run: analysis sim load {scenario_name}"
        )
    if explicit and resolved_data_dir is not None:
        write_current_scenario(resolved_data_dir, scenario_name)

    resolved_by = run_by or os.environ.get("USER") or "unknown"

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
@click.option("--start", required=True, help="First month (YYYY-MM-DD).")
@click.option("--end", default=None, help="Last month (YYYY-MM-DD); overrides --months.")
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
    start = _validate_iso_date(start)
    if end is not None:
        end = _validate_iso_date(end)
        if end < start:
            raise click.ClickException("--end must be on or after --start")
    else:
        end = str((pd.Period(start, freq="M") + months - 1).end_time.date())

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
    for (date, from_name, from_subkind, from_lot, from_account_id, from_lot_id,
         to_name, to_subkind, to_lot, to_account_id, to_lot_id,
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
            date, own, direction, _fmt_cents(amount).strip(),
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


def _prompt_with_completion(text: str, choices: list[str] | None = None,
                            default: str | None = None) -> str:
    """Prompt with readline tab-completion over ``choices``."""
    try:
        import readline
    except ImportError:
        return click.prompt(text, default=default,
                            show_default=default is not None)
    completer_choices = list(choices or [])

    def completer(prefix: str, state: int) -> str | None:
        opts = [c for c in completer_choices if c.lower().startswith(prefix.lower())]
        return opts[state] if state < len(opts) else None

    old = readline.get_completer()
    readline.set_completer(completer)
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    try:
        return click.prompt(text, default=default,
                            show_default=default is not None)
    finally:
        readline.set_completer(old)


def _parse_amount_cents(s: str) -> int:
    raw = s.strip().lstrip("$").replace(",", "")
    try:
        d = Decimal(raw)
    except InvalidOperation as e:
        raise click.ClickException(f"invalid amount: {s!r}") from e
    cents = d * 100
    if cents != cents.to_integral_value():
        raise click.ClickException(f"amount has sub-cent precision: {s!r}")
    return int(cents)


def _validate_iso_date(s: str) -> str:
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError as e:
        raise click.ClickException(f"date must be YYYY-MM-DD: {s!r}") from e
    return s


def _append_csv_row(path: Path, header: tuple[str, ...],
                    row: dict[str, str]) -> None:
    """Append a row to a CSV, aligning columns by name via pandas.

    If the file exists, its header must contain every column of ``header``
    (extra optional columns are kept and left blank); this catches schema
    drift instead of silently writing misaligned columns.
    """
    cols = list(header)
    if path.exists() and path.stat().st_size > 0:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        existing = tuple(df.columns)
        if not set(header) <= set(existing):
            raise click.ClickException(
                f"CSV header mismatch in {path}\n"
                f"  expected: {list(header)}\n"
                f"  found:    {list(existing)}"
            )
        cols = list(existing)
    else:
        df = pd.DataFrame(columns=cols)
    new_row = pd.DataFrame([{c: row.get(c, "") for c in cols}], columns=cols)
    df = pd.concat([df, new_row], ignore_index=True)
    df.to_csv(path, index=False)


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


def _pick_lot(
    info: dict[str, tuple[bool, list[str]]], account: str, lot: str | None, label: str,
) -> str:
    """The lot column value for ``account``: an explicit ``lot`` is checked, and
    investment accounts prompt for one; other accounts leave it blank."""
    investment, lots = info[account]
    if lot:
        if lot not in lots:
            raise click.ClickException(
                f"{account!r} has no lot {lot!r}; its lots: {', '.join(lots)}"
            )
        return lot
    if investment:
        return _prompt_with_completion(f"{label} lot", choices=lots)
    return ""


def _prompt_account(label: str, accounts: list[str]) -> str:
    return _prompt_with_completion(f"{label} account", choices=accounts)


@sim_group_command.group(name="transfer")
def transfer_group_command() -> None:
    """Inspect or modify one-time calendar transfers in a scenario."""


@transfer_group_command.command("add")
@click.argument("date", required=False)
@click.argument("from_account", required=False, shell_complete=_complete_account)
@click.argument("to_account", required=False, shell_complete=_complete_account)
@click.argument("amount", required=False)
@click.option("--from-lot", default=None, help="Lot of the from account (required for investment accounts).")
@click.option("--to-lot", default=None, help="Lot of the to account (required for investment accounts).")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--tax-character", default="none", show_default=True,
              help="ADR-003 tax_character (e.g. ordinary, ltcg, none).")
@click.option("--description", default="", help="Free-text description.")
@click.option("--series", default="", help="Optional series tag.")
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
) -> None:
    """Append a one-time transfer to <scenario>/calendar.csv."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, _ = _resolve_scenario_dir(data_dir_p, scenario)
    with open_intent_db(data_dir_p, scenario) as (con, _name):
        accounts = [r[0] for r in con.execute("SELECT name FROM account ORDER BY name")]
        lot_info = _lot_info(con)

    if not date:
        date = _prompt_with_completion("date (YYYY-MM-DD)")
    date = _validate_iso_date(date)

    if not from_account:
        click.echo(f"accounts: {', '.join(accounts)}")
        from_account = _prompt_account("from", accounts)
    if from_account not in accounts:
        raise click.ClickException(f"unknown from account: {from_account!r}")

    if not to_account:
        to_account = _prompt_account("to", accounts)
    if to_account not in accounts:
        raise click.ClickException(f"unknown to account: {to_account!r}")
    from_lot = _pick_lot(lot_info, from_account, from_lot, "from")
    to_lot = _pick_lot(lot_info, to_account, to_lot, "to")

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

    payload = {
        "account_1": from_account,
        "account_2": to_account,
        "amount": cents,
        "tax_character": tax_character,
    }
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
        "amount": str(cents),
        "tax_character": tax_character,
        "params_json": json.dumps(payload, separators=(",", ":")),
        "description": description,
        "tags": "",
    }
    path = scenario_dir / "calendar.csv"
    _append_csv_row(path, registries.CALENDAR_HEADER, row)
    click.echo(
        f"appended one_off_transfer {from_account} -> {to_account} "
        f"{_fmt_cents(cents).strip()} on {date} to {path}"
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
@click.option("--from-lot", default=None, help="Lot of the from account (required for investment accounts).")
@click.option("--to-lot", default=None, help="Lot of the to account (required for investment accounts).")
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
              help="Optional end date (YYYY-MM-DD).")
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
    scenario_dir, _ = _resolve_scenario_dir(data_dir_p, scenario)
    with open_intent_db(data_dir_p, scenario) as (con, _name):
        accounts = [r[0] for r in con.execute("SELECT name FROM account ORDER BY name")]
        lot_info = _lot_info(con)
        existing_names = {
            r[0] for r in con.execute("SELECT name FROM scheduled_transaction")
        }

    if not date:
        date = _prompt_with_completion("start date (YYYY-MM-DD)")
    date = _validate_iso_date(date)
    if end_date:
        end_date = _validate_iso_date(end_date)

    if not from_account:
        click.echo(f"accounts: {', '.join(accounts)}")
        from_account = _prompt_account("from", accounts)
    if from_account not in accounts:
        raise click.ClickException(f"unknown from account: {from_account!r}")

    if not to_account:
        to_account = _prompt_account("to", accounts)
    if to_account not in accounts:
        raise click.ClickException(f"unknown to account: {to_account!r}")
    from_lot = _pick_lot(lot_info, from_account, from_lot, "from")
    to_lot = _pick_lot(lot_info, to_account, to_lot, "to")

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
    _append_csv_row(path, registries.SCHEDULED_CSV_HEADER, row)
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
@click.option("--food-owner-account", default="Hiromi FECU Checking", show_default=True,
              help="Quicken account whose Food expenses are assigned to Hiromi Food.")
@click.option("--account-map", type=click.Path(dir_okay=False, path_type=Path),
              default=None,
              help="Optional category account map (default: sibling data-repo/quicken/expense_account_map.csv).")
@click.option("--from-account", required=True,
              help="Scenario account that funds all generated expense rows.")
@click.option("--from-lot", default="",
              help="Lot of --from-account (required for investment accounts).")
@click.option("--start", default=None,
              help="First scheduled month YYYY-MM-DD (default: scenario start).")
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
    food_owner_account: str,
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
    start = _validate_iso_date(start)

    merges, exclusions = load_category_map(category_map_path)
    baseline = build_expense_baseline(
        quicken_path,
        load_monthly_summaries(amazon_state_path),
        category_merges=merges,
        excluded_categories=exclusions,
        default_category="Other",
        food_owner_account=food_owner_account,
    )
    mappings = (
        load_expense_account_map(account_map_path)
        if account_map_path.exists()
        else {}
    )
    with open_intent_db(data_dir_p, scenario_name) as (con, _):
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
    replace_generated_scheduled_rows(scheduled_path, rows)
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

    data_dir_p = _resolve_data_dir(data_dir)
    with open_intent_db(data_dir_p, scenario) as (con, scen_name):
        table = Table(title=f"scheduled ({data_dir_p.name} / {scen_name})")
        table.add_column("Name")
        table.add_column("Cadence")
        table.add_column("From")
        table.add_column("To")
        table.add_column("Amount", justify="right")
        table.add_column("Start")
        table.add_column("End")
        for row in con.execute(
            "SELECT s.name, s.cadence, s.start, s.\"end\" AS end_date, "
            "       s.amount_rule_kind, s.amount_cents, "
            "       fa.name AS from_name, ta.name AS to_name "
            "FROM scheduled_transaction s "
            "JOIN chain fa ON fa.id = s.from_account "
            "JOIN chain ta ON ta.id = s.to_account "
            "ORDER BY s.priority, s.name"
        ):
            rule = row["amount_rule_kind"]
            if rule == "fixed_amount" and row["amount_cents"] is not None:
                amt = _fmt_cents(row["amount_cents"]).strip()
            elif rule == "percent_of_balance":
                amt = "% of bal"
            else:
                amt = rule
            table.add_row(
                row["name"], row["cadence"],
                row["from_name"], row["to_name"],
                amt, row["start"], row["end_date"] or "",
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
              help="Open date (YYYY-MM-DD).")
@click.option("--closed", "closed", default="",
              help="Close date (YYYY-MM-DD), if any.")
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
    with open_intent_db(data_dir_p, scenario) as (con, scen_name):
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
        opened = _validate_iso_date(opened)
    if closed:
        closed = _validate_iso_date(closed)

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
    accounts_path = data_dir_p / "scenarios" / scen_name / "accounts.csv"
    _append_csv_row(accounts_path, registries.ACCOUNTS_CSV_HEADER, row)
    click.echo(f"appended account '{name}' to {accounts_path}")

    if opening_balance is None and kind in {"asset", "liability"}:
        if click.confirm("set opening balance now?", default=True):
            opening_balance = _prompt_with_completion(
                "opening balance (dollars, e.g. 1234.56)"
            )

    if opening_balance:
        cents = _parse_amount_cents(opening_balance)
        values_path = data_dir_p / "scenarios" / scen_name / "account_values.csv"
        _append_csv_row(
            values_path,
            registries.ACCOUNT_VALUES_CSV_HEADER,
            {
                "account_name": name,
                "lot": registries.DEFAULT_LOT if subkind in INVESTMENT_ASSET_SUBKINDS else "",
                "opening_balance": f"{cents // 100}.{cents % 100:02d}",
                "opening_cost_basis": "",
                "prior_year_end_balance": "",
            },
        )
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
    scenario_dir, _ = _resolve_scenario_dir(data_dir, scenario)
    with open_intent_db(data_dir, scenario) as (con, _name):
        existing = {r[0] for r in con.execute("SELECT name FROM account")}

    if old_name not in existing:
        raise click.ClickException(f"unknown account: {old_name!r}")
    if new_name in existing and old_name != new_name:
        raise click.ClickException(f"account already exists: {new_name!r}")

    changed = False
    accounts_path = scenario_dir / "accounts.csv"
    changed |= _rename_account_row_values(
        accounts_path, ("name", "refill_chain", "overflow_chain"), old_name, new_name,
    )
    changed |= _rename_account_row_values(scenario_dir / "account_values.csv", ("account_name",), old_name, new_name)
    changed |= _rename_account_row_values(scenario_dir / "scheduled.csv",
                                          ("from_account", "to_account", "interest_account"),
                                          old_name, new_name)
    changed |= _rename_account_row_values(scenario_dir / "calendar.csv", ("account_1", "account_2"), old_name, new_name)
    for chain_path in sorted((scenario_dir / "chains").glob("*.csv")) if (scenario_dir / "chains").is_dir() else []:
        changed |= _rename_account_row_values(chain_path, ("account_name",), old_name, new_name)
    changed |= _rename_account_row_values(scenario_dir / "quicken_account_map.csv",
                                          ("account_name",),
                                          old_name, new_name)
    changed |= _rename_account_row_values(scenario_dir / "year_to_date.csv",
                                          ("from_account", "to_account"),
                                          old_name, new_name)

    if not changed:
        raise click.ClickException(f"account not found in scenario files: {old_name!r}")

    click.echo(f"renamed account '{old_name}' to '{new_name}' in {scenario_dir}")


_ACCOUNT_REFERENCE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("account_values.csv", ("account_name",)),
    ("scheduled.csv", ("from_account", "to_account", "interest_account")),
    ("calendar.csv", ("account_1", "account_2")),
    ("quicken_account_map.csv", ("account_name",)),
    ("year_to_date.csv", ("from_account", "to_account")),
)


def _account_reference_rows(scenario_dir: Path, name: str) -> list[str]:
    """Return direct CSV references to an account in deterministic display order."""
    references: list[str] = []
    accounts_path = scenario_dir / "accounts.csv"
    if accounts_path.exists():
        accounts = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
        chain_fields = [
            field for field in ("refill_chain", "overflow_chain") if field in accounts.columns
        ]
        for idx, row in accounts.iterrows():
            if row.get("name") == name:
                continue
            matched_fields = [field for field in chain_fields if row[field] == name]
            if matched_fields:
                references.append(
                    f"  accounts.csv row {idx + 2}: {', '.join(matched_fields)}"
                )

    paths = [
        (scenario_dir / filename, fields)
        for filename, fields in _ACCOUNT_REFERENCE_FIELDS
    ]
    chains_dir = scenario_dir / "chains"
    if chains_dir.is_dir():
        paths.extend((path, ("account_name",)) for path in sorted(chains_dir.glob("*.csv")))

    for path, fields in paths:
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        available_fields = tuple(field for field in fields if field in df.columns)
        for idx, row in df.iterrows():
            matched_fields = [field for field in available_fields if row[field] == name]
            if matched_fields:
                references.append(
                    f"  {path.relative_to(scenario_dir)} row {idx + 2}: "
                    f"{', '.join(matched_fields)}"
                )
    return references


def _delete_account_references(scenario_dir: Path, name: str) -> None:
    """Remove rows dependent on an account, preserving the CSV headers."""
    paths = [
        (scenario_dir / filename, fields)
        for filename, fields in _ACCOUNT_REFERENCE_FIELDS
    ]
    chains_dir = scenario_dir / "chains"
    if chains_dir.is_dir():
        paths.extend((path, ("account_name",)) for path in sorted(chains_dir.glob("*.csv")))

    for path, fields in paths:
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        available_fields = [field for field in fields if field in df.columns]
        if not available_fields:
            continue
        keep = ~df[available_fields].eq(name).any(axis=1)
        if not keep.all():
            df.loc[keep].to_csv(path, index=False)

    accounts_path = scenario_dir / "accounts.csv"
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    for field in ("refill_chain", "overflow_chain"):
        if field in df.columns:
            df.loc[df[field] == name, field] = ""
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
    scenario_dir, _ = _resolve_scenario_dir(data_dir_p, scenario)
    accounts_path = scenario_dir / "accounts.csv"
    if not accounts_path.exists():
        raise click.ClickException(f"accounts.csv not found: {accounts_path}")

    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    if "name" not in df.columns or not (df["name"] == name).any():
        raise click.ClickException(f"unknown account: {name!r}")

    references = _account_reference_rows(scenario_dir, name)
    if references:
        click.echo(f"account '{name}' is referenced by:")
        click.echo("\n".join(references))
        if not click.confirm("delete the account and these referenced rows?", default=False):
            click.echo("aborted")
            return

    _delete_account_references(scenario_dir, name)
    click.echo(f"deleted account '{name}' from {accounts_path}")


@account_group_command.command("edit")
@click.argument("name")
@click.option("--new-name", default=None, help="Update the account name.")
@click.option("--kind", default=None, help="New account kind.")
@click.option("--subkind", default=None, help="New account subkind.")
@click.option("--owner", default=None, help="New owner (person id or 'joint').")
@click.option("--tax-treatment", default=None, help="New tax treatment.")
@click.option("--opened", default=None, help="Open date (YYYY-MM-DD).")
@click.option("--closed", default=None, help="Close date (YYYY-MM-DD).")
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
    scenario_dir, _ = _resolve_scenario_dir(data_dir_p, scenario)
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

    with open_intent_db(data_dir_p, scenario) as (con, _name):
        persons = [r[0] for r in con.execute("SELECT id FROM person ORDER BY id")]

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
            opened = _validate_iso_date(opened)
        closed = click.prompt("closed", default=current.get("closed", ""), show_default=True)
        if closed:
            closed = _validate_iso_date(closed)
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
        opened = _validate_iso_date(opened)
    if closed:
        closed = _validate_iso_date(closed)
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
    df.to_csv(accounts_path, index=False)

    if new_name != name:
        _rename_account_in_scenario(data_dir_p, scenario, name, new_name)

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
              help="Resolve rates effective on or before this date (YYYY-MM-DD).")
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
        _validate_iso_date(as_of)
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
    with values_path.open("w", newline="", encoding="utf-8") as f:
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

    df.to_csv(values_path, index=False)
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
    """List one-time transfers: calendar one_off_transfer events and scheduled
    rows with cadence one_off."""
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
            "SELECT ce.date, ce.amount, "
            "       COALESCE(ce.tax_character, '') AS tax_character, "
            "       COALESCE(ce.description, '') AS description, "
            "       a1.name AS name_1, a1.subkind AS subkind_1, m1.lot AS lot_1, "
            "       a2.name AS name_2, a2.subkind AS subkind_2, m2.lot AS lot_2 "
            "FROM calendar_event ce "
            "LEFT JOIN chain c1 ON c1.name = ce.account_1 "
            "LEFT JOIN account_model_member m1 ON m1.id = c1.lot_id "
            "LEFT JOIN account a1 ON a1.id = c1.account_id "
            "LEFT JOIN chain c2 ON c2.name = ce.account_2 "
            "LEFT JOIN account_model_member m2 ON m2.id = c2.lot_id "
            "LEFT JOIN account a2 ON a2.id = c2.account_id "
            "WHERE ce.kind = 'one_off_transfer' "
        ):
            rows.append((
                row["date"], "calendar",
                _account_with_lot(row["name_1"], row["subkind_1"], row["lot_1"]),
                _account_with_lot(row["name_2"], row["subkind_2"], row["lot_2"]),
                _fmt_cents(row["amount"]).strip() if row["amount"] is not None else "",
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
    item_type: str,
    item_name: str,
    tag_tokens: tuple[str, ...],
    op: str,  # 'add' | 'rm'
) -> None:
    try:
        registries.parse_tags(" ".join(tag_tokens))
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    data_dir_p = _resolve_data_dir(data_dir)
    try:
        with open_scenario(data_dir_p, scenario) as sc:
            method = sc.add_tags if op == "add" else sc.remove_tags
            new_val = method(item_type, item_name, tag_tokens)  # type: ignore[arg-type]
    except (FileNotFoundError, KeyError, ValueError) as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"{item_type} '{item_name}' tags: {new_val or '(none)'}")


# ---------- scenario: create derived scenarios ----------


@sim_group_command.group(name="scenario")
def scenario_group_command() -> None:
    """Create scenarios."""


@scenario_group_command.command("create")
@click.argument("name")
@click.argument("parent")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--description", default=None,
              help="Default: 'Derived from <parent>'.")
def scenario_create(
    name: str,
    parent: str,
    data_dir: Path | None,
    description: str | None,
) -> None:
    """Create scenario NAME deriving from PARENT, and make it current.

    The new scenario inherits everything from PARENT; only the rows you add to
    its (empty) overlay CSVs differ. See ADR-003 §4.5 for merge semantics.
    """

    skeletons: dict[str, tuple[str, ...]] = {
        "accounts.csv": registries.ACCOUNTS_CSV_HEADER,
        "account_values.csv": registries.ACCOUNT_VALUES_CSV_HEADER,
        "scheduled.csv": registries.SCHEDULED_CSV_HEADER,
        "calendar.csv": registries.CALENDAR_HEADER,
    }
    try:
        data_dir_p = _resolve_data_dir(data_dir)
        target = sc.create_scenario(data_dir_p, name, parent,
                                        description=description)
        with tempfile.TemporaryDirectory() as td:
            try:
                load_scenario(
                    data_dir_p, name, intent_dir=Path(td), compute_stamps=False,
                    allow_dirty_data=True,
                )
            except LoadError as exc:
                for issue in exc.issues:
                    click.echo(f"  error: {issue}", err=True)
                raise click.ClickException(
                    f"created {target}, but it does not load; leaving it in place "
                    "for inspection"
                ) from None

        parent_ref = _resolve_scenario(data_dir_p, parent)
        write_current_scenario(data_dir_p, name)
        click.echo(f"created {target}")
        click.echo(f"  inherits: {parent_ref.name}")
        for filename in ("scenario.csv", *skeletons):
            click.echo(f"  {filename}")
        click.echo("  chains/")
        click.echo("  chains/.gitignore")
        click.echo("  chains/_asset__example.csv")
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
    for r in df.itertuples(index=False):
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
            for row in df.itertuples(index=False):
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
