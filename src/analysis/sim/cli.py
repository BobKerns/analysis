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

from analysis.sim import registries
from analysis.sim.accounts import load_accounts_view, render_accounts_table
from analysis.sim.engine import run_scenario
from analysis.sim.loader import (
    DataDirtyRefused,
    LoadError,
    load_scenario,
    open_intent_db,
)

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


# ---------- add: append new rows to a scenario ----------

_SCHEDULED_HEADER: tuple[str, ...] = (
    "name", "from_account", "to_account", "cadence", "month_of_year",
    "amount_rule_kind", "interest_account", "person", "series",
    "rate_micros", "claim_age", "amount", "base_amount", "base_date",
    "inflation_series", "tax_character", "start", "end", "priority",
    "description", "meta_json", "params_json",
)

_CALENDAR_HEADER: tuple[str, ...] = (
    "date", "kind", "scheduled_name", "account_1", "account_2", "series",
    "amount", "param_date", "tax_character", "params_json", "description",
)


def _resolve_scenario_dir(data_dir: Path, scenario: str | None) -> tuple[Path, str]:
    scenarios = data_dir / "scenarios"
    if not scenarios.is_dir():
        raise click.ClickException(f"no scenarios/ in {data_dir}")
    if scenario:
        d = scenarios / scenario
        if not d.is_dir():
            raise click.ClickException(f"scenario not found: {d}")
        return d, scenario
    subs = sorted(p for p in scenarios.iterdir() if p.is_dir())
    if len(subs) == 1:
        return subs[0], subs[0].name
    names = ", ".join(p.name for p in subs) or "(none)"
    raise click.ClickException(f"multiple scenarios; pass --scenario [{names}]")


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
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=list(header))
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in header})


def _prompt_account(label: str, accounts: list[str]) -> str:
    return _prompt_with_completion(f"{label} account", choices=accounts)


@sim_group_command.group(name="transfer")
def transfer_group_command() -> None:
    """Inspect or modify transfers in a scenario."""


@transfer_group_command.group(name="add")
def transfer_add_group_command() -> None:
    """Append a transfer to a scenario (one-time or recurring)."""


@transfer_add_group_command.command("fixed")
@click.argument("date", required=False)
@click.argument("from_account", required=False, shell_complete=_complete_account)
@click.argument("to_account", required=False, shell_complete=_complete_account)
@click.argument("amount", required=False)
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
@click.option("--tax-character", default="none", show_default=True,
              help="ADR-003 tax_character (e.g. ordinary, ltcg, none).")
@click.option("--description", default="", help="Free-text description.")
@click.option("--series", default="", help="Optional series tag.")
def add_fixed(
    date: str | None,
    from_account: str | None,
    to_account: str | None,
    amount: str | None,
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
        "account_2": to_account,
        "series": series,
        "amount": str(cents),
        "tax_character": tax_character,
        "params_json": json.dumps(payload, separators=(",", ":")),
        "description": description,
    }
    path = scenario_dir / "calendar.csv"
    _append_csv_row(path, _CALENDAR_HEADER, row)
    click.echo(
        f"appended one_off_transfer {from_account} -> {to_account} "
        f"{_fmt_cents(cents).strip()} on {date} to {path}"
    )


@transfer_add_group_command.command("scheduled")
@click.argument("date", required=False)
@click.argument("from_account", required=False, shell_complete=_complete_account)
@click.argument("to_account", required=False, shell_complete=_complete_account)
@click.argument("amount", required=False)
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
@click.option("--inflation-series", default="fixed", show_default=True,
              help="'fixed' for no inflation, else an inflation series (e.g. cpi).")
