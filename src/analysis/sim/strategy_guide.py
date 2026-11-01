"""What the ``sim strategy`` and ``sim target`` commands show and ask (#326,
ADR-010 §6), without the terminal: a kind's parameters and their docs,
parameters as written in ``params_json`` and as typed on a command line,
the decision log, and allocation drift over a run.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import sqlite3
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from analysis.frames import named_rows
from analysis.sim import drift, registries, units
from analysis.sim.asset_classes import weight_sets
from analysis.sim.strategy_api import Strategy, param_units

__all__ = [
    "ParamInfo", "breach_episodes", "breach_text", "breaches", "decision_log", "describe_kind", "drift_by_year",
    "kind_params", "parse_param_specs",
    "params_text", "read_rows", "summary", "target_sets", "write_rows",
]


# ---------- a kind's parameters ----------

@dataclass(frozen=True)
class ParamInfo:
    """One ``Params`` field, for people: its unit, default (as written in
    ``params_json``; ``blank`` for an optional one, None when required) and docstring."""

    name: str
    unit: str | None
    default: str | None
    doc: str


def _field_docs(params: type) -> dict[str, str]:
    """The string literal under each field of ``params`` (each attribute's
    docstring), read from its source; {} when the source can't be read."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(params)))
    except (OSError, TypeError, SyntaxError):
        return {}
    body = tree.body[0].body if tree.body and isinstance(tree.body[0], ast.ClassDef) else []
    docs: dict[str, str] = {}
    for stmt, after in zip(body, [*body[1:], None], strict=True):
        if (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                and isinstance(after, ast.Expr) and isinstance(after.value, ast.Constant)
                and isinstance(after.value.value, str)):
            docs[stmt.target.id] = " ".join(after.value.value.split())
    return docs


def written(unit: str | None, value: object) -> str:
    """A parameter's stored value as a person writes it in ``params_json``:
    ``50000`` millionths as ``5%``, cents as dollars."""
    match unit:
        case "percent" if isinstance(value, int):
            return units.micros_to_percent(value)
        case "dollars" if isinstance(value, int):
            return units.cents_to_dollars(value)
        case _:
            return value if isinstance(value, str) else json.dumps(value)


def kind_params(cls: type[Strategy]) -> list[ParamInfo]:
    """``cls.Params``'s fields in order."""
    docs = _field_docs(cls.Params)
    unit_of = param_units(cls.Params)
    out = []
    for f in dataclasses.fields(cls.Params):
        found = unit_of[f.name]
        unit = found.name if found else None
        if f.default is None:
            default: str | None = "blank"  # an optional parameter (#421)
        elif f.default is not dataclasses.MISSING:
            default = written(unit, f.default)
        elif f.default_factory is not dataclasses.MISSING:
            default = written(unit, f.default_factory())
        else:
            default = None
        out.append(ParamInfo(f.name, unit, default, docs.get(f.name, "")))
    return out


def describe_kind(cls: type[Strategy]) -> str:
    """The kind's docstring without its indentation, paragraphs kept."""
    return inspect.cleandoc(cls.__doc__ or "").strip()


# ---------- params_json and the command line ----------

def params_text(params: Mapping[str, Any]) -> str:
    """``params_json`` as written to a strategy file: compact, keys in order given."""
    return json.dumps(dict(params), separators=(",", ":")) if params else ""


def parse_param_specs(cls: type[Strategy], specs: Sequence[str]) -> dict[str, Any]:
    """``--param NAME=VALUE`` options as ``params_json`` values: a percent,
    dollar amount, tag or target set stays the text typed (the loader checks
    it); any other value is read as JSON when it parses, else kept as text.
    ``NAME=`` (nothing after the ``=``) drops the parameter, so its default
    applies."""
    fields = {f.name for f in dataclasses.fields(cls.Params)}
    unit_of = param_units(cls.Params)
    out: dict[str, Any] = {}
    for spec in specs:
        name, eq, value = spec.partition("=")
        name = name.strip()
        if not eq or not name:
            raise ValueError(f"--param takes NAME=VALUE, not {spec!r}")
        if name not in fields:
            raise ValueError(f"kind '{cls.kind}' has no parameter {name!r} (it has {', '.join(fields) or 'none'})")
        if value == "":
            out[name] = None
            continue
        if unit_of[name] is not None:
            out[name] = value
            continue
        try:
            out[name] = json.loads(value)
        except json.JSONDecodeError:
            out[name] = value
    return out


# ---------- the CSV rows ----------

def read_rows(path: Path, header: Sequence[str]) -> list[dict[str, str]]:
    """A scenario file's rows, every ``header`` column present (blank when
    the file leaves it out); [] for no file."""
    if not path.is_file() or path.stat().st_size == 0:
        return []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [{c: str(row.get(c, "")) for c in header} for row in frame.to_dict("records")]


