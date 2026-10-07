"""CSV -> intent .sqlite3 loader.

Reads a data repository per ADR-003 layout and writes an intent file per ADR-002.
Git-based provenance stamps (ADR-004) are computed via ``analysis.sim.provenance``.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import socket
import sqlite3
import tempfile
from collections.abc import Callable, Collection, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

from analysis.sim import registries
from analysis.sim.dates import (
    MONTH_FORMAT_HINT, VaryBoundRole, month_end_iso, parse_day, vary_date_iso,
)
from analysis.sim.distributions import (
    DistributionError,
    Triangular,
    TruncatedNormal,
    resolve_truncated_normal_bounds,
)
from analysis.sim.lineage import ScenarioCycleError, load_order, parse_parents, read_parents
from analysis.sim.notebook_ui.styled import StyledMarkdown
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
from analysis.sim.readme import readme_description
from analysis.sim.runs import insert_run_metadata
from analysis.sim.schema import SCHEMA_VERSION, apply_run_schema, inserted_id
from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS

__all__ = [
    "BATCH_MANIFEST_FILENAME",
    "CURRENT_SCENARIO_FILE", "DEFAULT_INTENT_DIR", "DEFAULT_RUNS_DIR",
    "DEFAULT_SCENARIO_NAME",
    "LoadError", "LoadResult", "DataDirtyRefused", "ScenarioRef",
    "batch_mode", "latest_ensemble_batch", "latest_run_path", "list_scenario_names", "load_scenario", "open_intent_db",
    "read_current_scenario", "resolve_scenario_name", "find_state_dir",
    "growth_rule_name",
    "write_current_scenario",
]

CURRENT_SCENARIO_FILE = ".current_scenario"
DEFAULT_SCENARIO_NAME = "base"
DEFAULT_STATE_DIR = Path("state")
DEFAULT_INTENT_DIR = Path("intent")
DEFAULT_RUNS_DIR = Path("runs")


@dataclass(frozen=True)
class LoadResult:
    intent_path: Path
    scenario_name: str
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    """Problems worth telling the author about that don't stop the load
    (ADR-008: e.g. a latent factor nothing depends on yet)."""


class LoadError(ValueError):
    def __init__(self, issues: list[str] | str) -> None:
        match issues:
            case str():
                issues = [issues]
        super().__init__(
            f"{len(issues)} issue(s) loading intent:\n  " + "\n  ".join(issues)
        )
        self.issues = issues


class _MoneyReferenceResolver:
    """Resolve ``@name`` monetary references from an ``itemizations/name.csv``.

    Itemizations are scenario-scoped like ``aux/`` and ``series/`` (ADR-003
    §4.1c): the file is looked for in the scenario's own ``itemizations/``
    folder, then each ancestor's via ``parent_scenario``, then the data repo's
    shared ``itemizations/`` as the fallback. The referenced CSV must include a
    ``Value`` column containing decimal-dollar amounts that are summed into
    integer cents.
    """

    def __init__(self, data_dir: Path, scenario_dirs: list[Path] | None = None) -> None:
        self.data_dir = data_dir
        self.scenario_dirs = scenario_dirs or []
        self._cache: dict[str, int] = {}
        self._resolving: set[str] = set()

    def resolve(self, token: str) -> int:
        if token in self._cache:
            return self._cache[token]
        if token in self._resolving:
            raise ValueError(f"cyclic money reference '@{token}'")

        path = _find_override_file(self.data_dir, self.scenario_dirs, "itemizations", f"{token}.csv")
        if path is None:
            searched = ", ".join(str(d / "itemizations") for d in (*self.scenario_dirs, self.data_dir))
            raise ValueError(
                f"money reference '@{token}' not found; looked in: {searched}"
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


def find_state_dir(data_dir: Path | str | None, default: Path | str) -> Path:
    """Resolve a data repo's ``state/intent`` or ``state/runs`` dir (ADR-004 §4).

    State lives beside the data dir, not inside it. Falls back to
    ``$ANALYSIS_DATA_DIR`` and then to ``default`` relative to the cwd.
    """
    if data_dir is None:
        data_dir = os.environ.get("ANALYSIS_DATA_DIR")
        if not data_dir:
            return Path(default).resolve()
    return (Path(data_dir).resolve().parent / default).resolve()


def latest_run_path(runs_dir: Path | str, scenario: str | None = None) -> Path | None:
    """Most recent run file under ``runs_dir``, or ``None`` if there is none.

    Run filenames are ``<timestamp>_<scenario>_<data-version>.sqlite3``, so they
    sort chronologically and can be filtered by scenario by name alone.
    """
    glob_pattern = f"*_{scenario}_*.sqlite3" if scenario else "*.sqlite3"
    re_pattern = re.compile(f".*_{scenario}_[^_]*[.]sqlite3") if scenario else re.compile(f".*")
    runs = (r for r in Path(runs_dir).glob(glob_pattern)
            if re_pattern.match(r.name))
    runs = sorted(runs)
    return runs[-1] if runs else None


BATCH_MANIFEST_FILENAME = "batch_manifest.json"
"""``ensemble.write_batch_manifest``'s filename -- named here, not in
``ensemble.py``, so ``latest_ensemble_batch`` can recognize a batch by it
without ``loader.py`` importing from ``ensemble.py`` (which itself imports
from here)."""


def batch_mode(batch_dir: Path) -> str:
    """A batch's ADR-007 §5 mode: from its ``batch_manifest.json``, else from
    any member run file's own ``ensemble_manifest.mode`` (a batch whose run
    was interrupted before the manifest was written), else ``"monte_carlo"``
    (every batch written before ADR-007, or one synthesized by a test)."""
    path = Path(batch_dir) / BATCH_MANIFEST_FILENAME
    if path.is_file():
        return json.loads(path.read_text()).get("mode", "monte_carlo")
    for member in sorted(Path(batch_dir).glob("member_*.sqlite3")):
        try:
            con = sqlite3.connect(f"file:{member}?mode=ro", uri=True)
            try:
                row = con.execute("SELECT mode FROM ensemble_manifest").fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue  # empty, partial, or pre-ADR-007: try the next member
        if row is not None:
            return row[0]
    return "monte_carlo"


def latest_ensemble_batch(recipe_dir: Path, mode: str | None = None) -> Path | None:
    """The most recently generated batch subfolder under ``recipe_dir`` that
    holds at least one member run file or a batch manifest, or ``None`` if
    there isn't one.

    A recipe's members live under ``<recipe_dir>/<timestamp>/member_NNNN.sqlite3``
    -- one subfolder per ``sim run`` invocation, timestamp-named so reruns
    never collide and every batch's history is kept (matching how an
    ordinary, non-ensemble run file is never overwritten either). The
    manifest check covers the batch where every member failed to resolve:
    no member file was ever created for any of them, but the batch itself
    (and its recorded failures) still needs to be discoverable, not silently
    invisible.

    ``mode`` (ADR-007 §5) restricts the search to ``"monte_carlo"`` or
    ``"sensitivity"`` batches; ``None`` accepts either.
    """
    recipe_dir = Path(recipe_dir)
    if not recipe_dir.is_dir():
        return None
    batches = sorted(
        p for p in recipe_dir.iterdir()
        if p.is_dir() and (
            any(p.glob("member_*.sqlite3")) or (p / BATCH_MANIFEST_FILENAME).is_file()
        ) and (mode is None or batch_mode(p) == mode)
    )
    return batches[-1] if batches else None


def _latest_ensemble_member_zero(runs_dir: Path, name: str) -> Path | None:
    """``member_0000.sqlite3`` of ``name``'s most recent ensemble batch, for
    a caller that only knows how to open one run file per scenario (ADR-006
    §8): member 0 is always the recipe's deterministic (non-drawn) member,
    so it stands in for "the" run the same way any other scenario's one run
    file would.
    """
    batch = latest_ensemble_batch(Path(runs_dir) / name)
    if batch is None:
        return None
    member = batch / "member_0000.sqlite3"
    return member if member.exists() else None


@dataclass(frozen=True)
class ScenarioRef:
    """A resolved scenario: what it is called, where it lives, what state it is in."""

    name: str
    data_dir: Path
    path: Path
    scenarios_dir: Path
    intent_dir: Path
    intent_path: Path
    runs_dir: Path
    run_path: Path | None
    state_dir: Path
    commit: str | None
    branch: str | None
    dirty: bool = False

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

    def _repr_html_(self) -> str:
        # The styled rendering, so its paths' inline code reads in VS Code.
        return StyledMarkdown(self.describe())._repr_html_()

    def __str__(self) -> str:
        version = f"@{self.commit[:12]}" if self.commit else "@unversioned"
        return f"{self.name} ({self.data_dir}{version}{'+dirty' if self.dirty else ''})"


def _mtime(path: Path) -> str:
    return f"{datetime.fromtimestamp(path.stat().st_mtime):%Y-%m-%d %H:%M}"


def resolve_scenario_name(
    data_dir: Path | str,
    scenario: str | None = None,
    *,
    intent_dir: Path | str | None = None,
    runs_dir: Path | str | None = None,
    run_path: Path | str | None = None,
    state_dir: Path | str | None = None,
    scenarios_dir: Path | str | None = None,
) -> ScenarioRef:
    """Pick a scenario from the data repo and describe where it lives.

    If ``scenario`` is given, validate it exists. Otherwise fall back, in order,
    to the name recorded in ``.current_scenario``, the sole scenario folder if
    there is only one, and finally ``current``.
    """
    data_dir = Path(data_dir).resolve()
    scenarios_dir = (Path(scenarios_dir).resolve() if scenarios_dir is not None
                     else data_dir / "scenarios")
    if not scenarios_dir.is_dir():
        raise LoadError([f"no scenarios/ in {data_dir}"])
    state_dir = (Path(state_dir) if state_dir is not None
                 else find_state_dir(data_dir, data_dir.parent / "state"))
    if scenario:
        if not (scenarios_dir / scenario).is_dir():
            raise LoadError([f"scenario not found: {scenarios_dir / scenario}"])
        name = scenario
    else:
        name = _default_scenario_name(data_dir, scenarios_dir)
    head = repo_head(data_dir)
    resolved_intent_dir = (
        Path(intent_dir).resolve() if intent_dir is not None
        else find_state_dir(data_dir, DEFAULT_STATE_DIR) / DEFAULT_INTENT_DIR
    )
    resolved_runs_dir = (
        Path(runs_dir).resolve() if runs_dir is not None
        else (Path(run_path).parent.resolve()
                if run_path is not None
                else find_state_dir(data_dir, DEFAULT_STATE_DIR) / DEFAULT_RUNS_DIR
        )
    )
    run_path = (Path(run_path).resolve() if run_path is not None
                else latest_run_path(resolved_runs_dir, name)
                     or _latest_ensemble_member_zero(resolved_runs_dir, name)
    )
    return ScenarioRef(
        name=name,
        data_dir=data_dir,
        scenarios_dir=scenarios_dir,
        path=scenarios_dir / name,
        intent_dir=resolved_intent_dir,
        intent_path=resolved_intent_dir / f"{name}.sqlite3",
        runs_dir=resolved_runs_dir,
        run_path=run_path,
        state_dir=state_dir,
        commit=head.commit,
        branch=head.branch,
        dirty=head.dirty,
    )


def list_scenario_names(data_dir: Path, scenarios_dir: Path | None = None) -> list[str]:
    """Scenario folder names under data_dir/scenarios, sorted."""
    scenarios = scenarios_dir if scenarios_dir is not None else Path(data_dir) / "scenarios"
    if not scenarios.is_dir():
        raise LoadError([f"no scenarios/ in {data_dir}"])
    return sorted(p.name for p in scenarios.iterdir() if p.is_dir())


def _default_scenario_name(data_dir: Path, scenarios: Path, scenarios_dir: Path | None = None) -> str:
    recorded = read_current_scenario(data_dir)
    if recorded:
        if not ((scenarios_dir if scenarios_dir is not None else Path(data_dir) / "scenarios") / recorded).is_dir():
            raise LoadError([
                f"{current_scenario_path(data_dir)} names a missing scenario: "
                f"{recorded!r}"
            ])
        return recorded
    subs = list_scenario_names(data_dir, scenarios_dir)
    if len(subs) == 1:
        return subs[0]
    if DEFAULT_SCENARIO_NAME in subs:
        return DEFAULT_SCENARIO_NAME
    raise LoadError(
        [f"multiple scenarios; pass --scenario [{', '.join(subs) or '(none)'}]"]
    )


def _default_lots(
    rows: list[dict[str, str]], issues: list[str], investment_accounts: set[str],
) -> list[dict[str, str]]:
    """Default each account_values.csv row's lot (investment accounts must name
    theirs), and flag repeated (account, lot) pairs here: the overlay merge that
    follows would silently collapse them."""
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(rows, start=2):
        if not row.get("lot") and row.get("account_name") in investment_accounts:
            issues.append(
                f"account_values.csv row {i} ({row['account_name']}): investment "
                "account requires an explicit lot (e.g. lot=default)"
            )
        row["lot"] = row.get("lot") or registries.DEFAULT_LOT
        key = (row.get("account_name", ""), row["lot"])
        if key[0] and key in seen:
            issues.append(
                f"account_values.csv row {i} ({key[0]}): duplicate account_name and lot"
            )
        seen.add(key)
    return rows


def _check_duplicate_calendar_names(rows: list[dict[str, str]], issues: list[str]) -> None:
    """A duplicate ``name`` within one scenario's own (pre-merge) calendar.csv
    rows -- e.g. two rows differing only in a field outside the parent/child
    merge key -- would otherwise collapse into one row before the final,
    merged-view duplicate check in ``_validate_calendar`` ever sees them
    (ADR-006). Checked per file, not across parent/child: a child row that
    intentionally repeats a parent's own name (plus the rest of the merge
    key) is a sanctioned override, not a duplicate.
    """
    seen: dict[str, int] = {}
    for i, row in enumerate(rows, start=2):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        if name in seen:
            issues.append(
                f"calendar.csv row {i}: duplicate name '{name}' "
                f"(first seen on row {seen[name]})"
            )
        else:
            seen[name] = i


def calendar_row_key(row: Mapping[str, str]) -> tuple[str, ...]:
    """The key ``_merge_calendar_rows`` gives a calendar.csv row as authored:
    its key columns trimmed and its dates canonical, as the load sees them.
    So ``2030-12`` and ``2030-12-31`` are the same date here, as they are
    there. For checking, before a write, that two rows would not merge."""
    canonical = {column: (value or "").strip() for column, value in row.items()}
    _normalize_month_columns([canonical], "calendar.csv", [])
    if canonical.get("kind") == registries.LATENT_FACTOR_KIND:
        return (registries.LATENT_FACTOR_KIND, canonical.get("name", ""))
    return tuple(canonical.get(column, "") for column in registries.CALENDAR_KEY_COLUMNS)


def _merge_calendar_rows(
    parent_rows: list[dict[str, str]],
    child_rows: list[dict[str, str]],
    key_columns: tuple[str, ...],
    issues: list[str],
) -> list[dict[str, str]]:
    """Like ``_merge_scenario_rows`` (overlay by the table's natural key,
    ``require_key=False``), except a child row landing on the same key as an
    already-named row is only accepted as an override if it doesn't rename
    that row out from under it.

    ``name`` is deliberately *not* part of the key: a child repeating a
    parent's unnamed row's other fields plus a new ``name`` (to start varying
    it) must still replace that one row, not add a second, doubled event
    (ADR-006) -- the same physical event, one-time-transfer amount included,
    is not created twice just because it gained a name. But two rows that
    both already have a name -- whether one from the parent and one freshly
    authored in the child, or two authored together in the same file -- and
    share every other key field are almost certainly two different events
    that happen to collide on it (an authoring mistake, not an override), so
    that combination is reported rather than silently collapsed one way.

    A ``latent_factor`` row (ADR-008) is the exception: it is keyed by its
    name alone.
    """
    def key_of(row: dict[str, str]) -> tuple[str, ...]:
        # A latent factor (ADR-008) has no date or event fields -- only its
        # name identifies it, so two factors don't collide on blank fields,
        # and a child scenario's factor of the same name overrides its parent's.
        if row.get("kind") == registries.LATENT_FACTOR_KIND:
            return (registries.LATENT_FACTOR_KIND, (row.get("name") or "").strip())
        return tuple(row.get(c, "") for c in key_columns)

    merged: dict[tuple[str, ...], dict[str, str]] = {
        key_of(row): dict(row) for row in parent_rows
    }
    order = list(merged)
    for row in child_rows:
        key = key_of(row)
        if key in merged:
            existing_name = (merged[key].get("name") or "").strip()
            new_name = (row.get("name") or "").strip()
            if existing_name and new_name and existing_name != new_name:
                issues.append(
                    f"calendar.csv: rows named '{existing_name}' and '{new_name}' "
                    f"share the same {key_columns}; give them one name (an "
                    "override) or change a field so they're recognized as "
                    "distinct events"
                )
            merged[key].update(row)
        else:
            order.append(key)
            merged[key] = dict(row)
    return [merged[key] for key in order]


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


def _strict_load_order(scenarios_dir: Path, scenario_name: str) -> list[str]:
    """``lineage.load_order``, with a cycle reported as a ``LoadError``."""
    try:
        return load_order(scenarios_dir, scenario_name, strict=True)
    except ScenarioCycleError as exc:
        raise LoadError([str(exc)]) from exc


def _read_scenario_inputs(
    data_dir: Path,
    scenario_name: str,
    issues: list[str],
    *,
    scenarios_dir: Path | None = None,
) -> tuple[dict[str, str], list[dict[str, str]], list[dict[str, str]],
           list[dict[str, str]], list[dict[str, str]],
           list[dict[str, str]], list[dict[str, str]]]:
    """Read a scenario and every scenario it inherits from, overlaying each in
    load order (``lineage.load_order``: root first, mix-ins after the main
    parent's line, the scenario itself last)."""
    scenarios_dir = Path(scenarios_dir or (data_dir / "scenarios"))
    scenario_row: dict[str, str] = {}
    accounts: list[dict[str, str]] = []
    model_rows: list[dict[str, str]] = []
    scheduled_rows: list[dict[str, str]] = []
    calendar_rows: list[dict[str, str]] = []
    lifespan_rows: list[dict[str, str]] = []
    ytd_rows: list[dict[str, str]] = []
    meta: dict[str, Any] = {}
    for name in _strict_load_order(scenarios_dir, scenario_name):
        scenario_dir = scenarios_dir / name
        if not scenario_dir.is_dir():
            raise LoadError([f"scenario folder not found: {scenario_dir}"])

        scenario_rows = _read_csv(scenario_dir / "scenario.csv", issues)
        _normalize_month_columns(scenario_rows, "scenario.csv", issues)
        if len(scenario_rows) != 1:
            issues.append(
                f"scenario.csv must have exactly one data row (got {len(scenario_rows)})"
            )
        own_scenario = scenario_rows[0] if scenario_rows else {}
        if own_scenario.get("name") and own_scenario["name"] != name:
            issues.append(
                f"scenario.csv name='{own_scenario['name']}' != folder '{name}'"
            )
        # Only a scenario with no parent of its own must supply the core
        # files; anything deriving from another may leave them out to inherit.
        has_parent = bool(parse_parents(own_scenario.get("parent_scenario")))

        def read_overlay(
            filename: str, *, required: bool = False,
            scenario_dir: Path = scenario_dir, has_parent: bool = has_parent,
        ) -> list[dict[str, str]]:
            rows = _read_csv(
                scenario_dir / filename,
                issues,
                optional=has_parent or not required,
            )
            _normalize_month_columns(rows, filename, issues)
            return rows

        own_meta = own_scenario.pop("meta_json", "")
        scenario_row.update({k: v for k, v in own_scenario.items() if v != ""})
        # meta_json merges key by key rather than whole, so a child's own
        # `{}` (what `sim scenario create` writes) or its own unrelated key
        # doesn't hide a recipe's `ensemble_size` (#74).
        if own_meta.strip():
            try:
                parsed = json.loads(own_meta)
            except json.JSONDecodeError as exc:
                issues.append(f"{name}: scenario.csv meta_json is not valid JSON: {exc}")
            else:
                if isinstance(parsed, dict):
                    meta.update(parsed)
                else:
                    issues.append(
                        f"{name}: scenario.csv meta_json must be a JSON object "
                        f"(got {own_meta!r})"
                    )
        accounts = _merge_scenario_rows(
            accounts, read_overlay("accounts.csv", required=True), ("name",)
        )
        model_rows = _merge_scenario_rows(
            model_rows, _default_lots(
                read_overlay("account_values.csv", required=True), issues,
                {a["name"] for a in accounts if a.get("subkind") in INVESTMENT_ASSET_SUBKINDS},
            ),
            ("account_name", "lot"),
        )
        scheduled_rows = _merge_scenario_rows(
            scheduled_rows, read_overlay("scheduled.csv", required=True), ("name",)
        )
        calendar_rows_raw = read_overlay("calendar.csv")
        _check_duplicate_calendar_names(calendar_rows_raw, issues)
        calendar_rows = _merge_calendar_rows(
            calendar_rows, calendar_rows_raw, registries.CALENDAR_KEY_COLUMNS, issues,
        )
        lifespan_rows = _merge_scenario_rows(
            lifespan_rows, read_overlay("lifespan.csv"), ("person",),
        )
        ytd_rows = _merge_scenario_rows(
            ytd_rows, read_overlay("year_to_date.csv"), ("name",),
        )
    scenario_row["name"] = scenario_name
    scenario_row["meta_json"] = json.dumps(meta, separators=(",", ":"))
    return (
        scenario_row, accounts, model_rows, scheduled_rows,
        calendar_rows, lifespan_rows, ytd_rows,
    )


@contextmanager
def open_intent_db(
    data_dir: Path | str,
    scenario: str | None = None,
) -> Generator[tuple[sqlite3.Connection, str]]:
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
    intent_dir: Path | None = None,
    intent_path: Path | None = None,
    scenarios_dir: Path | None = None,
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
    scenarios_dir = Path(scenarios_dir or (data_dir / "scenarios"))
    scenario_dir = scenarios_dir / scenario_name
    if not scenario_dir.is_dir():
        raise LoadError([f"scenario folder not found: {scenario_dir}"])
    if intent_dir is None:
        if intent_path is None:
            intent_dir = data_dir / "intent"
            intent_path = intent_dir / f"{scenario_name}.sqlite3"
        else:
            intent_dir = intent_path.parent
    elif intent_path is None:
        intent_path = intent_dir / f"{scenario_name}.sqlite3"
    elif intent_path.parent != intent_dir:
        raise LoadError([f"intent_path {intent_path} is not within intent_dir {intent_dir}"])

    lineage_names = _scenario_lineage_names(scenarios_dir, scenario_name)
    scenario_dirs = [scenarios_dir / name for name in lineage_names]
    money_resolver = _MoneyReferenceResolver(data_dir, scenario_dirs)

    issues: list[str] = []
    warnings: list[str] = []

    persons = _read_csv(data_dir / "persons.csv", issues)
    _validate_birthdates(persons, issues)
    securities = _read_csv(data_dir / "securities.csv", issues, optional=True)
    _validate_securities(securities, issues)
    allocations = _read_csv(data_dir / "allocations.csv", issues, optional=True)
    _normalize_month_columns(allocations, "allocations.csv", issues)
    _validate_allocations(allocations, issues)
    (
        scenario_row, accounts, model_rows, scheduled_rows,
        calendar_rows, lifespan_rows, ytd_rows,
    ) = _read_scenario_inputs(data_dir,
                              scenario_name,
                              issues,
                              scenarios_dir=scenarios_dir)
    _normalize_vary_dates(calendar_rows)
    parents = read_parents(scenarios_dir / scenario_name)
    load_order_names = _strict_load_order(scenarios_dir, scenario_name)
    # A scenario folder's own README.md, if there is one, is the scenario's
    # real description -- scenario.csv's own description column is only the
    # fallback for a scenario that doesn't have one (ADR-003 §4.1e). Baked
    # in now rather than read live at display time: a run file is read long
    # after the data repo it came from may have moved, changed, or gone, so
    # anything a reader needs has to travel inside the intent/run file itself.
    description = readme_description(scenario_dir, data_dir=data_dir)
    if description is not None:
        scenario_row["description"] = description
    chains = _read_chain_files(data_dir, scenario_name, issues)


    aux_files = [
        *_merged_override_files(data_dir, scenario_dirs, "aux"),
        *_merged_override_files(data_dir, scenario_dirs, "series", include_shared=False),
        *_merged_override_files(data_dir, scenario_dirs, "prices"),
    ]
    aux_rows = _read_aux_files(aux_files, issues, money_resolver=money_resolver)

    _derive_opening_balances(model_rows, aux_rows, scenario_row, issues)
    _expand_account_growth(
        accounts, model_rows, scheduled_rows, scenario_row, aux_rows, issues,
        allocations=allocations, warnings=warnings,
    )

    leaves = _leaf_index(accounts, model_rows)
    _validate_accounts(accounts, issues, money_resolver=money_resolver)
    _validate_account_model_money(
        model_rows,
        issues,
        money_resolver=money_resolver,
        accounts=accounts,
    )
    _validate_scheduled_money(scheduled_rows, issues, money_resolver=money_resolver)
    _validate_scheduled(scheduled_rows, leaves, set(chains), issues)
    _validate_calendar(calendar_rows, leaves, issues)
    _validate_calendar_correlation(calendar_rows, issues, warnings)
    _validate_series_references(scheduled_rows, calendar_rows, aux_rows, chains, issues)
    _validate_chains(chains, model_rows, leaves, issues, money_resolver=money_resolver)
    _validate_rmd_rules(scheduled_rows, leaves, chains, issues)
    _validate_lifespan(lifespan_rows, persons, issues)
    _validate_year_to_date(ytd_rows, leaves, issues, money_resolver=money_resolver)

    if issues:
        raise LoadError(issues)
    # A latent factor has no date of its own (ADR-008 §4); calendar_event.date
    # is required, so it carries the scenario start as an unread placeholder.
    for row in calendar_rows:
        if row.get("kind") == registries.LATENT_FACTOR_KIND and not row.get("date"):
            row["date"] = scenario_row.get("start", "")


    if compute_stamps:
        ds = data_stamps(data_dir, scenario_name)
        if ds.dirty and not allow_dirty_data:
            raise DataDirtyRefused(list(ds.dirty_paths))
        cs = code_stamps()
    else:
        ds = EMPTY_DATA_STAMPS
        cs = EMPTY_CODE_STAMPS

    intent_dir.mkdir(parents=True, exist_ok=True)
    if intent_path.exists():
        intent_path.unlink()


    con = sqlite3.connect(intent_path)
    try:
        con.execute("PRAGMA locking_mode = EXCLUSIVE")
        con.execute("PRAGMA foreign_keys = ON")
        apply_run_schema(con)
        counts = _write_intent(
            con,
            persons=persons,
            securities=securities,
            allocations=allocations,
            accounts=accounts,
            account_model_name="",  # obsolete
            account_model_rows=model_rows,
            aux_rows=aux_rows,
            scenario_row=scenario_row,
            parents=parents,
            lineage_order=load_order_names,
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
        insert_run_metadata(
            con,
            scenario_name=scenario_name,
            status="running",
            scenario_tree_id="",
            data_repo_path="DATA_REPO",
            data_repo_name=data_repo_name or data_dir.name,
            allow_dirty_data=False,
            allow_dirty_code=False,
            run_by="TEST",
            version=cs.version,
            data_version="0",
            data_dirty=False,
            run_args={},
            engine_options={},
            code_repo_path="CODE_REPO",
            code_dirty=False,
            finished_at=_now_iso(),
        )
        con.commit()
    finally:
        con.close()

    return LoadResult(intent_path=intent_path,
                      scenario_name=scenario_name,
                      counts=counts,
                      warnings=warnings)


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
            if any((v or "").strip() for v in row.values())
        ]


_MONTH_COLUMNS: dict[str, tuple[str, ...]] = {
    "scenario.csv": ("start", "end"),
    "accounts.csv": ("opened", "closed"),
    "scheduled.csv": ("start", "end", "base_date"),
    "calendar.csv": ("date", "param_date"),
    "lifespan.csv": ("final_month",),
    "allocations.csv": ("as_of",),
}
"""Per-file columns that name a month (#34): ``YYYY-MM`` or ``YYYY-MM-DD``,
stored as that month's last day. Day-precise columns (``birthdate``, a lot's
``acquired``, a price's ``effective_date``) aren't listed and stay exact."""

_VARY_DATE_ROLES: dict[str, VaryBoundRole] = {
    "vary_low": "low", "vary_high": "high", "vary_median": "point", "vary_mode": "point",
}
"""Where a bare ``YYYY-MM`` lands in each `vary_target='date'` bound (#34)."""


def _normalize_month_columns(
    rows: list[dict[str, str]], filename: str, issues: list[str],
) -> None:
    """Rewrite ``filename``'s month columns (``_MONTH_COLUMNS``) in place to
    their canonical month-end ISO date -- or, where the engine counts days
    (``_keeps_exact_day``), a full date's own day -- reporting and blanking
    any that don't parse."""
    for i, row in enumerate(rows, start=2):
        for col in _MONTH_COLUMNS.get(filename, ()):
            raw = row.get(col) or ""
            if not raw:
                continue
            exact = _keeps_exact_day(filename, col, row)
            try:
                if exact and len(raw) > len("YYYY-MM"):
                    row[col] = parse_day(raw).isoformat()
                elif exact == "required":
                    issues.append(
                        f"{filename} row {i}: {col} '{raw}' needs a full YYYY-MM-DD: "
                        "a biweekly rule's start is its first payday, the 14-day anchor"
                    )
                    row[col] = ""
                else:
                    row[col] = month_end_iso(raw)
            except ValueError:
                issues.append(f"{filename} row {i}: {col} '{raw}' is not {MONTH_FORMAT_HINT}")
                # Blank (NULL) from here on: later stages read these columns
                # before ``issues`` is raised, and must not trip on the raw text.
                row[col] = ""


def _keeps_exact_day(filename: str, col: str, row: dict[str, str]) -> str | None:
    """Whether a month column's full date keeps its day, because the engine
    counts days there: ``'required'`` (a bare month is an error), ``'kept'``
    (a bare month is still the month's end), or ``None`` (always month-end).

    A biweekly rule's ``start`` anchors its 14-day stride and its ``end``
    clips it; a ``retire_scheduled`` event's date becomes its rule's ``end``
    (ADR-006 §2: the day matters within its month)."""
    if filename == "scheduled.csv" and (row.get("cadence") or "") == "biweekly":
        return {"start": "required", "end": "kept"}.get(col)
    if filename == "calendar.csv" and col == "date" and row.get("kind") == "retire_scheduled":
        return "kept"
    return None


def _normalize_vary_dates(rows: list[dict[str, str]]) -> None:
    """Rewrite a `vary_target='date'` row's bounds in place to exact ISO dates
    (``_VARY_DATE_ROLES``). Left as-is when unparseable, for
    ``_parse_vary_params`` to report in the row's own context."""
    for row in rows:
        if (row.get("vary_target") or "").strip() != "date":
            continue
        for col, role in _VARY_DATE_ROLES.items():
            raw = (row.get(col) or "").strip()
            if raw:
                try:
                    row[col] = vary_date_iso(raw, role)
                except ValueError:
                    pass


def _find_override_file(
    data_dir: Path, scenario_dirs: list[Path], subfolder: str, filename: str,
) -> Path | None:
    """The most specific existing ``<dir>/<subfolder>/<filename>``: the scenario's
    own folder, then each ancestor's (``scenario_dirs``, most specific first),
    then the data repo's shared folder."""
    for d in (*scenario_dirs, data_dir):
        candidate = d / subfolder / filename
        if candidate.is_file():
            return candidate
    return None


def _merged_override_files(
    data_dir: Path, scenario_dirs: list[Path], subfolder: str, *, include_shared: bool = True,
) -> list[Path]:
    """Every ``<subfolder>/*.csv`` across the scenario's lineage and, if
    ``include_shared``, the shared data repo root, one path per filename: a
    more specific tier's file (the scenario's own, then its parent's, ...)
    replaces a less specific one of the same name wholesale, not merged row by
    row.

    ``series/`` is scenario-only (``include_shared=False``): growth rates and
    inflation series are per-scenario projections, not tax-law facts, so there
    is no shared ``series/`` at the data repo root -- what's "shared" is
    whatever the root scenario's own ``series/`` provides, inherited like any
    other per-scenario file (ADR-003 §4.1d).
    """
    merged: dict[str, Path] = {}
    tiers = (*((data_dir,) if include_shared else ()), *reversed(scenario_dirs))
    for d in tiers:
        folder = d / subfolder
        if folder.is_dir():
            for path in folder.glob("*.csv"):
                merged[path.name] = path
    return [merged[name] for name in sorted(merged)]


def _scenario_lineage_names(scenarios_dir: Path, scenario_name: str) -> list[str]:
    """``scenario_name`` and every scenario it inherits from, most specific
    first (the reverse of ``lineage.load_order``). Tolerant of read problems
    -- the main scenario read reports them properly; this is only used to seed
    override search paths."""
    return load_order(scenarios_dir, scenario_name)[::-1]


def _read_aux_files(
    csv_paths: list[Path],
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
) -> list[tuple[str, str | None, dict[str, Any], str]]:
    out: list[tuple[str, str | None, dict[str, Any], str]] = []
    for csv_path in csv_paths:
        stem = csv_path.stem
        if "__" in stem:
            kind, series = stem.split("__", 1)
        else:
            kind, series = stem, None
        if kind not in registries.AUX_KIND_COLUMNS:
            issues.append(f"aux/{csv_path.name}: unknown kind '{kind}'")
            continue
        cols = registries.AUX_KIND_COLUMNS[kind]
        money_cols = registries.AUX_MONEY_COLUMNS.get(kind, frozenset())
        micros_cols = registries.AUX_DOLLAR_MICROS_COLUMNS.get(kind, {})
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
                if c in micros_cols:
                    try:
                        payload[micros_cols[c]] = _dollars_to_micros(raw)
                    except (ValueError, InvalidOperation) as e:
                        issues.append(f"aux/{csv_path.name} row {i} ({c}): {e}")
                        row_ok = False
                        break
                elif c in money_cols:
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
            effective = row["effective_date"]
            # Prices are dated by their actual close, so keep the day; every
            # other aux and series row takes effect for a whole month (#34).
            try:
                if kind == "security_price":
                    parse_day(effective)
                else:
                    effective = month_end_iso(effective)
            except ValueError:
                expected = "YYYY-MM-DD" if kind == "security_price" else MONTH_FORMAT_HINT
                issues.append(
                    f"aux/{csv_path.name} row {i}: effective_date "
                    f"'{effective}' is not {expected}"
                )
                row_ok = False
            if row_ok:
                out.append((kind, series, payload, effective))
    return out


def _opt_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    v = raw.strip()
    return int(v) if v != "" else None


def _opt_float(raw: str | None) -> float | None:
    return float(raw) if raw not in (None, "") else None


def _vary_bound_writer(vary_target: str | None) -> Callable[[str | None], str | None]:
    """A `vary_low`/`vary_high`/etc. cell, stored in the target column's own
    unit (ADR-006): the row's own `amount` int, as text, for `vary_target`
    `'amount'`; the ISO date string as-is for `'date'`."""
    if vary_target == "amount":
        return lambda raw: None if _opt_int(raw) is None else str(_opt_int(raw))
    return lambda raw: (raw or "").strip() or None


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

class _LeafIndex(NamedTuple):
    """Every valid leaf reference (one per lot) and what it resolves to."""
    names: frozenset[str]
    account_of: dict[str, str]
    lot_labels: dict[str, list[str]]
    investment_accounts: frozenset[str]
    leaf_of: dict[tuple[str, str], str]


def _leaf_index(
    accounts: list[dict[str, str]], model_rows: list[dict[str, str]],
) -> _LeafIndex:
    labels: dict[str, list[str]] = {}
    for m in model_rows:
        labels.setdefault(m.get("account_name", ""), []).append(
            m.get("lot") or registries.DEFAULT_LOT
        )
    names: set[str] = set()
    account_of: dict[str, str] = {}
    lot_labels: dict[str, list[str]] = {}
    leaf_of: dict[tuple[str, str], str] = {}
    for a in accounts:
        name = a.get("name", "")
        lot_labels[name] = labels.get(name) or [registries.DEFAULT_LOT]
        for lot in lot_labels[name]:
            leaf = registries.leaf_chain_name(name, lot, a.get("subkind", ""))
            names.add(leaf)
            account_of[leaf] = name
            leaf_of[(name, lot)] = leaf
    return _LeafIndex(
        frozenset(names), account_of, lot_labels,
        frozenset(
            a["name"] for a in accounts
            if a.get("name") and a.get("subkind") in INVESTMENT_ASSET_SUBKINDS
        ),
        leaf_of,
    )


def _resolve_ref(
    row: dict[str, str], account_col: str, lot_col: str,
    leaves: _LeafIndex, chain_names: set[str] | frozenset[str],
) -> str | None:
    """Fold ``row[lot_col]`` into ``row[account_col]``, leaving the internal name
    of that lot's leaf chain there, and return why it can't be resolved (or None).

    An interior chain takes no lot. An investment account must name its lot; any
    other account may leave it blank, meaning ``default``.
    """
    ref, lot = row.get(account_col, ""), row.get(lot_col, "")
    if ref in chain_names:
        if lot:
            return f"'{ref}' is a chain, so {lot_col} must be blank"
        return None
    labels = leaves.lot_labels.get(ref)
    if labels is None:
        return f"'{ref}' is not a known account or chain"
    if not lot:
        if ref in leaves.investment_accounts:
            return (
                f"'{ref}' is an investment account and needs a {lot_col} "
                f"(its lots: {', '.join(labels)})"
            )
        lot = registries.DEFAULT_LOT
    leaf = leaves.leaf_of.get((ref, lot))
    if leaf is None:
        return f"'{ref}' has no lot '{lot}' (its lots: {', '.join(labels)})"
    row[account_col] = leaf
    return None


def _ref_problem(ref: str, leaves: _LeafIndex, chain_names: set[str] | frozenset[str]) -> str | None:
    """Why ``ref`` (a bare account or chain name with no lot column) is not a
    valid reference, or None if it is."""
    if ref and (ref in leaves.names or ref in chain_names):
        return None
    if ref in leaves.investment_accounts:
        return f"'{ref}' is an investment account; name a chain that lists its lot"
    return f"'{ref}' is not a known account or chain"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


_NAME_WORD_WIDTH = 4


def _abbreviate(name: str, width: int) -> str:
    """``name`` as a slug with each word cut to its first ``width`` letters."""
    return "_".join(word[:width] for word in _slug(name).split("_"))


def growth_rule_name(account: str, lot: str, taken: Collection[str] = ()) -> str:
    """Name of the rule generated for one lot's growth: ``growth_`` and the account
    and lot with each word trimmed to four letters, so ``Bill Roth Rollover`` /
    ``settlement`` is ``growth_bill_roth_roll_sett``. If that is already ``taken``,
    words are trimmed less until it is free (the full names, at the widest)."""
    widest = max(len(word) for word in _slug(f"{account}_{lot}").split("_"))
    for width in range(_NAME_WORD_WIDTH, max(widest, _NAME_WORD_WIDTH) + 1):
        parts = [_abbreviate(account, width)]
        if lot != registries.DEFAULT_LOT:
            parts.append(_abbreviate(lot, width))
        name = "growth_" + "_".join(parts)
        if name not in taken:
            return name
    return name


def _expand_account_growth(
    accounts: list[dict[str, str]],
    model_rows: list[dict[str, str]],
    scheduled_rows: list[dict[str, str]],
    scenario_row: dict[str, str],
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]],
    issues: list[str],
    *,
    allocations: Iterable[dict[str, str]] = (),
    warnings: list[str] | None = None,
) -> None:
    """Turn each lot's growth series into a ``percent_of_balance`` rule.

    A lot's series is its own ``series``, else a rate series named for its
    ``symbol``, else the blend of its symbol's allocation slices
    (:func:`_allocation_blend`), else its account's ``series`` (ADR-005 §5).
    The income side is
    the built-in account for the account's ``tax_character`` (ADR-003 §2.2),
    created here on first use so scenarios never wire it up by hand. Rules are
    named by :func:`growth_rule_name`.
    """
    account_names = {row.get("name", "") for row in accounts}
    rule_names = {row.get("name", "") for row in scheduled_rows}
    rate_series = {series for kind, series, *_ in aux_rows if kind == "rate"}
    weight_sets = _allocation_weight_sets(allocations)
    lots_by_account: dict[str, list[dict[str, str]]] = {}
    for m in model_rows:
        lots_by_account.setdefault(m.get("account_name", ""), []).append(m)
    start = scenario_row.get("start", "")
    for i, row in enumerate(accounts, start=2):
        name = row.get("name", "")
        account_series = row.get("series", "")
        character = row.get("tax_character", "")
        # (lot, series, blend): a blend replaces the series when it is set.
        lot_series: list[tuple[str, str, AllocationBlend | None]] = []
        for m in lots_by_account.get(name) or [{}]:
            symbol = m.get("symbol") or ""
            lot = m.get("lot") or registries.DEFAULT_LOT
            series = m.get("series") or (symbol if symbol in rate_series else "")
            blend = None
            if not series and symbol in weight_sets:
                blend = _allocation_blend(
                    symbol, weight_sets[symbol], rate_series,
                    f"account_values.csv ({name} / {lot})", warnings,
                )
            lot_series.append((lot, series or ("" if blend else account_series), blend))
        if not any(series or blend for _, series, blend in lot_series):
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
        for lot, series, blend in lot_series:
            if not series and not blend:
                continue
            rule_name = growth_rule_name(name, lot, rule_names)
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
                "to_lot": lot,
                "cadence": "monthly",
                "amount_rule_kind": "percent_of_balance",
                "series": series,
                "params_json": (
                    json.dumps({"blend": blend}, separators=(",", ":")) if blend else ""
                ),
                "tax_character": counterparty.tax_character,
                "start": start,
                "description": (
                    f"Growth on {name}"
                    + (f" / {lot}" if lot != registries.DEFAULT_LOT else "")
                    + f" ({_describe_blend(blend) if blend else series})"
                ),
            })


