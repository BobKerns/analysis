"""Validating a scenario's strategies and allocation targets at load (#326,
ADR-010 §4), and writing them into the intent.

The loader reads ``strategies.csv`` and ``targets.csv`` over the lineage and
hands the merged rows here, each with where it was authored. Every problem
is reported as a load error naming that row, as the loader does for the
other files: a strategy that can't run must not wait for its month to fail.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from analysis.sim import registries, units
from analysis.sim.asset_classes import UNCLASSIFIED, class_weights
from analysis.sim.strategy_api import Strategy, param_units
from analysis.sim.strategy_code import StrategyKinds

__all__ = ["PreparedStrategy", "parse_enabled", "prepare_strategies", "validate_targets",
           "write_strategies"]

_TRUE = {"", "true", "yes", "1"}
_FALSE = {"false", "no", "0"}


def parse_enabled(raw: str | None) -> bool:
    """``enabled``: blank or true/yes/1 is on, false/no/0 is off."""
    text = (raw or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"enabled must be true or false, not {raw!r}")


@dataclass(frozen=True)
class PreparedStrategy:
    """One enabled ``strategies.csv`` row, checked, with params in stored units."""

    name: str
    kind: str
    chain: str
    start: str | None
    end: str | None
    params: dict[str, Any]
    description: str


def validate_targets(located: Sequence[tuple[str, Mapping[str, str]]], issues: list[str]) -> set[str]:
    """Each (target_set, as_of) is a set of asset-class weights summing to
    100%, as in allocations.csv. Returns the set names."""
    sums: dict[tuple[str, str], int] = {}
    for where, row in located:
        name, as_of = row.get("target_set", ""), row.get("as_of", "")
        asset_class = row.get("asset_class", "")
        where = f"{where} ({name or '?'})"
        if not name:
            issues.append(f"{where}: missing target_set")
            continue
        if asset_class not in registries.ASSET_CLASSES:
            issues.append(
                f"{where}: unknown asset_class '{asset_class}'; "
                f"allowed: {', '.join(registries.ASSET_CLASSES)}"
            )
        try:
            weight = units.percent_to_micros(row.get("weight", ""))
        except ValueError as e:
            issues.append(f"{where}: weight {e}")
            continue
        if not 0 <= weight <= 1_000_000:
            issues.append(f"{where}: weight {row.get('weight')} must be between 0% and 100%")
            continue
        sums[(name, as_of)] = sums.get((name, as_of), 0) + weight
    for (name, as_of), total in sums.items():
        if total != 1_000_000:
            when = f" as of {as_of}" if as_of else ""
            issues.append(
                f"targets.csv ({name}{when}): weights add up to {total / 10_000:g}%, not 100%"
            )
    return {name for name, _as_of in sums}


def _convert_param(
    unit: str | None, value: object, *, tags: Collection[str], target_sets: Collection[str],
) -> object:
    match unit:
        case "percent":
            if not isinstance(value, str):
                raise ValueError(f"{value!r} must be a percent string like \"5%\"")
            return units.percent_to_micros(value)
        case "dollars":
            return units.json_dollars_to_cents(value)
        case "tag":
            if not isinstance(value, str) or ":" not in value:
                raise ValueError(f"{value!r} must be an account tag like \"portfolio:invest\"")
            if value not in tags:
                raise ValueError(f"no account carries tag {value!r}")
            return value
        case "target_set":
            if value not in target_sets:
                known = ", ".join(sorted(target_sets)) or "none in targets.csv"
                raise ValueError(f"no target set {value!r} (known: {known})")
            return value
        case _:
            return value


def convert_params(
    cls: type[Strategy], raw: Mapping[str, object], *,
    tags: Collection[str], target_sets: Collection[str],
) -> tuple[dict[str, Any], list[str]]:
    """``params_json`` in stored units, checked against ``cls.Params``; the
    converted values and the problems (one per bad field)."""
    problems: list[str] = []
    fields = {f.name: f for f in dataclasses.fields(cls.Params)}
    unit_of = param_units(cls.Params)
    for key in raw:
        if key not in fields:
            problems.append(f"unknown parameter '{key}' (known: {', '.join(fields) or 'none'})")
    out: dict[str, Any] = {}
    for key, f in fields.items():
        if key not in raw:
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                problems.append(f"parameter '{key}' is required")
            continue
        unit = unit_of[key]
        try:
            out[key] = _convert_param(
                unit.name if unit else None, raw[key], tags=tags, target_sets=target_sets,
            )
        except ValueError as e:
            problems.append(f"parameter '{key}': {e}")
    if not problems:
        try:
            cls.Params(**out)
        except (TypeError, ValueError) as e:
            problems.append(f"parameters: {e}")
    return out, problems


def _marked_steps(
    chain: str, chains: Mapping[str, tuple[str, list[dict[str, str]]]], seen: set[str] | None = None,
) -> str | None:
    """The first chain at or under ``chain`` with a step carrying a
    ``tax_character`` or ``basis_of``, or None."""
    seen = seen if seen is not None else set()
    if chain in seen or chain not in chains:
        return None
    seen.add(chain)
    steps = chains[chain][1]
    if any(step.get("tax_character") or step.get("basis_of") for step in steps):
        return chain
    for step in steps:
        found = _marked_steps(step.get("account_name", ""), chains, seen)
        if found:
            return found
    return None


def prepare_strategies(
    located: Sequence[tuple[str, Mapping[str, str]]], *,
    kinds: StrategyKinds,
    chains: Mapping[str, tuple[str, list[dict[str, str]]]],
    account_names: Collection[str],
    tags: Collection[str],
    target_sets: Collection[str],
    issues: list[str],
) -> list[PreparedStrategy]:
    """The enabled strategies, each checked: a known kind, an interior chain
    whose role fits a hook the kind implements, owned by no other enabled
    strategy, a start no later than its end, and params its ``Params``
    accepts. Disabled rows are checked too, except for chain ownership."""
    prepared: list[PreparedStrategy] = []
    owner_of: dict[str, str] = {}
    for where, row in located:
        name = row.get("name", "")
        where = f"{where} ({name or '?'})"
        problems: list[str] = [] if name else ["missing name"]
        try:
            enabled = parse_enabled(row.get("enabled"))
        except ValueError as e:
            problems.append(str(e))
            enabled = True
        kind = row.get("kind", "")
        cls = kinds.kinds.get(kind)
        if not kind:
            problems.append("missing kind")
        elif cls is None:
            problems.append(
                f"unknown kind '{kind}' (known: {', '.join(sorted(kinds.kinds)) or 'none'})"
            )
        chain = row.get("chain", "")
        role = chains.get(chain, (None, []))[0]
        if not chain:
            problems.append("missing chain")
        elif chain not in chains:
            problems.append(
                f"chain '{chain}' is an account; a strategy owns a chain with steps"
                if chain in account_names else f"no chain named '{chain}'"
            )
        elif cls is not None:
            hook = "draw" if role == "source" else "place"
            if hook not in cls.hooks:
                problems.append(
                    f"kind '{kind}' has no {hook} hook, which a {role} chain needs "
                    f"(it implements {', '.join(sorted(cls.hooks))})"
                )
            marked = _marked_steps(chain, chains) if role == "destination" else None
            if marked:
                # A step's character and basis recovery depend on the step
                # order the strategy would override (#199), nested chains'
                # steps too, since a strategy puts straight into a leaf.
                where = "" if marked == chain else f" (through chain '{marked}')"
                problems.append(
                    f"chain '{chain}' has steps with a tax_character or basis_of{where}; "
                    "a strategy can't place into such a chain"
                )
        start, end = row.get("start") or None, row.get("end") or None
        if start and end and start > end:
            problems.append(f"start {start} is after end {end}")
        params: dict[str, Any] = {}
        raw_json = (row.get("params_json") or "").strip() or "{}"
        try:
            raw = json.loads(raw_json)
        except json.JSONDecodeError as e:
            problems.append(f"params_json is not valid JSON: {e}")
            raw = None
        if raw is not None and not isinstance(raw, dict):
            problems.append("params_json must be a JSON object")
        elif raw is not None and cls is not None:
            params, bad = convert_params(cls, raw, tags=tags, target_sets=target_sets)
            problems.extend(bad)
        if enabled and chain in chains:
            if chain in owner_of:
                problems.append(f"chain '{chain}' is already owned by strategy '{owner_of[chain]}'")
            else:
                owner_of[chain] = name
        issues.extend(f"{where}: {p}" for p in problems)
        if enabled and not problems:
            prepared.append(PreparedStrategy(
                name=name, kind=kind, chain=chain, start=start, end=end,
                params=params, description=row.get("description", ""),
            ))
    return prepared


def check_portfolio_classes(
    prepared: Sequence[PreparedStrategy], *,
    kinds: StrategyKinds,
    lots: Sequence[tuple[str, str, str | None, str, str, frozenset[str]]],
    sets: Mapping[str, Sequence[tuple[str, Mapping[str, float]]]],
    start: str,
    issues: list[str],
) -> None:
    """Every asset lot in a strategy's tagged portfolio has an asset class
    (Bob, #326): an ``unclassified`` lot would silently skew the shares, so
    say which row to add. ``lots`` is ``(account, lot, symbol, account kind,
    account subkind, account tags)``."""
    for s in prepared:
        unit_of = param_units(kinds.get(s.kind).Params)
        for key, unit in unit_of.items():
            if unit is None or unit.name != "tag" or key not in s.params:
                continue
            tag = s.params[key]
            for account, lot, symbol, kind, subkind, tags in lots:
                if kind != "asset" or tag not in tags:
                    continue
                weights = class_weights(
                    symbol=symbol, account=account, lot=lot, subkind=subkind, sets=sets, date=start,
                )
                if UNCLASSIFIED in weights:
                    issues.append(
                        f"strategies.csv ({s.name}): lot {account}:{lot} in portfolio {tag} has no "
                        f"asset class; add allocations.csv rows for "
                        f"{'symbol ' + symbol if symbol else repr(f'{account}:{lot}')}"
                    )


def write_strategies(
    con: sqlite3.Connection, prepared: Sequence[PreparedStrategy], *,
    kinds: StrategyKinds,
    sources: Mapping[str, str],
    target_rows: Sequence[Mapping[str, str]],
) -> dict[str, int]:
    """Insert ``strategy``, ``allocation_target`` and, when an enabled
    strategy is a data-repo kind, every data-repo module's source
    (``strategy_source``): helpers are imported by path, so the snapshot is
    the whole folder."""
    counts: dict[str, int] = {}
    chain_id = dict(con.execute("SELECT name, id FROM chain").fetchall())
    for s in prepared:
        con.execute(
            'INSERT INTO strategy(name, kind, chain_id, start, "end", params_json, description) '
            "VALUES (?,?,?,?,?,?,?)",
            (s.name, s.kind, chain_id[s.chain], s.start, s.end,
             json.dumps(s.params, sort_keys=True, separators=(",", ":")), s.description),
        )
    counts["strategy"] = len(prepared)
    from_data_repo = any(kinds.origin.get(s.kind) != "built-in" for s in prepared)
    snapshot = sources if from_data_repo else {}
    for module, text in sorted(snapshot.items()):
        con.execute(
            "INSERT INTO strategy_source(module, sha256, source) VALUES (?,?,?)",
            (module, hashlib.sha256(text.encode()).hexdigest(), text),
        )
    counts["strategy_source"] = len(snapshot)
    for row in target_rows:
        con.execute(
            "INSERT INTO allocation_target(target_set, as_of, asset_class, weight_micros) "
            "VALUES (?,?,?,?)",
            (row["target_set"], row.get("as_of", ""), row["asset_class"],
             units.percent_to_micros(row["weight"])),
        )
    counts["allocation_target"] = len(target_rows)
    return counts