def write_rows(path: Path, header: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    """Write ``rows`` under ``header``; no rows removes the file."""
    if not rows:
        path.unlink(missing_ok=True)
        return
    pd.DataFrame([{c: row.get(c, "") for c in header} for row in rows], columns=list(header)).to_csv(
        path, index=False,
    )


def summary(row: Mapping[str, str]) -> str:
    """One line for a strategy file's row."""
    when = ""
    if row.get("start") or row.get("end"):
        when = f" {row.get('start') or '…'} to {row.get('end') or '…'}"
    off = "" if (row.get("enabled") or "").strip().lower() not in ("false", "no", "0") else " (disabled)"
    params = f" {row['params_json']}" if row.get("params_json") else ""
    owns = " and ".join(
        part for part in (row.get("chain", ""), f"rules {row['rules']}" if row.get("rules") else "") if part
    )
    on = f" on {owns}" if owns else ""  # a year-end kind owns nothing (#421)
    answers = f" answering {row['trigger']}" if (row.get("trigger") or "").strip() else ""
    return f"{row.get('kind', '')}{on}{answers}{when}{params}{off}"


def target_sets(rows: Sequence[Mapping[str, str]]) -> dict[str, list[tuple[str, dict[str, str]]]]:
    """targets.csv rows by set, each set's dated weights oldest first."""
    sets = weight_sets((row["target_set"], row.get("as_of", ""), row["asset_class"], row["weight"]) for row in rows)
    return dict(sorted(sets.items()))


# ---------- reading a run ----------

def has_rule_tags(con: sqlite3.Connection) -> bool:
    """Whether the file's strategies can own rules (#387): a run from before
    has only chains."""
    return any(r[1] == "rule_tag" for r in con.execute("PRAGMA table_info(strategy)"))


def owns_sql(con: sqlite3.Connection) -> str:
    """A SQL expression naming what strategy ``s`` owns, for a query that
    LEFT JOINs ``chain c ON c.id = s.chain_id``: the chain, ``rules <tag>``,
    or both; NULL for a year-end kind, which owns neither (#421)."""
    if not has_rule_tags(con):
        return "c.name"
    return (
        "CASE WHEN s.rule_tag IS NULL AND c.name IS NULL THEN NULL "
        "     WHEN s.rule_tag IS NULL THEN c.name "
        "     WHEN c.name IS NULL THEN 'rules ' || s.rule_tag "
        "     ELSE c.name || ' and rules ' || s.rule_tag END"
    )


def decision_log(
    con: sqlite3.Connection, *, strategy: str | None = None, year: int | None = None,
) -> pd.DataFrame:
    """A run's strategy decisions, oldest first: month, strategy, hook, asked,
    chosen and fallback (dollars), and the note. A ``scale`` decision's note
    starts with the rule it scaled, an ``on_breach`` one's with the breach it
    answered (#441): ``tight:annual 2027 entered ($54,658 past)``."""
    columns = {r[1] for r in con.execute("PRAGMA table_info(strategy_decision)")}
    has_rule = "scheduled_transaction_id" in columns
    has_breach = "breach_event_id" in columns
    rule = (
        "CASE WHEN d.scheduled_transaction_id IS NULL THEN d.note "
        "     WHEN d.note = '' THEN r.name ELSE r.name || ': ' || d.note END AS note"
        if has_rule else "d.note"
    )
    join = ("LEFT JOIN scheduled_transaction r ON r.id = d.scheduled_transaction_id "
            if has_rule else "")
    breach = ("b.rail AS b_rail, b.margin_kind AS b_kind, b.year AS b_year, b.state AS b_state, "
              "b.margin_cents AS b_margin" if has_breach else
              "NULL AS b_rail, NULL AS b_kind, NULL AS b_year, NULL AS b_state, NULL AS b_margin")
    if has_breach:
        join += "LEFT JOIN breach_event b ON b.id = d.breach_event_id "
    query = (
        "SELECT s.month, st.name AS strategy, d.hook, d.asked_cents, d.chosen_cents, "
        f"       d.fallback_cents, {rule}, {breach} "
        "FROM strategy_decision d JOIN simulation_step s ON s.id = d.step_id "
        f"JOIN strategy st ON st.id = d.strategy_id {join}WHERE 1=1"
    )
    args: list[Any] = []
    if strategy:
        query += " AND st.name = ?"
        args.append(strategy)
    if year is not None:
        query += " AND substr(s.month, 1, 4) = ?"
        args.append(f"{year:04d}")
    frame = pd.read_sql_query(query + " ORDER BY d.id", con, params=args)
    for column in ("asked", "chosen", "fallback"):
        frame[column] = frame.pop(f"{column}_cents") / 100
    told = frame["b_rail"].notna()
    if told.any():
        frame.loc[told, "note"] = [
            breach_text(rail, kind, int(year), state, int(margin)) + (f": {note}" if note else "")
            for rail, kind, year, state, margin, note in zip(
                frame.loc[told, "b_rail"], frame.loc[told, "b_kind"], frame.loc[told, "b_year"],
                frame.loc[told, "b_state"], frame.loc[told, "b_margin"], frame.loc[told, "note"],
                strict=True)
        ]
    return frame[["month", "strategy", "hook", "asked", "chosen", "fallback", "note"]]


def breach_text(rail: str, margin_kind: str, year: int, state: str, margin_cents: int) -> str:
    """One breach (#441): ``tight:annual 2027 entered ($54,658 past)``, or
    ``($1,200 inside)`` once back."""
    side = "past" if margin_cents < 0 else "inside"
    return f"{rail}:{margin_kind} {year} {state} (${abs(margin_cents) / 100:,.0f} {side})"


def breaches(con: sqlite3.Connection, *, strategy: str | None = None) -> pd.DataFrame:
    """A run's breach events (#441), oldest first: month told, strategy,
    rail, margin kind, year, state, years past, measure, threshold and
    margin (dollars, CPI-constant), and whether a decision answered it.
    Empty for a run from before triggers."""
    columns = ["month", "strategy", "rail", "margin_kind", "year", "state", "years_past",
               "measure", "threshold", "margin", "answered"]
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'breach_event'").fetchone():
        return pd.DataFrame(columns=columns)
    query = (
        "SELECT s.month, st.name AS strategy, b.rail, b.margin_kind, b.year, b.state, b.years_past, "
        "       b.measure_cents, b.threshold_cents, b.margin_cents, "
        "       EXISTS (SELECT 1 FROM strategy_decision d WHERE d.breach_event_id = b.id) AS answered "
        "FROM breach_event b JOIN simulation_step s ON s.id = b.step_id "
        "JOIN strategy st ON st.id = b.strategy_id"
    )
    args: list[Any] = []
    if strategy:
        query += " WHERE st.name = ?"
        args.append(strategy)
    frame = pd.read_sql_query(query + " ORDER BY b.id", con, params=args)
    for column in ("measure", "threshold", "margin"):
        frame[column] = frame.pop(f"{column}_cents") / 100
    frame["answered"] = frame["answered"].astype(bool)
    return frame[columns]


def breach_episodes(con: sqlite3.Connection) -> list[tuple[str, str, str, int, int | None, int]]:
    """A run's spells past a triggered rail (#441): ``(strategy, rail,
    margin_kind, first year past, year back inside or None, answered)``,
    oldest first, where ``answered`` counts the spell's breaches a
    decision answered."""
    told = breaches(con)
    open_: dict[tuple[str, str, str], list] = {}
    out: list[tuple[str, str, str, int, int | None, int]] = []
    for row in named_rows(told, index=False):
        key = (str(row.strategy), str(row.rail), str(row.margin_kind))
        if row.state == "entered" or key not in open_:
            if key in open_:
                out.append((*key, open_[key][0], None, open_[key][1]))
            open_[key] = [int(row.year), 0]
        open_[key][1] += int(bool(row.answered))
        if row.state == "left":
            out.append((*key, open_[key][0], int(row.year), open_[key][1]))
            del open_[key]
    out.extend((*key, first, None, answered) for key, (first, answered) in open_.items())
    return sorted(out, key=lambda e: (e[3], e[0], e[1], e[2]))


def drift_by_year(
    con: sqlite3.Connection, target_set: str, *, portfolio: str | None = None,
) -> pd.DataFrame:
    """Each year-end of a run (and its last month): the portfolio's share in
    each asset class, less the target, in percentage points; and
    ``max_abs``, the largest. ``portfolio`` is an account tag; without it,
    every asset lot counts. Index: month."""
    return drift.year_end_rows(drift.drift_by_month(con, target_set, portfolio=portfolio))


def lineage_targets(scenarios_dir: Path, scenario: str) -> list[tuple[dict[str, str], str]]:
    """targets.csv over the scenario's load order, as the loader merges it
    (by set, ``as_of`` and class), each row with the scenario it comes from."""
    from analysis.sim.dates import month_end_iso
    from analysis.sim.lineage import load_order
    from analysis.sim.lineage import scenario_dir as folder_of

    merged: dict[tuple[str, str, str], tuple[dict[str, str], str]] = {}
    for name in load_order(scenarios_dir, scenario):
        for row in read_rows(folder_of(scenarios_dir, name) / "targets.csv", registries.TARGETS_CSV_HEADER):
            as_of = row["as_of"]
            if as_of:
                try:
                    as_of = month_end_iso(as_of)
                except ValueError:
                    pass
            merged[(row["target_set"], as_of, row["asset_class"])] = ({**row, "as_of": as_of}, name)
    return [merged[key] for key in sorted(merged)]