type AllocationBlend = list[list[Any]]
"""A lot's growth blend (#78): ``[[as_of, [[series, weight_micros], ...]], ...]``,
oldest set first, read by ``engine._amount_percent_of_balance``."""


def _allocation_weight_sets(
    allocations: Iterable[dict[str, str]],
) -> dict[str, list[tuple[str, dict[str, int]]]]:
    """Symbol to its allocation sets, oldest first: ``(as_of, {asset_class: weight
    micros})``. Rows ``_validate_allocations`` rejects are left to its issues."""
    sets: dict[str, dict[str, dict[str, int]]] = {}
    for row in allocations:
        symbol, asset_class = row.get("symbol", ""), row.get("asset_class", "")
        if not symbol or asset_class not in registries.ASSET_CLASSES:
            continue
        try:
            weight = _percent_to_micros(row.get("weight", ""))
        except ValueError:
            continue
        sets.setdefault(symbol, {}).setdefault(row.get("as_of", ""), {})[asset_class] = weight
    return {symbol: sorted(by_date.items()) for symbol, by_date in sets.items()}


def _allocation_blend(
    symbol: str,
    weight_sets: list[tuple[str, dict[str, int]]],
    rate_series: Collection[str | None],
    where: str,
    warnings: list[str] | None,
) -> AllocationBlend | None:
    """The rate blend a lot holding ``symbol`` grows at (#78, ADR-005 §5), or None.

    Each asset class the symbol holds reads the rate series ``<symbol>.<class>``,
    else the class's own series (``stock_us``, ``bond``, ...). Only when every class
    resolves is there a blend; when none does the lot falls back to its account's
    series as before. Some but not all is a warning, and also falls back: a series
    that happens to share a class's name (a cash account's ``cash`` rate, say)
    mustn't stop a scenario that never meant to blend from loading.
    """
    # A class at 0% in every set is never read, so it needs no series.
    classes = sorted({c for _, weights in weight_sets for c, w in weights.items() if w})
    slice_series = {
        c: next((s for s in (f"{symbol}.{c}", c) if s in rate_series), None)
        for c in classes
    }
    missing = [c for c, s in slice_series.items() if s is None]
    if missing:
        if len(missing) < len(classes) and warnings is not None:
            warnings.append(
                f"{where}: {symbol} has a rate series for "
                + ", ".join(c for c in classes if c not in missing)
                + " but not for " + ", ".join(missing)
                + f" (rate__{missing[0]}.csv or rate__{symbol}.{missing[0]}.csv),"
                " so it grows at its account's series instead of its allocation's blend"
            )
        return None
    return [
        [as_of, [[slice_series[c], w] for c, w in sorted(weights.items()) if w]]
        for as_of, weights in weight_sets
    ]


