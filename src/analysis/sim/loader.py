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

from analysis.sim import life_table, registries, units
from analysis.sim.dates import (
    MONTH_FORMAT_HINT, VaryBoundRole, final_month_for_age, month_end_iso, parse_day,
    vary_date_iso,
)
from analysis.sim.distributions import (
    Discrete,
    DistributionError,
    OutcomeRole,
    Triangular,
    TruncatedNormal,
    parse_outcomes,
    resolve_truncated_normal_bounds,
    rewrite_outcomes,
)
from analysis.sim.lineage import ScenarioCycleError, load_order, parse_parents, read_parents
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
from analysis.sim.schema import SCHEMA_VERSION, apply_run_schema, inserted_id, connect_sim_file
from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS

__all__ = [
    "BATCH_MANIFEST_FILENAME",
    "CURRENT_SCENARIO_FILE", "DEFAULT_INTENT_DIR", "DEFAULT_RUNS_DIR",
    "DEFAULT_SCENARIO_NAME",
    "LoadError", "LoadResult", "DataDirtyRefused", "ScenarioRef",
    "batch_mode", "latest_ensemble_batch", "latest_run_path", "list_scenario_names", "load_scenario", "open_intent_db",
    "read_current_scenario", "resolve_scenario_name", "scenario_run_path", "find_state_dir",
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
    (e.g. an allocation with rate series for only some of its classes)."""
    unused: list[str] = field(default_factory=list)
    """Things defined but not read yet: a latent factor with no dependents,
    vary_tail_dependence on a row nothing correlates with, or a set_rate on
    an existing series nothing reads. Often deliberate (work in progress, or
    meant for child scenarios), so only `sim check` prints them (#204)."""


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
            label = _data_label(path, self.data_dir)
            with path.open(newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh), [])
            if "Value" not in header:
                raise ValueError(f"{label}: expected a 'Value' column for '@{token}'")
            problems: list[str] = []
            rows = _read_csv(path, problems, label=label)
            if problems:
                raise ValueError(problems[0])
            for where, row in _located(rows, label):
                raw = row.get("Value") or ""
                if raw == "":
                    raise ValueError(f"{where}: missing Value for '@{token}'")
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
    sort chronologically and can be filtered by scenario by name alone. Neither
    the timestamp nor the data version has an underscore, so the scenario is
    matched whole: ``base`` doesn't pick up ``higher_base``'s runs.
    """
    glob_pattern = f"*_{scenario}_*.sqlite3" if scenario else "*.sqlite3"
    re_pattern = (re.compile(rf"[^_]+_{re.escape(scenario)}_[^_]*[.]sqlite3") if scenario
                  else re.compile(".*"))
    runs = (r for r in Path(runs_dir).glob(glob_pattern)
            if re_pattern.fullmatch(r.name))
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
            con = connect_sim_file(f"file:{member}?mode=ro", uri=True)
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
    batches = sorted(p for p in recipe_dir.iterdir() if is_ensemble_batch(p, mode))
    return batches[-1] if batches else None


def is_ensemble_batch(batch_dir: Path, mode: str | None = None) -> bool:
    """Whether ``batch_dir`` is a run batch (of ``mode``, any when ``None``):
    a folder holding a member run file or a batch manifest."""
    return batch_dir.is_dir() and (
        any(batch_dir.glob("member_*.sqlite3"))
        or (batch_dir / BATCH_MANIFEST_FILENAME).is_file()
    ) and (mode is None or batch_mode(batch_dir) == mode)


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


def scenario_run_path(runs_dir: Path | str, name: str) -> Path | None:
    """The run file that stands for scenario ``name`` under ``runs_dir``: its
    latest run, else member 0 of its latest ensemble batch; ``None`` when it
    has neither."""
    return latest_run_path(runs_dir, name) or _latest_ensemble_member_zero(Path(runs_dir), name)


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
        # Imported here: it brings in IPython, which the CLI has no use for.
        from analysis.sim.notebook_ui.styled import StyledMarkdown
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
        # A scenario is a folder directly under scenarios/: a path such as
        # "../x" never names one, even when it reaches a folder.
        if (scenario in (".", "..") or Path(scenario).name != scenario
                or not (scenarios_dir / scenario).is_dir()):
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
                else scenario_run_path(resolved_runs_dir, name)
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
    """Default each account_lots.csv row's lot (investment accounts must name
    theirs), and flag repeated (account, lot) pairs here: the overlay merge that
    follows would silently collapse them."""
    seen: set[tuple[str, str]] = set()
    for where, row in _located(rows, "account_lots.csv"):
        if not row.get("lot") and row.get("account_name") in investment_accounts:
            issues.append(
                f"{where} ({row['account_name']}): investment "
                "account requires an explicit lot (e.g. lot=default)"
            )
        row["lot"] = row.get("lot") or registries.DEFAULT_LOT
        key = (row.get("account_name", ""), row["lot"])
        if key[0] and key in seen:
            issues.append(
                f"{where} ({key[0]}): duplicate account_name and lot"
            )
        seen.add(key)
    return rows


def _check_duplicate_calendar_names(rows: list[dict[str, str]], issues: list[str]) -> None:
    """A duplicate ``name`` within one scenario's own (pre-merge) calendar.csv
    rows would otherwise collapse into one row -- the second overriding the
    first by name -- before the final, merged-view duplicate check in
    ``_validate_calendar`` ever sees them (ADR-006). Checked per file, not
    across parent/child: a child row that repeats a parent's own name is a
    sanctioned override, not a duplicate.
    """
    seen: dict[str, str] = {}
    for where, row in _located(rows, "calendar.csv"):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        if name in seen:
            issues.append(
                f"{where}: duplicate name '{name}' "
                f"(first seen on {seen[name]})"
            )
        else:
            seen[name] = where


def calendar_row_key(row: Mapping[str, str]) -> tuple[str, ...]:
    """The key ``_merge_calendar_rows`` gives a calendar.csv row as authored:
    its key columns trimmed and its dates and amounts canonical, as the load
    sees them. So ``2030-12`` and ``2030-12-31`` are the same date here, and
    ``4.5`` and ``4.50`` the same rate, as they are there. For checking,
    before a write, that two rows would not merge."""
    canonical = {column: (value or "").strip() for column, value in row.items()}
    _normalize_month_columns([canonical], "calendar.csv", [])
    _normalize_units([canonical], "calendar.csv", [])
    if canonical.get("kind") == registries.LATENT_FACTOR_KIND:
        return (registries.LATENT_FACTOR_KIND, canonical.get("name", ""))
    return (
        *(canonical.get(column, "") for column in registries.CALENDAR_KEY_COLUMNS),
        *_swapped_aux_kind(canonical),
    )


def _swapped_aux_kind(row: Mapping[str, str]) -> tuple[str, ...]:
    """A swap_aux_table row's aux kind, as one more key field (#197): swaps of
    two kinds without a series (federal brackets and the standard deduction
    on the day a law changes) are otherwise alike in every key column."""
    if row.get("kind") != "swap_aux_table":
        return ()
    params = _safe_json(row.get("params_json")) or {}
    return (str(params.get("aux_kind", "")),)


def _merge_calendar_rows(
    parent_rows: list[dict[str, str]],
    child_rows: list[dict[str, str]],
    key_columns: tuple[str, ...],
    issues: list[str],
) -> list[dict[str, str]]:
    """Overlay a scenario's own calendar.csv rows on the calendar it inherits.

    A child row with a ``name`` that a row in the inherited calendar already
    has overrides that row, whatever its other fields say: a name is the
    row's identity within the scenario (ADR-003), so a child changing a named
    row's amount, date or bounds is changing that event, not adding a second
    one beside it (#136).

    Any other row is matched by the table's natural key, as
    ``_merge_scenario_rows`` does with ``require_key=False``. ``name`` is
    deliberately *not* part of that key: a child repeating a parent's unnamed
    row's other fields plus a new ``name`` (to start varying it) must still
    replace that one row, not add a second, doubled event (ADR-006) -- the
    same physical event, one-time-transfer amount included, is not created
    twice just because it gained a name. But two rows that both already have
    a name -- whether one from the parent and one freshly authored in the
    child, or two authored together in the same file -- and share every
    other key field are almost certainly two different events that happen to
    collide on it (an authoring mistake, not an override), so that
    combination is reported rather than silently collapsed one way.

    A ``latent_factor`` row (ADR-008) is keyed by its name alone.
    """
    def key_of(row: dict[str, str]) -> tuple[str, ...]:
        # A latent factor (ADR-008) has no date or event fields -- only its
        # name identifies it, so two factors don't collide on blank fields,
        # and a child scenario's factor of the same name overrides its parent's.
        if row.get("kind") == registries.LATENT_FACTOR_KIND:
            return (registries.LATENT_FACTOR_KIND, (row.get("name") or "").strip())
        return (*(row.get(c, "") for c in key_columns), *_swapped_aux_kind(row))

    def name_of(row: dict[str, str]) -> str:
        return (row.get("name") or "").strip()

    def collision(existing: dict[str, str], row: dict[str, str]) -> str:
        return (
            f"{existing.get(ORIGIN) or 'calendar.csv'} and "
            f"{row.get(ORIGIN) or 'calendar.csv'}: rows named '{name_of(existing)}' "
            f"and '{name_of(row)}' share the same {key_columns}; give them one "
            "name (an override) or change a field so they're recognized as "
            "distinct events"
        )

    merged: dict[tuple[str, ...], dict[str, str]] = {
        key_of(row): dict(row) for row in parent_rows
    }
    order = list(merged)
    by_name = {name_of(row): key for key, row in merged.items() if name_of(row)}
    for row in child_rows:
        name = name_of(row)
        if name in by_name:
            old_key = by_name[name]
            updated = {**merged[old_key], **row}
            new_key = key_of(updated)
            if new_key != old_key and new_key in merged:
                issues.append(collision(merged[new_key], row))
                continue
            del merged[old_key]
            merged[new_key] = updated
            order[order.index(old_key)] = new_key
            by_name[name] = new_key
            continue
        key = key_of(row)
        if key in merged:
            existing_name = name_of(merged[key])
            if existing_name and name and existing_name != name:
                issues.append(collision(merged[key], row))
            merged[key].update(row)
            # The update may have renamed or unnamed the row (a blank name
            # cell), so its old name must stop finding it.
            current = name_of(merged[key])
            if existing_name and existing_name != current:
                by_name.pop(existing_name, None)
            if current:
                by_name[current] = key
        else:
            order.append(key)
            merged[key] = dict(row)
            if name:
                by_name[name] = key
    return [merged[key] for key in order]


_UNIT_CONTEXT_COLUMNS = ("kind", "vary_target")
"""The calendar.csv columns that decide what unit a row's amount cells are in
(``units.calendar_row_to_storage``) and whether its date keeps its day
(``_keeps_exact_day``)."""


def _inherit_unit_context(
    rows: list[dict[str, str]], inherited: list[dict[str, str]],
) -> None:
    """Give a child calendar.csv row that overrides an inherited row by name
    the ``_UNIT_CONTEXT_COLUMNS`` its own file leaves out, before its cells
    are normalized: a ``name,amount`` override inherits its ``kind``, so its
    ``5%`` must be read as that kind's unit, not left as text, and a
    ``name,date`` override of a ``retire_scheduled`` row keeps its day (#136).
    Only columns missing from the child's header are filled -- exactly what
    the merge would carry over anyway; a blank cell is the child's own
    value."""
    by_name = {
        name: row for row in inherited if (name := (row.get("name") or "").strip())
    }
    for row in rows:
        source = by_name.get((row.get("name") or "").strip())
        if source is None:
            continue
        for column in _UNIT_CONTEXT_COLUMNS:
            if column not in row and column in source:
                row[column] = source[column]


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
    for i, row in enumerate(child_rows, start=2):
        key = tuple(row.get(column, "") for column in key_columns)
        if require_key and not all(key):
            cells = {k: v for k, v in row.items() if k != ORIGIN}
            raise LoadError([
                f"{_where(row, 'override', i)}: derived scenario override missing "
                f"key {key_columns}: {cells}"
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
    account_clashes: list[tuple[str, str, str]] | None = None,
) -> tuple[dict[str, str], list[dict[str, str]], list[dict[str, str]],
           list[dict[str, str]], list[dict[str, str]],
           list[dict[str, str]], list[dict[str, str]]]:
    """Read a scenario and every scenario it inherits from, overlaying each in
    load order (``lineage.load_order``: root first, mix-ins after the main
    parent's line, the scenario itself last)."""
    scenarios_dir = Path(scenarios_dir or (data_dir / "scenarios"))
    scenario_row: dict[str, str] = {}
    accounts: list[dict[str, str]] = []
    value_rows: list[dict[str, str]] = []
    scheduled_rows: list[dict[str, str]] = []
    calendar_rows: list[dict[str, str]] = []
    lifespan_rows: list[dict[str, str]] = []
    ytd_rows: list[dict[str, str]] = []
    meta: dict[str, Any] = {}
    account_from: dict[str, str] = {}  # account name -> the scenario whose row is in force
    for name in _strict_load_order(scenarios_dir, scenario_name):
        scenario_dir = scenarios_dir / name
        if not scenario_dir.is_dir():
            raise LoadError([f"scenario folder not found: {scenario_dir}"])

        scenario_rows = _read_csv(
            scenario_dir / "scenario.csv", issues,
            label=_data_label(scenario_dir / "scenario.csv", data_dir),
        )
        _normalize_month_columns(scenario_rows, "scenario.csv", issues)
        if len(scenario_rows) != 1:
            issues.append(
                f"scenario.csv must have exactly one data row (got {len(scenario_rows)})"
            )
        own_scenario = scenario_rows[0] if scenario_rows else {}
        if own_scenario.get("name") and own_scenario["name"] != name:
            issues.append(
                f"{_where(own_scenario, 'scenario.csv', 2)}: "
                f"name='{own_scenario['name']}' != folder '{name}'"
            )
        # Only a scenario with no parent of its own must supply the core
        # files; anything deriving from another may leave them out to inherit.
        has_parent = bool(parse_parents(own_scenario.get("parent_scenario")))

        def read_overlay(
            filename: str, *, required: bool = False,
            scenario_dir: Path = scenario_dir, has_parent: bool = has_parent,
            inherited: list[dict[str, str]] | None = None,
        ) -> list[dict[str, str]]:
            rows = _read_csv(
                scenario_dir / filename,
                issues,
                optional=has_parent or not required,
                label=_data_label(scenario_dir / filename, data_dir),
            )
            if filename == "scheduled.csv":
                _legacy_units_header(scenario_dir / filename, filename, issues)
            if filename == "lifespan.csv":
                _old_lifespan_header(
                    scenario_dir / filename, _data_label(scenario_dir / filename, data_dir), issues,
                )
            # Before both normalizations: a kind decides a date's day as well
            # as an amount's unit (``_keeps_exact_day``).
            if inherited:
                _inherit_unit_context(rows, inherited)
            _normalize_month_columns(rows, filename, issues)
            _normalize_units(rows, filename, issues)
            return rows

        own_meta = own_scenario.pop("meta_json", "")
        scenario_row.update(
            {k: v for k, v in own_scenario.items() if v != "" and k != ORIGIN}
        )
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
        own_accounts = read_overlay("accounts.csv", required=True)
        # A row replacing an account from a scenario this one doesn't build on
        # (a parent's account, say, from a mix-in) is likely a name clash
        # rather than an override; its lots from both still apply (#233).
        ancestors = set(_scenario_lineage_names(scenarios_dir, name))
        for row in own_accounts:
            account = row.get("name", "")
            earlier = account_from.get(account)
            if earlier is not None and earlier not in ancestors and account_clashes is not None:
                account_clashes.append((account, name, earlier))
            account_from[account] = name
        accounts = _merge_scenario_rows(accounts, own_accounts, ("name",))
        # #217 renamed the lots file; a leftover old one would otherwise be
        # silently ignored, losing every opening balance in it.
        if (scenario_dir / "account_values.csv").exists():
            issues.append(
                f"{_data_label(scenario_dir / 'account_values.csv', data_dir)}: renamed to "
                "account_lots.csv (#217); run scripts/migrate_217.py on the data repo"
            )
        value_rows = _merge_scenario_rows(
            value_rows, _default_lots(
                read_overlay("account_lots.csv", required=True), issues,
                {a["name"] for a in accounts if a.get("subkind") in INVESTMENT_ASSET_SUBKINDS},
            ),
            ("account_name", "lot"),
        )
        scheduled_rows = _merge_scenario_rows(
            scheduled_rows, read_overlay("scheduled.csv", required=True), ("name",)
        )
        calendar_rows_raw = read_overlay("calendar.csv", inherited=calendar_rows)
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
    for col in ("start", "end"):
        if not scenario_row.get(col):
            # Blank all the way down (a root scenario's own blank, say): the
            # run has no span. Said here rather than tripping later (#190).
            issues.append(f"{scenario_name}: scenario.csv {col} is blank here and in every scenario it builds on")
            scenario_row[col] = ""
    if "reserve" in meta:
        # #163: the ensemble reports' reserve, in CPI-constant dollars.
        try:
            units.json_dollars_to_cents(meta["reserve"])
        except ValueError as exc:
            issues.append(f"{scenario_name}: scenario.csv meta_json reserve: {exc}")
    if "ensemble_size" in meta:
        # #169: stored as its int, so `sim run` needn't reparse it.
        try:
            meta["ensemble_size"] = units.json_ensemble_size(meta["ensemble_size"])
        except ValueError as exc:
            issues.append(f"{scenario_name}: scenario.csv meta_json {exc}")
    if "after_tax" in meta:
        _check_after_tax(meta["after_tax"], scenario_name, issues)
    if "irmaa" in meta:
        _check_irmaa_meta(meta["irmaa"], scenario_name, issues)
    if meta.get("roth_survivor", "own") not in registries.ROTH_SURVIVOR_ELECTIONS:
        issues.append(
            f"{scenario_name}: scenario.csv meta_json roth_survivor must be one of "
            f"{', '.join(registries.ROTH_SURVIVOR_ELECTIONS)} (got {meta['roth_survivor']!r})"
        )
    scenario_row["name"] = scenario_name
    scenario_row["meta_json"] = json.dumps(meta, separators=(",", ":"))
    return (
        scenario_row, accounts, value_rows, scheduled_rows,
        calendar_rows, lifespan_rows, ytd_rows,
    )


def _check_after_tax(settings: object, scenario_name: str, issues: list[str]) -> None:
    """#251: the after-tax net worth's settings, ``{"drawdown_years": N,
    "step_up": bool}``, both optional; the engine reads them each year-end."""
    where = f"{scenario_name}: scenario.csv meta_json after_tax"
    if not isinstance(settings, dict):
        issues.append(f"{where} must be a JSON object (got {settings!r})")
        return
    for key in sorted(set(settings) - {"drawdown_years", "step_up"}):
        issues.append(f"{where}: unknown key {key!r} (expected drawdown_years, step_up)")
    years = settings.get("drawdown_years", 1)
    if isinstance(years, bool) or not isinstance(years, int) or years < 1:
        issues.append(f"{where} drawdown_years must be a whole number of years, at least 1 "
                      f"(got {years!r})")
    if not isinstance(settings.get("step_up", False), bool):
        issues.append(f"{where} step_up must be true or false (got {settings['step_up']!r})")


def _check_irmaa_meta(settings: object, scenario_name: str, issues: list[str]) -> None:
    """#253: ``{"magi": {"<year>": <dollars>, ...}}``, the household's MAGI
    for tax years before the run, which the first two premium years read."""
    where = f"{scenario_name}: scenario.csv meta_json irmaa"
    if not isinstance(settings, dict):
        issues.append(f"{where} must be a JSON object (got {settings!r})")
        return
    for key in sorted(set(settings) - {"magi"}):
        issues.append(f"{where}: unknown key {key!r} (expected magi)")
    magi = settings.get("magi", {})
    if not isinstance(magi, dict):
        issues.append(f"{where} magi must map years to dollars (got {magi!r})")
        return
    for year, amount in magi.items():
        if not (isinstance(year, str) and len(year) == 4 and year.isdigit()):
            issues.append(f"{where} magi: {year!r} is not a four-digit year")
        try:
            units.json_dollars_to_cents(amount)
        except ValueError as exc:
            issues.append(f"{where} magi {year}: {exc}")


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
        con = connect_sim_file(Path(td) / f"{name}.sqlite3")
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
    unused: list[str] = []

    persons = _read_csv(data_dir / "persons.csv", issues)
    _validate_birthdates(persons, issues)
    securities = _read_csv(data_dir / "securities.csv", issues, optional=True)
    _validate_securities(securities, issues)
    allocations = _read_csv(data_dir / "allocations.csv", issues, optional=True)
    _normalize_month_columns(allocations, "allocations.csv", issues)
    _validate_allocations(allocations, issues)
    (
        scenario_row, accounts, value_rows, scheduled_rows,
        calendar_rows, lifespan_rows, ytd_rows,
    ) = _read_scenario_inputs(data_dir,
                              scenario_name,
                              issues,
                              scenarios_dir=scenarios_dir,
                              account_clashes=(account_clashes := []))
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
    aux_rows = _read_aux_files(
        aux_files, issues, data_dir=data_dir, money_resolver=money_resolver,
    )

    _derive_opening_balances(value_rows, aux_rows, scenario_row, issues)
    _expand_account_growth(
        accounts, value_rows, scheduled_rows, scenario_row, aux_rows, issues,
        allocations=allocations, warnings=warnings,
    )

    leaves = _leaf_index(accounts, value_rows)
    _validate_accounts(accounts, issues, money_resolver=money_resolver)
    _validate_lot_money(
        value_rows,
        issues,
        money_resolver=money_resolver,
        accounts=accounts,
    )
    _validate_scheduled_money(scheduled_rows, issues, money_resolver=money_resolver)
    _validate_scheduled(scheduled_rows, leaves, set(chains), issues)
    _validate_calendar(
        calendar_rows, leaves, set(chains), issues,
        asset_accounts=frozenset(a["name"] for a in accounts if a.get("kind") == "asset"),
    )
    life_tables_ok = _validate_life_tables(aux_rows, issues)
    lifespan_links = _validate_lifespan(
        lifespan_rows, persons, scenario_row.get("start", ""), issues,
        life_tables=[(eff, payload) for kind, _, payload, eff in aux_rows
                     if kind == life_table.LIFE_TABLE_KIND] if life_tables_ok else None,
    )
    _validate_calendar_correlation(calendar_rows, issues, unused, lifespan_links)
    _validate_series_references(
        scheduled_rows, calendar_rows, aux_rows, chains, issues, unused,
    )
    _validate_swap_aux_tables(calendar_rows, aux_rows, issues)
    _warn_state_standard_deduction(scheduled_rows, calendar_rows, aux_rows, warnings)
    _validate_chains(
        chains, value_rows, leaves, issues, money_resolver=money_resolver,
        asset_accounts=frozenset(a["name"] for a in accounts if a.get("kind") == "asset"),
        retirement_accounts=frozenset(
            a["name"] for a in accounts if a.get("subkind") == registries.RETIREMENT_SUBKIND),
    )
    _validate_chain_roles(chains, scheduled_rows, calendar_rows, value_rows, issues)
    roth_leaves = frozenset(
        leaves.leaf_of[(m.get("account_name", ""), m.get("lot") or registries.DEFAULT_LOT)]
        for m in value_rows
        if registries.RETIREMENT_LOT_SUBKINDS.get(m.get("subkind") or "") == "tax_free"
        and (m.get("account_name", ""), m.get("lot") or registries.DEFAULT_LOT) in leaves.leaf_of
    )
    _validate_rmd_rules(scheduled_rows, leaves, chains, issues, roth_leaves=roth_leaves)
    _validate_fill_bracket(
        scheduled_rows, chains, issues, roth_leaves=roth_leaves,
        traditional_leaves=frozenset(
            leaves.leaf_of[(m.get("account_name", ""), m.get("lot") or registries.DEFAULT_LOT)]
            for m in value_rows
            if registries.RETIREMENT_LOT_SUBKINDS.get(m.get("subkind") or "") == "tax_deferred"
            and (m.get("account_name", ""), m.get("lot") or registries.DEFAULT_LOT)
            in leaves.leaf_of
        ),
        bracket_rates=_bracket_rates(aux_rows),
        irmaa_tiers=frozenset(
            payload["tier"] for kind, table, payload, _effective in aux_rows
            if kind == registries.IRMAA_TIERS and table is None
            and isinstance(payload.get("tier"), int)),
    )
    _validate_retirement_owners(accounts, persons, issues)
    _validate_irmaa(scheduled_rows, aux_rows, persons, scenario_row, issues, warnings)
    _validate_year_to_date(ytd_rows, leaves, issues, money_resolver=money_resolver)
    _validate_business(
        accounts, scheduled_rows, calendar_rows, ytd_rows, chains, leaves, issues,
    )

    for account, replacing, replaced in account_clashes:
        if any(f"({account})" in issue or f"({account} /" in issue for issue in issues):
            issues.append(
                f"({account}): the account is defined in both {replaced} and {replacing}, "
                f"which doesn't build on {replaced}; {replacing}'s row replaces "
                f"{replaced}'s here, but the lots of both apply. If they are two "
                "accounts, rename one; if one, define it in one scenario only"
            )

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


    con = connect_sim_file(intent_path)
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
            value_rows=value_rows,
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
                      warnings=warnings,
                      unused=unused)


# ---------- CSV reading ----------

ORIGIN = "\0origin"
"""The key under which ``_read_csv`` records where a row was authored, e.g.
``scenarios/base/calendar.csv line 12``. Not a column: no CSV header can
hold a NUL, and nothing writes rows out by iterating their keys. It rides
along through scenario overlays (an override's ``update`` carries the child's
location in), so a message about a merged row names the file and line a
person would open, not the row's position in the merged list (#136)."""


def _data_label(path: Path, data_dir: Path) -> str:
    """``path`` as a message names it: relative to the data repo when it's
    inside it, so two scenarios' files of the same name are told apart."""
    try:
        return path.relative_to(data_dir).as_posix()
    except ValueError:
        return path.as_posix()


def _read_csv(
    path: Path, issues: list[str], *, optional: bool = False, label: str | None = None,
) -> list[dict[str, str]]:
    """Read ``path``'s rows, stripped, skipping blank ones, each tagged with its
    ``ORIGIN``: ``label`` (default: the file's name) and the physical line its
    record starts on, which is what an editor shows -- blank lines and quoted
    cells spanning lines included."""
    if not path.exists():
        if not optional:
            issues.append(f"missing required file: {path}")
        return []
    label = label or path.name
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        last_line = reader.line_num
        for values in reader:
            start, last_line = last_line + 1, reader.line_num
            cells = [v.strip() for v in values]
            if not any(cells):
                continue
            if len(cells) > len(header):
                issues.append(
                    f"{label} line {start}: {len(cells)} cells but the header has "
                    f"{len(header)} columns"
                )
            padded = (cells + [""] * len(header))[:len(header)]
            row = dict(zip(header, padded, strict=True))
            row[ORIGIN] = f"{label} line {start}"
            rows.append(row)
    return rows


def _located(
    rows: Iterable[dict[str, str]], filename: str,
) -> Generator[tuple[str, dict[str, str]]]:
    """Each row with where to say it is: its ``ORIGIN`` when ``_read_csv``
    recorded one, else (a row the loader made itself) ``filename`` and its
    position."""
    for i, row in enumerate(rows, start=2):
        yield _where(row, filename, i), row


def _where(row: Mapping[str, str], filename: str, i: int) -> str:
    """``_located`` for a row known by its position ``i`` in ``filename``."""
    return row.get(ORIGIN) or f"{filename} row {i}"


_MONTH_COLUMNS: dict[str, tuple[str, ...]] = {
    "scenario.csv": ("start", "end"),
    "accounts.csv": ("opened", "closed"),
    "scheduled.csv": ("start", "end", "base_date"),
    "calendar.csv": ("date", "param_date"),
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
    for where, row in _located(rows, filename):
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
                        f"{where}: {col} '{raw}' needs a full YYYY-MM-DD: "
                        "a biweekly rule's start is its first payday, the 14-day anchor"
                    )
                    row[col] = ""
                else:
                    row[col] = month_end_iso(raw)
            except ValueError:
                issues.append(f"{where}: {col} '{raw}' is not {MONTH_FORMAT_HINT}")
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
        raw = (row.get("vary_outcomes") or "").strip()
        if raw:
            try:
                row["vary_outcomes"] = rewrite_outcomes(raw, vary_date_iso)
            except (ValueError, DistributionError):
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
    data_dir: Path,
    money_resolver: _MoneyReferenceResolver,
) -> list[tuple[str, str | None, dict[str, Any], str]]:
    out: list[tuple[str, str | None, dict[str, Any], str]] = []
    for csv_path in csv_paths:
        label = _data_label(csv_path, data_dir)
        stem = csv_path.stem
        if "__" in stem:
            kind, series = stem.split("__", 1)
        else:
            kind, series = stem, None
        if kind in registries.RETIRED_AUX_KINDS:
            issues.append(f"{label}: {registries.RETIRED_AUX_KINDS[kind]}")
            continue
        if kind not in registries.AUX_KIND_COLUMNS:
            issues.append(f"{label}: unknown kind '{kind}'")
            continue
        if kind == "security_price" and series:
            _validate_symbol_case(label, series, issues)
        cols = registries.AUX_KIND_COLUMNS[kind]
        money_cols = registries.AUX_MONEY_COLUMNS.get(kind, frozenset())
        micros_cols = registries.AUX_MICROS_COLUMNS.get(kind, {})
        if _legacy_units_header(csv_path, label, issues):
            continue
        rows = _read_csv(csv_path, issues, label=label)
        for where, row in _located(rows, label):
            missing = [c for c in cols if c not in row]
            if missing:
                issues.append(
                    f"{where}: missing columns {missing}"
                )
                continue
            payload: dict[str, Any] = {}
            row_ok = True
            for c in cols:
                if c == "effective_date":
                    continue
                raw = row[c]
                if c in micros_cols:
                    key, unit = micros_cols[c]
                    try:
                        payload[key] = _AUX_MICROS_PARSERS[unit](raw)
                    except (ValueError, InvalidOperation) as e:
                        issues.append(f"{where} ({c}): {e}")
                        row_ok = False
                        break
                elif c in money_cols:
                    if raw != "":
                        try:
                            payload[c] = _dollars_to_cents(raw, resolver=money_resolver)
                        except ValueError as e:
                            issues.append(
                                f"{where} ({c}): {e}"
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
                    f"{where}: effective_date "
                    f"'{effective}' is not {expected}"
                )
                row_ok = False
            if row_ok:
                out.append((kind, series, payload, effective))
    return out


_AUX_MICROS_PARSERS: dict[str, Callable[[str], int]] = {
    "price": lambda raw: _dollars_to_micros(raw),
    "percent": units.percent_to_micros,
    "number": units.number_to_micros,
}
"""How each `registries.AUX_MICROS_COLUMNS` unit is read."""


def _legacy_units_header(path: Path, where: str, issues: list[str]) -> bool:
    """Whether the CSV at ``path`` still has a pre-#130 micros column,
    reporting it if so: such a file's values would be read as percents or
    plain numbers 10,000 or a million times too large. Read from the header
    itself, so a file with no rows yet is refused too."""
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh), [])
    legacy = sorted({c.strip() for c in header} & registries.LEGACY_MICROS_COLUMNS)
    if legacy:
        issues.append(f"{where}: column {', '.join(legacy)}: {registries.UNITS_CONVERSION_HINT}")
    return bool(legacy)


