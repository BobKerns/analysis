"""CSV -> intent .sqlite3 loader.

Reads a data repository per ADR-003 layout and writes an intent file per ADR-002.
Git-based provenance stamps (ADR-004) are computed via ``analysis.sim.provenance``.
"""

from __future__ import annotations

import csv
import json
import os
import re
import socket
import sqlite3
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

from analysis.sim import registries
from analysis.sim.provenance import (
    EMPTY_CODE_STAMPS,
    EMPTY_DATA_STAMPS,
    CodeStamps,
    DataDirtyRefused,
    DataStamps,
    code_stamps,
    data_stamps,
    repo_head,
)
from analysis.sim.schema import INTENT_DDL, SCHEMA_VERSION

__all__ = [
    "CURRENT_SCENARIO_FILE", "DEFAULT_INTENT_DIR", "DEFAULT_RUNS_DIR",
    "DEFAULT_SCENARIO_NAME",
    "LoadError", "LoadResult", "DataDirtyRefused", "ScenarioRef",
    "latest_run_path", "load_scenario", "open_intent_db", "read_current_scenario",
    "resolve_scenario_name", "state_dir", "write_current_scenario",
]

CURRENT_SCENARIO_FILE = ".current_scenario"
DEFAULT_SCENARIO_NAME = "current"
DEFAULT_INTENT_DIR = Path("state/intent")
DEFAULT_RUNS_DIR = Path("state/runs")


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


class _MoneyReferenceResolver:
    """Resolve ``@name`` monetary references from ``itemizations/name.csv``.

    The referenced CSV must include a ``Value`` column containing decimal-dollar
    amounts that are summed into integer cents.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._cache: dict[str, int] = {}
        self._resolving: set[str] = set()

    def resolve(self, token: str) -> int:
        if token in self._cache:
            return self._cache[token]
        if token in self._resolving:
            raise ValueError(f"cyclic money reference '@{token}'")

        path = self.data_dir / "itemizations" / f"{token}.csv"
        if not path.exists():
            raise ValueError(
                f"money reference '@{token}' not found; expected {path}"
            )

        self._resolving.add(token)
        try:
            total = 0
            with path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                if not reader.fieldnames or "Value" not in reader.fieldnames:
                    raise ValueError(
                        f"{path}: expected a 'Value' column for '@{token}'"
                    )
                for i, row in enumerate(reader, start=2):
                    if not any((v or "").strip() for v in row.values()):
                        continue
                    raw = (row.get("Value") or "").strip()
                    if raw == "":
                        raise ValueError(
                            f"{path} row {i}: missing Value for '@{token}'"
                        )
                    total += _dollars_to_cents(raw, resolver=self)
            self._cache[token] = total
            return total
        finally:
            self._resolving.discard(token)


def current_scenario_path(data_dir: Path | str) -> Path:
    """Path of the file recording the data repo's most recently loaded scenario."""
    return Path(data_dir) / CURRENT_SCENARIO_FILE


def read_current_scenario(data_dir: Path | str) -> str | None:
    """Return the recorded scenario name, or ``None`` if nothing is recorded."""
    path = current_scenario_path(data_dir)
    try:
        name = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def write_current_scenario(data_dir: Path | str, scenario: str) -> None:
    """Record ``scenario`` as the data repo's current scenario."""
    current_scenario_path(data_dir).write_text(f"{scenario}\n", encoding="utf-8")


def state_dir(data_dir: Path | str | None, default: Path) -> Path:
    """Resolve a data repo's ``state/intent`` or ``state/runs`` dir (ADR-004 §4).

    State lives beside the data dir, not inside it. Falls back to
    ``$ANALYSIS_DATA_DIR`` and then to ``default`` relative to the cwd.
    """
    if data_dir is None:
        data_dir = os.environ.get("ANALYSIS_DATA_DIR")
        if not data_dir:
            return default
    return Path(data_dir).resolve().parent / default


def latest_run_path(runs_dir: Path | str, scenario: str | None = None) -> Path | None:
    """Most recent run file under ``runs_dir``, or ``None`` if there is none.

    Run filenames are ``<timestamp>_<scenario>_<data-version>.sqlite3``, so they
    sort chronologically and can be filtered by scenario by name alone.
    """
    pattern = f"*_{scenario}_*.sqlite3" if scenario else "*.sqlite3"
    runs = sorted(Path(runs_dir).glob(pattern))
    return runs[-1] if runs else None