def _describe_blend(blend: AllocationBlend) -> str:
    """``60% stock_us + 40% bond``, of the latest set (a glide path notes it has more)."""
    _, latest = blend[-1]
    text = " + ".join(f"{w / 10_000:g}% {s}" for s, w in latest)
    return text + (f", {len(blend)} dated sets" if len(blend) > 1 else "")


def _validate_birthdates(rows: list[dict[str, str]], issues: list[str]) -> None:
    """A birthdate is day-precise (ages turn on it), so a strict YYYY-MM-DD."""
    for i, row in enumerate(rows, start=2):
        birthdate = row.get("birthdate", "")
        if birthdate:
            try:
                parse_day(birthdate)
            except ValueError:
                issues.append(
                    f"persons.csv row {i} ({row.get('id') or '?'}): birthdate "
                    f"'{birthdate}' is not YYYY-MM-DD"
                )


def _validate_securities(rows: list[dict[str, str]], issues: list[str]) -> None:
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):
        symbol, name = row.get("symbol", ""), row.get("name", "")
        if not symbol:
            issues.append(f"securities.csv row {i}: missing symbol")
            continue
        if symbol in seen:
            issues.append(f"securities.csv row {i} ({symbol}): duplicate symbol")
        seen.add(symbol)
        if not name:
            issues.append(f"securities.csv row {i} ({symbol}): missing name")