def _old_lifespan_header(path: Path, where: str, issues: list[str]) -> None:
    """Report a lifespan.csv still using ``final_month`` (#221), from the
    header itself, so a file with no rows yet is refused too."""
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as fh:
        header = {c.strip() for c in next(csv.reader(fh), [])}
    if "final_month" in header:
        issues.append(
            f"{where}: final_month was replaced by age_at_death (#221), "
            "a decimal age in years; replace the column"
        )


def _normalize_units(rows: list[dict[str, str]], filename: str, issues: list[str]) -> None:
    """Rewrite one file's natural-unit cells (#130) as the integers the rest of
    the load reads, in place, as ``_normalize_month_columns`` does for dates.

    Done per file, before scenario inheritance merges rows, so a child's
    ``4.50`` overrides its parent's ``4.5``. In scheduled.csv a rule's `rate`
    (percent) becomes `rate_micros`; in calendar.csv, `amount` and its
    `vary_*` bounds become cents or micros by kind (``units.calendar_row_to_storage``).
    """
    if filename == "scheduled.csv":
        # A pre-#130 file has no `rate` column, so its rows pass through
        # untouched; read_overlay reports its column, which fails the load.
        for where, row in _located(rows, "scheduled.csv"):
            if "rate" not in row:
                continue
            raw = row.pop("rate")
            row["rate_micros"] = ""
            if raw:
                try:
                    row["rate_micros"] = str(units.percent_to_micros(raw))
                except ValueError as e:
                    issues.append(f"{where} ({row.get('name', '')}): rate {e}")
    elif filename == "calendar.csv":
        for where, row in _located(rows, "calendar.csv"):
            for problem in units.calendar_row_to_storage(row):
                issues.append(f"{where}: {problem}")


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
    """Parse a human-facing dollar amount (``'1234.56'``, ``'-150'``, the only
    shapes ``units.dollars_to_cents`` takes) or an ``@name`` rollup into
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
    return units.dollars_to_cents(raw)


def _percent_to_micros(s: str) -> int:
    """Parse a human-facing share such as ``25%`` into millionths, 0-100%."""
    micros = units.percent_to_micros(s)
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
    accounts: list[dict[str, str]], value_rows: list[dict[str, str]],
) -> _LeafIndex:
    labels: dict[str, list[str]] = {}
    for m in value_rows:
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


def _both_chains(
    row: dict[str, str], from_col: str, to_col: str, chain_names: set[str] | frozenset[str],
) -> bool:
    """Whether both sides of a transfer name an interior chain, which the
    engine can't route: it sweeps out of a lot or draws into one."""
    return row.get(from_col, "") in chain_names and row.get(to_col, "") in chain_names


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
    value_rows: list[dict[str, str]],
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
    The income side is the built-in account for the account's
    ``tax_character`` (ADR-003 §2.2), or for a retirement lot, the one its
    subkind names, created here on first use so scenarios never wire it up by
    hand. Rules are named by :func:`growth_rule_name`.
    """
    account_names = {row.get("name", "") for row in accounts}
    rule_names = {row.get("name", "") for row in scheduled_rows}
    rate_series = {series for kind, series, *_ in aux_rows if kind == "rate"}
    weight_sets = _allocation_weight_sets(allocations)
    lots_by_account: dict[str, list[dict[str, str]]] = {}
    for m in value_rows:
        lots_by_account.setdefault(m.get("account_name", ""), []).append(m)
    start = scenario_row.get("start", "")
    for where, row in _located(accounts, "accounts.csv"):
        name = row.get("name", "")
        account_series = row.get("series", "")
        character = row.get("tax_character", "")
        retirement = row.get("subkind") == registries.RETIREMENT_SUBKIND
        # (lot, series, blend, character): a blend replaces the series when it
        # is set. A retirement lot's subkind says how its growth is sheltered,
        # so Roth and traditional lots in one account grow from different
        # built-ins whatever the account's tax_character says (#224).
        lot_series: list[tuple[str, str, AllocationBlend | None, str]] = []
        for m in lots_by_account.get(name) or [{}]:
            symbol = m.get("symbol") or ""
            lot = m.get("lot") or registries.DEFAULT_LOT
            series = m.get("series") or (symbol if symbol in rate_series else "")
            blend = None
            if not series and symbol in weight_sets:
                blend = _allocation_blend(
                    symbol, weight_sets[symbol], rate_series,
                    f"account_lots.csv ({name} / {lot})", warnings,
                )
            shelter = registries.RETIREMENT_LOT_SUBKINDS.get(m.get("subkind") or "")
            lot_character = (
                ("tax_free" if shelter == "tax_free" else "deferred")
                if retirement and shelter else character
            )
            lot_series.append(
                (lot, series or ("" if blend else account_series), blend, lot_character))
        growing = [entry for entry in lot_series if entry[1] or entry[2]]
        if not growing:
            if character:
                issues.append(
                    f"{where} ({name}): tax_character without series "
                    "has no effect"
                )
            continue
        if row.get("kind") != "asset":
            issues.append(
                f"{where} ({name}): series is only supported for "
                "asset accounts"
            )
            continue
        # A retirement account's own character, if set, must still be valid.
        checked = {c for *_, c in growing} | ({character} if character else set())
        unknown = sorted(checked - registries.GROWTH_COUNTERPARTY_ACCOUNTS.keys())
        if unknown:
            issues.append(
                f"{where} ({name}): series requires tax_character in "
                f"{sorted(registries.GROWTH_COUNTERPARTY_ACCOUNTS)}, got '{unknown[0]}'"
            )
            continue
        for *_, lot_character in growing:
            counterparty = registries.GROWTH_COUNTERPARTY_ACCOUNTS[lot_character]
            if counterparty.name in account_names:
                continue
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
            value_rows.append({
                "account_name": counterparty.name, "opening_balance": "0.00",
            })
        for lot, series, blend, lot_character in growing:
            counterparty = registries.GROWTH_COUNTERPARTY_ACCOUNTS[lot_character]
            rule_name = growth_rule_name(name, lot, rule_names)
            if rule_name in rule_names:
                issues.append(
                    f"{where} ({name}): series would synthesize rule "
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
    """A birthdate is day-precise (ages turn on it), so a strict YYYY-MM-DD.
    An actual person (``PERSON_ROLES``) must have one; a pseudo-person such
    as ``joint`` needn't."""
    for where, row in _located(rows, "persons.csv"):
        birthdate = row.get("birthdate", "")
        if not birthdate and (row.get("role") or "").strip() in registries.PERSON_ROLES:
            issues.append(
                f"{where} ({row.get('id') or '?'}): a {row['role'].strip()} person needs a birthdate"
            )
        if birthdate:
            try:
                parse_day(birthdate)
            except ValueError:
                issues.append(
                    f"{where} ({row.get('id') or '?'}): birthdate "
                    f"'{birthdate}' is not YYYY-MM-DD"
                )
        sex = (row.get("sex") or "").strip()
        if sex and sex not in registries.PERSON_SEXES:
            issues.append(
                f"{where} ({row.get('id') or '?'}): sex must be one of "
                f"{list(registries.PERSON_SEXES)} or blank (got '{sex}')"
            )