@click.option("--priority", type=int, default=200, show_default=True)
@click.option("--description", default="")
@click.option("--series", default="")
def add_scheduled(
    date: str | None,
    from_account: str | None,
    to_account: str | None,
    amount: str | None,
    scenario: str | None,
    data_dir: Path | None,
    row_name: str | None,
    cadence: str,
    month_of_year: int | None,
    end_date: str,
    tax_character: str,
    inflation_series: str,
    priority: int,
    description: str,
    series: str,
) -> None:
    """Append a recurring fixed-amount transfer to <scenario>/scheduled.csv."""
    data_dir_p = _resolve_data_dir(data_dir)
    scenario_dir, _ = _resolve_scenario_dir(data_dir_p, scenario)
    with open_intent_db(data_dir_p, scenario) as (con, _name):
        accounts = [r[0] for r in con.execute("SELECT name FROM account ORDER BY name")]
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

    if amount is None:
        amount = _prompt_with_completion("amount (dollars, e.g. 1234.56)")
    cents = _parse_amount_cents(amount)
    if cents <= 0:
        raise click.ClickException("amount must be positive")
    base_amount = f"{cents // 100}.{cents % 100:02d}"

    if cadence == "annual" and month_of_year is None:
        month_of_year = int(date[5:7])

    if tax_character not in registries.TAX_CHARACTERS:
        raise click.ClickException(
            f"unknown tax_character: {tax_character!r}; "
            f"allowed: {sorted(registries.TAX_CHARACTERS)}"
        )

    if not row_name:
        slug_from = from_account.lower().replace(" ", "_")
        slug_to = to_account.lower().replace(" ", "_")
        row_name = f"xfer_{slug_from}_to_{slug_to}_{date}"
    if row_name in existing_names:
        raise click.ClickException(
            f"scheduled transaction already exists: {row_name!r}"
        )

    row = {
        "name": row_name,
        "from_account": from_account,
        "to_account": to_account,
        "cadence": cadence,
        "month_of_year": str(month_of_year) if month_of_year else "",
        "amount_rule_kind": "fixed_amount",
        "series": series,
        "base_amount": base_amount,
        "base_date": date,
        "inflation_series": inflation_series,
        "tax_character": tax_character,
        "start": date,
        "end": end_date,
        "priority": str(priority),
        "description": description,
        "meta_json": "{}",
        "params_json": "{}",
    }
    path = scenario_dir / "scheduled.csv"
    _append_csv_row(path, _SCHEDULED_HEADER, row)
    click.echo(
        f"appended scheduled row '{row_name}' ({cadence}) "
        f"{from_account} -> {to_account} {_fmt_cents(cents).strip()} to {path}"
    )


_ACCOUNTS_HEADER: tuple[str, ...] = (
    "name", "kind", "subkind", "owner", "tax_treatment",
    "opened", "closed", "meta_json",
)

