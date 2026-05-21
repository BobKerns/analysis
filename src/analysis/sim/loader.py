"""CSV -> intent .sqlite3 loader.

Reads a data repository per ADR-003 layout and writes an intent file per ADR-002.
Git-based provenance stamps (ADR-004) are computed via ``analysis.sim.provenance``.
"""

from __future__ import annotations

import csv
import json
import os
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from analysis.sim import registries
from analysis.sim.provenance import (
    EMPTY_CODE_STAMPS,
    EMPTY_DATA_STAMPS,
    CodeStamps,
    DataDirtyRefused,
    DataStamps,
    code_stamps,
    data_stamps,
)
from analysis.sim.schema import INTENT_DDL, SCHEMA_VERSION

__all__ = ["LoadError", "LoadResult", "DataDirtyRefused", "load_scenario"]


@dataclass(frozen=True)
class LoadResult:
    intent_path: Path
    scenario_name: str
    counts: dict[str, int] = field(default_factory=dict)


class LoadError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        super().__init__(
            f"{len(issues)} issue(s) loading intent:\n  " + "\n  ".join(issues)
        )
        self.issues = issues


def load_scenario(
    data_dir: Path,
    scenario_name: str,
    *,
    intent_dir: Path,
    data_repo_name: str | None = None,
    loaded_by: str | None = None,
    allow_dirty_data: bool = False,
    compute_stamps: bool = True,
) -> LoadResult:
    """Build ``<intent_dir>/<scenario_name>.sqlite3`` from the data repo.

    When ``compute_stamps`` is true (default), git stamps per ADR-004 are
    computed and written into ``intent_manifest``. If the data repo is dirty
    within the scenario's scoped paths and ``allow_dirty_data`` is false,
    raises ``DataDirtyRefused``.
    """
    data_dir = Path(data_dir).resolve()
    scenario_dir = data_dir / "scenarios" / scenario_name
    if not scenario_dir.is_dir():
        raise LoadError([f"scenario folder not found: {scenario_dir}"])

    issues: list[str] = []
    persons = _read_csv(data_dir / "persons.csv", issues)
    accounts = _read_csv(data_dir / "accounts.csv", issues)
    scenario_rows = _read_csv(scenario_dir / "scenario.csv", issues)
    scheduled_rows = _read_csv(scenario_dir / "scheduled.csv", issues)
    waterfall_rows = _read_csv(scenario_dir / "waterfall_sources.csv", issues, optional=True)
    calendar_rows = _read_csv(scenario_dir / "calendar.csv", issues, optional=True)
    priors_rows = _read_csv(scenario_dir / "priors.csv", issues, optional=True)

    if len(scenario_rows) != 1:
        issues.append(
            f"scenario.csv must have exactly one data row (got {len(scenario_rows)})"
        )
    scenario_row = scenario_rows[0] if scenario_rows else {}
    if scenario_row.get("name") and scenario_row["name"] != scenario_name:
        issues.append(
            f"scenario.csv name='{scenario_row['name']}' != folder '{scenario_name}'"
        )

    account_model_name = scenario_row.get("account_model", "")
    model_rows: list[dict[str, str]] = []
    if account_model_name:
        model_rows = _read_csv(
            data_dir / "account_models" / f"{account_model_name}.csv", issues
        )

    aux_rows = _read_aux_dir(data_dir / "aux", issues)

    _validate_accounts(accounts, issues)
    _validate_scheduled(scheduled_rows, accounts, issues)
    _validate_calendar(calendar_rows, issues)
    _validate_waterfall(waterfall_rows, scheduled_rows, issues)
    _validate_priors(priors_rows, accounts, issues)

    if issues:
        raise LoadError(issues)

    if compute_stamps:
        ds = data_stamps(data_dir, scenario_name, account_model_name)
        if ds.dirty and not allow_dirty_data:
            raise DataDirtyRefused(list(ds.dirty_paths))
        cs = code_stamps()
    else:
        ds = EMPTY_DATA_STAMPS
        cs = EMPTY_CODE_STAMPS

    intent_dir.mkdir(parents=True, exist_ok=True)
    intent_path = intent_dir / f"{scenario_name}.sqlite3"
    if intent_path.exists():
        intent_path.unlink()

    con = sqlite3.connect(intent_path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(INTENT_DDL)
        counts = _write_intent(
            con,
            persons=persons,
            accounts=accounts,
            account_model_name=account_model_name,
            account_model_rows=model_rows,
            aux_rows=aux_rows,
            scenario_row=scenario_row,
            scheduled_rows=scheduled_rows,
            waterfall_rows=waterfall_rows,
            calendar_rows=calendar_rows,
            priors_rows=priors_rows,
        )
        _write_manifest(
            con,
            scenario_name=scenario_name,
            data_dir=data_dir,
            data_repo_name=data_repo_name or data_dir.name,
            loaded_by=loaded_by or os.environ.get("ANALYSIS_USER", "unknown"),
            code=cs,
            data=ds,
        )
        con.commit()
    finally:
        con.close()

    return LoadResult(intent_path=intent_path, scenario_name=scenario_name, counts=counts)


# ---------- CSV reading ----------

def _read_csv(
    path: Path, issues: list[str], *, optional: bool = False
) -> list[dict[str, str]]:
    if not path.exists():
        if not optional:
            issues.append(f"missing required file: {path}")
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [
            {k: (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(fh)
        ]


def _read_aux_dir(
    aux_dir: Path, issues: list[str]
) -> list[tuple[str, str | None, dict[str, Any], str]]:
    out: list[tuple[str, str | None, dict[str, Any], str]] = []
    if not aux_dir.is_dir():
        return out
    for csv_path in sorted(aux_dir.glob("*.csv")):
        stem = csv_path.stem
        if "__" in stem:
            kind, raw_series = stem.split("__", 1)
            series = raw_series.replace("_", " ")
        else:
            kind, series = stem, None
        if kind not in registries.AUX_KIND_COLUMNS:
            issues.append(f"aux/{csv_path.name}: unknown kind '{kind}'")
            continue
        cols = registries.AUX_KIND_COLUMNS[kind]
        for i, row in enumerate(_read_csv(csv_path, issues), start=2):
            missing = [c for c in cols if c not in row]
            if missing:
                issues.append(
                    f"aux/{csv_path.name} row {i}: missing columns {missing}"
                )
                continue
            payload = {c: _coerce(row[c]) for c in cols if c != "effective_date"}
            out.append((kind, series, payload, row["effective_date"]))
    return out


def _opt_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    v = raw.strip()
    return int(v) if v != "" else None


def _coerce(s: str) -> Any:
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return s


def _dollars_to_cents(s: str) -> int:
    """Parse a human-facing dollar amount (e.g. ``'1234.56'``, ``'-150'``) into
    integer cents. CSV money columns are kept in dollars-and-cents so they can
    be hand-edited; the engine continues to work in integer cents.
    """
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"invalid money value: {s!r}") from e
    cents = d * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"money value {s!r} has sub-cent precision")
    return int(cents)


# ---------- validation ----------

def _validate_accounts(rows: list[dict[str, str]], issues: list[str]) -> None:
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):
        name = row.get("name", "")
        if not name:
            issues.append(f"accounts.csv row {i}: missing name")
            continue
        if name in seen:
            issues.append(f"accounts.csv row {i}: duplicate name '{name}'")
        seen.add(name)
        kind = row.get("kind", "")
        subkind = row.get("subkind", "")
        valid = registries.ACCOUNT_SUBKINDS.get(kind)
        if valid is None:
            issues.append(f"accounts.csv row {i} ({name}): unknown kind '{kind}'")
        elif subkind not in valid:
            issues.append(
                f"accounts.csv row {i} ({name}): subkind '{subkind}' not allowed for kind '{kind}'"
            )
        tt = row.get("tax_treatment", "")
        if tt and tt not in registries.TAX_TREATMENTS:
            issues.append(f"accounts.csv row {i} ({name}): unknown tax_treatment '{tt}'")


