"""Validating a scenario's strategies and allocation targets at load (#326,
ADR-010 §4), and writing them into the intent.

The loader reads ``strategies/<name>.csv``, ``strategy_vary/<name>.csv`` and
``targets.csv`` over the lineage and hands the merged rows here, each with
where it was authored. Every problem
is reported as a load error naming that row, as the loader does for the
other files: a strategy that can't run must not wait for its month to fail.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, get_type_hints

from analysis.sim import registries, units
from analysis.sim.distributions import DistributionError, rewrite_outcomes
from analysis.sim.asset_classes import UNCLASSIFIED, class_weights
from analysis.sim.strategy_api import ScenarioView, Strategy, param_units
from analysis.sim.strategy_code import StrategyKinds

__all__ = ["PreparedStrategy", "PreparedVary", "parse_enabled", "prepare_strategies",
           "prepare_vary", "validate_targets", "write_strategies"]

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
    """One enabled strategy file's row, checked, with params in stored units."""

    name: str
    kind: str
    chain: str
    rules: str
    start: str | None
    end: str | None
    params: dict[str, Any]
    description: str
    triggers: tuple[tuple[str, str], ...] = ()
    """The guard rails it answers (#441): ``(rail, margin kind)``, in file order."""


@dataclass(frozen=True)
class PreparedVary:
    """One varied strategy parameter (#408), checked: its bounds in the
    parameter's stored unit, as text, and its authored value, ``base``."""

    strategy: str
    param: str
    base: float
    unit: str  # its stored unit: cents, micros or number
    whole: bool
    columns: dict[str, str | None]  # vary_* cell -> stored-unit text, or None

    @property
    def node(self) -> str:
        return f"{registries.STRATEGY_NODE_PREFIX}{self.strategy}.{self.param}"


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


OWNING_HOOKS = frozenset({"draw", "place", "scale"})
"""Hooks that act on what a strategy owns: a chain, or rules by tag."""