def _validate_allocations(rows: list[dict[str, str]], issues: list[str]) -> None:
    """Each (symbol, as_of) is a set of asset-class weights that sums to 100%."""
    sets: dict[tuple[str, str], int] = {}
    seen: set[tuple[str, str, str]] = set()
    for i, row in enumerate(rows, start=2):
        symbol, as_of = row.get("symbol", ""), row.get("as_of", "")
        asset_class = row.get("asset_class", "")
        where = f"allocations.csv row {i} ({symbol or '?'})"
        if not symbol:
            issues.append(f"{where}: missing symbol")
            continue
        if as_of:
            try:
                date.fromisoformat(as_of)
            except ValueError:
                continue  # already reported by _normalize_month_columns
        if asset_class not in registries.ASSET_CLASSES:
            issues.append(
                f"{where}: unknown asset_class '{asset_class}'; "
                f"allowed: {', '.join(registries.ASSET_CLASSES)}"
            )
        elif (symbol, as_of, asset_class) in seen:
            issues.append(f"{where}: duplicate {asset_class} for as_of '{as_of}'")
        else:
            seen.add((symbol, as_of, asset_class))
        try:
            weight = _percent_to_micros(row.get("weight", ""))
        except ValueError as e:
            issues.append(f"{where}: weight {e}")
            continue
        sets[(symbol, as_of)] = sets.get((symbol, as_of), 0) + weight
    for (symbol, as_of), total in sets.items():
        if total != 1_000_000:
            when = f" as of {as_of}" if as_of else ""
            issues.append(
                f"allocations.csv ({symbol}{when}): weights add up to {total / 10_000:g}%, not 100%"
            )


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
        if ":" in name:
            issues.append(f"accounts.csv row {i} ({name}): name may not contain ':'")
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
        moved = [c for c in registries.CASH_MANAGEMENT_COLUMNS if row.get(c)]
        if moved:
            issues.append(
                f"accounts.csv row {i} ({name}): {', '.join(moved)} moved to "
                "account_values.csv, where each lot has its own"
            )
        series = row.get("series", "")
        if series:
            _validate_no_spaces("accounts.csv", i, name, "series", series, issues)
        _validate_tags("accounts.csv", i, name, row.get("tags", ""), issues)