class ScenarioRef(NamedTuple):
    """A resolved scenario: what it is called, where it lives, what state it is in."""

    name: str
    data_dir: Path
    path: Path
    intent_path: Path
    runs_dir: Path
    run_path: Path | None
    commit: str | None
    branch: str | None
    dirty: bool

    @property
    def loaded(self) -> bool:
        """True once ``sim load`` has built this scenario's intent file."""
        return self.intent_path.exists()

    def describe(self) -> str:
        """Markdown summary of the scenario, its data repo, and its DBs."""
        if self.commit:
            where = f"`{self.commit[:12]}`"
            if self.branch:
                where += f" on `{self.branch}`"
            if self.dirty:
                where += " *(uncommitted changes)*"
        else:
            where = "*(not a git repo)*"
        intent = f"`{self.intent_path}`"
        if self.loaded:
            intent += f" — loaded {_mtime(self.intent_path)}"
        else:
            intent += " — *not loaded; run `sim load`*"
        if self.run_path is None:
            run = f"*no runs in `{self.runs_dir}`; run `sim run`*"
        else:
            run = f"`{self.run_path}` — {_mtime(self.run_path)}"
        return (
            f"**scenario `{self.name}`**\n\n"
            f"| | |\n|---|---|\n"
            f"| scenario | `{self.path}` |\n"
            f"| data repo | `{self.data_dir}` {where} |\n"
            f"| intent db | {intent} |\n"
            f"| latest run | {run} |\n"
        )

    def _repr_markdown_(self) -> str:
        return self.describe()

    def __str__(self) -> str:
        version = f"@{self.commit[:12]}" if self.commit else "@unversioned"
        return f"{self.name} ({self.data_dir}{version}{'+dirty' if self.dirty else ''})"


def _mtime(path: Path) -> str:
    return f"{datetime.fromtimestamp(path.stat().st_mtime):%Y-%m-%d %H:%M}"


def resolve_scenario_name(
    data_dir: Path | str,
    scenario: str | None = None,
    *,
    intent_dir: Path | None = None,
    runs_dir: Path | None = None,
) -> ScenarioRef:
    """Pick a scenario from the data repo and describe where it lives.

    If ``scenario`` is given, validate it exists. Otherwise fall back, in order,
    to the name recorded in ``.current_scenario``, the sole scenario folder if
    there is only one, and finally ``current``.
    """
    data_dir = Path(data_dir).resolve()
    scenarios = data_dir / "scenarios"
    if not scenarios.is_dir():
        raise LoadError([f"no scenarios/ in {data_dir}"])
    if scenario:
        if not (scenarios / scenario).is_dir():
            raise LoadError([f"scenario not found: {scenarios / scenario}"])
        name = scenario
    else:
        name = _default_scenario_name(data_dir, scenarios)
    head = repo_head(data_dir)
    resolved_intent_dir = (
        Path(intent_dir) if intent_dir is not None
        else state_dir(data_dir, DEFAULT_INTENT_DIR)
    )
    resolved_runs_dir = (
        Path(runs_dir) if runs_dir is not None
        else state_dir(data_dir, DEFAULT_RUNS_DIR)
    )
    return ScenarioRef(
        name=name,
        data_dir=data_dir,
        path=scenarios / name,
        intent_path=resolved_intent_dir / f"{name}.sqlite3",
        runs_dir=resolved_runs_dir,
        run_path=latest_run_path(resolved_runs_dir, name),
        commit=head.commit,
        branch=head.branch,
        dirty=head.dirty,
    )


def _default_scenario_name(data_dir: Path, scenarios: Path) -> str:
    recorded = read_current_scenario(data_dir)
    if recorded:
        if not (scenarios / recorded).is_dir():
            raise LoadError([
                f"{current_scenario_path(data_dir)} names a missing scenario: "
                f"{recorded!r}"
            ])
        return recorded
    subs = sorted(p.name for p in scenarios.iterdir() if p.is_dir())
    if len(subs) == 1:
        return subs[0]
    if DEFAULT_SCENARIO_NAME in subs:
        return DEFAULT_SCENARIO_NAME
    raise LoadError(
        [f"multiple scenarios; pass --scenario [{', '.join(subs) or '(none)'}]"]
    )


def _merge_scenario_rows(
    parent_rows: list[dict[str, str]],
    child_rows: list[dict[str, str]],
    key_columns: tuple[str, ...],
    *,
    require_key: bool = True,
) -> list[dict[str, str]]:
    """Overlay child CSV rows on parent rows using the table's natural key."""
    merged = {tuple(row.get(column, "") for column in key_columns): dict(row)
              for row in parent_rows}
    order = list(merged)
    for row in child_rows:
        key = tuple(row.get(column, "") for column in key_columns)
        if require_key and not all(key):
            raise LoadError([
                f"derived scenario override missing key {key_columns}: {row}"
            ])
        if key not in merged:
            order.append(key)
            merged[key] = dict(row)
        else:
            merged[key].update(row)
    return [merged[key] for key in order]