def _validate_scheduled(
    rows: list[dict[str, str]], accounts: list[dict[str, str]], issues: list[str]
) -> None:
    account_names = {r["name"] for r in accounts if r.get("name")}
    for i, row in enumerate(rows, start=2):
        name = row.get("name", "")
        if not name:
            issues.append(f"scheduled.csv row {i}: missing name")
            continue
        kind = row.get("amount_rule_kind", "")
        if kind not in registries.AMOUNT_RULE_KINDS:
            issues.append(f"scheduled.csv row {i} ({name}): unknown amount_rule_kind '{kind}'")
        cad = row.get("cadence", "")
        if cad not in registries.CADENCES:
            issues.append(f"scheduled.csv row {i} ({name}): unknown cadence '{cad}'")
        if cad == "annual" and not row.get("month_of_year"):
            issues.append(
                f"scheduled.csv row {i} ({name}): annual cadence requires month_of_year"
            )
        tc = row.get("tax_character", "")
        if tc not in registries.TAX_CHARACTERS:
            issues.append(f"scheduled.csv row {i} ({name}): unknown tax_character '{tc}'")
        for col in ("from_account", "to_account"):
            ref = row.get(col, "")
            if not ref or ref not in account_names:
                issues.append(
                    f"scheduled.csv row {i} ({name}): {col} '{ref}' not in accounts.csv"
                )
        params_json = row.get("amount_rule_params_json") or "{}"
        try:
            json.loads(params_json)
        except json.JSONDecodeError as e:
            issues.append(
                f"scheduled.csv row {i} ({name}): amount_rule_params_json invalid: {e}"
            )