def _convert_param(
    unit: str | None, value: object, *, tags: Collection[str], target_sets: Collection[str],
    accounts: Collection[str] | None = None,
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
        case "account":
            if not isinstance(value, str) or not value:
                raise ValueError(f"{value!r} must be an account name")
            if accounts is not None and value not in accounts:
                raise ValueError(f"no account named {value!r}")
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
    tags: Collection[str], target_sets: Collection[str], accounts: Collection[str] | None = None,
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
        if raw[key] is None and f.default is None:
            out[key] = None  # an optional parameter left unset
            continue
        try:
            out[key] = _convert_param(
                unit.name if unit else None, raw[key], tags=tags, target_sets=target_sets,
                accounts=accounts,
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
    rules: Sequence[tuple[str, str, frozenset[str]]] = (),
    scenario: ScenarioView | None = None,
    rails: Collection[str] = (),
    issues: list[str],
) -> list[PreparedStrategy]:
    """The enabled strategies, each checked: a known kind; an interior chain
    whose role fits a hook the kind implements, or a rule tag (``rules``) on
    ``fixed_amount`` rules for a kind with a ``scale`` hook, or both; each
    chain and rule owned by no other enabled strategy; a start no later than
    its end; and params its ``Params`` accepts. Disabled rows are checked
    too, except for ownership. ``rules`` is each scheduled rule's ``(name,
    amount_rule_kind, tags)``. With ``scenario``, the kind's own
    :meth:`Strategy.check` runs on params that passed (#421). A ``trigger``
    names rails among ``rails`` (the lineage's guard rails, #441), each
    ``<rail>:annual`` or ``<rail>:cumulative``, for a kind with an
    ``on_breach`` hook."""
    prepared: list[PreparedStrategy] = []
    owner_of: dict[str, str] = {}
    rule_owner: dict[str, str] = {}
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
        rule_tag = (row.get("rules") or "").strip()
        role = chains.get(chain, (None, []))[0]
        owned_rules = _owned_rules(rule_tag, rules, kind, cls, problems) if rule_tag else []
        if not chain:
            if not rule_tag and cls is not None and cls.hooks & OWNING_HOOKS:
                # A year_end kind acts on the lots its parameters name (#421).
                problems.append("give a chain, a rules tag, or both")
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
        triggers = _triggers(row.get("trigger") or "", rails, kind, cls, problems)
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
            params, bad = convert_params(
                cls, raw, tags=tags, target_sets=target_sets, accounts=account_names)
            problems.extend(bad)
            if not bad and scenario is not None:
                try:
                    problems.extend(cls.check(cls.Params(**params), scenario))
                except Exception as e:  # a data-repo kind's check is its author's code
                    problems.append(f"kind '{kind}' check failed: {type(e).__name__}: {e}")
        if enabled and chain in chains:
            if chain in owner_of:
                problems.append(f"chain '{chain}' is already owned by strategy '{owner_of[chain]}'")
            else:
                owner_of[chain] = name
        if enabled:
            for rule in owned_rules:
                if rule in rule_owner:
                    problems.append(
                        f"rule '{rule}' (tag {rule_tag}) is already owned by strategy "
                        f"'{rule_owner[rule]}'"
                    )
                else:
                    rule_owner[rule] = name
        issues.extend(f"{where}: {p}" for p in problems)
        if enabled and not problems:
            prepared.append(PreparedStrategy(
                name=name, kind=kind, chain=chain, rules=rule_tag, start=start, end=end,
                params=params, description=row.get("description", ""), triggers=triggers,
            ))
    return prepared


def _triggers(
    cell: str, rails: Collection[str], kind: str, cls: type[Strategy] | None, problems: list[str],
) -> tuple[tuple[str, str], ...]:
    """A strategy file's ``trigger`` cell as ``(rail, margin kind)`` pairs."""
    out: list[tuple[str, str]] = []
    for token in cell.split():
        rail, colon, margin_kind = token.rpartition(":")
        if not colon or not rail or margin_kind not in registries.TRIGGER_MARGIN_KINDS:
            problems.append(
                f"trigger {token!r} must be <rail>:annual or <rail>:cumulative")
            continue
        if rail not in rails:
            known = ", ".join(sorted(rails)) or "none in guard_rails.csv"
            problems.append(f"trigger {token!r}: no guard rail named {rail!r} (known: {known})")
            continue
        if (rail, margin_kind) in out:
            problems.append(f"trigger {token!r} is named twice")
            continue
        out.append((rail, margin_kind))
    if out and cls is not None and "on_breach" not in cls.hooks:
        problems.append(
            f"kind '{kind}' has no on_breach hook, which a trigger needs "
            f"(it implements {', '.join(sorted(cls.hooks))})")
    return tuple(out)


def _owned_rules(
    rule_tag: str, rules: Sequence[tuple[str, str, frozenset[str]]], kind: str,
    cls: type[Strategy] | None, problems: list[str],
) -> list[str]:
    """The rules ``rule_tag`` gives a strategy: only ``fixed_amount`` rules
    can be scaled, and a tag no rule carries is a mistake."""
    if ":" not in rule_tag:
        problems.append(f"rules {rule_tag!r} must be a rule tag like \"spending:travel\"")
        return []
    if cls is not None and "scale" not in cls.hooks:
        problems.append(
            f"kind '{kind}' has no scale hook, which owning rules needs "
            f"(it implements {', '.join(sorted(cls.hooks))})"
        )
    tagged = [(name, rule_kind) for name, rule_kind, tags in rules if rule_tag in tags]
    if not tagged:
        problems.append(f"no scheduled rule carries tag {rule_tag}")
    for name, rule_kind in tagged:
        if rule_kind != "fixed_amount":
            problems.append(
                f"rule '{name}' carries tag {rule_tag} but is {rule_kind}; "
                "a strategy scales only fixed_amount rules"
            )
    return [name for name, _ in tagged]


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
                        f"strategies/{s.name}.csv: lot {account}:{lot} in portfolio {tag} has no "
                        f"asset class; add allocations.csv rows for "
                        f"{'symbol ' + symbol if symbol else repr(f'{account}:{lot}')}"
                    )