def _read_scenario_inputs(
    data_dir: Path,
    scenario_name: str,
    issues: list[str],
    *,
    stack: tuple[str, ...] = (),
) -> tuple[dict[str, str], list[dict[str, str]], list[dict[str, str]],
           list[dict[str, str]], list[dict[str, str]],
           list[dict[str, str]], list[dict[str, str]]]:
    """Read a scenario and recursively overlay any declared parent."""
    scenario_dir = data_dir / "scenarios" / scenario_name
    if scenario_name in stack:
        raise LoadError([f"scenario inheritance cycle: {' -> '.join((*stack, scenario_name))}"])
    if not scenario_dir.is_dir():
        raise LoadError([f"scenario folder not found: {scenario_dir}"])

    scenario_rows = _read_csv(scenario_dir / "scenario.csv", issues)
    if len(scenario_rows) != 1:
        issues.append(
            f"scenario.csv must have exactly one data row (got {len(scenario_rows)})"
        )
    child_scenario = scenario_rows[0] if scenario_rows else {}
    if child_scenario.get("name") and child_scenario["name"] != scenario_name:
        issues.append(
            f"scenario.csv name='{child_scenario['name']}' != folder '{scenario_name}'"
        )
    parent_name = child_scenario.get("parent_scenario", "")
    if parent_name:
        parent = _read_scenario_inputs(
            data_dir, parent_name, issues, stack=(*stack, scenario_name)
        )
        parent_scenario, parent_accounts, parent_models, parent_scheduled, \
            parent_calendar, parent_lifespan, parent_ytd = parent
    else:
        parent_scenario = {}
        parent_accounts = parent_models = parent_scheduled = []
        parent_calendar = parent_lifespan = []
        parent_ytd = []

    def read_overlay(filename: str, *, required: bool = False) -> list[dict[str, str]]:
        return _read_csv(
            scenario_dir / filename,
            issues,
            optional=bool(parent_name) or not required,
        )

    scenario_row = dict(parent_scenario)
    scenario_row.update({k: v for k, v in child_scenario.items() if v != ""})
    scenario_row["name"] = scenario_name
    accounts = _merge_scenario_rows(
        parent_accounts, read_overlay("accounts.csv", required=True), ("name",)
    )
    model_rows = _merge_scenario_rows(
        parent_models, read_overlay("account_values.csv", required=True), ("account_name",)
    )
    scheduled_rows = _merge_scenario_rows(
        parent_scheduled, read_overlay("scheduled.csv", required=True), ("name",)
    )
    calendar_rows = _merge_scenario_rows(
        parent_calendar, read_overlay("calendar.csv"),
        ("date", "kind", "scheduled_name", "account_1", "account_2", "series", "amount"),
        require_key=False,
    )
    lifespan_rows = _merge_scenario_rows(
        parent_lifespan, read_overlay("lifespan.csv"), ("person",),
    )
    ytd_rows = _merge_scenario_rows(
        parent_ytd, read_overlay("year_to_date.csv"), ("name",),
    )
    return (
        scenario_row, accounts, model_rows, scheduled_rows,
        calendar_rows, lifespan_rows, ytd_rows,
    )


@contextmanager
def open_intent_db(
    data_dir: Path | str,
    scenario: str | None = None,
) -> Iterator[tuple[sqlite3.Connection, str]]:
    """Load a scenario into a temporary intent DB and yield ``(connection, name)``.

    Single source of truth for read-only inspection: CLI ``list`` commands and
    duplicate checks for ``add`` commands go through the loader instead of
    re-parsing CSVs.
    """
    data_dir = Path(data_dir).resolve()
    name = resolve_scenario_name(data_dir, scenario).name
    with tempfile.TemporaryDirectory() as td:
        load_scenario(
            data_dir, name, intent_dir=Path(td),
            compute_stamps=False, allow_dirty_data=True,
        )
        con = sqlite3.connect(Path(td) / f"{name}.sqlite3")
        con.row_factory = sqlite3.Row
        try:
            yield con, name
        finally:
            con.close()


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

    money_resolver = _MoneyReferenceResolver(data_dir)

    issues: list[str] = []

    persons = _read_csv(data_dir / "persons.csv", issues)
    (
        scenario_row, accounts, model_rows, scheduled_rows,
        calendar_rows, lifespan_rows, ytd_rows,
    ) = _read_scenario_inputs(data_dir, scenario_name, issues)
    chains = _read_chain_files(data_dir, scenario_name, issues)


    aux_rows = _read_aux_dir(data_dir / "aux", issues, money_resolver=money_resolver)

    _expand_account_growth(
        accounts, model_rows, scheduled_rows, scenario_row, issues,
    )

    _validate_accounts(accounts, issues, money_resolver=money_resolver)
    _validate_account_model_money(
        model_rows,
        issues,
        money_resolver=money_resolver,
        accounts=accounts,
    )
    _validate_scheduled_money(scheduled_rows, issues, money_resolver=money_resolver)
    _validate_scheduled(scheduled_rows, accounts, set(chains), issues)
    _validate_calendar(calendar_rows, issues)
    _validate_chains(chains, accounts, issues, money_resolver=money_resolver)
    _validate_lifespan(lifespan_rows, persons, issues)
    _validate_year_to_date(ytd_rows, accounts, issues, money_resolver=money_resolver)

    if issues:
        raise LoadError(issues)


    # account_model_name is obsolete; pass empty string for compatibility
    if compute_stamps:
        ds = data_stamps(data_dir, scenario_name, "")
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
        con.execute("PRAGMA locking_mode = EXCLUSIVE")
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(INTENT_DDL)
        counts = _write_intent(
            con,
            persons=persons,
            accounts=accounts,
            account_model_name="",  # obsolete
            account_model_rows=model_rows,
            aux_rows=aux_rows,
            scenario_row=scenario_row,
            scheduled_rows=scheduled_rows,
            chains=chains,
            calendar_rows=calendar_rows,
            lifespan_rows=lifespan_rows,
            ytd_rows=ytd_rows,
            money_resolver=money_resolver,
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
    aux_dir: Path,
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
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
        money_cols = registries.AUX_MONEY_COLUMNS.get(kind, frozenset())
        for i, row in enumerate(_read_csv(csv_path, issues), start=2):
            missing = [c for c in cols if c not in row]
            if missing:
                issues.append(
                    f"aux/{csv_path.name} row {i}: missing columns {missing}"
                )
                continue
            payload: dict[str, Any] = {}
            row_ok = True
            for c in cols:
                if c == "effective_date":
                    continue
                raw = row[c]
                if c in money_cols:
                    if raw != "":
                        try:
                            payload[c] = _dollars_to_cents(raw, resolver=money_resolver)
                        except ValueError as e:
                            issues.append(
                                f"aux/{csv_path.name} row {i} ({c}): {e}"
                            )
                            row_ok = False
                            break
                    else:
                        payload[c] = None
                else:
                    payload[c] = _coerce(raw)
            if row_ok:
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


def _dollars_to_cents(
    s: str,
    *,
    resolver: _MoneyReferenceResolver | None = None,
) -> int:
    """Parse a human-facing dollar amount (e.g. ``'1234.56'``, ``'-150'``) into
    integer cents. CSV money columns are kept in dollars-and-cents so they can
    be hand-edited; the engine continues to work in integer cents.
    """
    raw = s.strip()
    if raw.startswith("@"):
        if resolver is None:
            raise ValueError(f"money reference {raw!r} requires a data-dir resolver")
        token = raw[1:].strip()
        if not token:
            raise ValueError("money reference '@' missing token (e.g. '@home_basis')")
        return resolver.resolve(token)
    try:
        d = Decimal(raw)
    except InvalidOperation as e:
        raise ValueError(f"invalid money value: {s!r}") from e
    cents = d * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"money value {s!r} has sub-cent precision")
    return int(cents)