def _validate_scheduled(
    rows: list[dict[str, str]], leaves: _LeafIndex,
    chain_names: set[str], issues: list[str],
) -> None:
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
        if kind == "on_death" and tc != "tax_free":
            issues.append(
                f"scheduled.csv row {i} ({name}): on_death requires "
                f"tax_character='tax_free' (got {tc!r}) -- death-triggered "
                "liquidations/payouts are not taxable events"
            )
        if kind == "home_sale" and tc != "none":
            issues.append(
                f"scheduled.csv row {i} ({name}): home_sale requires "
                f"tax_character='none' (got {tc!r}) -- the row's own "
                "tax_character is unused; _apply_home_sale posts its own "
                "return_of_capital / excluded_gain / ltcg legs"
            )
        tax_year = row.get("tax_year", "")
        if tax_year:
            try:
                if len(tax_year) != 4:
                    raise ValueError
                int(tax_year)
            except ValueError:
                issues.append(f"scheduled.csv row {i} ({name}): tax_year must be a four-digit year")
        for retired, merged in (("base_amount", "amount"), ("inflation_series", "series")):
            if row.get(retired, "") != "":
                issues.append(
                    f"scheduled.csv row {i} ({name}): '{retired}' was merged into "
                    f"'{merged}' (schema v12); move the value and drop the column"
                )
        series = row.get("series", "")
        if series != "" and kind not in registries.SERIES_RULE_KINDS:
            issues.append(
                f"scheduled.csv row {i} ({name}): amount_rule_kind '{kind}' does not "
                f"use 'series'; it is only read by "
                f"{', '.join(sorted(registries.SERIES_RULE_KINDS))}"
            )
        if series:
            _validate_no_spaces("scheduled.csv", i, name, "series", series, issues)
        for col, lot_col in (("from_account", "from_lot"), ("to_account", "to_lot")):
            problem = _resolve_ref(row, col, lot_col, leaves, chain_names)
            if problem:
                issues.append(f"scheduled.csv row {i} ({name}): {col} {problem}")
        params_json = row.get("amount_rule_params_json") or "{}"
        try:
            json.loads(params_json)
        except json.JSONDecodeError as e:
            issues.append(
                f"scheduled.csv row {i} ({name}): amount_rule_params_json invalid: {e}"
            )
        ytd_amount = row.get("ytd_amount", "")
        ytd_preroll = row.get("ytd_preroll", "")
        if ytd_preroll not in ("", "0"):
            if kind != "fixed_amount":
                issues.append(
                    f"scheduled.csv row {i} ({name}): ytd_preroll only "
                    "valid for amount_rule_kind='fixed_amount'"
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
    for i, row in enumerate(rows, start=2):
        name = row.get("account_name", "")
        if not name:
            issues.append(f"account_values.csv row {i}: missing account_name")
            continue
        lot = row.get("lot") or registries.DEFAULT_LOT
        if ":" in lot:
            issues.append(
                f"account_values.csv row {i} ({name}): lot '{lot}' may not contain ':'"
            )
        if name not in account_names:
            issues.append(
                f"account_values.csv row {i} ({name}): account_name not in accounts.csv"
            )
        ob = row.get("opening_balance", "")
        if ob == "":
            issues.append(
                f"account_values.csv row {i} ({name}): opening_balance is required "
                "(or give quantity, symbol and a security_price)"
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
        _validate_cash_management(row, f"account_values.csv row {i} ({name})",
                                  issues, money_resolver=money_resolver)
        pyeb = row.get("prior_year_end_balance", "")
        if pyeb != "":
            try:
                _dollars_to_cents(pyeb, resolver=money_resolver)
            except ValueError as e:
                issues.append(
                    f"account_values.csv row {i} ({name}): prior_year_end_balance {e}"
                )
        if row.get("symbol") in account_names:
            issues.append(
                f"account_values.csv row {i} ({name}): symbol '{row['symbol']}' is also "
                "an account name, so its rate series would be ambiguous"
            )
        symbol = row.get("symbol", "")
        if symbol:
            _validate_no_spaces("account_values.csv", i, name, "symbol", symbol, issues)
        lot_series = row.get("series", "")
        if lot_series:
            _validate_no_spaces("account_values.csv", i, name, "series", lot_series, issues)
        acquired = row.get("acquired", "")
        if acquired:
            try:
                parse_day(acquired)
            except ValueError:
                issues.append(
                    f"account_values.csv row {i} ({name}): acquired '{acquired}' "
                    "is not YYYY-MM-DD"
                )
        quantity = row.get("quantity", "")
        if quantity:
            try:
                if _quantity_to_micros(quantity) < 0:
                    raise ValueError
            except (ValueError, InvalidOperation):
                issues.append(
                    f"account_values.csv row {i} ({name}): quantity '{quantity}' "
                    "must be a non-negative number"
                )

    rows_by_account: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        rows_by_account.setdefault(row.get("account_name", ""), []).append(row)
    for a in accounts:
        lots = rows_by_account.get(a.get("name", ""), [])
        if len(lots) < 2:
            continue
        name = a["name"]
        with_pyeb = sum(1 for r in lots if r.get("prior_year_end_balance"))
        if 0 < with_pyeb < len(lots):
            issues.append(
                f"account_values.csv ({name}): prior_year_end_balance must be set "
                "on every lot of an account or on none"
            )


def _validate_cash_management(
    row: dict[str, str], where: str, issues: list[str],
    *, money_resolver: _MoneyReferenceResolver,
) -> None:
    """The per-lot balance-management columns, apart from the chain references
    (which need the chain names; see ``_validate_chains``)."""
    for column in ("minimum_balance", "maximum_balance", "refill_target", "refill_amount"):
        value = row.get(column, "")
        if value:
            try:
                _dollars_to_cents(value, resolver=money_resolver)
            except ValueError as e:
                issues.append(f"{where}: {column} {e}")
    target, amount = row.get("refill_target", ""), row.get("refill_amount", "")
    if target and amount:
        issues.append(f"{where}: refill_target and refill_amount are mutually exclusive; set at most one")
    if (target or amount) and not row.get("refill_chain", ""):
        issues.append(f"{where}: refill_target/refill_amount requires refill_chain to be set")
    for chain_col, lot_col in (("refill_chain", "refill_lot"), ("overflow_chain", "overflow_lot")):
        if row.get(lot_col) and not row.get(chain_col):
            issues.append(f"{where}: {lot_col} set without {chain_col}")


def _dollars_to_micros(raw: str) -> int:
    """Decimal dollars to integer micros of a dollar; positive, at most 6 places."""
    value = Decimal(raw.strip().lstrip("$").replace(",", "")) * 1_000_000
    if value <= 0 or value != value.to_integral_value():
        raise ValueError(f"'{raw}' must be a positive amount with at most 6 decimal places")
    return int(value)


def _derive_opening_balances(
    model_rows: list[dict[str, str]],
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]],
    scenario_row: dict[str, str],
    issues: list[str],
) -> None:
    """Fill a lot's blank ``opening_balance`` from ``quantity`` x the latest
    ``security_price`` for its ``symbol`` on or before the scenario start
    (ADR-005 §4). A given balance is left alone: it stays authoritative. Likewise a
    blank ``prior_year_end_balance`` in an account that uses it (see below)."""
    start = scenario_row.get("start", "")
    prices: dict[str, list[tuple[str, int]]] = {}
    for kind, series, payload, effective in aux_rows:
        if kind == "security_price":
            prices.setdefault(series or "", []).append((effective, payload["price_micros"]))
    for i, row in enumerate(model_rows, start=2):
        if row.get("opening_balance") or not row.get("quantity"):
            continue
        name, symbol = row.get("account_name", ""), row.get("symbol", "")
        try:
            quantity = _quantity_to_micros(row["quantity"])
        except (ValueError, InvalidOperation):
            continue
        if not symbol:
            issues.append(
                f"account_values.csv row {i} ({name}): quantity without "
                "opening_balance needs a symbol"
            )
            continue
        known = [p for p in prices.get(symbol, []) if p[0] <= start]
        if not known:
            issues.append(
                f"account_values.csv row {i} ({name}): no security_price for "
                f"'{symbol}' on or before {start} (aux/security_price__{symbol}.csv)"
            )
            continue
        row["opening_balance"] = _price_value(quantity, max(known)[1])

    # An account that uses prior_year_end_balance (it is set on some lot) needs it
    # on every lot, so a blank one is derived the same way at the prior Dec 31, from
    # today's share count (which ignores buys and reinvestment since; a stated value wins).
    if not start:
        return
    prior_dec = f"{int(start[:4]) - 1}-12-31"
    used = {r.get("account_name", "") for r in model_rows if r.get("prior_year_end_balance")}
    for row in model_rows:
        symbol = row.get("symbol", "")
        if (row.get("prior_year_end_balance") or row.get("account_name", "") not in used
                or not symbol or not row.get("quantity")):
            continue
        known = [p for p in prices.get(symbol, []) if p[0] <= prior_dec]
        try:
            quantity = _quantity_to_micros(row["quantity"])
        except (ValueError, InvalidOperation):
            continue
        if known:
            row["prior_year_end_balance"] = _price_value(quantity, max(known)[1])


def _price_value(quantity_micros: int, price_micros: int) -> str:
    """Dollars for a share count times a price, rounded half up to the cent."""
    cents = (quantity_micros * price_micros + 5_000_000_000) // 10_000_000_000
    return f"{cents // 100}.{cents % 100:02d}"


def _quantity_to_micros(raw: str) -> int:
    return int((Decimal(raw.strip().replace(",", "")) * 1_000_000).to_integral_value())


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


def _chain_leaf_names(
    chain: str, chains: dict[str, tuple[str, list[dict[str, str]]]], seen: set[str] | None = None,
) -> set[str]:
    """Leaf (lot) names reachable through ``chain``'s steps, recursively."""
    seen = seen if seen is not None else set()
    if chain in seen or chain not in chains:
        return set()
    seen.add(chain)
    leaves: set[str] = set()
    for row in chains[chain][1]:
        member = row.get("account_name", "")
        leaves |= _chain_leaf_names(member, chains, seen) if member in chains else {member}
    return leaves