def _validate_calendar(rows: list[dict[str, str]], issues: list[str]) -> None:
    for i, row in enumerate(rows, start=2):
        kind = row.get("kind", "")
        if kind not in registries.CALENDAR_EVENT_KINDS:
            issues.append(f"calendar.csv row {i}: unknown event kind '{kind}'")
        payload_json = row.get("payload_json") or "{}"
        try:
            json.loads(payload_json)
        except json.JSONDecodeError as e:
            issues.append(f"calendar.csv row {i}: payload_json invalid: {e}")


def _validate_waterfall(
    waterfall_rows: list[dict[str, str]],
    scheduled_rows: list[dict[str, str]],
    issues: list[str],
) -> None:
    sched_by_name = {r["name"]: r for r in scheduled_rows if r.get("name")}
    by_sched: dict[str, list[dict[str, str]]] = {}
    for i, row in enumerate(waterfall_rows, start=2):
        name = row.get("scheduled_name", "")
        if not name:
            issues.append(f"waterfall_sources.csv row {i}: missing scheduled_name")
            continue
        sched = sched_by_name.get(name)
        if sched is None:
            issues.append(
                f"waterfall_sources.csv row {i}: scheduled_name '{name}' not in scheduled.csv"
            )
            continue
        if sched.get("amount_rule_kind") != "waterfall_fill":
            issues.append(
                f"waterfall_sources.csv row {i}: '{name}' is not a waterfall_fill rule"
            )
        by_sched.setdefault(name, []).append(row)
    for name, group in by_sched.items():
        positions = sorted(int(r["position"]) for r in group if r.get("position"))
        if positions != list(range(1, len(positions) + 1)):
            issues.append(
                f"waterfall_sources.csv: '{name}' positions not 1..N (got {positions})"
            )
        first = next((r for r in group if int(r.get("position") or 0) == 1), None)
        sched = sched_by_name.get(name)
        if first and sched and sched.get("from_account") != first.get("account_name"):
            issues.append(
                f"scheduled.csv '{name}': from_account='{sched.get('from_account')}' "
                f"must equal waterfall_sources position-1 '{first.get('account_name')}' "
                "(ADR-003 §2.4)"
            )