def _validate_securities(rows: list[dict[str, str]], issues: list[str]) -> None:
    seen: set[str] = set()
    for where, row in _located(rows, "securities.csv"):
        symbol, name = row.get("symbol", ""), row.get("name", "")
        if not symbol:
            issues.append(f"{where}: missing symbol")
            continue
        _validate_symbol_case(where, symbol, issues)
        if symbol in seen:
            issues.append(f"{where} ({symbol}): duplicate symbol")
        seen.add(symbol)
        if not name:
            issues.append(f"{where} ({symbol}): missing name")


def _validate_allocations(rows: list[dict[str, str]], issues: list[str]) -> None:
    """Each (symbol, as_of) is a set of asset-class weights that sums to 100%."""
    sets: dict[tuple[str, str], int] = {}
    seen: set[tuple[str, str, str]] = set()
    for where, row in _located(rows, "allocations.csv"):
        symbol, as_of = row.get("symbol", ""), row.get("as_of", "")
        asset_class = row.get("asset_class", "")
        where = f"{where} ({symbol or '?'})"
        if not symbol:
            issues.append(f"{where}: missing symbol")
            continue
        _validate_symbol_case(where, symbol, issues)
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
    for where, row in _located(rows, "accounts.csv"):
        name = row.get("name", "")
        if not name:
            issues.append(f"{where}: missing name")
            continue
        if name in seen:
            issues.append(f"{where}: duplicate name '{name}'")
        seen.add(name)
        if ":" in name:
            issues.append(f"{where} ({name}): name may not contain ':'")
        kind = row.get("kind", "")
        subkind = row.get("subkind", "")
        valid = registries.ACCOUNT_SUBKINDS.get(kind)
        if valid is None:
            issues.append(f"{where} ({name}): unknown kind '{kind}'")
        elif kind == "asset" and subkind in registries.RETIREMENT_LOT_SUBKINDS:
            issues.append(
                f"{where} ({name}): subkind '{subkind}' is now account subkind "
                f"'{registries.RETIREMENT_SUBKIND}' with lot subkind '{subkind}' (#217); "
                "run scripts/migrate_217.py on the data repo"
            )
        elif subkind not in valid:
            issues.append(
                f"{where} ({name}): subkind '{subkind}' not allowed for kind '{kind}'"
            )
        tt = row.get("tax_treatment", "")
        if tt and tt not in registries.TAX_TREATMENTS:
            issues.append(f"{where} ({name}): unknown tax_treatment '{tt}'")
        elif tt and subkind == registries.RETIREMENT_SUBKIND:
            issues.append(
                f"{where} ({name}): a retirement account's tax_treatment is its lots' "
                "subkinds' (Roth or traditional); leave it blank"
            )
        moved = [c for c in registries.CASH_MANAGEMENT_COLUMNS if row.get(c)]
        if moved:
            issues.append(
                f"{where} ({name}): {', '.join(moved)} moved to "
                "account_lots.csv, where each lot has its own"
            )
        series = row.get("series", "")
        if series:
            _validate_no_spaces(where, name, "series", series, issues)
        _validate_tags(where, name, row.get("tags", ""), issues)


def _validate_scheduled(
    rows: list[dict[str, str]], leaves: _LeafIndex,
    chain_names: set[str], issues: list[str],
) -> None:
    for where, row in _located(rows, "scheduled.csv"):
        name = row.get("name", "")
        if not name:
            issues.append(f"{where}: missing name")
            continue
        kind = row.get("amount_rule_kind", "")
        if kind == "home_sale":
            issues.append(
                f"{where} ({name}): the home_sale rule kind is gone (#199); sell "
                "the home with a transfer_balance calendar event into a "
                "destination chain whose steps recover its basis (tax_character "
                "home_sale, basis_of the home), take the exclusion (excluded_gain, "
                "with a maximum) and then the gain (ltcg)"
            )
        elif kind not in registries.AMOUNT_RULE_KINDS:
            issues.append(f"{where} ({name}): unknown amount_rule_kind '{kind}'")
        cad = row.get("cadence", "")
        if cad not in registries.CADENCES:
            issues.append(f"{where} ({name}): unknown cadence '{cad}'")
        if cad == "annual" and not row.get("month_of_year"):
            issues.append(
                f"{where} ({name}): annual cadence requires month_of_year"
            )
        tc = row.get("tax_character", "")
        if tc not in registries.TAX_CHARACTERS:
            issues.append(f"{where} ({name}): unknown tax_character '{tc}'")
        if kind == "on_death" and tc != "tax_free":
            issues.append(
                f"{where} ({name}): on_death requires "
                f"tax_character='tax_free' (got {tc!r}) -- death-triggered "
                "liquidations/payouts are not taxable events"
            )
        tax_year = row.get("tax_year", "")
        if tax_year:
            try:
                if len(tax_year) != 4:
                    raise ValueError
                int(tax_year)
            except ValueError:
                issues.append(f"{where} ({name}): tax_year must be a four-digit year")
        for retired, merged in (("base_amount", "amount"), ("inflation_series", "series")):
            if row.get(retired, "") != "":
                issues.append(
                    f"{where} ({name}): '{retired}' was merged into "
                    f"'{merged}' (schema v12); move the value and drop the column"
                )
        series = row.get("series", "")
        if series != "" and kind not in registries.SERIES_RULE_KINDS:
            issues.append(
                f"{where} ({name}): amount_rule_kind '{kind}' does not "
                f"use 'series'; it is only read by "
                f"{', '.join(sorted(registries.SERIES_RULE_KINDS))}"
            )
        if series:
            _validate_no_spaces(where, name, "series", series, issues)
        for col, lot_col in (("from_account", "from_lot"), ("to_account", "to_lot")):
            problem = _resolve_ref(row, col, lot_col, leaves, chain_names)
            if problem:
                issues.append(f"{where} ({name}): {col} {problem}")
        if _both_chains(row, "from_account", "to_account", chain_names):
            issues.append(
                f"{where} ({name}): from_account and to_account "
                "may not both be chains"
            )
        params_json = row.get("amount_rule_params_json") or "{}"
        try:
            json.loads(params_json)
        except json.JSONDecodeError as e:
            issues.append(
                f"{where} ({name}): amount_rule_params_json invalid: {e}"
            )
        ytd_amount = row.get("ytd_amount", "")
        ytd_preroll = row.get("ytd_preroll", "")
        if ytd_preroll not in ("", "0"):
            if kind not in registries.YTD_PREROLL_KINDS:
                issues.append(
                    f"{where} ({name}): ytd_preroll only valid for "
                    "amount_rule_kind 'fixed_amount' or 'mortgage_payment'"
                )
        if ytd_amount != "" and ytd_preroll not in ("", "0"):
            issues.append(
                f"{where} ({name}): ytd_amount and ytd_preroll "
                "are mutually exclusive"
            )
        if ytd_preroll not in ("", "0", "1"):
            issues.append(
                f"{where} ({name}): ytd_preroll must be 0 or 1"
            )
        inflation_cadence = row.get("inflation_cadence", "")
        if inflation_cadence not in ("", "monthly", "annual"):
            issues.append(
                f"{where} ({name}): inflation_cadence must be "
                "'monthly' or 'annual'"
            )
        if inflation_cadence and not row.get("series"):
            issues.append(
                f"{where} ({name}): inflation_cadence set without "
                "series"
            )
        _validate_tags(where, name, row.get("tags", ""), issues)