def write_strategies(
    con: sqlite3.Connection, prepared: Sequence[PreparedStrategy], *,
    kinds: StrategyKinds,
    sources: Mapping[str, str],
    target_rows: Sequence[Mapping[str, str]],
    rail_rows: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, int]:
    """Insert ``strategy``, ``allocation_target`` and, when an enabled
    strategy is a data-repo kind, every data-repo module's source
    (``strategy_source``): helpers are imported by path, so the snapshot is
    the whole folder. Each trigger's rail goes in as its merged
    ``guard_rails.csv`` row from ``rail_rows`` (#441)."""
    counts: dict[str, int] = {}
    chain_id = dict(con.execute("SELECT name, id FROM chain").fetchall())
    triggers = 0
    for s in prepared:
        cur = con.execute(
            'INSERT INTO strategy(name, kind, chain_id, rule_tag, start, "end", params_json, '
            "description) VALUES (?,?,?,?,?,?,?,?)",
            (s.name, s.kind, chain_id[s.chain] if s.chain else None, s.rules or None, s.start, s.end,
             json.dumps(s.params, sort_keys=True, separators=(",", ":")), s.description),
        )
        for rail, margin_kind in s.triggers:
            cells = (rail_rows or {})[rail]
            con.execute(
                "INSERT INTO strategy_trigger(strategy_id, rail, margin_kind, rail_json) "
                "VALUES (?,?,?,?)",
                (cur.lastrowid, rail, margin_kind,
                 json.dumps(dict(cells), sort_keys=True, separators=(",", ":"))),
            )
            triggers += 1
    counts["strategy"] = len(prepared)
    counts["strategy_trigger"] = triggers
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


_VARY_TEXT_COLUMNS = ("vary_low", "vary_high", "vary_mode", "vary_median", "vary_sd")
_VARY_SHAPE_COLUMNS = ("vary_skew", "vary_tailweight")


def _vary_unit(cls: type[Strategy], param: str) -> tuple[str | None, bool] | None:
    """How ``cls``'s parameter ``param`` varies: its unit (``percent``,
    ``dollars`` or None for a plain number) and whether it takes whole
    numbers; None when it can't vary (a tag, a target set, a flag, text)."""
    unit = param_units(cls.Params).get(param)
    if unit is not None:
        return (unit.name, True) if unit.name in ("percent", "dollars") else None
    hint = get_type_hints(cls.Params).get(param)
    if hint is bool:
        return None
    if hint is int:
        return None, True
    if hint is float:
        return None, False
    return None


def _stored(unit: str | None, text: str) -> float:
    """A bound written in the parameter's own unit, in its stored unit. A
    spread (``vary_sd``) is written the same way: ``1%`` or ``500.00``."""
    match unit:
        case "percent":
            return units.percent_to_micros(text)
        case "dollars":
            return units.dollars_to_cents(text)
        case _:
            value = float(text)
            if not math.isfinite(value):
                raise ValueError(f"{text!r} is not a finite number")
            return value