def _percent_to_micros(s: str) -> int:
    """Parse a human-facing percentage such as ``25`` into millionths."""
    try:
        value = Decimal(s.strip()) * 10_000
    except InvalidOperation as e:
        raise ValueError(f"invalid percent value: {s!r}") from e
    if value != value.to_integral_value():
        raise ValueError(f"percent value {s!r} has excessive precision")
    micros = int(value)
    if not 0 <= micros <= 1_000_000:
        raise ValueError(f"percent value {s!r} must be between 0 and 100")
    return micros


# ---------- account-declared growth ----------

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


def _expand_account_growth(
    accounts: list[dict[str, str]],
    model_rows: list[dict[str, str]],
    scheduled_rows: list[dict[str, str]],
    scenario_row: dict[str, str],
    issues: list[str],
) -> None:
    """Turn an asset account's ``series`` into a ``percent_of_balance`` rule.

    The income side is the built-in account for the row's ``tax_character``
    (ADR-003 §2.2), created here on first use so scenarios never wire it up by
    hand. Rules are named ``growth_<account slug>``.
    """
    account_names = {row.get("name", "") for row in accounts}
    rule_names = {row.get("name", "") for row in scheduled_rows}
    start = scenario_row.get("start", "")
    for i, row in enumerate(accounts, start=2):
        name = row.get("name", "")
        series = row.get("series", "")
        character = row.get("tax_character", "")
        if not series:
            if character:
                issues.append(
                    f"accounts.csv row {i} ({name}): tax_character without series "
                    "has no effect"
                )
            continue
        if row.get("kind") != "asset":
            issues.append(
                f"accounts.csv row {i} ({name}): series is only supported for "
                "asset accounts"
            )
            continue
        counterparty = registries.GROWTH_COUNTERPARTY_ACCOUNTS.get(character)
        if counterparty is None:
            issues.append(
                f"accounts.csv row {i} ({name}): series requires tax_character in "
                f"{sorted(registries.GROWTH_COUNTERPARTY_ACCOUNTS)}, got '{character}'"
            )
            continue
        if counterparty.name not in account_names:
            account_names.add(counterparty.name)
            accounts.append({
                "name": counterparty.name, "kind": counterparty.kind,
                "subkind": counterparty.subkind,
                "owner": "", "tax_treatment": counterparty.tax_treatment,
                "opened": "", "closed": "", "tags": "",
                "meta_json": json.dumps(
                    {"builtin": "growth", "description": counterparty.description},
                    separators=(",", ":"),
                ),
            })
            model_rows.append({
                "account_name": counterparty.name, "opening_balance": "0.00",
            })
        rule_name = f"growth_{_slug(name)}"
        if rule_name in rule_names:
            issues.append(
                f"accounts.csv row {i} ({name}): series would synthesize rule "
                f"'{rule_name}', which scheduled.csv already defines"
            )
            continue
        rule_names.add(rule_name)
        scheduled_rows.append({
            "name": rule_name,
            "from_account": counterparty.name,
            "to_account": name,
            "cadence": "monthly",
            "amount_rule_kind": "percent_of_balance",
            "series": series,
            "tax_character": counterparty.tax_character,
            "start": start,
            "description": f"Growth on {name} ({series})",
        })


# ---------- validation ----------

def _validate_accounts(
    rows: list[dict[str, str]], issues: list[str], *, money_resolver: _MoneyReferenceResolver,
) -> None:
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
        for column in ("minimum_balance", "maximum_balance"):
            value = row.get(column, "")
            if value:
                try:
                    _dollars_to_cents(value, resolver=money_resolver)
                except ValueError as e:
                    issues.append(f"accounts.csv row {i} ({name}): {column} {e}")
        _validate_tags("accounts.csv", i, name, row.get("tags", ""), issues)