def _validate_rmd_rules(
    rows: list[dict[str, str]],
    leaves: _LeafIndex,
    chains: dict[str, tuple[str, list[dict[str, str]]]],
    issues: list[str],
) -> None:
    """An ``rmd`` rule's amount comes from its *subject account* (all of its lots),
    and the money comes from ``from_account``. On an account with several lots
    the source must be an explicit chain, so which lots fund the RMD, and in what
    order, is written down; the chain may only draw from the subject's own lots
    (ADR-005 §8). Run after the rows' references have been resolved."""
    for i, row in enumerate(rows, start=2):
        if row.get("amount_rule_kind") != "rmd":
            continue
        where = f"scheduled.csv row {i} ({row.get('name', '')})"
        try:
            params = json.loads(row.get("params_json") or "{}")
        except json.JSONDecodeError:
            continue  # reported elsewhere
        subject = params.get("subject_account")
        source = row.get("from_account", "")
        if subject is not None and subject not in leaves.lot_labels:
            issues.append(f"{where}: subject_account '{subject}' is not an account")
            continue
        if source in leaves.account_of:
            account = leaves.account_of[source]
            if subject is not None and subject != account:
                issues.append(
                    f"{where}: subject_account '{subject}' is not the account "
                    f"'{account}' that from_account draws from"
                )
            elif len(leaves.lot_labels[account]) > 1:
                issues.append(
                    f"{where}: rmd on multi-lot account '{account}' needs a chain as "
                    "from_account, listing the lots it draws from in order, and "
                    f"params_json {{\"subject_account\": \"{account}\"}}"
                )
        elif source in chains:
            if subject is None:
                issues.append(
                    f"{where}: an rmd drawing from chain '{source}' needs "
                    "params_json {\"subject_account\": <the account the RMD is for>}"
                )
                continue
            outside = sorted(
                leaf for leaf in _chain_leaf_names(source, chains)
                if leaves.account_of.get(leaf) != subject
            )
            if outside:
                issues.append(
                    f"{where}: chain '{source}' draws from {', '.join(outside)}, "
                    f"which are not lots of the subject account '{subject}'"
                )


def _validate_year_to_date(
    rows: list[dict[str, str]],
    leaves: _LeafIndex,
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
) -> None:
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):
        name = row.get("name", "")
        if not name:
            issues.append(f"year_to_date.csv row {i}: missing name")
            continue
        if name in seen:
            issues.append(f"year_to_date.csv row {i} ({name}): duplicate name")
        seen.add(name)
        for col, lot_col in (("from_account", "from_lot"), ("to_account", "to_lot")):
            problem = _resolve_ref(row, col, lot_col, leaves, frozenset())
            if problem:
                issues.append(f"year_to_date.csv row {i} ({name}): {col} {problem}")
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


_VARY_BOUND_COLUMNS = ("vary_low", "vary_high", "vary_median", "vary_sd", "vary_mode")
"""vary_* parameters in the target column's own unit (ints, or ISO dates)."""

_VARY_SHAPE_COLUMNS = ("vary_skew", "vary_tailweight")
"""Unitless truncated_normal shape parameters (#31)."""


def _validate_calendar(
    rows: list[dict[str, str]], leaves: _LeafIndex, issues: list[str],
) -> None:
    seen_names: dict[str, int] = {}
    for i, row in enumerate(rows, start=2):
        kind = row.get("kind", "")
        if kind not in registries.CALENDAR_EVENT_KINDS:
            issues.append(f"calendar.csv row {i}: unknown event kind '{kind}'")
        if kind == "one_off_transfer":
            for col, lot_col in (("account_1", "lot_1"), ("account_2", "lot_2")):
                problem = _resolve_ref(row, col, lot_col, leaves, frozenset())
                if problem:
                    issues.append(f"calendar.csv row {i}: {col} {problem}")
        params_json = row.get("params_json") or "{}"
        try:
            json.loads(params_json)
        except json.JSONDecodeError as e:
            issues.append(f"calendar.csv row {i}: params_json invalid: {e}")
        series = row.get("series", "")
        if series:
            _validate_no_spaces("calendar.csv", i, row.get("scheduled_name") or kind,
                                "series", series, issues)
        _validate_tags("calendar.csv", i, row.get("scheduled_name") or kind,
                       row.get("tags", ""), issues)
        name = (row.get("name") or "").strip()
        if name:
            if name in seen_names:
                issues.append(
                    f"calendar.csv row {i}: duplicate name '{name}' "
                    f"(first seen on row {seen_names[name]})"
                )
            else:
                seen_names[name] = i
        _validate_calendar_vary(row, i, kind, name, issues)


def _validate_calendar_vary(
    row: dict[str, str], i: int, kind: str, name: str, issues: list[str],
) -> None:
    """ADR-006: a row with `vary_distribution` set has a `vary_target` in
    `VARY_TARGETS`, an amount target only on a kind with a real amount to
    vary, only the columns its distribution actually reads, a fully
    resolvable and valid-ordered set of bounds, and (only for
    `vary_target='amount'` on a `set_rate` row) an optional `vary_ramp_months`.
    """
    where = f"calendar.csv row {i} ({name or '?'})"
    for old, new in registries.RENAMED_CALENDAR_COLUMNS.items():
        if (row.get(old) or "").strip():
            issues.append(
                f"{where}: {old} was renamed to {new} (now the median of the truncated "
                f"distribution, so the base case is exactly the authored value); "
                f"rename the column"
            )
    distribution = (row.get("vary_distribution") or "").strip()
    target = (row.get("vary_target") or "").strip()
    ramp_months = (row.get("vary_ramp_months") or "").strip()
    if not distribution:
        if target or ramp_months or any(
            row.get(c) for c in (*_VARY_BOUND_COLUMNS, *_VARY_SHAPE_COLUMNS)
        ):
            issues.append(f"{where}: vary_* columns set without vary_distribution")
        return
    if not name:
        issues.append(f"{where}: name is required when vary_distribution is set")
    if distribution not in registries.ENSEMBLE_DISTRIBUTIONS:
        issues.append(f"{where}: unknown vary_distribution '{distribution}'")
        return
    if target not in registries.VARY_TARGETS:
        issues.append(
            f"{where}: vary_target must be one of {sorted(registries.VARY_TARGETS)} "
            f"when vary_distribution is set (got '{target}')"
        )
        return
    if target == "amount" and kind not in registries.AMOUNT_VARYING_KINDS:
        issues.append(
            f"{where}: vary_target='amount' is only valid on kind in "
            f"{sorted(registries.AMOUNT_VARYING_KINDS)} (got kind='{kind}')"
        )
    if target == "date" and distribution == "truncated_normal":
        issues.append(
            f"{where}: truncated_normal is not valid with vary_target='date' -- "
            "its vary_sd is a numeric spread, not a date span"
        )
        return

    parsed, shape, parse_ok = _parse_vary_params(row, distribution, target, where, issues)
    effective_low = (
        _check_vary_distribution(row, distribution, parsed, shape, where, issues)
        if parse_ok or distribution != "truncated_normal" else None
    )

    if (
        target == "amount" and kind == "one_off_transfer"
        and effective_low is not None and effective_low <= 0
    ):
        # A transfer amount is a real movement of money whose direction is
        # fixed by account_1/account_2; a nonpositive draw would reverse or
        # silently omit it, not just draw an unlikely value. Checked on the
        # *effective* low (given directly, or resolved from mean/sd) so this
        # one rule covers both distributions.
        issues.append(
            f"{where}: one_off_transfer requires vary_low > 0 (given directly, or "
            "implied by vary_median/vary_sd) -- a transfer amount must stay positive"
        )

    if ramp_months:
        if not (target == "amount" and kind == "set_rate"):
            issues.append(
                f"{where}: vary_ramp_months is only valid with vary_target='amount' "
                "on a kind='set_rate' row"
            )
        else:
            try:
                if int(ramp_months) <= 0:
                    raise ValueError
            except ValueError:
                issues.append(f"{where}: vary_ramp_months must be a positive integer")


_FACTOR_FORBIDDEN_COLUMNS = (
    "scheduled_name", "account_1", "lot_1", "account_2", "lot_2", "series", "amount",
    "param_date", "tax_character", "params_json", "tags",
    "vary_target", "vary_distribution", "vary_low", "vary_high",
    "vary_median", "vary_sd", "vary_skew", "vary_tailweight", "vary_mode", "vary_ramp_months",
)
"""Columns a ``latent_factor`` row must leave blank: it has no date, amount
or series meaning, and its value is a standard normal score, not a draw from
an authored distribution (ADR-008 §3)."""


def _validate_calendar_correlation(
    rows: list[dict[str, str]], issues: list[str], warnings: list[str],
) -> None:
    """ADR-008 §4, over the merged calendar (so a child scenario's row can
    name a parent scenario's factor): latent_factor rows are named and carry
    nothing but a description and correlation columns; the correlation
    columns come as a pair, on a varying row or a factor, with rho in
    [-1, 1]; every parent exists; there are no cycles. A factor nothing
    depends on is a warning: work in progress, or meant for child scenarios.
    ``vary_tail_dependence`` (#68) is a number >= 1 on a tree's root only; on a
    root nothing depends on it is a warning, for the same reasons."""
    nodes: dict[str, int] = {}
    parents: dict[str, str] = {}
    tails: dict[str, int] = {}
    for i, row in enumerate(rows, start=2):
        kind = row.get("kind", "")
        name = (row.get("name") or "").strip()
        where = f"calendar.csv row {i} ({name or '?'})"
        is_factor = kind == registries.LATENT_FACTOR_KIND
        varying = bool((row.get("vary_distribution") or "").strip())
        if is_factor:
            if not name:
                issues.append(f"{where}: a latent_factor row needs a name")
            stray = [c for c in _FACTOR_FORBIDDEN_COLUMNS if (row.get(c) or "").strip()]
            if stray:
                issues.append(
                    f"{where}: a latent_factor row has no {', '.join(stray)} -- only a name, "
                    "a description, and optionally vary_correlate_with/vary_correlation "
                    "or vary_tail_dependence"
                )
        if (is_factor or varying) and name:
            nodes[name] = i
        raw_nu = (row.get("vary_tail_dependence") or "").strip()
        if raw_nu:
            if not (is_factor or varying):
                issues.append(
                    f"{where}: vary_tail_dependence is only valid on a varying row "
                    "(vary_distribution set) or a latent_factor row"
                )
            else:
                try:
                    nu = float(raw_nu)
                except ValueError:
                    issues.append(f"{where}: vary_tail_dependence '{raw_nu}' is not a number")
                else:
                    if not registries.MIN_TAIL_DEPENDENCE <= nu < math.inf:  # also rejects NaN
                        issues.append(
                            f"{where}: vary_tail_dependence must be a number from "
                            f"{registries.MIN_TAIL_DEPENDENCE:g} up (got {raw_nu}); "
                            "leave it blank for the Gaussian copula"
                        )
                    elif name:
                        tails[name] = i
        parent = (row.get("vary_correlate_with") or "").strip()
        raw_rho = (row.get("vary_correlation") or "").strip()
        if not parent and not raw_rho:
            continue
        if not (is_factor or varying):
            issues.append(
                f"{where}: vary_correlate_with/vary_correlation are only valid on a varying "
                "row (vary_distribution set) or a latent_factor row"
            )
            continue
        if not parent or not raw_rho:
            issues.append(f"{where}: vary_correlate_with and vary_correlation go together")
            continue
        try:
            rho = float(raw_rho)
        except ValueError:
            issues.append(f"{where}: vary_correlation '{raw_rho}' is not a number")
            continue
        if not -1.0 <= rho <= 1.0:  # also rejects NaN
            issues.append(f"{where}: vary_correlation must be between -1 and 1 (got {raw_rho})")
            continue
        if name:
            parents[name] = parent

    for child, parent in parents.items():
        if parent not in nodes:
            issues.append(
                f"calendar.csv row {nodes[child]} ({child}): vary_correlate_with '{parent}' "
                "names no varying row or latent_factor"
            )
    reported: set[str] = set()
    for start in parents:
        path: list[str] = []
        node: str | None = start
        while node in parents and node not in path:
            path.append(node)
            node = parents[node]
        if node in path:
            cycle = path[path.index(node):]
            if not reported & set(cycle):
                reported.update(cycle)
                issues.append(
                    "calendar.csv: correlation cycle "
                    f"{' -> '.join([*cycle, cycle[0]])} (each row may have one parent, "
                    "and the chain of parents must end)"
                )
    used = set(parents.values())
    for name, i in tails.items():
        if name in parents:
            root, seen = name, {name}
            while root in parents and parents[root] not in seen:
                root = parents[root]
                seen.add(root)
            issues.append(
                f"calendar.csv row {i} ({name}): vary_tail_dependence goes on the root of a "
                f"correlation tree ('{root}' here), not on a row with a parent: it scales every "
                "row in the tree together, so it only has an effect where the whole tree shares it"
            )
        elif name not in used and rows[i - 2].get("kind") != registries.LATENT_FACTOR_KIND:
            # (An unused factor gets its own warning below.)
            warnings.append(
                f"calendar.csv row {i} ({name}): vary_tail_dependence on a row nothing correlates "
                "with affects nothing yet (it acts only on the rows of the tree below it)"
            )
    unused = [
        (name, i) for name, i in nodes.items()
        if rows[i - 2].get("kind") == registries.LATENT_FACTOR_KIND and name not in used
    ]
    if len(unused) == 1:
        name, i = unused[0]
        warnings.append(
            f"calendar.csv row {i} ({name}): latent_factor has no dependents yet, so it "
            "affects nothing (fine for work in progress, or a factor for child scenarios)"
        )
    elif unused:
        # One line, not one per factor: a scenario that defines a factor
        # structure for its children (#80) has many, and they'd bury the rest.
        warnings.append(
            f"calendar.csv: {len(unused)} latent_factors have no dependents yet, so they "
            "affect nothing (fine for work in progress, or factors for child scenarios): "
            + ", ".join(name for name, _ in unused)
        )