def _base_value(cls: type[Strategy], param: str, params: Mapping[str, Any]) -> float | None:
    """The authored value of ``param``: params_json's, else the kind's default."""
    if param in params:
        value = params[param]
    else:
        field = next(f for f in dataclasses.fields(cls.Params) if f.name == param)
        if field.default is not dataclasses.MISSING:
            value = field.default
        elif field.default_factory is not dataclasses.MISSING:
            value = field.default_factory()
        else:
            return None
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def prepare_vary(
    files: Mapping[str, tuple[str, Sequence[tuple[str, Mapping[str, str]]]]], *,
    strategy_rows: Sequence[Mapping[str, str]],
    prepared: Sequence[PreparedStrategy],
    kinds: StrategyKinds,
    issues: list[str],
    unused: list[str],
) -> tuple[list[PreparedVary], dict[str, tuple[str, str]]]:
    """Each strategy's ``strategy_vary/<name>.csv`` rows (#408), checked: the
    strategy is defined, the parameter is one its kind takes as a percent,
    dollars or a number, each row's distribution is one ensembles know
    with only the columns it reads, and the authored value (member 0's) lies
    within its range. A blank ``vary_mode`` or ``vary_median`` is the
    authored value. A disabled strategy's file is checked as far as its
    kind allows and noted as unused. Returns the varied parameters and
    their correlation links (node -> (parent or "", where)), for the
    calendar's correlation check."""
    from analysis.sim.ensemble import EnsembleError, build_distribution

    defined = {row.get("name", ""): row for row in strategy_rows}
    enabled = {s.name: s for s in prepared}
    out: list[PreparedVary] = []
    links: dict[str, tuple[str, str]] = {}
    for strategy, (label, rows) in sorted(files.items()):
        if strategy not in defined:
            issues.append(f"{label}: no strategy '{strategy}' (strategies/{strategy}.csv)")
            continue
        if strategy not in enabled:
            if rows:
                unused.append(f"{label}: strategy '{strategy}' is disabled, so nothing varies")
            continue
        s = enabled[strategy]
        cls = kinds.kinds[s.kind]
        seen: set[str] = set()
        for where, row in rows:
            param = (row.get("param") or "").strip()
            where = f"{where} ({param or '?'})"
            stray = sorted(c for c in row if not c.startswith("\0")
                           and c not in registries.STRATEGY_VARY_CSV_HEADER and (row[c] or "").strip())
            if stray:
                issues.append(f"{where}: unknown column(s) {', '.join(stray)}")
                continue
            if not param:
                issues.append(f"{where}: missing param")
                continue
            if param in seen:
                issues.append(f"{where}: {param} is varied twice")
                continue
            seen.add(param)
            fields = {f.name for f in dataclasses.fields(cls.Params)}
            if param not in fields:
                issues.append(f"{where}: kind '{s.kind}' has no parameter '{param}' "
                              f"(known: {', '.join(sorted(fields)) or 'none'})")
                continue
            how = _vary_unit(cls, param)
            if how is None:
                issues.append(f"{where}: '{param}' can't vary: only a percent, dollars or a number can")
                continue
            unit, whole = how
            base = _base_value(cls, param, s.params)
            if base is None:
                issues.append(f"{where}: '{param}' has no value to vary around; set it in params_json")
                continue
            prepared_row = _vary_columns(row, unit, base, where, issues)
            if prepared_row is None:
                continue
            stored = {"percent": "micros", "dollars": "cents"}.get(unit or "", "number")
            vary = PreparedVary(strategy, param, base, stored, whole, prepared_row)
            try:
                dist = build_distribution({**prepared_row, "name": vary.node}, "amount")
            except EnsembleError as e:
                issues.append(f"{where}: {str(e).split(': ', 1)[-1]}")
                continue
            low, high = dist.quantile(0.0), dist.quantile(1.0)
            if not low <= base <= high:
                issues.append(
                    f"{where}: the authored {param} must lie within the range it varies over: "
                    "it is the base case, member 0"
                )
                continue
            if whole and math.floor(high) < math.ceil(low):
                issues.append(f"{where}: {param} takes whole numbers, and none lies in its range")
                continue
            parent = _link(row, vary.node, where, issues)
            links[vary.node] = (parent, where)
            vary.columns.update(
                vary_correlate_with=parent or None,
                vary_correlation=(row.get("vary_correlation") or "").strip() if parent else None,
            )
            out.append(vary)
    return out, links