def _validate_lot_money(
    rows: list[dict[str, str]],
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
    accounts: list[dict[str, str]],
) -> None:
    account_names = {r.get("name", "") for r in accounts}
    retirement = {
        r.get("name", "") for r in accounts
        if r.get("subkind") == registries.RETIREMENT_SUBKIND
    }
    for where, row in _located(rows, "account_lots.csv"):
        name = row.get("account_name", "")
        if not name:
            issues.append(f"{where}: missing account_name")
            continue
        lot = row.get("lot") or registries.DEFAULT_LOT
        if ":" in lot:
            issues.append(
                f"{where} ({name}): lot '{lot}' may not contain ':'"
            )
        if name not in account_names:
            issues.append(
                f"{where} ({name}): account_name not in accounts.csv"
            )
        _validate_lot_subkind(row.get("subkind", ""), name in retirement,
                              f"{where} ({name} / {lot})", issues)
        ob = row.get("opening_balance", "")
        if ob == "":
            issues.append(
                f"{where} ({name}): opening_balance is required "
                "(or give quantity, symbol and a security_price)"
            )
        else:
            try:
                _dollars_to_cents(ob, resolver=money_resolver)
            except ValueError as e:
                issues.append(
                    f"{where} ({name}): opening_balance {e}"
                )
        ocb = row.get("opening_cost_basis", "")
        if ocb != "":
            try:
                _dollars_to_cents(ocb, resolver=money_resolver)
            except ValueError as e:
                issues.append(
                    f"{where} ({name}): opening_cost_basis {e}"
                )
        _validate_cash_management(row, f"{where} ({name})",
                                  issues, money_resolver=money_resolver)
        pyeb = row.get("prior_year_end_balance", "")
        if pyeb != "":
            try:
                _dollars_to_cents(pyeb, resolver=money_resolver)
            except ValueError as e:
                issues.append(
                    f"{where} ({name}): prior_year_end_balance {e}"
                )
        if row.get("symbol") in account_names:
            issues.append(
                f"{where} ({name}): symbol '{row['symbol']}' is also "
                "an account name, so its rate series would be ambiguous"
            )
        symbol = row.get("symbol", "")
        if symbol:
            _validate_no_spaces(where, name, "symbol", symbol, issues)
            _validate_symbol_case(f"{where} ({name})", symbol, issues)
        lot_series = row.get("series", "")
        if lot_series:
            _validate_no_spaces(where, name, "series", lot_series, issues)
        acquired = row.get("acquired", "")
        if acquired:
            try:
                parse_day(acquired)
            except ValueError:
                issues.append(
                    f"{where} ({name}): acquired '{acquired}' "
                    "is not YYYY-MM-DD"
                )
        quantity = row.get("quantity", "")
        if quantity:
            try:
                if _quantity_to_micros(quantity) < 0:
                    raise ValueError
            except (ValueError, InvalidOperation):
                issues.append(
                    f"{where} ({name}): quantity '{quantity}' "
                    "must be a non-negative number"
                )

    rows_by_account: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        rows_by_account.setdefault(row.get("account_name", ""), []).append(row)
    for a in accounts:
        lots = rows_by_account.get(a.get("name", ""), [])
        if not lots and a.get("name") in retirement:
            # The zero-balance default lot an account gets otherwise would
            # have no subkind, so nothing would say how it is taxed.
            issues.append(
                f"account_lots.csv ({a['name']}): a retirement account needs at least "
                "one lot, with its subkind; add its row to account_lots.csv"
            )
        # Only lots that could owe an RMD need prior_year_end_balance; a Roth
        # lot beside a traditional one needn't carry it (#217).
        lots = [r for r in lots
                if registries.RETIREMENT_LOT_SUBKINDS.get(r.get("subkind", "")) != "tax_free"]
        if len(lots) < 2:
            continue
        name = a["name"]
        with_pyeb = sum(1 for r in lots if r.get("prior_year_end_balance"))
        if 0 < with_pyeb < len(lots):
            # The lots may come from different scenarios of the lineage, so
            # say where each is and which side it is on (#233).
            sides = {True: [], False: []}
            for r in lots:
                sides[bool(r.get("prior_year_end_balance"))].append(
                    (r.get("lot") or registries.DEFAULT_LOT)
                    + (f" ({r[ORIGIN]})" if r.get(ORIGIN) else "")
                )
            issues.append(
                f"account_lots.csv ({name}): prior_year_end_balance must be set "
                "on every lot of an account or on none (Roth lots aside); "
                f"set on {'; '.join(sides[True])}, but not on {'; '.join(sides[False])}"
            )


def _validate_retirement_owners(
    accounts: list[dict[str, str]], persons: list[dict[str, str]], issues: list[str],
) -> None:
    """A retirement account belongs to one actual person (``PERSON_ROLES``,
    who must have a birthdate): RMDs and whether a Roth distribution is
    qualified turn on the owner's age (#217)."""
    people = {p.get("id", "") for p in persons
              if (p.get("role") or "").strip() in registries.PERSON_ROLES}
    roles = " or ".join(sorted(registries.PERSON_ROLES))
    for where, a in _located(accounts, "accounts.csv"):
        if a.get("subkind") != registries.RETIREMENT_SUBKIND:
            continue
        owner = a.get("owner", "")
        if owner not in people:
            found = f"owner {owner!r}" if owner else "no owner"
            issues.append(
                f"{where} ({a.get('name', '')}): a retirement account's owner must be a "
                f"person whose role in persons.csv is {roles} ({found})"
            )


IRMAA_PARAMS = frozenset({"parts", "filing_status_aux"})
IRMAA_PARTS = ("B+D", "B", "D")


def _validate_irmaa(
    rows: list[dict[str, str]],
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]],
    persons: list[dict[str, str]], scenario_row: dict[str, str],
    issues: list[str], warnings: list[str],
) -> None:
    """An ``irmaa`` rule (#253) charges one enrolled person the Medicare
    surcharge, each month, for the tier the household's MAGI two years
    earlier lands in: so it needs a person, a federal tax rule to compute
    MAGI, and an ``irmaa_tiers`` table. The first two premium years read
    MAGI from before the run, in ``meta_json`` ``irmaa.magi``; a year left
    out charges nothing, which is worth a warning."""
    rules = [(where, row) for where, row in _located(rows, "scheduled.csv")
             if row.get("amount_rule_kind") == "irmaa"]
    if not rules:
        return
    people = {p.get("id", "") for p in persons
              if (p.get("role") or "").strip() in registries.PERSON_ROLES}
    federal = sum(
        row.get("amount_rule_kind") == "tax_accumulator"
        and (_safe_json(row.get("params_json")) or {}).get("scope", "federal") == "federal"
        for row in rows
    )
    for where, row in rules:
        where = f"{where} ({row.get('name', '')})"
        if row.get("person", "") not in people:
            issues.append(f"{where}: irmaa needs person, the one enrolled in Medicare")
        if row.get("cadence") != "monthly":
            issues.append(f"{where}: irmaa needs cadence monthly")
        if row.get("amount"):
            issues.append(f"{where}: irmaa takes no amount; its tier table sets it")
        if not federal:
            issues.append(
                f"{where}: irmaa reads the MAGI a federal tax_accumulator rule computes, "
                "and there is none")
        elif federal > 1:
            # Each would record its own federal_magi over the other's.
            issues.append(
                f"{where}: irmaa reads the MAGI a federal tax_accumulator rule computes, "
                f"and there are {federal}; it needs exactly one")
        params = _safe_json(row.get("params_json") or "{}")
        if params is None:
            issues.append(f"{where}: params_json is not a JSON object")
            continue
        unknown = sorted(set(params) - IRMAA_PARAMS)
        if unknown:
            issues.append(
                f"{where}: unknown params_json key(s) {', '.join(unknown)} "
                f"(known: {', '.join(sorted(IRMAA_PARAMS))})")
        if params.get("parts", "B+D") not in IRMAA_PARTS:
            issues.append(f"{where}: parts must be one of {', '.join(IRMAA_PARTS)}")
        status_aux = params.get("filing_status_aux", "filing_status")
        if not any(kind == status_aux for kind, _table, _payload, _effective in aux_rows):
            issues.append(f"{where}: filing_status_aux {status_aux!r} has no aux table")
    tiers = [(table, payload, effective) for kind, table, payload, effective in aux_rows
             if kind == registries.IRMAA_TIERS]
    if not any(table is None for table, _, _ in tiers):
        issues.append(
            f"scheduled.csv: an irmaa rule needs aux/{registries.IRMAA_TIERS}.csv, "
            "the Medicare surcharge tiers")
    _validate_irmaa_tiers(tiers, issues)

    start = scenario_row.get("start", "")
    if len(start) < 4 or not start[:4].isdigit():
        return
    first = int(start[:4])
    meta = json.loads(scenario_row.get("meta_json") or "{}")
    # A malformed irmaa setting is already an issue (_check_irmaa_meta).
    settings = meta.get("irmaa") if isinstance(meta, dict) else None
    magi = settings.get("magi") if isinstance(settings, dict) else None
    known = set(magi) if isinstance(magi, dict) else set()
    for premium_year in (first, first + 1):
        charged = any(
            row.get("start", "")[:4] <= str(premium_year)
            and (not row.get("end") or row["end"][:4] >= str(premium_year))
            for _, row in rules
        )
        magi_year = str(premium_year - 2)
        if charged and magi_year not in known:
            warnings.append(
                f"scenario.csv meta_json has no irmaa.magi for {magi_year}, before the run "
                f"starts, so {premium_year} charges no IRMAA surcharge")


def _validate_irmaa_tiers(
    tiers: list[tuple[str | None, dict[str, Any], str]], issues: list[str],
) -> None:
    """Each tier table's rows: whole tiers from 1, money present and not
    negative, and within one date and filing status, floors rising with
    the tier."""
    groups: dict[tuple[str | None, str, str], list[tuple[int, int]]] = {}
    for table, payload, effective in tiers:
        file = f"aux/{registries.IRMAA_TIERS}{'__' + table if table else ''}.csv"
        where = f"{file} ({effective}, {payload.get('filing_status')} tier {payload.get('tier')})"
        tier = payload.get("tier")
        if not isinstance(tier, int) or tier < 1:
            issues.append(f"{where}: tier must be a whole number from 1")
            continue
        money = [m for c in ("magi_floor", "part_b", "part_d")
                 if isinstance(m := payload.get(c), int) and m >= 0]
        if len(money) < 3:
            issues.append(f"{where}: magi_floor, part_b and part_d are dollars, 0 or more")
            continue
        groups.setdefault(
            (table, effective, str(payload.get("filing_status"))), []).append((tier, money[0]))
    for (table, effective, status), rows in groups.items():
        file = f"aux/{registries.IRMAA_TIERS}{'__' + table if table else ''}.csv"
        rows.sort()
        numbers = [tier for tier, _ in rows]
        if len(set(numbers)) != len(numbers):
            issues.append(f"{file} ({effective}, {status}): a tier appears twice")
        floors = [floor for _, floor in rows]
        if any(b <= a for a, b in zip(floors, floors[1:], strict=False)):
            issues.append(f"{file} ({effective}, {status}): magi_floor must rise with tier")


def _validate_lot_subkind(subkind: str, retirement: bool, where: str, issues: list[str]) -> None:
    """A retirement account's lot must say what it is (#217), and only such a
    lot may: its subkind is what sets its tax treatment."""
    allowed = sorted(registries.RETIREMENT_LOT_SUBKINDS)
    if retirement and not subkind:
        issues.append(f"{where}: a retirement account's lot needs a subkind ({', '.join(allowed)})")
    elif retirement and subkind not in registries.RETIREMENT_LOT_SUBKINDS:
        issues.append(f"{where}: unknown lot subkind '{subkind}' ({', '.join(allowed)})")
    elif subkind and not retirement:
        issues.append(f"{where}: subkind '{subkind}' is only for the lots of a retirement account")


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
    value_rows: list[dict[str, str]],
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
    for where, row in _located(value_rows, "account_lots.csv"):
        if row.get("opening_balance") or not row.get("quantity"):
            continue
        name, symbol = row.get("account_name", ""), row.get("symbol", "")
        try:
            quantity = _quantity_to_micros(row["quantity"])
        except (ValueError, InvalidOperation):
            continue
        if not symbol:
            issues.append(
                f"{where} ({name}): quantity without "
                "opening_balance needs a symbol"
            )
            continue
        known = [p for p in prices.get(symbol, []) if p[0] <= start]
        if not known:
            issues.append(
                f"{where} ({name}): no security_price for "
                f"'{symbol}' on or before {start} (prices/security_price__{symbol}.csv)"
            )
            continue
        row["opening_balance"] = _price_value(quantity, max(known)[1])

    # An account that uses prior_year_end_balance (it is set on some lot) needs it
    # on every lot, so a blank one is derived the same way at the prior Dec 31, from
    # today's share count (which ignores buys and reinvestment since; a stated value wins).
    if not start:
        return
    prior_dec = f"{int(start[:4]) - 1}-12-31"
    used = {r.get("account_name", "") for r in value_rows if r.get("prior_year_end_balance")}
    for row in value_rows:
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
    for where, row in _located(rows, "scheduled.csv"):
        name = row.get("name", "")
        amount = row.get("amount", "")
        if amount != "":
            try:
                _dollars_to_cents(amount, resolver=money_resolver)
            except ValueError as e:
                issues.append(f"{where} ({name}): amount {e}")
        ytd_amount = row.get("ytd_amount", "")
        if ytd_amount != "":
            try:
                _dollars_to_cents(ytd_amount, resolver=money_resolver)
            except ValueError as e:
                issues.append(f"{where} ({name}): ytd_amount {e}")


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
    *,
    roth_leaves: frozenset[str] = frozenset(),
) -> None:
    """An ``rmd`` rule's amount comes from its *subject account* (its traditional
    lots: a Roth lot has no RMD and does not count toward one, #217),
    and the money comes from ``from_account``. On an account with several lots
    the source must be an explicit chain, so which lots fund the RMD, and in what
    order, is written down; the chain may only draw from the subject's own lots
    (ADR-005 §8). Run after the rows' references have been resolved."""
    for where, row in _located(rows, "scheduled.csv"):
        if row.get("amount_rule_kind") != "rmd":
            continue
        where = f"{where} ({row.get('name', '')})"
        try:
            params = json.loads(row.get("params_json") or "{}")
        except json.JSONDecodeError:
            continue  # reported elsewhere
        subject = params.get("subject_account")
        source = row.get("from_account", "")
        if subject is not None and subject not in leaves.lot_labels:
            issues.append(f"{where}: subject_account '{subject}' is not an account")
            continue
        drawn = _chain_leaf_names(source, chains) if source in chains else {source}
        roth = sorted(drawn & roth_leaves)
        if roth:
            issues.append(f"{where}: an rmd may not draw from a Roth lot ({', '.join(roth)})")
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


FILL_BRACKET_PARAMS = frozenset(
    {"scope", "max_amount", "max_marginal_rate", "tax_rule", "irmaa_tier"})


def _bracket_rates(
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]],
) -> dict[tuple[str, str | None], set[int]]:
    """Every marginal rate in each (aux kind, series) table of the scenario."""
    rates: dict[tuple[str, str | None], set[int]] = {}
    for kind, series, payload, _ in aux_rows:
        rate = payload.get("marginal_rate_micros")
        if isinstance(rate, int):
            rates.setdefault((kind, series), set()).add(rate)
    return rates


def _tax_rule_bracket_table(
    params: Mapping[str, Any], scope: str, rates: Mapping[tuple[str, str | None], set[int]],
) -> tuple[str, str | None] | None:
    """The (aux kind, series) a tax rule reads its brackets from, as
    ``engine._year_tax_estimate`` picks it, or None when that is decided only
    at run time (a state rule with several bracket series and none named)."""
    kind = str(params.get("bracket_aux") or (
        "federal_brackets" if scope == "federal" else "state_brackets"))
    series = params.get("bracket_series")
    if series is None and kind == "state_brackets":
        named = [s for k, s in rates if k == kind and s is not None]
        if len(named) != 1:
            return None
        series = named[0]
    return kind, series