def _parse_vary_params(
    row: dict[str, str], distribution: str, target: str, where: str, issues: list[str],
) -> tuple[dict[str, int], dict[str, float], bool]:
    """A varying row's parameter cells: bounds in the target's own unit
    (ints, or ISO dates as ordinals), and unitless shape numbers (#31).
    A column the distribution doesn't read is reported, not parsed. Returns
    ``(parsed, shape, parse_ok)``."""
    allowed = registries.DISTRIBUTION_PARAMS[distribution]
    parsed: dict[str, int] = {}
    shape: dict[str, float] = {}
    parse_ok = True
    for col in (*_VARY_BOUND_COLUMNS, *_VARY_SHAPE_COLUMNS):
        raw = (row.get(col) or "").strip()
        if not raw:
            continue
        if col not in allowed:
            issues.append(f"{where}: {col} is not used by {distribution} (ignored by the draw)")
            continue
        try:
            if col in _VARY_SHAPE_COLUMNS:
                shape[col] = float(raw)
            else:
                parsed[col] = parse_day(raw).toordinal() if target == "date" else int(raw)
        except ValueError:
            unit = "number" if col in _VARY_SHAPE_COLUMNS else (
                MONTH_FORMAT_HINT if target == "date" else "integer"
            )
            prefix = "is not a" if unit == "number" else "is not a valid"
            issues.append(f"{where}: {col} '{raw}' {prefix} {unit}")
            parse_ok = False
    return parsed, shape, parse_ok


def _check_vary_distribution(
    row: dict[str, str], distribution: str, parsed: dict[str, int], shape: dict[str, float],
    where: str, issues: list[str],
) -> float | None:
    """Per-family checks on a varying row's parsed parameters; returns the
    effective low bound (given, or resolved), for the transfer-positivity
    rule, or ``None`` when it can't be determined."""
    if distribution in ("uniform", "triangular"):
        for col in registries.DISTRIBUTION_REQUIRED_PARAMS[distribution]:
            if not (row.get(col) or "").strip():
                issues.append(f"{where}: {distribution} requires {col}")
        low, high = parsed.get("vary_low"), parsed.get("vary_high")
        if low is None or high is None:
            return low
        if distribution == "uniform":
            if low > high:
                issues.append(f"{where}: vary_low must be <= vary_high")
            return low
        try:
            Triangular(low, parsed.get("vary_mode", (low + high) / 2), high)
        except DistributionError as e:
            issues.append(f"{where}: {e}")
        return low
    # truncated_normal: any of median/sd/low/high may be inferred from the
    # others (ADR-006, #31) -- resolve, then build it, which checks the
    # median lies strictly inside and is reachable for the shape.
    skew = shape.get("vary_skew", 0.0)
    tailweight = shape.get("vary_tailweight", 1.0)
    try:
        median, sd, low, high = resolve_truncated_normal_bounds(
            parsed.get("vary_median"), parsed.get("vary_sd"),
            parsed.get("vary_low"), parsed.get("vary_high"),
            skew=skew, tailweight=tailweight,
        )
        TruncatedNormal(median, sd, low, high, skew=skew, tailweight=tailweight)
    except DistributionError as e:
        issues.append(f"{where}: {e}")
        return None
    return low


def _safe_json(raw: str | None) -> dict[str, Any] | None:
    """``json.loads(raw or "{}")``, or ``None`` on invalid JSON -- callers
    skip rather than re-report, since the owning row's own validator already
    flags malformed JSON in its column.
    """
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None


def _validate_series_references(
    scheduled_rows: list[dict[str, str]],
    calendar_rows: list[dict[str, str]],
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]],
    chains: dict[str, tuple[str, list[dict[str, str]]]],
    issues: list[str],
) -> None:
    """Cross-check every `(aux_kind, series)` pair a scheduled.csv rule or a
    chains/*.csv step reads against every pair something actually supplies,
    in both directions.

    A `percent_of_balance`/`fixed_amount`/`ss_benefit` row (including the
    ones `_expand_account_growth` synthesizes from accounts.csv's own
    `series` column) whose `(aux_kind, series)` nothing supplies would only
    fail once run, with `EngineError: no aux row for kind=... series=...`
    (`engine._amount_percent_of_balance`/`_apply_ss_benefit`/
    `_cumulative_inflation_factor`) -- worth catching at load time instead.
    Conversely, a calendar.csv `set_rate` row whose `(aux_kind, series)` no
    scheduled row or chain step reads is a silent no-op: its drawn or
    literal value lands in `aux_table` but nothing ever looks it up, so it
    never affects a run (this is what varying the wrong `series`/`aux_kind`
    pair under ADR-006 looks like -- every member ends up bit-identical). A
    chain step's own `series` (inflating its `maximum` cap over time --
    `engine._chain_step_allocation`) always reads `aux_kind='inflation'`,
    same as `fixed_amount`/`ss_benefit`, and only actually gets read when
    `maximum` is also set (`_validate_chains` separately flags `series`
    without `maximum`).
    """
    supplied: set[tuple[str, str]] = {
        (kind, series) for kind, series, *_ in aux_rows if series is not None
    }
    for row in calendar_rows:
        if row.get("kind") != "set_rate":
            continue
        series = row.get("series", "")
        if not series:
            continue
        params = _safe_json(row.get("params_json"))
        if params is None:
            continue  # malformed JSON already reported by _validate_calendar
        supplied.add((params.get("aux_kind", "rate"), series))

    demanded: set[tuple[str, str]] = set()
    for name, (kind, rows) in chains.items():
        for i, row in enumerate(rows, start=2):
            series = row.get("series", "")
            if not series or not row.get("maximum"):
                continue  # _chain_step_allocation only reads series when maximum is also set
            demanded.add(("inflation", series))
            if ("inflation", series) not in supplied:
                issues.append(
                    f"chains/{kind}__{name}.csv row {i}: references series "
                    f"'{series}' under aux_kind 'inflation', but nothing "
                    "supplies it -- this would fail at run time with 'no "
                    "aux row' once reached"
                )

    for i, row in enumerate(scheduled_rows, start=2):
        kind = row.get("amount_rule_kind", "")
        series = row.get("series", "")
        if kind == "percent_of_balance" and not series:
            # A fund's allocation blend (#78) reads each slice's series; they
            # were checked to exist when the blend was built.
            params = _safe_json(row.get("params_json")) or {}
            for _, slices in params.get("blend") or ():
                demanded.update(("rate", slice_series) for slice_series, _ in slices)
            continue
        if not series or kind not in registries.SERIES_RULE_KINDS:
            continue
        if kind == "fixed_amount" and series == "fixed":
            continue  # the CLI's default sentinel for "no inflation"
        if kind == "percent_of_balance":
            params = _safe_json(row.get("amount_rule_params_json"))
            if params is None:
                continue  # already reported by _validate_scheduled
            aux_kind = params.get("rate_kind", "rate")
        else:
            aux_kind = "inflation"
        demanded.add((aux_kind, series))
        if (aux_kind, series) not in supplied:
            name = row.get("name", "")
            issues.append(
                f"scheduled.csv row {i} ({name}): references series "
                f"'{series}' under aux_kind '{aux_kind}', but nothing "
                "supplies it -- no series/aux/prices file and no "
                "calendar.csv set_rate row for that (aux_kind, series) pair; "
                "this would fail at run time with 'no aux row' once reached"
            )

    for i, row in enumerate(calendar_rows, start=2):
        if row.get("kind") != "set_rate":
            continue
        series = row.get("series", "")
        if not series:
            continue
        params = _safe_json(row.get("params_json"))
        if params is None:
            continue
        aux_kind = params.get("aux_kind", "rate")
        if (aux_kind, series) not in demanded:
            issues.append(
                f"calendar.csv row {i}: set_rate series '{series}' under "
                f"aux_kind '{aux_kind}' is never read by any scheduled.csv "
                "row (percent_of_balance/fixed_amount/ss_benefit) or "
                "account growth series -- this value would have no effect"
            )


def _validate_no_spaces(file: str, row_i: int, owner: str, field: str, value: str,
                        issues: list[str]) -> None:
    """A series/symbol/chain name becomes a `<kind>__<name>.csv`-style
    filename segment (ADR-003 §4.1); a literal space there is legal on most
    filesystems but needs shell quoting and reads awkwardly, so it's
    forbidden outright rather than silently decoded/encoded.
    """
    if " " in value:
        issues.append(f"{file} row {row_i} ({owner}): {field} '{value}' may not contain a space")


def _validate_tags(file: str, row_i: int, name: str, raw: str,
                   issues: list[str]) -> None:
    try:
        registries.parse_tags(raw)
    except ValueError as e:
        issues.append(f"{file} row {row_i} ({name}): {e}")


def _chain_name_from_filename(path: Path) -> tuple[str, str] | None:
    """Parse a chain filename: ``<kind>__<name>.csv``, name literal (no
    space decoding -- ADR-003 §4.1 names that become filenames use their
    filename form everywhere, spaces forbidden)."""
    stem = path.stem
    if "__" not in stem:
        return None
    kind, name = stem.split("__", 1)
    return kind, name


