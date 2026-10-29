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

from analysis.sim import drift, registries, units
from analysis.sim.asset_classes import weight_sets
from analysis.sim.strategy_api import Strategy, param_units

__all__ = [
    "ParamInfo", "decision_log", "describe_kind", "drift_by_year", "kind_params", "parse_param_specs",
    "params_text", "read_rows", "summary", "target_sets", "write_rows",
]


# ---------- a kind's parameters ----------

@dataclass(frozen=True)
class ParamInfo:
    """One ``Params`` field, for people: its unit, default (as written in
    ``params_json``, or None when required) and docstring."""

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
        if f.default is not dataclasses.MISSING:
            default: str | None = written(unit, f.default)
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
    """``params_json`` as written to strategies.csv: compact, keys in order given."""
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
    """One line for a strategies.csv row."""
    when = ""
    if row.get("start") or row.get("end"):
        when = f" {row.get('start') or '…'} to {row.get('end') or '…'}"
    off = "" if (row.get("enabled") or "").strip().lower() not in ("false", "no", "0") else " (disabled)"
    params = f" {row['params_json']}" if row.get("params_json") else ""
    return f"{row.get('kind', '')} on {row.get('chain', '')}{when}{params}{off}"


def target_sets(rows: Sequence[Mapping[str, str]]) -> dict[str, list[tuple[str, dict[str, str]]]]:
    """targets.csv rows by set, each set's dated weights oldest first."""
    sets = weight_sets((row["target_set"], row.get("as_of", ""), row["asset_class"], row["weight"]) for row in rows)
    return dict(sorted(sets.items()))


# ---------- reading a run ----------

def decision_log(
    con: sqlite3.Connection, *, strategy: str | None = None, year: int | None = None,
) -> pd.DataFrame:
    """A run's strategy decisions, oldest first: month, strategy, hook, asked,
    chosen and fallback (dollars), and the note."""
    query = (
        "SELECT s.month, st.name AS strategy, d.hook, d.asked_cents, d.chosen_cents, "
        "       d.fallback_cents, d.note "
        "FROM strategy_decision d JOIN simulation_step s ON s.id = d.step_id "
        "JOIN strategy st ON st.id = d.strategy_id WHERE 1=1"
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
    return frame[["month", "strategy", "hook", "asked", "chosen", "fallback", "note"]]


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