def _validate_priors(
    rows: list[dict[str, str]], _accounts: list[dict[str, str]], issues: list[str]
) -> None:
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(rows, start=2):
        year = row.get("year", "")
        bucket = row.get("bucket", "")
        amount = row.get("amount", "")
        if not year or not bucket or amount == "":
            issues.append(f"priors.csv row {i}: year, bucket, amount are required")
            continue
        try:
            int(year)
            _dollars_to_cents(amount)
        except ValueError:
            issues.append(
                f"priors.csv row {i}: year must be int, amount must be dollars"
            )
            continue
        key = (year, bucket)
        if key in seen:
            issues.append(
                f"priors.csv row {i}: duplicate (year={year}, bucket={bucket})"
            )
        seen.add(key)


# ---------- writers ----------

def _write_intent(
    con: sqlite3.Connection,
    *,
    persons: list[dict[str, str]],
    accounts: list[dict[str, str]],
    account_model_name: str,
    account_model_rows: list[dict[str, str]],
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]],
    scenario_row: dict[str, str],
    scheduled_rows: list[dict[str, str]],
    waterfall_rows: list[dict[str, str]],
    calendar_rows: list[dict[str, str]],
    priors_rows: list[dict[str, str]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    now = _now_iso()

    for p in persons:
        con.execute(
            "INSERT INTO person(id, display, birthdate, role, meta_json) "
            "VALUES (?,?,?,?,?)",
            (
                p["id"], p["display"], p["birthdate"],
                p.get("role") or None,
                p.get("meta_json") or "{}",
            ),
        )
    counts["person"] = len(persons)

    account_ids: dict[str, int] = {}
    for a in accounts:
        cur = con.execute(
            "INSERT INTO account(name, kind, subkind, owner, tax_treatment, "
            "opened, closed, meta_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                a["name"], a["kind"], a["subkind"],
                a.get("owner") or None, a.get("tax_treatment") or None,
                a.get("opened") or None, a.get("closed") or None,
                a.get("meta_json") or "{}",
            ),
        )
        account_ids[a["name"]] = cur.lastrowid
    counts["account"] = len(accounts)

    cur = con.execute(
        "INSERT INTO account_model(name, description, created_at) VALUES (?,?,?)",
        (account_model_name, None, now),
    )
    model_id = cur.lastrowid
    for m in account_model_rows:
        con.execute(
            "INSERT INTO account_model_member("
            "model_id, account_id, opening_balance, opening_cost_basis, "
            "prior_year_end_balance) VALUES (?,?,?,?,?)",
            (
                model_id,
                account_ids[m["account_name"]],
                _dollars_to_cents(m["opening_balance"]),
                _dollars_to_cents(m["opening_cost_basis"]) if m.get("opening_cost_basis") else None,
                _dollars_to_cents(m["prior_year_end_balance"]) if m.get("prior_year_end_balance") else None,
            ),
        )
    counts["account_model_member"] = len(account_model_rows)

    cur = con.execute(
        "INSERT INTO calendar(name, description, created_at) VALUES (?,?,?)",
        (scenario_row.get("name", ""), None, now),
    )
    calendar_id = cur.lastrowid
    for ce in calendar_rows:
        con.execute(
            "INSERT INTO calendar_event("
            "calendar_id, date, kind, "
            "scheduled_name, account_1, account_2, series, amount, "
            "param_date, tax_character, payload_json, description) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                calendar_id, ce["date"], ce["kind"],
                ce.get("scheduled_name") or None,
                ce.get("account_1") or None,
                ce.get("account_2") or None,
                ce.get("series") or None,
                _opt_int(ce.get("amount")),
                ce.get("param_date") or None,
                ce.get("tax_character") or None,
                ce.get("params_json") or None,
                ce.get("description") or None,
            ),
        )
    counts["calendar_event"] = len(calendar_rows)

    sched_ids: dict[str, int] = {}
    for s in scheduled_rows:
        prio = (
            int(s["priority"]) if s.get("priority")
            else registries.DEFAULT_PRIORITIES.get(s["amount_rule_kind"], 500)
        )
        cur = con.execute(
            "INSERT INTO scheduled_transaction("
            "model_id, name, from_account, to_account, cadence, month_of_year, "
            "amount_rule_kind, "
            "account_1, account_2, person, series, rate_micros, claim_age, amount_cents, "
            "params_json, base_amount, base_date, "
            "inflation_series, tax_character, start, \"end\", priority, "
            "description, meta_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                model_id, s["name"],
                account_ids[s["from_account"]], account_ids[s["to_account"]],
                s["cadence"],
                int(s["month_of_year"]) if s.get("month_of_year") else None,
                s["amount_rule_kind"],
                s.get("account_1") or None,
                s.get("account_2") or None,
                s.get("person") or None,
                s.get("series") or None,
                _opt_int(s.get("rate_micros")),
                _opt_int(s.get("claim_age")),
                _opt_int(s.get("amount_cents")),
                s.get("params_json") or None,
                _dollars_to_cents(s["base_amount"]) if s.get("base_amount") else None,
                s.get("base_date") or None, s.get("inflation_series") or None,
                s["tax_character"], s["start"], s.get("end") or None, prio,
                s.get("description") or None, s.get("meta_json") or "{}",
            ),
        )
        sched_ids[s["name"]] = cur.lastrowid
    counts["scheduled_transaction"] = len(scheduled_rows)

    for w in waterfall_rows:
        con.execute(
            "INSERT INTO scheduled_transaction_source("
            "scheduled_transaction_id, position, account_id) VALUES (?,?,?)",
            (
                sched_ids[w["scheduled_name"]],
                int(w["position"]),
                account_ids[w["account_name"]],
            ),
        )
    counts["scheduled_transaction_source"] = len(waterfall_rows)

    for kind, series, payload, eff in aux_rows:
        con.execute(
            "INSERT INTO aux_table("
            "kind, series, effective_date, payload_json, description) "
            "VALUES (?,?,?,?,?)",
            (kind, series, eff, json.dumps(payload, separators=(",", ":")), None),
        )
    counts["aux_table"] = len(aux_rows)

    cur = con.execute(
        "INSERT INTO scenario("
        "name, description, account_model_id, calendar_id, start, \"end\", "
        "seed, parent_id, created_at, created_by, meta_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            scenario_row["name"], scenario_row.get("description") or None,
            model_id, calendar_id,
            scenario_row["start"], scenario_row["end"],
            int(scenario_row["seed"]) if scenario_row.get("seed") else None, None,
            now, scenario_row.get("created_by") or "unknown",
            scenario_row.get("meta_json") or "{}",
        ),
    )
    scenario_id = cur.lastrowid
    counts["scenario"] = 1

    for r in priors_rows:
        con.execute(
            "INSERT INTO prior_tax_activity("
            "scenario_id, year, bucket, amount, source_note) "
            "VALUES (?,?,?,?,?)",
            (
                scenario_id, int(r["year"]), r["bucket"], _dollars_to_cents(r["amount"]),
                r.get("source_note") or None,
            ),
        )
    counts["prior_tax_activity"] = len(priors_rows)

    return counts


def _write_manifest(
    con: sqlite3.Connection,
    *,
    scenario_name: str,
    data_dir: Path,
    data_repo_name: str,
    loaded_by: str,
    code: CodeStamps,
    data: DataStamps,
) -> None:
    con.execute(
        "INSERT INTO intent_manifest("
        "id, scenario_name, schema_version, code_version, data_version, "
        "data_dirty, scenario_tree_id, data_repo_path, data_repo_name, "
        "loaded_at, loaded_by, host) VALUES (1,?,?,?,?,?,?,?,?,?,?,?)",
        (
            scenario_name, SCHEMA_VERSION,
            code.version, data.version, int(data.dirty), data.tree_id,
            str(data_dir), data_repo_name, _now_iso(), loaded_by, socket.gethostname(),
        ),
    )


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