def _validate_fill_bracket(
    rows: list[dict[str, str]], chains: dict[str, tuple[str, list[dict[str, str]]]],
    issues: list[str], *,
    roth_leaves: frozenset[str], traditional_leaves: frozenset[str],
    bracket_rates: Mapping[tuple[str, str | None], set[int]],
    irmaa_tiers: frozenset[int] = frozenset(),
) -> None:
    """A ``fill_bracket`` rule (#252) converts from one traditional lot to
    one Roth lot, once a year, to a target that is either a bracket (``rate``)
    or a taxable income (``amount``), measured with a ``tax_accumulator`` rule
    of its scope. It has no priority: it always runs after every other rule
    of its month, since the year's RMD has to come out before any of the
    IRA can be converted. Run after the rows' references have been resolved."""
    tax_rules: list[tuple[str, str, dict[str, Any]]] = []
    rmd_months: dict[str, list[int]] = {}  # traditional lot -> its RMD rules' months
    for _, row in _located(rows, "scheduled.csv"):
        if row.get("amount_rule_kind") == "rmd" and row.get("month_of_year", "").isdigit():
            source = row.get("from_account", "")
            for leaf in _chain_leaf_names(source, chains) if source in chains else {source}:
                rmd_months.setdefault(leaf, []).append(int(row["month_of_year"]))
        if row.get("amount_rule_kind") == "tax_accumulator":
            params = _safe_json(row.get("params_json")) or {}
            tax_rules.append(
                (row.get("name", ""), str(params.get("scope", "federal")), params))
    for where, row in _located(rows, "scheduled.csv"):
        if row.get("amount_rule_kind") != "fill_bracket":
            continue
        where = f"{where} ({row.get('name', '')})"
        if row.get("from_account", "") not in traditional_leaves:
            issues.append(f"{where}: fill_bracket converts from a traditional IRA or 401(k) lot")
        if row.get("to_account", "") not in roth_leaves:
            issues.append(f"{where}: fill_bracket converts to a Roth IRA or Roth 401(k) lot")
        if bool(row.get("rate_micros")) == bool(row.get("amount")):
            issues.append(
                f"{where}: fill_bracket needs one of rate (the bracket to fill, e.g. 22%) "
                "or amount (the taxable income to fill up to)"
            )
        if row.get("priority"):
            issues.append(
                f"{where}: fill_bracket takes no priority; it runs after every other "
                "rule of its month, so the year's RMD comes out first"
            )
        if row.get("cadence") != "annual":
            issues.append(f"{where}: fill_bracket needs cadence annual")
        if row.get("tax_character") != "tax_deferred_out":
            issues.append(f"{where}: fill_bracket needs tax_character tax_deferred_out")
        if row.get("rate_micros") and row.get("series") not in ("", "fixed"):
            issues.append(f"{where}: a bracket target follows its brackets; series is only for amount")
        if row.get("series") not in ("", "fixed") and not row.get("base_date"):
            issues.append(f"{where}: series {row.get('series')} needs a base_date to grow from")
        if row.get("tax_year"):
            issues.append(
                f"{where}: fill_bracket takes no tax_year; it converts for, and is taxed "
                "in, the year it fires")
        month = row.get("month_of_year", "")
        late = [m for m in rmd_months.get(row.get("from_account", ""), [])
                if month.isdigit() and m > int(month)]
        if late:
            issues.append(
                f"{where}: fires in month {month}, before the RMD from the same lot "
                f"(month {max(late)}); an RMD can't be converted, so it must come out first")
        raw = row.get("params_json") or "{}"
        params = _safe_json(raw)
        if params is None:
            issues.append(f"{where}: params_json is not a JSON object")
            continue
        unknown = sorted(set(params) - FILL_BRACKET_PARAMS)
        if unknown:
            issues.append(
                f"{where}: unknown params_json key(s) {', '.join(unknown)} "
                f"(known: {', '.join(sorted(FILL_BRACKET_PARAMS))})"
            )
        scope = params.get("scope", "federal")
        if scope not in ("federal", "state"):
            issues.append(f"{where}: scope must be federal or state")
            continue
        if "max_amount" in params:
            try:
                if units.json_dollars_to_cents(params["max_amount"]) < 0:
                    raise ValueError("must not be negative")
            except ValueError as e:
                issues.append(f"{where}: max_amount {e}")
        if "irmaa_tier" in params:
            tier = params["irmaa_tier"]
            if isinstance(tier, bool) or not isinstance(tier, int) or tier < 0:
                issues.append(f"{where}: irmaa_tier must be a whole number, 0 or more (got {tier!r})")
            if not irmaa_tiers:
                issues.append(
                    f"{where}: irmaa_tier needs aux/{registries.IRMAA_TIERS}.csv, the tiers to stay within")
            elif isinstance(tier, int) and not any(t > tier for t in irmaa_tiers):
                # Staying within the top tier caps nothing; say so rather
                # than run as if the cap were in force.
                issues.append(
                    f"{where}: irmaa_tier {tier} is the top tier or above "
                    f"(aux/{registries.IRMAA_TIERS}.csv tops out at {max(irmaa_tiers)}), "
                    "so it caps nothing")
            if not any(s == "federal" for _, s, _ in tax_rules):
                issues.append(
                    f"{where}: irmaa_tier measures MAGI with a federal tax_accumulator rule, "
                    "and there is none")
        irmaa_rules = any(r.get("amount_rule_kind") == "irmaa" for r in rows)
        if (params.get("scope", "federal") != "federal"
                and ("irmaa_tier" in params or ("max_marginal_rate" in params and irmaa_rules))
                and sum(s == "federal" for _, s, _ in tax_rules) > 1):
            # MAGI is federal whatever the scope; with a state target there
            # is no tax_rule naming which federal rule measures it.
            issues.append(
                f"{where}: a state fill_bracket that watches IRMAA measures MAGI with the "
                "federal tax_accumulator rule, and there is more than one")
        if "max_marginal_rate" in params:
            try:
                if not 0 <= units.percent_to_micros(str(params["max_marginal_rate"])) <= 1_000_000:
                    raise ValueError("must be from 0% to 100%")
            except ValueError as e:
                issues.append(f"{where}: max_marginal_rate {e}")
        wanted = params.get("tax_rule")
        named = [s for n, s, _ in tax_rules if n == wanted]
        matches = [(n, p) for n, s, p in tax_rules if s == scope and (n == wanted or not wanted)]
        if wanted and not named:
            issues.append(f"{where}: tax_rule '{wanted}' is not a tax_accumulator rule")
        elif wanted and not matches:
            issues.append(
                f"{where}: tax_rule '{wanted}' is a {named[0]} rule, not a {scope} one")
        elif not matches:
            issues.append(
                f"{where}: fill_bracket measures with a {scope} tax_accumulator rule, "
                "and there is none"
            )
        elif len(matches) > 1:
            issues.append(
                f"{where}: there are {len(matches)} {scope} tax_accumulator rules "
                f"({', '.join(n for n, _ in matches)}); name one with tax_rule in params_json"
            )
        table = _tax_rule_bracket_table(matches[0][1], scope, bracket_rates) if len(
            matches) == 1 else None
        rates = bracket_rates.get(table) if table else None
        rate = row.get("rate_micros")
        if rate and rates and int(rate) not in rates:
            issues.append(
                f"{where}: no {scope} bracket is taxed at {units.micros_to_percent(int(rate))} "
                f"(rates: {', '.join(units.micros_to_percent(r) for r in sorted(rates))})"
            )


def _validate_business(
    accounts: list[dict[str, str]],
    scheduled_rows: list[dict[str, str]],
    calendar_rows: list[dict[str, str]],
    ytd_rows: list[dict[str, str]],
    chains: dict[str, tuple[str, list[dict[str, str]]]],
    leaves: _LeafIndex,
    issues: list[str],
) -> None:
    """A sole proprietor's business (#231): ``business_income`` is paid into a
    ``business`` account and ``business_expense`` out of one, each named
    directly, so the tax accumulator knows whose Schedule C it is and whether
    it's a hobby. Runs after the references are resolved to leaf chains."""
    business = frozenset(
        a.get("name", "") for a in accounts if a.get("subkind") == registries.BUSINESS_SUBKIND
    )
    for where, row in _located(accounts, "accounts.csv"):
        try:
            meta = json.loads(row.get("meta_json") or "{}")
        except json.JSONDecodeError:
            meta = None
        if not isinstance(meta, dict):
            # The tax accumulator reads a business account's hobby flag.
            if row.get("name") in business:
                issues.append(
                    f"{where} ({row.get('name')}): meta_json must be a JSON object"
                )
            continue
        if "hobby" not in meta:
            continue
        if row.get("name") not in business:
            issues.append(
                f"{where} ({row.get('name')}): meta_json hobby belongs on a "
                f"{registries.BUSINESS_SUBKIND} account"
            )
        elif not isinstance(meta["hobby"], bool):
            issues.append(f"{where} ({row.get('name')}): meta_json hobby must be true or false")

    def check(where: str, character: str, from_ref: str, to_ref: str) -> None:
        if character not in registries.BUSINESS_TAX_CHARACTERS:
            return
        income = character == registries.BUSINESS_INCOME
        ref, side = (to_ref, "to") if income else (from_ref, "from")
        account = leaves.account_of.get(ref)
        if account not in business:
            issues.append(
                f"{where}: tax_character {character} must be paid "
                f"{'into' if income else 'out of'} a {registries.BUSINESS_SUBKIND} "
                f"account, named directly ({side}: '{account or ref}'); give the "
                "account a refill chain to fund it from household cash"
            )

    for where, row in _located(scheduled_rows, "scheduled.csv"):
        check(f"{where} ({row.get('name', '')})", row.get("tax_character", ""),
              row.get("from_account", ""), row.get("to_account", ""))
    for where, row in _located(ytd_rows, "year_to_date.csv"):
        check(f"{where} ({row.get('name', '')})", row.get("tax_character", ""),
              row.get("from_account", ""), row.get("to_account", ""))
    for where, row in _located(calendar_rows, "calendar.csv"):
        character = row.get("tax_character", "")
        if row.get("kind") == "one_off_transfer":
            check(where, character, row.get("account_1", ""), row.get("account_2", ""))
        elif character in registries.BUSINESS_TAX_CHARACTERS:
            issues.append(f"{where}: tax_character {character} is only for one_off_transfer")
    for name, (role, rows) in chains.items():
        for where, row in _located(rows, _chain_label(role, name)):
            if row.get("tax_character") in registries.BUSINESS_TAX_CHARACTERS:
                issues.append(
                    f"{where}: tax_character {row.get('tax_character')} isn't for a chain "
                    f"step; pay the {registries.BUSINESS_SUBKIND} account directly"
                )


def _validate_year_to_date(
    rows: list[dict[str, str]],
    leaves: _LeafIndex,
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
) -> None:
    seen: set[str] = set()
    for where, row in _located(rows, "year_to_date.csv"):
        name = row.get("name", "")
        if not name:
            issues.append(f"{where}: missing name")
            continue
        if name in seen:
            issues.append(f"{where} ({name}): duplicate name")
        seen.add(name)
        for col, lot_col in (("from_account", "from_lot"), ("to_account", "to_lot")):
            problem = _resolve_ref(row, col, lot_col, leaves, frozenset())
            if problem:
                issues.append(f"{where} ({name}): {col} {problem}")
        tc = row.get("tax_character", "")
        if tc not in registries.TAX_CHARACTERS:
            issues.append(f"{where} ({name}): unknown tax_character '{tc}'")
        year = row.get("year", "")
        if year != "":
            try:
                int(year)
            except ValueError:
                issues.append(f"{where} ({name}): year must be an int")
        amount = row.get("amount", "")
        if amount == "":
            issues.append(f"{where} ({name}): amount is required")
        else:
            try:
                _dollars_to_cents(amount, resolver=money_resolver)
            except ValueError as e:
                issues.append(f"{where} ({name}): amount {e}")


_VARY_BOUND_COLUMNS = ("vary_low", "vary_high", "vary_median", "vary_sd", "vary_mode")
"""vary_* parameters in the target column's own unit (ints, or ISO dates)."""

_VARY_SHAPE_COLUMNS = ("vary_skew", "vary_tailweight")
"""Unitless truncated_normal shape parameters (#31)."""

_VARY_OUTCOMES = "vary_outcomes"
"""A `discrete` row's weighted outcomes (#165): parsed whole, not as a bound."""

_VARY_PROCESS_COLUMNS = ("vary_process", "vary_persistence", "vary_innovation_sd")
"""A set_rate row's year-by-year path (ADR-006 §4a, #115)."""

_MINUS_100_PERCENT_MICROS = -1_000_000


def _validate_calendar(
    rows: list[dict[str, str]], leaves: _LeafIndex, chain_names: set[str],
    issues: list[str], *, asset_accounts: frozenset[str] = frozenset(),
) -> None:
    seen_names: dict[str, str] = {}
    for where, row in _located(rows, "calendar.csv"):
        kind = row.get("kind", "")
        if kind not in registries.CALENDAR_EVENT_KINDS:
            issues.append(f"{where}: unknown event kind '{kind}'")
        if kind == "one_off_transfer":
            for col, lot_col in (("account_1", "lot_1"), ("account_2", "lot_2")):
                problem = _resolve_ref(row, col, lot_col, leaves, chain_names)
                if problem:
                    issues.append(f"{where}: {col} {problem}")
            if _both_chains(row, "account_1", "account_2", chain_names):
                issues.append(
                    f"{where}: account_1 and account_2 may not both be chains"
                )
        elif kind == "transfer_balance":
            _validate_transfer_balance(row, where, leaves, chain_names, asset_accounts, issues)
        elif kind == "swap_aux_table":
            _validate_swap_aux_table_row(row, where, issues)
        params_json = row.get("params_json") or "{}"
        try:
            if not isinstance(json.loads(params_json), dict):
                issues.append(f"{where}: params_json must be a JSON object")
        except json.JSONDecodeError as e:
            issues.append(f"{where}: params_json invalid: {e}")
        series = row.get("series", "")
        if series:
            _validate_no_spaces(where, row.get("scheduled_name") or kind,
                                "series", series, issues)
        _validate_tags(where, row.get("scheduled_name") or kind,
                       row.get("tags", ""), issues)
        name = (row.get("name") or "").strip()
        if name:
            if name in seen_names:
                issues.append(
                    f"{where}: duplicate name '{name}' "
                    f"(first seen on {seen_names[name]})"
                )
            else:
                seen_names[name] = where
        _validate_calendar_vary(row, where, kind, name, issues)


def _validate_transfer_balance(
    row: dict[str, str], where: str, leaves: _LeafIndex,
    chain_names: set[str], asset_accounts: frozenset[str], issues: list[str],
) -> None:
    """ADR-003 \u00a73.7: the source is one asset lot, since "the balance" only
    means something for one real balance; the destination is a lot or a chain,
    as for one_off_transfer; and there is no amount to give."""
    source = row.get("account_1", "")
    if source in chain_names:
        issues.append(
            f"{where}: account_1 '{source}' is a chain; transfer_balance "
            "moves one lot's balance, so name an account (one row per lot)"
        )
    else:
        problem = _resolve_ref(row, "account_1", "lot_1", leaves, chain_names)
        if problem:
            issues.append(f"{where}: account_1 {problem}")
        elif source not in asset_accounts:
            issues.append(
                f"{where}: account_1 '{source}' is not an asset account; "
                "transfer_balance moves an asset's balance"
            )
    problem = _resolve_ref(row, "account_2", "lot_2", leaves, chain_names)
    if problem:
        issues.append(f"{where}: account_2 {problem}")
    elif row.get("account_1") == row.get("account_2"):
        issues.append(
            f"{where}: transfer_balance moves a lot's balance into "
            "the same lot; name a different account_2 or lot_2"
        )
    if (row.get("amount") or "").strip():
        issues.append(
            f"{where}: transfer_balance takes no amount (it moves the "
            "whole balance); leave amount blank"
        )