_ACCOUNT_MODEL_HEADER: tuple[str, ...] = (
    "account_name", "opening_balance", "opening_cost_basis", "prior_year_end_balance",
)


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
def add_account(
    name: str | None,
    kind: str | None,
    subkind: str | None,
    owner: str | None,
    tax_treatment: str | None,
    opened: str,
    closed: str,
    opening_balance: str | None,
    scenario: str | None,
    data_dir: Path | None,
) -> None:
    """Append an account to accounts.csv (and opening balance to the model)."""
    data_dir_p = _resolve_data_dir(data_dir)
    with open_intent_db(data_dir_p, scenario) as (con, scen_name):
        existing = {r[0] for r in con.execute("SELECT name FROM account")}
        persons = [r[0] for r in con.execute("SELECT id FROM person ORDER BY id")]
        model_row = con.execute(
            "SELECT m.name FROM scenario s "
            "JOIN account_model m ON m.id = s.account_model_id"
        ).fetchone()
    model_name = model_row[0] if model_row else ""

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

    row = {
        "name": name, "kind": kind, "subkind": subkind,
        "owner": owner, "tax_treatment": tax_treatment,
        "opened": opened, "closed": closed, "meta_json": "{}",
    }
    accounts_path = data_dir_p / "accounts.csv"
    _append_csv_row(accounts_path, _ACCOUNTS_HEADER, row)
    click.echo(f"appended account '{name}' to {accounts_path}")

    if opening_balance is None and kind in {"asset", "liability"}:
        if click.confirm("set opening balance now?", default=True):
            opening_balance = _prompt_with_completion(
                "opening balance (dollars, e.g. 1234.56)"
            )

    if opening_balance:
        cents = _parse_amount_cents(opening_balance)
        if not model_name:
            raise click.ClickException(
                f"scenario {scen_name} has no account_model; "
                "cannot record opening balance"
            )
        model_path = data_dir_p / "account_models" / f"{model_name}.csv"
        _append_csv_row(
            model_path,
            _ACCOUNT_MODEL_HEADER,
            {
                "account_name": name,
                "opening_balance": f"{cents // 100}.{cents % 100:02d}",
                "opening_cost_basis": "",
                "prior_year_end_balance": "",
            },
        )
        click.echo(
            f"appended opening balance {_fmt_cents(cents).strip()} to {model_path}"
        )


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
def list_accounts(
    scenario: str | None,
    data_dir: Path | None,
    as_of: str | None,
) -> None:
    """List accounts with opening balance and applicable rate (rich table)."""
    from rich.console import Console

    data_dir_p = _resolve_data_dir(data_dir)
    if as_of:
        _validate_iso_date(as_of)
    rows = load_accounts_view(data_dir_p, scenario, as_of=as_of)
    title = f"accounts ({data_dir_p.name}"
    if scenario:
        title += f" / {scenario}"
    title += ")"
    Console().print(render_accounts_table(rows, title=title))


@transfer_group_command.command("list")
@click.option("--scenario", default=None,
              help="Scenario folder name (default: the only scenario in data-dir).")
@click.option("--data-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def list_transfers(scenario: str | None, data_dir: Path | None) -> None:
    """List one-off (calendar) and recurring (scheduled) transfers."""
    from rich.console import Console
    from rich.table import Table

    data_dir_p = _resolve_data_dir(data_dir)
    with open_intent_db(data_dir_p, scenario) as (con, scen_name):
        table = Table(title=f"transfers ({data_dir_p.name} / {scen_name})")
        table.add_column("When")
        table.add_column("Source")
        table.add_column("From")
        table.add_column("To")
        table.add_column("Amount", justify="right")
        table.add_column("Tax")
        table.add_column("Description")

        for row in con.execute(
            "SELECT date, account_1, account_2, amount, "
            "       COALESCE(tax_character, '') AS tax_character, "
            "       COALESCE(description, '') AS description "
            "FROM calendar_event WHERE kind = 'one_off_transfer' "
            "ORDER BY date"
        ):
            amt = _fmt_cents(row["amount"]).strip() if row["amount"] is not None else ""
            table.add_row(
                row["date"], "one-off",
                row["account_1"] or "", row["account_2"] or "",
                amt, row["tax_character"], row["description"],
            )

        for row in con.execute(
            "SELECT s.name, s.cadence, s.start, s.\"end\" AS end_date, "
            "       s.amount_rule_kind, s.base_amount, "
            "       COALESCE(s.tax_character, '') AS tax_character, "
            "       COALESCE(s.description, '') AS description, "
            "       fa.name AS from_name, ta.name AS to_name "
            "FROM scheduled_transaction s "
            "JOIN account fa ON fa.id = s.from_account "
            "JOIN account ta ON ta.id = s.to_account "
            "ORDER BY s.priority, s.name"
        ):
            when = row["cadence"]
            if row["end_date"]:
                when = f"{when} until {row['end_date']}"
            when = f"{when} from {row['start']}"
            rule = row["amount_rule_kind"]
            if rule == "fixed_amount" and row["base_amount"] is not None:
                amt = _fmt_cents(row["base_amount"]).strip()
            elif rule == "percent_of_balance":
                amt = "% of bal"
            else:
                amt = rule
            table.add_row(
                when, row["name"], row["from_name"], row["to_name"],
                amt, row["tax_character"], row["description"],
            )

    Console().print(table)