def _validate_scheduled(
    rows: list[dict[str, str]], accounts: list[dict[str, str]],
    chain_names: set[str], issues: list[str],
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
        for retired, merged in (("base_amount", "amount"), ("inflation_series", "series")):
            if row.get(retired, "") != "":
                issues.append(
                    f"scheduled.csv row {i} ({name}): '{retired}' was merged into "
                    f"'{merged}' (schema v12); move the value and drop the column"
                )
        if row.get("series", "") != "" and kind not in registries.SERIES_RULE_KINDS:
            issues.append(
                f"scheduled.csv row {i} ({name}): amount_rule_kind '{kind}' does not "
                f"use 'series'; it is only read by "
                f"{', '.join(sorted(registries.SERIES_RULE_KINDS))}"
            )
        for col in ("from_account", "to_account"):
            ref = row.get(col, "")
            valid = ref in account_names if col == "from_account" else ref in account_names | chain_names
            if not ref or not valid:
                issues.append(
                    f"scheduled.csv row {i} ({name}): {col} '{ref}' not in accounts.csv or chains/"
                )
        params_json = row.get("amount_rule_params_json") or "{}"
        try:
            json.loads(params_json)
        except json.JSONDecodeError as e:
            issues.append(
                f"scheduled.csv row {i} ({name}): amount_rule_params_json invalid: {e}"
            )
        ytd_amount = row.get("ytd_amount", "")
        ytd_preroll = row.get("ytd_preroll", "")
        if ytd_amount != "" or ytd_preroll not in ("", "0"):
            if kind != "fixed_amount" or cad != "monthly":
                issues.append(
                    f"scheduled.csv row {i} ({name}): ytd_amount/ytd_preroll only "
                    "valid for amount_rule_kind='fixed_amount' with cadence='monthly'"
                )
            if ytd_amount != "" and ytd_preroll not in ("", "0"):
                issues.append(
                    f"scheduled.csv row {i} ({name}): ytd_amount and ytd_preroll "
                    "are mutually exclusive"
                )
        if ytd_preroll not in ("", "0", "1"):
            issues.append(
                f"scheduled.csv row {i} ({name}): ytd_preroll must be 0 or 1"
            )
        inflation_cadence = row.get("inflation_cadence", "")
        if inflation_cadence not in ("", "monthly", "annual"):
            issues.append(
                f"scheduled.csv row {i} ({name}): inflation_cadence must be "
                "'monthly' or 'annual'"
            )
        if inflation_cadence and not row.get("series"):
            issues.append(
                f"scheduled.csv row {i} ({name}): inflation_cadence set without "
                "series"
            )
        _validate_tags("scheduled.csv", i, name, row.get("tags", ""), issues)


def _validate_account_model_money(
    rows: list[dict[str, str]],
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
    accounts: list[dict[str, str]],
) -> None:
    account_names = {r.get("name", "") for r in accounts}
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):
        name = row.get("account_name", "")
        if not name:
            issues.append(f"account_values.csv row {i}: missing account_name")
            continue
        if name in seen:
            issues.append(
                f"account_values.csv row {i} ({name}): duplicate account_name"
            )
        seen.add(name)
        if name not in account_names:
            issues.append(
                f"account_values.csv row {i} ({name}): account_name not in accounts.csv"
            )
        ob = row.get("opening_balance", "")
        if ob == "":
            issues.append(
                f"account_values.csv row {i} ({name}): opening_balance is required"
            )
        else:
            try:
                _dollars_to_cents(ob, resolver=money_resolver)
            except ValueError as e:
                issues.append(
                    f"account_values.csv row {i} ({name}): opening_balance {e}"
                )
        ocb = row.get("opening_cost_basis", "")
        if ocb != "":
            try:
                _dollars_to_cents(ocb, resolver=money_resolver)
            except ValueError as e:
                issues.append(
                    f"account_values.csv row {i} ({name}): opening_cost_basis {e}"
                )
        pyeb = row.get("prior_year_end_balance", "")
        if pyeb != "":
            try:
                _dollars_to_cents(pyeb, resolver=money_resolver)
            except ValueError as e:
                issues.append(
                    f"account_values.csv row {i} ({name}): prior_year_end_balance {e}"
                )


def _validate_scheduled_money(
    rows: list[dict[str, str]],
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
) -> None:
    for i, row in enumerate(rows, start=2):
        name = row.get("name", "")
        amount = row.get("amount", "")
        if amount != "":
            try:
                _dollars_to_cents(amount, resolver=money_resolver)
            except ValueError as e:
                issues.append(f"scheduled.csv row {i} ({name}): amount {e}")
        ytd_amount = row.get("ytd_amount", "")
        if ytd_amount != "":
            try:
                _dollars_to_cents(ytd_amount, resolver=money_resolver)
            except ValueError as e:
                issues.append(f"scheduled.csv row {i} ({name}): ytd_amount {e}")