def _validate_swap_aux_table_row(
    row: dict[str, str], where: str, issues: list[str],
) -> None:
    """ADR-003 \u00a73.5 (#197): the replacement table is named in params_json;
    nothing else on the row means anything. Whether both tables exist is
    checked against the loaded aux files (`_validate_swap_aux_tables`)."""
    params = _safe_json(row.get("params_json"))
    if params is None:
        return  # malformed JSON is reported by _validate_calendar
    aux_kind = params.get("aux_kind")
    source = params.get("source_series")
    if aux_kind not in registries.SWAPPABLE_AUX_KINDS:
        issues.append(
            f"{where}: swap_aux_table needs params_json aux_kind, one of "
            f"{', '.join(sorted(registries.SWAPPABLE_AUX_KINDS))}; got {aux_kind!r}"
        )
    if not isinstance(source, str) or not source:
        issues.append(
            f"{where}: swap_aux_table needs params_json source_series, the "
            "series whose table takes over"
        )
    elif source == (row.get("series") or ""):
        issues.append(
            f"{where}: swap_aux_table source_series '{source}' is the series "
            "being replaced"
        )
    for col in ("amount", "account_1", "lot_1", "account_2", "lot_2",
                "scheduled_name", "param_date", "tax_character"):
        if (row.get(col) or "").strip():
            issues.append(f"{where}: swap_aux_table takes no {col}; leave it blank")


def _warn_state_standard_deduction(
    scheduled_rows: list[dict[str, str]],
    calendar_rows: list[dict[str, str]],
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]],
    warnings: list[str],
) -> None:
    """A state tax rule with no state_standard_deduction__<state> table for
    its brackets' state uses the federal standard deduction (#248), which
    overstates most states' (California's is about a third of it), so its
    tax comes out low. A move to a state (a swap_aux_table of its
    state_brackets) with no such table falls back the same way; without a
    state tax rule, nothing reads either table, so a move says nothing."""
    supplied = {(kind, series) for kind, series, *_ in aux_rows}
    state_rule = False
    states = sorted(series for kind, series in supplied
                    if kind == "state_brackets" and series is not None)
    for where, row in _located(scheduled_rows, "scheduled.csv"):
        if row.get("amount_rule_kind") != "tax_accumulator":
            continue
        params = _safe_json(row.get("amount_rule_params_json") or row.get("params_json")) or {}
        if params.get("scope") != "state":
            continue
        state_rule = True
        if "standard_deduction_aux" in params:
            continue
        state = params.get("bracket_series") or (states[0] if len(states) == 1 else None)
        if state is None or (registries.STATE_STANDARD_DEDUCTION, state) in supplied:
            continue  # no state to name: the run reports the missing bracket_series
        warnings.append(
            f"{where} ({row.get('name', '')}): state tax uses the federal standard "
            f"deduction; add aux/{registries.STATE_STANDARD_DEDUCTION}__{state}.csv "
            "(effective_date, filing_status, amount) with the state's own"
        )
    for where, row in _located(calendar_rows, "calendar.csv") if state_rule else ():
        params = _safe_json(row.get("params_json")) or {}
        state = params.get("source_series")
        if (row.get("kind") == "swap_aux_table" and params.get("aux_kind") == "state_brackets"
                and isinstance(state, str)
                and (registries.STATE_STANDARD_DEDUCTION, state) not in supplied):
            warnings.append(
                f"{where}: after the move to {state}, state tax uses the federal standard "
                f"deduction; add aux/{registries.STATE_STANDARD_DEDUCTION}__{state}.csv"
            )


def _validate_swap_aux_tables(
    calendar_rows: list[dict[str, str]],
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]],
    issues: list[str],
) -> None:
    """A swap_aux_table row names two tables that must both be loaded: the one
    it replaces and the one replacing it, so a misspelt series fails here
    rather than when the run reaches the row."""
    supplied = {(kind, series) for kind, series, *_ in aux_rows}
    for where, row in _located(calendar_rows, "calendar.csv"):
        if row.get("kind") != "swap_aux_table":
            continue
        params = _safe_json(row.get("params_json")) or {}
        aux_kind, source = params.get("aux_kind"), params.get("source_series")
        if aux_kind not in registries.SWAPPABLE_AUX_KINDS or not isinstance(source, str):
            continue  # reported by _validate_swap_aux_table_row
        for label, series in (("series", row.get("series") or None), ("source_series", source)):
            if (aux_kind, series) not in supplied:
                file = f"{aux_kind}__{series}.csv" if series else f"{aux_kind}.csv"
                issues.append(
                    f"{where}: swap_aux_table {label} names {file}, which no "
                    "aux/ or series/ folder this scenario reads supplies"
                )


def _validate_calendar_vary(
    row: dict[str, str], location: str, kind: str, name: str, issues: list[str],
) -> None:
    """ADR-006: a row with `vary_distribution` set has a `vary_target` in
    `VARY_TARGETS`, an amount target only on a kind with a real amount to
    vary, only the columns its distribution actually reads, a fully
    resolvable and valid-ordered set of bounds, and (only for
    `vary_target='amount'` on a `set_rate` row) an optional `vary_ramp_months`
    or an optional process (§4a).
    """
    where = f"{location} ({name or '?'})"
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
            (row.get(c) or "").strip()
            for c in (
                *_VARY_BOUND_COLUMNS, *_VARY_SHAPE_COLUMNS, *_VARY_PROCESS_COLUMNS, _VARY_OUTCOMES,
            )
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

    # A `discrete` outcome of exactly 0 is "nothing happens" (#165), a real
    # case; only a negative one would reverse the transfer.
    if (
        target == "amount" and kind == "one_off_transfer" and effective_low is not None
        and (effective_low < 0 if distribution == "discrete" else effective_low <= 0)
    ):
        # A transfer amount is a real movement of money whose direction is
        # fixed by account_1/account_2; a nonpositive draw would reverse or
        # silently omit it, not just draw an unlikely value. Checked on the
        # *effective* low (given directly, or resolved from mean/sd) so this
        # one rule covers both distributions.
        issues.append(
            f"{where}: one_off_transfer requires no negative outcome"
            if distribution == "discrete" else
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

    _validate_vary_process(row, target, kind, ramp_months, effective_low, where, issues)


def _validate_vary_process(
    row: dict[str, str], target: str, kind: str, ramp_months: str,
    effective_low: float | None, where: str, issues: list[str],
) -> None:
    """ADR-006 §4a: `vary_process` only on a varying set_rate amount row,
    with both of its parameters and no ramp; phi in [0, 1); a positive shock
    size; and a level whose lower bound is above -100%, a total loss, which
    no path of growth around it can mean."""
    process = (row.get("vary_process") or "").strip()
    raw_phi = (row.get("vary_persistence") or "").strip()
    raw_sd = (row.get("vary_innovation_sd") or "").strip()
    if not process:
        if raw_phi or raw_sd:
            issues.append(
                f"{where}: vary_persistence/vary_innovation_sd are only read with vary_process"
            )
        return
    if process not in registries.VARY_PROCESSES:
        issues.append(
            f"{where}: unknown vary_process '{process}' "
            f"(one of {sorted(registries.VARY_PROCESSES)}, or blank for one held draw)"
        )
        return
    if not (target == "amount" and kind == "set_rate"):
        issues.append(
            f"{where}: vary_process is only valid with vary_target='amount' on a "
            "kind='set_rate' row"
        )
        return
    if ramp_months:
        issues.append(
            f"{where}: vary_process and vary_ramp_months don't combine on one row; a ramp "
            "row followed by a later process row for the same series expresses both"
        )
    if not raw_phi or not raw_sd:
        issues.append(f"{where}: vary_process requires vary_persistence and vary_innovation_sd")
        return
    try:
        phi = float(raw_phi)
    except ValueError:
        issues.append(f"{where}: vary_persistence '{raw_phi}' is not a number")
    else:
        if not 0.0 <= phi < 1.0:  # also rejects NaN
            issues.append(f"{where}: vary_persistence must be at least 0 and below 1 (got {raw_phi})")
    # A cell units.calendar_row_to_storage couldn't read is left unconverted,
    # and is already reported.
    if raw_sd.lstrip("-").isdigit() and int(raw_sd) <= 0:
        issues.append(
            f"{where}: vary_innovation_sd must be positive "
            f"(got {units.micros_to_percent(int(raw_sd))})"
        )
    if effective_low is not None and effective_low <= _MINUS_100_PERCENT_MICROS:
        issues.append(
            f"{where}: a process row's level must stay above -100% "
            f"(its lower bound, given or inferred, is "
            f"{units.micros_to_percent(round(effective_low))}), "
            "since its path is growth around that level"
        )


_FACTOR_FORBIDDEN_COLUMNS = (
    "scheduled_name", "account_1", "lot_1", "account_2", "lot_2", "series", "amount",
    "param_date", "tax_character", "params_json", "tags",
    "vary_target", "vary_distribution", "vary_low", "vary_high",
    "vary_median", "vary_sd", "vary_skew", "vary_tailweight", "vary_mode", "vary_ramp_months",
    *_VARY_PROCESS_COLUMNS, _VARY_OUTCOMES,
)
"""Columns a ``latent_factor`` row must leave blank: it has no date, amount
or series meaning, and its value is a standard normal score, not a draw from
an authored distribution (ADR-008 §3)."""


def _validate_calendar_correlation(
    rows: list[dict[str, str]], issues: list[str], unused_warnings: list[str],
    lifespan_links: Mapping[str, tuple[str, str]] | None = None,
) -> None:
    """ADR-008 §4, over the merged calendar (so a child scenario's row can
    name a parent scenario's factor): latent_factor rows are named and carry
    nothing but a description and correlation columns; the correlation
    columns come as a pair, on a varying row or a factor, with rho in
    [-1, 1]; every parent exists; there are no cycles. A factor nothing
    depends on is a warning: work in progress, or meant for child scenarios.
    ``vary_tail_dependence`` (#68) is a number >= 1 on a tree's root only; on a
    root nothing depends on it is a warning, for the same reasons.

    ``lifespan_links`` (#221) are the varying lifespans' nodes
    (``lifespan:<person>``), each mapped to its parent (or "") and where to
    report it: they join the same forest, so a lifespan and a calendar row
    can name each other."""
    lifespan_links = lifespan_links or {}
    nodes: dict[str, int] = {}
    parents: dict[str, str] = {}
    tails: dict[str, int] = {}
    tail_nu: dict[str, float] = {}
    processes: dict[str, int] = {}
    for i, row in enumerate(rows, start=2):
        kind = row.get("kind", "")
        name = (row.get("name") or "").strip()
        where = f"{_where(row, 'calendar.csv', i)} ({name or '?'})"
        is_factor = kind == registries.LATENT_FACTOR_KIND
        varying = bool((row.get("vary_distribution") or "").strip())
        if name.startswith(registries.LIFESPAN_NODE_PREFIX):
            issues.append(
                f"{where}: a calendar row's name can't start with "
                f"'{registries.LIFESPAN_NODE_PREFIX}', which names a lifespan (#221)"
            )
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
            if varying and (row.get("vary_process") or "").strip():
                processes[name] = i
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
                        tail_nu[name] = nu
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
        if parent not in nodes and parent not in lifespan_links:
            issues.append(
                f"{_where(rows[nodes[child] - 2], 'calendar.csv', nodes[child])} ({child}): "
                f"vary_correlate_with '{parent}' "
                "names no varying row, varying lifespan or latent_factor"
            )
    for child, (parent, where) in lifespan_links.items():
        if parent and parent not in nodes and parent not in lifespan_links:
            issues.append(
                f"{where}: vary_correlate_with '{parent}' "
                "names no varying calendar row, varying lifespan or latent_factor"
            )
        elif parent:
            parents[child] = parent
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
                f"{_where(rows[i - 2], 'calendar.csv', i)} ({name}): vary_tail_dependence goes on the root of a "
                f"correlation tree ('{root}' here), not on a row with a parent: it scales every "
                "row in the tree together, so it only has an effect where the whole tree shares it"
            )
        elif name not in used and rows[i - 2].get("kind") != registries.LATENT_FACTOR_KIND:
            # (An unused factor gets its own warning below.)
            unused_warnings.append(
                f"{_where(rows[i - 2], 'calendar.csv', i)} ({name}): vary_tail_dependence on a row nothing correlates "
                "with affects nothing yet (it acts only on the rows of the tree below it)"
            )
    for name, i in processes.items():
        root, seen = name, {name}
        while root in parents and parents[root] not in seen:
            root = parents[root]
            seen.add(root)
        nu = tail_nu.get(root)
        if nu is not None and nu <= registries.MIN_PROCESS_TAIL_DEPENDENCE:
            issues.append(
                f"{_where(rows[i - 2], 'calendar.csv', i)} ({name}): a process row's tree needs vary_tail_dependence "
                f"above {registries.MIN_PROCESS_TAIL_DEPENDENCE:g} ('{root}' sets {nu:g}): its "
                "yearly shocks are t with that many degrees of freedom, whose spread is "
                "infinite at 2 or below"
            )
    unused = [
        (name, i) for name, i in nodes.items()
        if rows[i - 2].get("kind") == registries.LATENT_FACTOR_KIND and name not in used
    ]
    if len(unused) == 1:
        name, i = unused[0]
        unused_warnings.append(
            f"{_where(rows[i - 2], 'calendar.csv', i)} ({name}): latent_factor has no dependents yet, so it "
            "affects nothing (fine for work in progress, or a factor for child scenarios)"
        )
    elif unused:
        # One line, not one per factor: a scenario that defines a factor
        # structure for its children (#80) has many, and they'd bury the rest.
        unused_warnings.append(
            f"calendar.csv: {len(unused)} latent_factors have no dependents yet, so they "
            "affect nothing (fine for work in progress, or factors for child scenarios): "
            + ", ".join(name for name, _ in unused)
        )


def _parse_vary_params(
    row: dict[str, str], distribution: str, target: str, where: str, issues: list[str],
) -> tuple[dict[str, int], dict[str, float], bool]:
    """A varying row's parameter cells: bounds in the target's own stored unit
    (cents or micros, or ISO dates as ordinals), and unitless shape numbers (#31).
    A column the distribution doesn't read is reported, not parsed. Returns
    ``(parsed, shape, parse_ok)``."""
    allowed = registries.DISTRIBUTION_PARAMS[distribution]
    parsed: dict[str, int] = {}
    shape: dict[str, float] = {}
    parse_ok = True
    for col in (*_VARY_BOUND_COLUMNS, *_VARY_SHAPE_COLUMNS, _VARY_OUTCOMES):
        raw = (row.get(col) or "").strip()
        if not raw:
            continue
        if col not in allowed:
            issues.append(f"{where}: {col} is not used by {distribution} (ignored by the draw)")
            continue
        if col == _VARY_OUTCOMES:
            continue  # read whole by _check_discrete
        try:
            if col in _VARY_SHAPE_COLUMNS:
                shape[col] = float(raw)
            else:
                parsed[col] = parse_day(raw).toordinal() if target == "date" else int(raw)
        except ValueError:
            parse_ok = False
            if col in _VARY_SHAPE_COLUMNS:
                issues.append(f"{where}: {col} '{raw}' is not a number")
            elif target == "date":
                issues.append(f"{where}: {col} '{raw}' is not a valid {MONTH_FORMAT_HINT}")
            # An amount bound that isn't an integer by now is one
            # units.calendar_row_to_storage couldn't read (and reported), or
            # sits on a kind with no amount (reported by the caller).
    return parsed, shape, parse_ok