def _read_chain_files(
    data_dir: Path, scenario_name: str, issues: list[str],
) -> dict[str, tuple[str, list[dict[str, str]]]]:
    """Read ``chains/*.csv`` for a scenario and every scenario it inherits
    from, in load order.

    Unlike the other per-scenario CSVs, chains merge by WHOLE FILE per chain
    name (ADR-003 §4.7): a later scenario's ``chains/<kind>__<name>.csv``
    entirely replaces an earlier one's file of that name; a chain no later
    scenario overrides is inherited unchanged.
    """
    scenarios_dir = data_dir / "scenarios"
    chains: dict[str, tuple[str, list[dict[str, str]]]] = {}
    for name in _strict_load_order(scenarios_dir, scenario_name):
        chains_dir = scenarios_dir / name / "chains"
        if not chains_dir.is_dir():
            continue
        for path in sorted(chains_dir.glob("*.csv")):
            if path.stem.startswith("_"):
                continue
            parsed = _chain_name_from_filename(path)
            if parsed is None:
                issues.append(f"chains/{path.name}: filename must be '<kind>__<name>.csv'")
                continue
            kind, chain_name = parsed
            chains[chain_name] = (kind, _read_csv(path, issues))
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
    model_rows: list[dict[str, str]],
    leaves: _LeafIndex,
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
) -> None:
    for name in sorted(set(chains) & leaves.names):
        issues.append(f"chains/*.csv: chain name '{name}' collides with an account name")
    for name in sorted(n for n in chains if ":" in n):
        issues.append(f"chains/*.csv: chain name '{name}' may not contain ':'")
    for name in sorted(n for n in chains if " " in n):
        issues.append(f"chains/*.csv: chain name '{name}' may not contain a space")

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
            problem = _resolve_ref(row, "account_name", "lot", leaves, set(chains))
            if problem:
                issues.append(f"chains/{kind}__{name}.csv row {i}: {problem}")
            elif member in chains:
                edges[name].add(member)
            maximum = row.get("maximum", "")
            if maximum:
                try:
                    _dollars_to_cents(maximum, resolver=money_resolver)
                except ValueError as e:
                    issues.append(f"chains/{kind}__{name}.csv row {i}: maximum {e}")
            percent = row.get("percent", "")
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
            if series and not maximum:
                issues.append(
                    f"chains/{kind}__{name}.csv row {i}: series requires maximum"
                )
            if series:
                _validate_no_spaces(f"chains/{kind}__{name}.csv", i, member,
                                    "series", series, issues)

    for i, row in enumerate(model_rows, start=2):
        holder = leaves.leaf_of.get(
            (row.get("account_name", ""), row.get("lot") or registries.DEFAULT_LOT)
        )
        for column, lot_column in (
            ("refill_chain", "refill_lot"), ("overflow_chain", "overflow_lot"),
        ):
            if not row.get(column):
                continue
            problem = _resolve_ref(row, column, lot_column, leaves, set(chains))
            if problem:
                issues.append(
                    f"account_values.csv row {i} ({row.get('account_name', '')}): "
                    f"{column} {problem}"
                )
            elif row[column] in chains and holder:
                edges.setdefault(holder, set()).add(row[column])

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
    securities: list[dict[str, str]],
    allocations: list[dict[str, str]],
    accounts: list[dict[str, str]],
    account_model_name: str,
    account_model_rows: list[dict[str, str]],
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]],
    scenario_row: dict[str, str],
    parents: list[str],
    lineage_order: list[str],
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

    for sec in securities:
        con.execute(
            "INSERT INTO security(symbol, name) VALUES (?,?)", (sec["symbol"], sec["name"]),
        )
    counts["security"] = len(securities)

    for row in allocations:
        con.execute(
            "INSERT INTO security_allocation(symbol, as_of, asset_class, weight_micros) "
            "VALUES (?,?,?,?)",
            (row["symbol"], row.get("as_of", ""), row["asset_class"],
             _percent_to_micros(row["weight"])),
        )
    counts["security_allocation"] = len(allocations)

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
        account_ids[a["name"]] = inserted_id(cur)
        _insert_tags(con, inserted_id(cur), a.get("tags", ""),
                     "account_tag", "account_id", tag_cache)
    counts["account"] = len(accounts)

    cur = con.execute(
        "INSERT INTO account_model(name, description, created_at) VALUES (?,?,?)",
        (account_model_name, None, now),
    )
    model_id = inserted_id(cur)

    # One account_model_member row per lot (ADR-005). An account with no
    # account_values.csv row gets a single zero-balance default lot. Rows are
    # written in accounts.csv order so lot ids follow account order.
    rows_by_account: dict[str, list[dict[str, str]]] = {}
    for m in account_model_rows:
        rows_by_account.setdefault(m["account_name"], []).append(m)
    lot_ids: dict[str, int] = {}
    lot_account: dict[str, str] = {}
    lot_rows: dict[str, dict[str, str]] = {}
    member_count = 0
    for a in accounts:
        for m in rows_by_account.get(a["name"]) or [
            {"account_name": a["name"], "opening_balance": "0.00"}
        ]:
            lot = m.get("lot") or registries.DEFAULT_LOT
            cur = con.execute(
                "INSERT INTO account_model_member("
                "model_id, account_id, lot, symbol, acquired, quantity_micros, "
                "series, opening_balance, opening_cost_basis, "
                "prior_year_end_balance, minimum_balance_cents, "
                "maximum_balance_cents, refill_target_cents, refill_amount_cents) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    model_id,
                    account_ids[a["name"]],
                    lot,
                    m.get("symbol") or None,
                    m.get("acquired") or None,
                    _quantity_to_micros(m["quantity"]) if m.get("quantity") else None,
                    m.get("series") or None,
                    _dollars_to_cents(m["opening_balance"], resolver=money_resolver),
                    _dollars_to_cents(m["opening_cost_basis"], resolver=money_resolver)
                    if m.get("opening_cost_basis") else None,
                    _dollars_to_cents(m["prior_year_end_balance"], resolver=money_resolver)
                    if m.get("prior_year_end_balance") else None,
                    _dollars_to_cents(m["minimum_balance"], resolver=money_resolver)
                    if m.get("minimum_balance") else 0,
                    _dollars_to_cents(m["maximum_balance"], resolver=money_resolver)
                    if m.get("maximum_balance") else None,
                    _dollars_to_cents(m["refill_target"], resolver=money_resolver)
                    if m.get("refill_target") else None,
                    _dollars_to_cents(m["refill_amount"], resolver=money_resolver)
                    if m.get("refill_amount") else None,
                ),
            )
            leaf = registries.leaf_chain_name(a["name"], lot, a["subkind"])
            lot_ids[leaf] = inserted_id(cur)
            lot_rows[leaf] = m
            lot_account[leaf] = a["name"]
            member_count += 1
    counts["account_model_member"] = member_count

    # Liquidity chains (ADR-003 §4.7): one implicit leaf chain per lot, plus
    # one interior chain per chains/*.csv file, in one shared name space.
    chain_ids: dict[str, int] = {}
    for leaf, lot_id in lot_ids.items():
        account_name = lot_account[leaf]
        account = next(a for a in accounts if a["name"] == account_name)
        cur = con.execute(
            "INSERT INTO chain(name, kind, account_id, lot_id) VALUES (?,?,?,?)",
            (leaf, account["kind"], account_ids[account_name], lot_id),
        )
        chain_ids[leaf] = inserted_id(cur)
    for name, (kind, _rows) in chains.items():
        cur = con.execute(
            "INSERT INTO chain(name, kind, account_id, lot_id) VALUES (?,?,NULL,NULL)",
            (name, kind),
        )
        chain_ids[name] = inserted_id(cur)
    counts["chain"] = len(chain_ids)

    step_count = 0
    for name, (_kind, rows) in chains.items():
        for i, row in enumerate(rows, start=1):
            con.execute(
                "INSERT INTO chain_steps(chain_id, step_order, draw_from_chain_id, "
                "maximum_cents, percent_micros, series, cadence) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    chain_ids[name], i, chain_ids[row["account_name"]],
                    _dollars_to_cents(row["maximum"], resolver=money_resolver)
                    if row.get("maximum") else None,
                    _percent_to_micros(row["percent"])
                    if row.get("percent") else 1_000_000,
                    row.get("series") or None,
                    row.get("cadence") or None,
                ),
            )
            step_count += 1
    counts["chain_steps"] = step_count

    for leaf, m in lot_rows.items():
        refill = m.get("refill_chain") or None
        overflow = m.get("overflow_chain") or None
        if refill or overflow:
            con.execute(
                "UPDATE account_model_member SET refill_chain_id = ?, "
                "overflow_chain_id = ? WHERE id = ?",
                (
                    chain_ids[refill] if refill else None,
                    chain_ids[overflow] if overflow else None,
                    lot_ids[leaf],
                ),
            )

    cur = con.execute(
        "INSERT INTO calendar(name, description, created_at) VALUES (?,?,?)",
        (scenario_row.get("name", ""), None, now),
    )
    calendar_id = inserted_id(cur)
    for ce in calendar_rows:
        vary_target = ce.get("vary_target") or None
        vary_bound = _vary_bound_writer(vary_target)
        cur = con.execute(
            "INSERT INTO calendar_event("
            "calendar_id, date, kind, name, "
            "scheduled_name, account_1, account_2, series, amount, "
            "param_date, tax_character, payload_json, description, "
            "vary_target, vary_distribution, vary_low, vary_high, vary_median, "
            "vary_sd, vary_skew, vary_tailweight, vary_mode, vary_ramp_months, "
            "vary_correlate_with, vary_correlation, vary_tail_dependence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                calendar_id, ce["date"], ce["kind"], ce.get("name") or None,
                ce.get("scheduled_name") or None,
                ce.get("account_1") or None,
                ce.get("account_2") or None,
                ce.get("series") or None,
                _opt_int(ce.get("amount")),
                ce.get("param_date") or None,
                ce.get("tax_character") or None,
                ce.get("params_json") or None,
                ce.get("description") or None,
                vary_target,
                ce.get("vary_distribution") or None,
                vary_bound(ce.get("vary_low")),
                vary_bound(ce.get("vary_high")),
                vary_bound(ce.get("vary_median")),
                vary_bound(ce.get("vary_sd")),
                _opt_float(ce.get("vary_skew")),
                _opt_float(ce.get("vary_tailweight")),
                vary_bound(ce.get("vary_mode")),
                _opt_int(ce.get("vary_ramp_months")),
                ce.get("vary_correlate_with") or None,
                _opt_float(ce.get("vary_correlation")),
                _opt_float(ce.get("vary_tail_dependence")),
            ),
        )
        _insert_tags(con, inserted_id(cur), ce.get("tags", ""),
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
            "tax_character, tax_year, start, \"end\", priority, "
            "description, meta_json, ytd_amount, ytd_preroll, inflation_cadence"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                model_id, s["name"],
                chain_ids[s["from_account"]],
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
                s["tax_character"], _opt_int(s.get("tax_year")),
                s["start"], s.get("end") or None, prio,
                s.get("description") or None, s.get("meta_json") or "{}",
                _dollars_to_cents(s["ytd_amount"], resolver=money_resolver)
                if s.get("ytd_amount") else None,
                int(s.get("ytd_preroll") or 0),
                s.get("inflation_cadence") or None,
            ),
        )
        sched_ids[s["name"]] = inserted_id(cur)
        _insert_tags(con, inserted_id(cur), s.get("tags", ""),
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
        "seed, parents_json, load_order_json, created_at, created_by, meta_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            scenario_row["name"], scenario_row.get("description") or None,
            model_id, calendar_id,
            scenario_row["start"], scenario_row["end"],
            int(scenario_row["seed"]) if scenario_row.get("seed") else None,
            json.dumps(parents), json.dumps(lineage_order),
            now, scenario_row.get("created_by") or "unknown",
            scenario_row.get("meta_json") or "{}",
        ),
    )
    scenario_id = inserted_id(cur)
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
            "scenario_id, name, from_account, to_account, from_lot_id, to_lot_id, "
            "amount, tax_character, year, description) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                scenario_id, y["name"],
                account_ids[lot_account[y["from_account"]]],
                account_ids[lot_account[y["to_account"]]],
                lot_ids[y["from_account"]], lot_ids[y["to_account"]],
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