def _validate_year_to_date(
    rows: list[dict[str, str]],
    accounts: list[dict[str, str]],
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
) -> None:
    account_names = {r["name"] for r in accounts if r.get("name")}
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):
        name = row.get("name", "")
        if not name:
            issues.append(f"year_to_date.csv row {i}: missing name")
            continue
        if name in seen:
            issues.append(f"year_to_date.csv row {i} ({name}): duplicate name")
        seen.add(name)
        for col in ("from_account", "to_account"):
            ref = row.get(col, "")
            if not ref or ref not in account_names:
                issues.append(
                    f"year_to_date.csv row {i} ({name}): {col} '{ref}' not in accounts.csv"
                )
        tc = row.get("tax_character", "")
        if tc not in registries.TAX_CHARACTERS:
            issues.append(f"year_to_date.csv row {i} ({name}): unknown tax_character '{tc}'")
        year = row.get("year", "")
        if year != "":
            try:
                int(year)
            except ValueError:
                issues.append(f"year_to_date.csv row {i} ({name}): year must be an int")
        amount = row.get("amount", "")
        if amount == "":
            issues.append(f"year_to_date.csv row {i} ({name}): amount is required")
        else:
            try:
                _dollars_to_cents(amount, resolver=money_resolver)
            except ValueError as e:
                issues.append(f"year_to_date.csv row {i} ({name}): amount {e}")


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
        _validate_tags("calendar.csv", i, row.get("scheduled_name") or kind,
                       row.get("tags", ""), issues)


def _validate_tags(file: str, row_i: int, name: str, raw: str,
                   issues: list[str]) -> None:
    try:
        registries.parse_tags(raw)
    except ValueError as e:
        issues.append(f"{file} row {row_i} ({name}): {e}")


def _chain_name_from_filename(path: Path) -> tuple[str, str] | None:
    """Parse a ``chains/<kind>__<name>.csv`` filename into ``(kind, name)``."""
    stem = path.stem
    if "__" not in stem:
        return None
    kind, name = stem.split("__", 1)
    return kind, name


def _read_chain_files(
    data_dir: Path, scenario_name: str, issues: list[str], *, stack: tuple[str, ...] = (),
) -> dict[str, tuple[str, list[dict[str, str]]]]:
    """Read ``chains/*.csv`` for a scenario, recursively overlaying any parent.

    Unlike the other per-scenario CSVs, chains merge by WHOLE FILE per chain
    name (ADR-003 §2.9): a child's ``chains/<kind>__<name>.csv`` entirely
    replaces the parent's file of that name; a chain not overridden by the
    child is inherited unchanged.
    """
    scenario_dir = data_dir / "scenarios" / scenario_name
    if scenario_name in stack:
        raise LoadError([f"scenario inheritance cycle: {' -> '.join((*stack, scenario_name))}"])
    scenario_rows = _read_csv(scenario_dir / "scenario.csv", issues, optional=True)
    parent_name = scenario_rows[0].get("parent_scenario", "") if scenario_rows else ""
    chains: dict[str, tuple[str, list[dict[str, str]]]] = {}
    if parent_name:
        chains.update(
            _read_chain_files(data_dir, parent_name, issues, stack=(*stack, scenario_name))
        )
    chains_dir = scenario_dir / "chains"
    if chains_dir.is_dir():
        for path in sorted(chains_dir.glob("*.csv")):
            if path.stem.startswith("_"):
                continue
            parsed = _chain_name_from_filename(path)
            if parsed is None:
                issues.append(f"chains/{path.name}: filename must be '<kind>__<name>.csv'")
                continue
            kind, name = parsed
            chains[name] = (kind, _read_csv(path, issues))
    return chains