def _vary_columns(
    row: Mapping[str, str], unit: str | None, base: float, where: str, issues: list[str],
) -> dict[str, str | None] | None:
    """A strategy_vary row's distribution cells in stored units, or None
    (with the problems in ``issues``)."""
    distribution = (row.get("vary_distribution") or "").strip()
    if distribution not in registries.ENSEMBLE_DISTRIBUTIONS:
        issues.append(
            f"{where}: vary_distribution must be one of {sorted(registries.ENSEMBLE_DISTRIBUTIONS)} "
            f"(got '{distribution}')"
        )
        return None
    allowed = registries.DISTRIBUTION_PARAMS[distribution]
    out: dict[str, str | None] = {"vary_distribution": distribution}
    ok = True
    for col in (*_VARY_TEXT_COLUMNS, *_VARY_SHAPE_COLUMNS, "vary_outcomes"):
        raw = (row.get(col) or "").strip()
        out[col] = None
        if not raw:
            continue
        if col not in allowed:
            issues.append(f"{where}: {col} is not used by {distribution} (ignored by the draw)")
            ok = False
            continue
        try:
            if col == "vary_outcomes":
                out[col] = rewrite_outcomes(raw, lambda text, _role: _number(_stored(unit, text)))
            elif col in _VARY_SHAPE_COLUMNS:
                out[col] = _number(float(raw))
            else:
                out[col] = _number(_stored(unit, raw))
        except (ValueError, DistributionError) as e:
            issues.append(f"{where}: {col} {e}")
            ok = False
    for col in registries.DISTRIBUTION_REQUIRED_PARAMS.get(distribution, ()):
        if not (row.get(col) or "").strip():
            issues.append(f"{where}: {distribution} requires {col}")
            ok = False
    if distribution == "triangular" and out["vary_mode"] is None:
        out["vary_mode"] = _number(base)
    if distribution == "truncated_normal" and out["vary_median"] is None:
        out["vary_median"] = _number(base)
    return out if ok else None


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _link(row: Mapping[str, str], node: str, where: str, issues: list[str]) -> str:
    """A varied parameter's correlation parent, or "" (none, or a bad pair)."""
    parent = (row.get("vary_correlate_with") or "").strip()
    raw_rho = (row.get("vary_correlation") or "").strip()
    if bool(parent) != bool(raw_rho):
        issues.append(f"{where}: vary_correlate_with and vary_correlation go together")
        return ""
    if not raw_rho:
        return ""
    try:
        rho = float(raw_rho)
    except ValueError:
        issues.append(f"{where}: vary_correlation '{raw_rho}' is not a number")
        return ""
    if not -1.0 <= rho <= 1.0:  # also rejects NaN
        issues.append(f"{where}: vary_correlation must be between -1 and 1 (got {raw_rho})")
        return ""
    if parent == node:
        issues.append(f"{where}: a parameter can't correlate with itself")
        return ""
    return parent


def write_strategy_vary(con: sqlite3.Connection, varies: Sequence[PreparedVary]) -> dict[str, int]:
    """Insert ``strategy_vary``, after ``write_strategies``."""
    ids = dict(con.execute("SELECT name, id FROM strategy").fetchall())
    for v in varies:
        c = v.columns
        con.execute(
            "INSERT INTO strategy_vary(strategy_id, param, name, base, unit, whole, vary_distribution, "
            "vary_low, vary_high, vary_mode, vary_median, vary_sd, vary_skew, vary_tailweight, "
            "vary_outcomes, vary_correlate_with, vary_correlation) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ids[v.strategy], v.param, v.node, v.base, v.unit, int(v.whole), c["vary_distribution"],
             c["vary_low"], c["vary_high"], c["vary_mode"], c["vary_median"], c["vary_sd"],
             None if c["vary_skew"] is None else float(c["vary_skew"]),
             None if c["vary_tailweight"] is None else float(c["vary_tailweight"]),
             c["vary_outcomes"], c["vary_correlate_with"],
             None if c["vary_correlation"] is None else float(c["vary_correlation"])),
        )
    return {"strategy_vary": len(varies)}