def _check_discrete(row: dict[str, str], where: str, issues: list[str]) -> float | None:
    """A `discrete` row's outcomes (#165), already in the stored unit: they
    parse, the weights add to 100%, and no two overlap. Returns the lowest
    value, or ``None`` when they can't be read."""
    raw = (row.get(_VARY_OUTCOMES) or "").strip()
    if not raw:
        issues.append(f"{where}: discrete requires {_VARY_OUTCOMES}")
        return None
    target = (row.get("vary_target") or "").strip()
    unreadable = False

    def value(text: str, _role: OutcomeRole) -> float:
        nonlocal unreadable
        try:
            return float(parse_day(text).toordinal() if target == "date" else int(text))
        except ValueError:
            unreadable = True
            raise
    try:
        dist = Discrete(parse_outcomes(raw, value))
    except DistributionError as e:
        # An amount that isn't an integer by now is one
        # units.calendar_row_to_storage couldn't read, and already reported.
        if not (unreadable and target == "amount"):
            issues.append(f"{where}: {e}")
        return None
    return dist.outcomes[0].low


def _check_vary_distribution(
    row: dict[str, str], distribution: str, parsed: dict[str, int], shape: dict[str, float],
    where: str, issues: list[str],
) -> float | None:
    """Per-family checks on a varying row's parsed parameters; returns the
    effective low bound (given, or resolved), for the transfer-positivity
    rule, or ``None`` when it can't be determined."""
    if distribution == "discrete":
        return _check_discrete(row, where, issues)
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
    """``json.loads(raw or "{}")``, or ``None`` on invalid JSON or JSON that
    isn't an object -- callers skip rather than re-report, since the owning
    row's own validator already flags malformed JSON in its column.
    """
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _validate_series_references(
    scheduled_rows: list[dict[str, str]],
    calendar_rows: list[dict[str, str]],
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]],
    chains: dict[str, tuple[str, list[dict[str, str]]]],
    issues: list[str],
    unused_warnings: list[str],
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
    pair under ADR-006 looks like -- every member ends up bit-identical).
    A set_rate row adjusts a series; it doesn't create one. So its
    `(aux_kind, series)` must have a series/aux/prices file in the scenario's
    lineage, or it's an unresolved reference -- a typo, or the wrong
    `aux_kind` -- and a load error (#204). One with a file that nothing
    reads is only a warning, which only `sim check` prints (#92, #204): a
    mix-in or base scenario can set a series that only the scenarios
    inheriting from it read, and each scenario is validated on its own. A
    chain step's own `series` (inflating its `maximum` cap over time --
    `engine._chain_step_allocation`) always reads `aux_kind='inflation'`,
    same as `fixed_amount`/`ss_benefit`, and only actually gets read when
    `maximum` is also set (`_validate_chains` separately flags `series`
    without `maximum`).
    """
    # Only a file supplies a series; a set_rate row adjusts one (#204).
    supplied: set[tuple[str, str]] = {
        (kind, series) for kind, series, *_ in aux_rows if series is not None
    }

    demanded: set[tuple[str, str]] = set()
    for name, (role, rows) in chains.items():
        for where, row in _located(rows, _chain_label(role, name)):
            series = row.get("series", "")
            if not series or not row.get("maximum"):
                continue  # _chain_step_allocation only reads series when maximum is also set
            demanded.add(("inflation", series))
            if ("inflation", series) not in supplied:
                issues.append(
                    f"{where}: references series "
                    f"'{series}' under aux_kind 'inflation', but nothing "
                    "supplies it -- this would fail at run time with 'no "
                    "aux row' once reached"
                )

    for where, row in _located(scheduled_rows, "scheduled.csv"):
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
        if kind in ("fixed_amount", "fill_bracket") and series == "fixed":
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
                f"{where} ({name}): references series "
                f"'{series}' under aux_kind '{aux_kind}', but nothing "
                "supplies it -- no series/aux/prices file for that "
                "(aux_kind, series) pair; this would fail at run time with "
                "'no aux row' once reached"
            )

    if any(row.get("amount_rule_kind") == "irmaa" or (
            row.get("amount_rule_kind") == "fill_bracket"
            and "irmaa_tier" in (_safe_json(row.get("params_json")) or {}))
           for row in scheduled_rows):
        # An irmaa rule, or a fill_bracket keeping within a tier (#253),
        # reads the inflation series its tier table names for each floor and
        # add-on, swapped-in tables included.
        for kind, table, payload, effective in aux_rows:
            if kind != registries.IRMAA_TIERS:
                continue
            for column in registries.IRMAA_SERIES_COLUMNS:
                series = payload.get(column)
                if not series:
                    continue
                demanded.add(("inflation", str(series)))
                if ("inflation", str(series)) not in supplied:
                    file = f"{kind}__{table}.csv" if table else f"{kind}.csv"
                    issues.append(
                        f"aux/{file} ({effective}, {payload.get('filing_status')} tier "
                        f"{payload.get('tier')}): {column} '{series}' doesn't exist -- "
                        f"no series/inflation__{series}.csv in this scenario or a parent"
                    )

    for where, row in _located(calendar_rows, "calendar.csv"):
        if row.get("kind") != "set_rate":
            continue
        series = row.get("series", "")
        if not series:
            continue
        params = _safe_json(row.get("params_json"))
        if params is None:
            continue
        aux_kind = params.get("aux_kind", "rate")
        if (aux_kind, series) not in supplied:
            issues.append(
                f"{where}: set_rate series '{series}' under aux_kind "
                f"'{aux_kind}' doesn't exist -- no series/aux/prices file for "
                "that (aux_kind, series) pair in this scenario or a parent "
                f"(e.g. series/{aux_kind}__{series}.csv)"
            )
        elif (aux_kind, series) not in demanded:
            unused_warnings.append(
                f"{where}: set_rate series '{series}' under "
                f"aux_kind '{aux_kind}' is never read by any scheduled.csv "
                "row (percent_of_balance/fixed_amount/ss_benefit), chain step "
                "cap or account growth series -- this value has no effect "
                "here, unless a scenario inheriting from this one reads it"
            )


def _validate_symbol_case(where: str, symbol: str, issues: list[str]) -> None:
    """Tickers are upper case. A lower-case spelling would be a second symbol,
    with its own price file and allocation, that nothing else matches."""
    if symbol != symbol.upper():
        issues.append(f"{where}: symbol '{symbol}' must be upper case ('{symbol.upper()}')")


def _validate_no_spaces(where: str, owner: str, field: str, value: str,
                        issues: list[str]) -> None:
    """A series/symbol/chain name becomes a `<kind>__<name>.csv`-style
    filename segment (ADR-003 §4.1); a literal space there is legal on most
    filesystems but needs shell quoting and reads awkwardly, so it's
    forbidden outright rather than silently decoded/encoded.
    """
    if " " in value:
        issues.append(f"{where} ({owner}): {field} '{value}' may not contain a space")


def _validate_tags(where: str, name: str, raw: str,
                   issues: list[str]) -> None:
    try:
        registries.parse_tags(raw)
    except ValueError as e:
        issues.append(f"{where} ({name}): {e}")


def _chain_label(role: str, name: str) -> str:
    """A chain file as error messages name it: ``chains/<role>/<name>.csv``."""
    return f"chains/{role}/{name}.csv"


def _read_chain_files(
    data_dir: Path, scenario_name: str, issues: list[str],
) -> dict[str, tuple[str, list[dict[str, str]]]]:
    """Read ``chains/<role>/*.csv`` for a scenario and every scenario it
    inherits from, in load order: chain name to its role and step rows.

    Unlike the other per-scenario CSVs, chains merge by WHOLE FILE per chain
    name (ADR-003 §4.7): a later scenario's file for a chain entirely
    replaces an earlier one's, whichever role directory either is in; a
    chain no later scenario overrides is inherited unchanged. The file name
    is the chain name, literally (ADR-003 §4.1: no space decoding).
    """
    scenarios_dir = data_dir / "scenarios"
    chains: dict[str, tuple[str, list[dict[str, str]]]] = {}
    for name in _strict_load_order(scenarios_dir, scenario_name):
        chains_dir = scenarios_dir / name / "chains"
        if not chains_dir.is_dir():
            continue
        for path in sorted(chains_dir.glob("*.csv")):
            if not path.stem.startswith("_"):
                issues.append(
                    f"{_data_label(path, data_dir)}: a chain file belongs in chains/source/ "
                    "(drawn from) or chains/destination/ (paid into), named <name>.csv"
                )
        for folder in sorted(p for p in chains_dir.iterdir() if p.is_dir()):
            if folder.name not in registries.CHAIN_ROLES and any(
                    not path.stem.startswith("_") for path in folder.glob("*.csv")):
                issues.append(
                    f"{_data_label(folder, data_dir)}: not a chain role; a chain file belongs "
                    "in chains/source/ (drawn from) or chains/destination/ (paid into)"
                )
        own: dict[str, str] = {}
        for role in registries.CHAIN_ROLES:
            for path in sorted((chains_dir / role).glob("*.csv")):
                if path.stem.startswith("_"):
                    continue
                chain_name = path.stem
                if chain_name in own:
                    issues.append(
                        f"scenarios/{name}/chains: chain '{chain_name}' is in both "
                        f"{own[chain_name]}/ and {role}/; keep one"
                    )
                    continue
                own[chain_name] = role
                chains[chain_name] = (
                    role, _read_csv(path, issues, label=_data_label(path, data_dir)),
                )
    return chains


def _validate_chain_roles(
    chains: dict[str, tuple[str, list[dict[str, str]]]],
    scheduled_rows: list[dict[str, str]],
    calendar_rows: list[dict[str, str]],
    value_rows: list[dict[str, str]],
    issues: list[str],
) -> None:
    """Each named chain is used the way its role says (ADR-003 §4.7): a
    source chain is drawn from, a destination chain paid into, and a chain's
    steps that are themselves chains share its role."""

    def check(where: str, column: str, chain: str, role: str) -> None:
        if chain in chains and chains[chain][0] != role:
            issues.append(
                f"{where}: {column} '{chain}' is a {chains[chain][0]} chain; "
                f"{column} takes a {role} chain (chains/{role}/)"
            )

    for rows, filename, from_col, to_col in (
        (scheduled_rows, "scheduled.csv", "from_account", "to_account"),
        (calendar_rows, "calendar.csv", "account_1", "account_2"),
    ):
        for where, row in _located(rows, filename):
            where = f"{where} ({row.get('name', '')})" if row.get("name") else where
            check(where, from_col, row.get(from_col, ""), "source")
            check(where, to_col, row.get(to_col, ""), "destination")
    for where, row in _located(value_rows, "account_lots.csv"):
        where = f"{where} ({row.get('account_name', '')})"
        check(where, "refill_chain", row.get("refill_chain", ""), "source")
        check(where, "overflow_chain", row.get("overflow_chain", ""), "destination")
    for name, (role, rows) in chains.items():
        for where, row in _located(rows, _chain_label(role, name)):
            check(where, "account_name", row.get("account_name", ""), role)


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


def _path_to(start: str, goal: str, members: dict[str, list[str]]) -> list[str] | None:
    """The chain path from ``start`` down to the leaf ``goal``, or None.

    Only interior chains are descended; cycles among them are reported
    separately by ``_reject_cycles``, so ``seen`` just keeps this finite.
    """
    seen: set[str] = set()

    def walk(node: str, path: list[str]) -> list[str] | None:
        if node == goal:
            return path
        if node in seen:
            return None
        seen.add(node)
        for member in members.get(node, ()):
            found = walk(member, [*path, member])
            if found:
                return found
        return None

    return walk(start, [start])


def _validate_chains(
    chains: dict[str, tuple[str, list[dict[str, str]]]],
    value_rows: list[dict[str, str]],
    leaves: _LeafIndex,
    issues: list[str],
    *,
    money_resolver: _MoneyReferenceResolver,
    asset_accounts: frozenset[str] = frozenset(),
    retirement_accounts: frozenset[str] = frozenset(),
) -> None:
    for name in sorted(set(chains) & leaves.names):
        issues.append(f"chains/*.csv: chain name '{name}' collides with an account name")
    for name in sorted(n for n in chains if ":" in n):
        issues.append(f"chains/*.csv: chain name '{name}' may not contain ':'")
    for name in sorted(n for n in chains if " " in n):
        issues.append(f"chains/*.csv: chain name '{name}' may not contain a space")

    edges: dict[str, set[str]] = {name: set() for name in chains}
    members: dict[str, list[str]] = {name: [] for name in chains}
    for name, (role, rows) in chains.items():
        if not rows:
            issues.append(f"{_chain_label(role, name)}: must have at least one row")
        for where, row in _located(rows, _chain_label(role, name)):
            member = row.get("account_name", "")
            if not member:
                issues.append(f"{where}: missing account_name")
                continue
            problem = _resolve_ref(row, "account_name", "lot", leaves, set(chains))
            if problem:
                issues.append(f"{where}: {problem}")
            else:
                members[name].append(row["account_name"])
                if member in chains:
                    edges[name].add(member)
            maximum = row.get("maximum", "")
            if maximum:
                try:
                    _dollars_to_cents(maximum, resolver=money_resolver)
                except ValueError as e:
                    issues.append(f"{where}: maximum {e}")
            percent = row.get("percent", "")
            if percent:
                try:
                    _percent_to_micros(percent)
                except ValueError as e:
                    issues.append(f"{where}: percent {e}")
            series = row.get("series", "")
            cadence = row.get("cadence", "")
            if cadence not in ("", "monthly", "annual"):
                issues.append(
                    f"{where}: cadence must be monthly or annual"
                )
            if cadence and not series:
                issues.append(
                    f"{where}: cadence set without series"
                )
            if series and not maximum:
                issues.append(
                    f"{where}: series requires maximum"
                )
            if series:
                _validate_no_spaces(where, member,
                                    "series", series, issues)
        _validate_chain_tax_steps(role, rows, _chain_label(role, name), leaves,
                                  asset_accounts, issues, retirement_accounts)

    for where, row in _located(value_rows, "account_lots.csv"):
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
                    f"{where} ({row.get('account_name', '')}): "
                    f"{column} {problem}"
                )
                continue
            # The engine skips a step naming the lot it is drawing into (or
            # sweeping out of), so a self-reference would silently supply
            # nothing; the cycle check below can't see it, since a leaf step
            # adds no edge.
            loop = holder and _path_to(row[column], holder, members)
            if loop:
                issues.append(
                    f"{where} ({row.get('account_name', '')}): "
                    f"{column} leads back to the lot itself: {' -> '.join(loop)}"
                )
            elif row[column] in chains and holder:
                edges.setdefault(holder, set()).add(row[column])

    _reject_cycles(edges, issues, label="chains/*.csv")


def _validate_chain_tax_steps(
    role: str, rows: list[dict[str, str]], label: str, leaves: _LeafIndex,
    asset_accounts: frozenset[str], issues: list[str],
    retirement_accounts: frozenset[str] = frozenset(),
) -> None:
    """A chain step's ``tax_character`` and ``basis_of`` (#199, ADR-003
    §4.7): a step that sets a character gives it to the money it places and
    to every later step until another sets one; a step with ``basis_of``
    recovers that lot's basis or adds to it. ``basis_of`` is resolved in
    place to the lot's leaf name, as ``account_name`` is."""
    after_basis_step = False
    for where, row in _located(rows, label):
        character = row.get("tax_character", "")
        basis_of = row.get("basis_of", "")
        if after_basis_step and not character:
            issues.append(
                f"{where}: the step after a basis step must set its own "
                "tax_character, or the gain would be characterized as basis"
            )
        after_basis_step = False
        if row.get("basis_of_lot") and not basis_of:
            issues.append(f"{where}: basis_of_lot needs basis_of")
        if not character and not basis_of:
            continue
        if role != "destination":
            issues.append(
                f"{where}: tax_character and basis_of belong to destination "
                "chains, which characterize the money paid through them"
            )
            continue
        if character and character not in registries.TAX_CHARACTERS:
            issues.append(f"{where}: unknown tax_character '{character}'")
            continue
        basis_character = (character in registries.BASIS_TAX_CHARACTERS
                           or character == "capital_improvement")
        if basis_of and not basis_character:
            issues.append(
                f"{where}: basis_of needs tax_character "
                f"{', '.join(sorted(registries.BASIS_TAX_CHARACTERS))} "
                "(recover the basis of a sale) or capital_improvement (add to it)"
            )
        elif basis_character and not basis_of:
            issues.append(f"{where}: tax_character {character} needs basis_of, the asset lot")
        if not basis_of:
            continue
        if basis_of not in asset_accounts:
            issues.append(f"{where}: basis_of '{basis_of}' is not an asset account")
            continue
        if basis_of in retirement_accounts:
            # A retirement lot's basis follows its own rules (#217), not a sale's.
            issues.append(
                f"{where}: basis_of '{basis_of}' is a retirement account; its basis "
                "is tracked for you, so leave basis_of blank"
            )
            continue
        problem = _resolve_ref(row, "basis_of", "basis_of_lot", leaves, frozenset())
        if problem:
            issues.append(f"{where}: basis_of {problem}")
        if character in registries.BASIS_TAX_CHARACTERS:
            after_basis_step = True
            sized = [c for c in ("maximum", "percent") if row.get(c)]
            if sized:
                issues.append(
                    f"{where}: a basis step is sized by the lot's basis, so "
                    f"{' and '.join(sized)} must be blank"
                )


def _lifespan_age(raw: str, col: str, where: str, issues: list[str]) -> float | None:
    """A lifespan.csv age cell (#221) as decimal years, or ``None`` (reported)."""
    try:
        age = float(raw)
    except ValueError:
        issues.append(f"{where}: {col} '{raw}' is not an age in years")
        return None
    if not 0 < age <= registries.MAX_AGE_AT_DEATH:  # also rejects NaN
        issues.append(
            f"{where}: {col} must be an age above 0 and at most "
            f"{registries.MAX_AGE_AT_DEATH:g} (got {raw})"
        )
        return None
    return age


def _validate_lifespan(
    rows: list[dict[str, str]],
    persons: list[dict[str, str]],
    scenario_start: str,
    issues: list[str],
    *,
    life_tables: list[tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, tuple[str, str]]:
    """lifespan.csv (#221): each row names a person and an ``age_at_death``,
    which becomes the row's ``final_month`` from the person's birthdate. A
    varying row (``vary_distribution``) has age bounds that hold the age and
    its mode; its lowest age can't fall before the scenario starts, since
    no member can die before then. ``vary_correlate_with``/
    ``vary_correlation`` go together, on a varying row, rho in [-1, 1]; the
    parent is checked with the calendar's forest. A ``life_table`` row
    (#222) takes no bounds; its set-forward is solved here from the person's
    sex and the aux ``life_table`` in ``life_tables`` (effective date,
    payload), and kept on the row for the writer; ``None`` (a broken table,
    already reported) skips that.

    Returns each varying row's correlation node (``lifespan:<person>``),
    mapped to its parent (or "") and where to report it."""
    birthdates = {p["id"]: p.get("birthdate", "") for p in persons if p.get("id")}
    sexes = {p["id"]: (p.get("sex") or "").strip() for p in persons if p.get("id")}
    seen: set[str] = set()
    links: dict[str, tuple[str, str]] = {}
    for where, row in _located(rows, "lifespan.csv"):
        person = row.get("person", "")
        where = f"{where} ({person or '?'})"
        if "final_month" in row:
            continue  # _old_lifespan_header reported the file
        raw_age = row.get("age_at_death", "")
        if not person or not raw_age:
            issues.append(f"{where}: person and age_at_death are required")
            continue
        if person not in birthdates:
            issues.append(f"{where}: person '{person}' not in persons.csv")
            continue
        if person in seen:
            issues.append(f"{where}: duplicate person '{person}'")
        seen.add(person)
        age = _lifespan_age(raw_age, "age_at_death", where, issues)
        if age is None:
            continue
        if not birthdates[person]:
            issues.append(
                f"{where}: an age at death needs '{person}'s birthdate in persons.csv"
            )
            continue
        try:
            row["final_month"] = final_month_for_age(birthdates[person], age)
        except ValueError:
            continue  # persons.csv reports the bad birthdate
        _validate_lifespan_vary(row, age, birthdates[person], scenario_start, where, issues, links)
        if (row.get("vary_distribution") or "").strip() == registries.LIFE_TABLE_DISTRIBUTION:
            _solve_life_table(
                row, sexes[person], birthdates[person], scenario_start, life_tables,
                where, issues,
            )
    return links


_SET_FORWARD_KEY = "life_table_set_forward"
"""Where ``_solve_life_table`` leaves a row's solved set-forward (#222)."""


def _solve_life_table(
    row: dict[str, Any], sex: str, birthdate: str, scenario_start: str,
    life_tables: list[tuple[str, dict[str, Any]]] | None, where: str, issues: list[str],
) -> None:
    """A ``life_table`` lifespan's set-forward (#222): the one that puts the
    table's median age at death, given alive at the scenario start, at
    ``age_at_death``."""
    if sex not in registries.PERSON_SEXES:
        issues.append(
            f"{where}: a life_table lifespan needs '{row['person']}'s sex "
            f"({'/'.join(registries.PERSON_SEXES)}) in persons.csv's sex column"
        )
        return
    if not scenario_start or life_tables is None:
        return  # reported where the start or the table is read
    try:
        table = life_table.life_table_for(life_tables, sex, scenario_start)
        start = life_table.months_since_birth(birthdate, scenario_start)
        median = round(float(row["age_at_death"]) * 12)
        row[_SET_FORWARD_KEY] = life_table.solve_set_forward(table, start, median)
    except life_table.LifeTableError as e:
        issues.append(f"{where}: life_table age_at_death {row['age_at_death']}: {e}")
    except ValueError:
        return  # the bad birthdate or start is reported where it's read


def _validate_life_tables(
    aux_rows: list[tuple[str, str | None, dict[str, Any], str]], issues: list[str],
) -> bool:
    """Each ``life_table`` vintage (#222): its ages run 0, 1, 2, ... once
    each, and every q is a probability. Whether all of them do."""
    before = len(issues)
    vintages: dict[str, list[dict[str, Any]]] = {}
    for kind, _, payload, effective in aux_rows:
        if kind == life_table.LIFE_TABLE_KIND:
            vintages.setdefault(effective, []).append(payload)
    for effective, payloads in sorted(vintages.items()):
        where = f"aux life_table.csv ({effective[:7]})"
        ages = sorted(p["age"] for p in payloads if isinstance(p["age"], int))
        if len(ages) != len(payloads) or ages != list(range(len(ages))):
            issues.append(f"{where}: ages must be whole numbers 0, 1, 2, ... each once, no gaps")
        for p in payloads:
            for column in registries.LIFE_TABLE_Q_COLUMNS.values():
                q = p[column + "_micros"]
                if not 0 <= q <= 1_000_000:
                    issues.append(
                        f"{where} age {p['age']}: {column} must be between 0 and 1 "
                        f"(got {q / 1_000_000:g})"
                    )
    return len(issues) == before


def _validate_lifespan_vary(
    row: dict[str, str], age: float, birthdate: str, scenario_start: str, where: str,
    issues: list[str], links: dict[str, tuple[str, str]],
) -> None:
    """A lifespan row's ``vary_*`` and correlation cells (#221)."""
    distribution = (row.get("vary_distribution") or "").strip()
    stray = [
        c for c in ("vary_low", "vary_high", "vary_mode", "vary_correlate_with",
                    "vary_correlation")
        if (row.get(c) or "").strip()
    ]
    if not distribution:
        if stray:
            issues.append(f"{where}: {', '.join(stray)} need vary_distribution")
        return
    if distribution not in registries.LIFESPAN_DISTRIBUTIONS:
        issues.append(
            f"{where}: a lifespan's vary_distribution must be one of "
            f"{list(registries.LIFESPAN_DISTRIBUTIONS)} (got '{distribution}')"
        )
        return
    if distribution == registries.LIFE_TABLE_DISTRIBUTION:
        bounded = [c for c in ("vary_low", "vary_high", "vary_mode") if (row.get(c) or "").strip()]
        if bounded:
            issues.append(
                f"{where}: life_table draws around age_at_death, its median; "
                f"{', '.join(bounded)} must be blank"
            )
        _validate_lifespan_link(row, where, issues, links)
        return
    bounds: dict[str, float] = {}
    for col in ("vary_low", "vary_high", "vary_mode"):
        raw = (row.get(col) or "").strip()
        if not raw:
            continue
        if col == "vary_mode" and distribution != "triangular":
            issues.append(f"{where}: vary_mode is only used by triangular")
            continue
        value = _lifespan_age(raw, col, where, issues)
        if value is not None:
            bounds[col] = value
    if not (row.get("vary_low") or "").strip() or not (row.get("vary_high") or "").strip():
        issues.append(f"{where}: {distribution} needs vary_low and vary_high (ages)")
        return
    if "vary_low" not in bounds or "vary_high" not in bounds:
        return
    low, high = bounds["vary_low"], bounds["vary_high"]
    mode = bounds.get("vary_mode", age)
    if not low < high:
        issues.append(f"{where}: vary_low must be below vary_high (got {low:g}..{high:g})")
        return
    if not low <= age <= high:
        issues.append(
            f"{where}: age_at_death {age:g} must lie within vary_low..vary_high "
            f"({low:g}..{high:g}): it is the base case, member 0"
        )
    if not low <= mode <= high:
        issues.append(f"{where}: vary_mode {mode:g} must lie within {low:g}..{high:g}")
    if scenario_start and final_month_for_age(birthdate, low) < scenario_start:
        issues.append(
            f"{where}: vary_low {low:g} is the month {final_month_for_age(birthdate, low)[:7]}, "
            f"before the scenario starts ({scenario_start[:7]}); a member can't die before then"
        )
    _validate_lifespan_link(row, where, issues, links)


def _validate_lifespan_link(
    row: dict[str, str], where: str, issues: list[str], links: dict[str, tuple[str, str]],
) -> None:
    """A varying lifespan's correlation cells, and its node in ``links``."""
    parent = (row.get("vary_correlate_with") or "").strip()
    raw_rho = (row.get("vary_correlation") or "").strip()
    node = registries.LIFESPAN_NODE_PREFIX + row["person"]
    if bool(parent) != bool(raw_rho):
        issues.append(f"{where}: vary_correlate_with and vary_correlation go together")
        parent = ""
    elif raw_rho:
        try:
            rho = float(raw_rho)
        except ValueError:
            issues.append(f"{where}: vary_correlation '{raw_rho}' is not a number")
            parent = ""
        else:
            if not -1.0 <= rho <= 1.0:  # also rejects NaN
                issues.append(
                    f"{where}: vary_correlation must be between -1 and 1 (got {raw_rho})"
                )
                parent = ""
    if parent == node:
        issues.append(f"{where}: a lifespan can't correlate with itself")
        parent = ""
    links[node] = (parent, where)


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
    value_rows: list[dict[str, str]],
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
            "INSERT INTO person(id, display, birthdate, sex, role, meta_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                p["id"], p["display"], p["birthdate"],
                (p.get("sex") or "").strip() or None,
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

    # One account_lot row per lot (ADR-005). An account with no
    # account_lots.csv row gets a single zero-balance default lot. Rows are
    # written in accounts.csv order so lot ids follow account order.
    rows_by_account: dict[str, list[dict[str, str]]] = {}
    for m in value_rows:
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
                "INSERT INTO account_lot("
                "account_id, lot, subkind, tax_treatment, symbol, acquired, quantity_micros, "
                "series, opening_balance, opening_cost_basis, "
                "prior_year_end_balance, minimum_balance_cents, "
                "maximum_balance_cents, refill_target_cents, refill_amount_cents) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account_ids[a["name"]],
                    lot,
                    m.get("subkind") or None,
                    registries.RETIREMENT_LOT_SUBKINDS.get(m.get("subkind") or "")
                    or a.get("tax_treatment") or None,
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
    counts["account_lot"] = member_count

    # Liquidity chains (ADR-003 §4.7): one implicit leaf chain per lot, plus
    # one interior chain per chains/<role>/*.csv file, in one shared name space.
    chain_ids: dict[str, int] = {}
    for leaf, lot_id in lot_ids.items():
        account_name = lot_account[leaf]
        cur = con.execute(
            "INSERT INTO chain(name, role, account_id, lot_id) VALUES (?,NULL,?,?)",
            (leaf, account_ids[account_name], lot_id),
        )
        chain_ids[leaf] = inserted_id(cur)
    for name, (role, _rows) in chains.items():
        cur = con.execute(
            "INSERT INTO chain(name, role, account_id, lot_id) VALUES (?,?,NULL,NULL)",
            (name, role),
        )
        chain_ids[name] = inserted_id(cur)
    counts["chain"] = len(chain_ids)

    step_count = 0
    for name, (_role, rows) in chains.items():
        for i, row in enumerate(rows, start=1):
            con.execute(
                "INSERT INTO chain_steps(chain_id, step_order, draw_from_chain_id, "
                "maximum_cents, percent_micros, series, cadence, tax_character, "
                "basis_of_lot_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    chain_ids[name], i, chain_ids[row["account_name"]],
                    _dollars_to_cents(row["maximum"], resolver=money_resolver)
                    if row.get("maximum") else None,
                    _percent_to_micros(row["percent"])
                    if row.get("percent") else 1_000_000,
                    row.get("series") or None,
                    row.get("cadence") or None,
                    row.get("tax_character") or None,
                    lot_ids[row["basis_of"]] if row.get("basis_of") else None,
                ),
            )
            step_count += 1
    counts["chain_steps"] = step_count

    for leaf, m in lot_rows.items():
        refill = m.get("refill_chain") or None
        overflow = m.get("overflow_chain") or None
        if refill or overflow:
            con.execute(
                "UPDATE account_lot SET refill_chain_id = ?, "
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
            "vary_correlate_with, vary_correlation, vary_tail_dependence, "
            "vary_process, vary_persistence, vary_innovation_sd, vary_outcomes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                ce.get("vary_process") or None,
                _opt_float(ce.get("vary_persistence")),
                _opt_int(ce.get("vary_innovation_sd")),
                ce.get("vary_outcomes") or None,
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
            "name, from_account, to_account, cadence, month_of_year, "
            "amount_rule_kind, "
            "interest_account, person, series, rate_micros, claim_age, amount_cents, "
            "params_json, base_date, "
            "tax_character, tax_year, start, \"end\", priority, "
            "description, meta_json, ytd_amount, ytd_preroll, inflation_cadence"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s["name"],
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
        "name, description, calendar_id, start, \"end\", "
        "seed, parents_json, load_order_json, created_at, created_by, meta_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            scenario_row["name"], scenario_row.get("description") or None,
            calendar_id,
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
            "INSERT INTO person_lifespan(scenario_id, person, age_at_death, final_month, "
            "vary_distribution, vary_low, vary_high, vary_mode, vary_correlate_with, "
            "vary_correlation, life_table_set_forward) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                scenario_id, lf["person"], float(lf["age_at_death"]), lf["final_month"],
                lf.get("vary_distribution") or None,
                _opt_float(lf.get("vary_low")), _opt_float(lf.get("vary_high")),
                # A blank mode is the authored age, so the base case is the
                # most likely value (#221).
                (_opt_float(lf.get("vary_mode")) or float(lf["age_at_death"]))
                if lf.get("vary_distribution") == "triangular" else None,
                lf.get("vary_correlate_with") or None, _opt_float(lf.get("vary_correlation")),
                lf.get(_SET_FORWARD_KEY),
            ),
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