def _reject_cycles(edges: dict[str, set[str]], issues: list[str], *, label: str) -> None:
    """Report a load error for any cycle in a name -> {referenced names} graph."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(edges, WHITE)

    def visit(node: str, path: list[str]) -> None:
        color[node] = GRAY
        for neighbor in edges.get(node, ()):
            if color.get(neighbor, WHITE) == GRAY:
                issues.append(f"{label}: cycle detected: {' -> '.join((*path, neighbor))}")
            elif color.get(neighbor, WHITE) == WHITE:
                visit(neighbor, [*path, neighbor])
        color[node] = BLACK

    for node in list(edges):
        if color[node] == WHITE:
            visit(node, [node])


def _validate_chains(
    chains: dict[str, tuple[str, list[dict[str, str]]]],
    accounts: list[dict[str, str]],
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
) -> None:
    account_names = {a["name"] for a in accounts if a.get("name")}
    for name in sorted(set(chains) & account_names):
        issues.append(f"chains/*.csv: chain name '{name}' collides with an account name")
    resolvable = account_names | set(chains)

    edges: dict[str, set[str]] = {name: set() for name in chains}
    for name, (kind, rows) in chains.items():
        if kind not in registries.CHAIN_KINDS:
            issues.append(
                f"chains/{kind}__{name}.csv: unknown kind '{kind}'; "
                f"allowed: {sorted(registries.CHAIN_KINDS)}"
            )
        if not rows:
            issues.append(f"chains/{kind}__{name}.csv: must have at least one row")
        for i, row in enumerate(rows, start=2):
            member = row.get("account_name", "")
            if not member:
                issues.append(f"chains/{kind}__{name}.csv row {i}: missing account_name")
                continue
            if member not in resolvable:
                issues.append(
                    f"chains/{kind}__{name}.csv row {i}: '{member}' is not a known "
                    "account or chain"
                )
            elif member in chains:
                edges[name].add(member)
            threshold = row.get("threshold", "")
            if threshold:
                try:
                    _dollars_to_cents(threshold, resolver=money_resolver)
                except ValueError as e:
                    issues.append(f"chains/{kind}__{name}.csv row {i}: threshold {e}")
            amount = row.get("amount", "")
            percent = row.get("percent", "")
            if amount and percent:
                issues.append(
                    f"chains/{kind}__{name}.csv row {i}: amount and percent are mutually exclusive"
                )
            if amount:
                try:
                    _dollars_to_cents(amount, resolver=money_resolver)
                except ValueError as e:
                    issues.append(f"chains/{kind}__{name}.csv row {i}: amount {e}")
            if percent:
                try:
                    _percent_to_micros(percent)
                except ValueError as e:
                    issues.append(f"chains/{kind}__{name}.csv row {i}: percent {e}")
            series = row.get("series", "")
            cadence = row.get("cadence", "")
            if cadence not in ("", "monthly", "annual"):
                issues.append(
                    f"chains/{kind}__{name}.csv row {i}: cadence must be monthly or annual"
                )
            if cadence and not series:
                issues.append(
                    f"chains/{kind}__{name}.csv row {i}: cadence set without series"
                )
            if series and not amount:
                issues.append(
                    f"chains/{kind}__{name}.csv row {i}: series requires amount"
                )

        allocated = [row for row in rows if row.get("amount") or row.get("percent")]
        if allocated:
            if len(allocated) != len(rows):
                issues.append(
                    f"chains/{kind}__{name}.csv: every row must specify amount or percent "
                    "when using allocation fields"
                )
            elif not rows[-1].get("percent"):
                issues.append(
                    f"chains/{kind}__{name}.csv: final row must specify percent=100 "
                    "to receive the remaining amount"
                )
            else:
                try:
                    final_percent = _percent_to_micros(rows[-1]["percent"])
                except ValueError:
                    final_percent = None
                if final_percent != 1_000_000:
                    issues.append(
                        f"chains/{kind}__{name}.csv: final row must specify percent=100 "
                        "to receive the remaining amount"
                    )

    for a in accounts:
        for column in ("refill_chain", "overflow_chain"):
            target = a.get(column, "")
            if not target:
                continue
            if target not in resolvable:
                issues.append(
                    f"accounts.csv ({a.get('name', '')}): {column} '{target}' is not "
                    "a known account or chain"
                )
            elif target in chains:
                edges.setdefault(a["name"], set()).add(target)

    _reject_cycles(edges, issues, label="chains/*.csv")


def _validate_lifespan(
    rows: list[dict[str, str]],
    persons: list[dict[str, str]],
    issues: list[str],
) -> None:
    person_ids = {p["id"] for p in persons if p.get("id")}
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):
        person = row.get("person", "")
        final_month = row.get("final_month", "")
        if not person or not final_month:
            issues.append(f"lifespan.csv row {i}: person and final_month are required")
            continue
        if person not in person_ids:
            issues.append(f"lifespan.csv row {i}: person '{person}' not in persons.csv")
        if person in seen:
            issues.append(f"lifespan.csv row {i}: duplicate person '{person}'")
        seen.add(person)


# ---------- writers ----------

def _insert_tags(
    con: sqlite3.Connection,
    item_id: int,
    raw: str,
    link_table: str,
    link_col: str,
    cache: dict[tuple[str, str], int],
) -> None:
    for role, name in registries.parse_tags(raw):
        key = (role, name)
        tag_id = cache.get(key)
        if tag_id is None:
            con.execute(
                "INSERT OR IGNORE INTO tag(role, name) VALUES (?,?)", (role, name)
            )
            tag_id = con.execute(
                "SELECT id FROM tag WHERE role=? AND name=?", (role, name)
            ).fetchone()[0]
            cache[key] = tag_id
        con.execute(
            f"INSERT OR IGNORE INTO {link_table}({link_col}, tag_id) VALUES (?,?)",
            (item_id, tag_id),
        )


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
    chains: dict[str, tuple[str, list[dict[str, str]]]],
    calendar_rows: list[dict[str, str]],
    lifespan_rows: list[dict[str, str]],
    ytd_rows: list[dict[str, str]],
    money_resolver: _MoneyReferenceResolver,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    now = _now_iso()
    tag_cache: dict[tuple[str, str], int] = {}

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
            "opened, closed, minimum_balance_cents, maximum_balance_cents, "
            "meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                a["name"], a["kind"], a["subkind"],
                a.get("owner") or None, a.get("tax_treatment") or None,
                a.get("opened") or None, a.get("closed") or None,
                _dollars_to_cents(a["minimum_balance"], resolver=money_resolver)
                if a.get("minimum_balance") else 0,
                _dollars_to_cents(a["maximum_balance"], resolver=money_resolver)
                if a.get("maximum_balance") else None,
                a.get("meta_json") or "{}",
            ),
        )
        account_ids[a["name"]] = cur.lastrowid
        _insert_tags(con, cur.lastrowid, a.get("tags", ""),
                     "account_tag", "account_id", tag_cache)
    counts["account"] = len(accounts)

    # Liquidity chains (ADR-003 §2.9): one implicit leaf chain per real account,
    # plus one interior chain per chains/*.csv file, in one shared name space.
    chain_ids: dict[str, int] = {}
    for a in accounts:
        cur = con.execute(
            "INSERT INTO chain(name, kind, account_id) VALUES (?,?,?)",
            (a["name"], a["kind"], account_ids[a["name"]]),
        )
        chain_ids[a["name"]] = cur.lastrowid
    for name, (kind, _rows) in chains.items():
        cur = con.execute(
            "INSERT INTO chain(name, kind, account_id) VALUES (?,?,NULL)",
            (name, kind),
        )
        chain_ids[name] = cur.lastrowid
    counts["chain"] = len(chain_ids)

    step_count = 0
    for name, (_kind, rows) in chains.items():
        for i, row in enumerate(rows, start=1):
            con.execute(
                "INSERT INTO chain_steps(chain_id, step_order, draw_from_chain_id, "
                "threshold_cents, amount_cents, percent_micros, series, cadence) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    chain_ids[name], i, chain_ids[row["account_name"]],
                    _dollars_to_cents(row["threshold"], resolver=money_resolver)
                    if row.get("threshold") else 0,
                    _dollars_to_cents(row["amount"], resolver=money_resolver)
                    if row.get("amount") else None,
                    _percent_to_micros(row["percent"])
                    if row.get("percent") else None,
                    row.get("series") or None,
                    row.get("cadence") or None,
                ),
            )
            step_count += 1
    counts["chain_steps"] = step_count

    for a in accounts:
        refill = a.get("refill_chain") or None
        overflow = a.get("overflow_chain") or None
        if refill or overflow:
            con.execute(
                "UPDATE account SET refill_chain_id = ?, overflow_chain_id = ? "
                "WHERE id = ?",
                (
                    chain_ids[refill] if refill else None,
                    chain_ids[overflow] if overflow else None,
                    account_ids[a["name"]],
                ),
            )

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
                _dollars_to_cents(m["opening_balance"], resolver=money_resolver),
                _dollars_to_cents(m["opening_cost_basis"], resolver=money_resolver)
                if m.get("opening_cost_basis") else None,
                _dollars_to_cents(m["prior_year_end_balance"], resolver=money_resolver)
                if m.get("prior_year_end_balance") else None,
            ),
        )
    counts["account_model_member"] = len(account_model_rows)

    cur = con.execute(
        "INSERT INTO calendar(name, description, created_at) VALUES (?,?,?)",
        (scenario_row.get("name", ""), None, now),
    )
    calendar_id = cur.lastrowid
    for ce in calendar_rows:
        cur = con.execute(
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
        _insert_tags(con, cur.lastrowid, ce.get("tags", ""),
                     "calendar_event_tag", "calendar_event_id", tag_cache)
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
            "interest_account, person, series, rate_micros, claim_age, amount_cents, "
            "params_json, base_date, "
            "tax_character, start, \"end\", priority, "
            "description, meta_json, ytd_amount, ytd_preroll, inflation_cadence"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                model_id, s["name"],
                account_ids[s["from_account"]],
                chain_ids[s["to_account"]],
                s["cadence"],
                int(s["month_of_year"]) if s.get("month_of_year") else None,
                s["amount_rule_kind"],
                s.get("interest_account") or None,
                s.get("person") or None,
                s.get("series") or None,
                _opt_int(s.get("rate_micros")),
                _opt_int(s.get("claim_age")),
                _dollars_to_cents(s["amount"], resolver=money_resolver)
                if s.get("amount") else None,
                s.get("params_json") or None,
                s.get("base_date") or None,
                s["tax_character"], s["start"], s.get("end") or None, prio,
                s.get("description") or None, s.get("meta_json") or "{}",
                _dollars_to_cents(s["ytd_amount"], resolver=money_resolver)
                if s.get("ytd_amount") else None,
                int(s.get("ytd_preroll") or 0),
                s.get("inflation_cadence") or None,
            ),
        )
        sched_ids[s["name"]] = cur.lastrowid
        _insert_tags(con, cur.lastrowid, s.get("tags", ""),
                     "scheduled_transaction_tag", "scheduled_transaction_id",
                     tag_cache)
    counts["scheduled_transaction"] = len(scheduled_rows)

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

    for lf in lifespan_rows:
        con.execute(
            "INSERT INTO person_lifespan(scenario_id, person, final_month) "
            "VALUES (?,?,?)",
            (scenario_id, lf["person"], lf["final_month"]),
        )
    counts["person_lifespan"] = len(lifespan_rows)

    for y in ytd_rows:
        con.execute(
            "INSERT INTO year_to_date("
            "scenario_id, name, from_account, to_account, amount, "
            "tax_character, year, description) VALUES (?,?,?,?,?,?,?,?)",
            (
                scenario_id, y["name"],
                account_ids[y["from_account"]], account_ids[y["to_account"]],
                _dollars_to_cents(y["amount"], resolver=money_resolver),
                y["tax_character"], _opt_int(y.get("year")), y.get("description") or None,
            ),
        )
    counts["year_to_date"] = len(ytd_rows)

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
