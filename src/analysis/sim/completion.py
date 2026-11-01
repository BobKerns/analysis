"""Click parameter types that tab-complete the names a scenario defines
(#240): ``type=ACCOUNT`` on an argument or option makes ``sim ... <TAB>``
offer the scenario's accounts.

A type here takes any text, as ``click.STRING`` does, because the scenario
a name belongs to may come later on the line. Once the whole line is parsed,
``cli`` re-asks at a terminal for a name that isn't among ``choices`` (a
mistyped one); otherwise the command checks the name itself.

Completion reads what the line has so far. The data repo is ``--data-dir``
or ``$ANALYSIS_DATA_DIR``; the scenario is the ``--scenario`` option or the
command's scenario argument, else the current scenario; a lot belongs to
the account argument it pairs with (``--from-lot`` with FROM_ACCOUNT).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import click
from click.shell_completion import CompletionItem

from analysis.sim import names, registries
from analysis.sim.loader import read_current_scenario

__all__ = [
    "ACCOUNT", "ACCOUNT_OR_NEW", "ANY_TAG", "AUX_KIND", "CALENDAR_ROW", "CHAIN",
    "CORRELATE_WITH", "ENDPOINT", "ENSEMBLE", "ITEMIZATION", "OWNER", "PERSON", "RULE",
    "RULE_TAG", "SCENARIO", "SCENARIO_OR_RUNFILE", "SCENARIO_PARAMS", "SCHEDULED_SERIES",
    "SENSITIVITY_ROW", "STRATEGY",
    "STRATEGY_KIND", "SYMBOL", "TAG", "TAG_FILTER", "TARGET_SET",
    "TARGET_SET_OR_NEW", "YTD_ROW", "LotName", "NameType", "SeriesName", "type_for",
]

# The params, nearest first, whose value is the scenario a name belongs to.
SCENARIO_PARAMS = ("scenario", "scenario_name", "baseline")
# More scenarios whose names count too: `ensemble compare`'s and `sim
# compare`'s --account may name an account only a variant has.
_OTHER_SCENARIO_PARAMS = ("variant", "variants")


def _line_params(ctx: click.Context) -> dict[str, Any]:
    """The command's params as the line has them so far.

    When the word being completed is an option's value, click stops parsing
    at that option and never assigns the arguments before it: they are left
    in ``ctx.args``. They're matched to the command's arguments here, in
    order, so ``--to-lot <TAB>`` still knows TO_ACCOUNT."""
    params = dict(ctx.params)
    pending = list(ctx.args)
    for param in ctx.command.params:
        if not pending:
            break
        if not isinstance(param, click.Argument) or param.name is None:
            continue
        if params.get(param.name) not in (None, ()):
            continue
        if param.nargs == -1:
            params[param.name], pending = tuple(pending), []
        elif param.nargs == 1:
            params[param.name] = pending.pop(0)
    return params


def _given(ctx: click.Context, key: str) -> Any:
    """``key``'s value on the line so far, in this command or a group above it."""
    params: dict[str, Any] | None = _line_params(ctx)
    parent: click.Context | None = ctx
    while parent is not None:
        value = (params if params is not None else parent.params).get(key)
        if value not in (None, "", ()):
            return value
        params, parent = None, parent.parent
    return None


def _data_dir(ctx: click.Context) -> Path | None:
    given = _given(ctx, "data_dir") or os.environ.get("ANALYSIS_DATA_DIR")
    return Path(given) if given else None


def _scenario(ctx: click.Context, data_dir: Path) -> str | None:
    for key in SCENARIO_PARAMS:
        if isinstance(value := _given(ctx, key), str):
            return value
    return read_current_scenario(data_dir)


def _line_scenarios(ctx: click.Context, data_dir: Path) -> list[str]:
    """The scenarios whose names the line may use: the scenario, and the
    compared variants."""
    found = [scenario] if (scenario := _scenario(ctx, data_dir)) else []
    for key in _OTHER_SCENARIO_PARAMS:
        other = _given(ctx, key)
        for name in (other if isinstance(other, tuple) else (other,)):
            if isinstance(name, str) and name not in found:
                found.append(name)
    return found


def _union(read: Callable[[str], Sequence[str]], scenarios: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(n for s in scenarios for n in read(s)))


class NameType(click.ParamType):
    """Any text, completed from ``lister(ctx, data_dir)``.

    The lister gets the data repo already found; it returns [] when what it
    needs (a scenario, an account) isn't known yet. ``what`` (and ``plural``,
    when adding an "s" won't do) names the thing for a re-ask; ``check`` is
    False where a name not in the list is fine (a new tag, a new symbol).
    ``known`` lists more names that aren't mistyped, though TAB doesn't
    offer them."""

    name = "text"  # so help shows TEXT, as it did for a plain string

    def __init__(self, lister: Callable[[click.Context, Path], Sequence[str]], what: str, *,
                 plural: str | None = None, check: bool = True,
                 known: Callable[[click.Context, Path], Sequence[str]] | None = None) -> None:
        self.lister = lister
        self.what = what
        self.plural = plural or f"{what}s"
        self.check = check
        self.known = known

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        return value

    def choices(self, ctx: click.Context) -> list[str]:
        """The names the line so far allows; [] when they can't be known
        (no data repo, an unknown scenario, a broken file)."""
        return self._read(self.lister, ctx)

    def is_known(self, ctx: click.Context, value: str) -> bool:
        """Whether ``value`` is one of ``known``'s names (not a typo)."""
        return self.known is not None and value in self._read(self.known, ctx)

    @staticmethod
    def _read(lister: Callable[[click.Context, Path], Sequence[str]], ctx: click.Context) -> list[str]:
        data_dir = _data_dir(ctx)
        if data_dir is None:
            return []
        try:
            return list(lister(ctx, data_dir))
        except Exception:  # noqa: BLE001 - completion and re-asking are best effort
            return []

    def shell_complete(
        self, ctx: click.Context, param: click.Parameter, incomplete: str,
    ) -> list[CompletionItem]:
        typed = incomplete.lower()
        return [CompletionItem(c) for c in self.choices(ctx) if c.lower().startswith(typed)]


def _in_scenario(
    read: Callable[[Path, str], Sequence[str]],
) -> Callable[[click.Context, Path], Sequence[str]]:
    """A lister for names that belong to the line's scenario."""
    def lister(ctx: click.Context, data_dir: Path) -> Sequence[str]:
        return _union(lambda s: read(data_dir, s), _line_scenarios(ctx, data_dir))
    return lister


def _ensembles(ctx: click.Context, data_dir: Path) -> list[str]:  # noqa: ARG001
    import analysis.sim.scenario as sc  # it imports this module's importer, cli
    return [
        name for name in names.scenario_names(data_dir)
        if sc.ensemble_settings(data_dir, name)[1] is not None
    ]


def _pools(ctx: click.Context, data_dir: Path) -> list[str]:  # noqa: ARG001
    from analysis.sim.lineage import is_pool
    return [name for name in names.scenario_names(data_dir) if is_pool(data_dir / "scenarios", name)]


def _pool_components(ctx: click.Context, data_dir: Path) -> list[str]:
    """What a pool can draw from: the ensembles that aren't pools (ADR-009)."""
    pools = set(_pools(ctx, data_dir))
    return [name for name in _ensembles(ctx, data_dir) if name not in pools]


class PoolComponent(NameType):
    """A pool's ``COMPONENT[=PCT]``: TAB completes the name before any ``=``;
    not re-asked, since the weight makes it more than a name."""

    def shell_complete(
        self, ctx: click.Context, param: click.Parameter, incomplete: str,
    ) -> list[CompletionItem]:
        if "=" in incomplete:
            return []
        typed = incomplete.lower()
        return [CompletionItem(c) for c in self.choices(ctx) if c.lower().startswith(typed)]


def _correlate_with(data_dir: Path, scenario: str) -> list[str]:
    """What a ``vary_correlate_with`` can name: a calendar row, a person's
    lifespan node, or a varied strategy parameter's (#408)."""
    lifespans = [f"{registries.LIFESPAN_NODE_PREFIX}{p}" for p in names.person_ids(data_dir)]
    return [*names.calendar_row_names(data_dir, scenario), *lifespans,
            *names.strategy_vary_nodes(data_dir, scenario)]


def _sensitivity_rows(ctx: click.Context, data_dir: Path) -> list[str]:
    """The rows a sensitivity batch varied (#329): the line's --batch of its
    ensemble, else its latest sensitivity batch, under --runs-dir or the
    data repo's state/runs."""
    from analysis.sim.ensemble import read_batch_manifest
    from analysis.sim.loader import DEFAULT_RUNS_DIR, DEFAULT_STATE_DIR, find_state_dir, latest_ensemble_batch

    scenario = _scenario(ctx, data_dir)
    if scenario is None or _given(ctx, "mode") == "monte_carlo":
        return []
    given = _given(ctx, "runs_dir")
    runs = Path(given) if given else find_state_dir(data_dir, DEFAULT_STATE_DIR) / DEFAULT_RUNS_DIR
    batch = _given(ctx, "batch")
    batch_dir = (runs / scenario / batch if batch
                 else latest_ensemble_batch(runs / scenario, mode="sensitivity", wait=False))
    manifest = read_batch_manifest(batch_dir) if batch_dir is not None else None
    return list((manifest or {}).get("rows", []))


def _scenarios(ctx: click.Context, data_dir: Path) -> list[str]:  # noqa: ARG001
    return names.scenario_names(data_dir)


SCENARIO = NameType(_scenarios, "scenario")


class ParentList(NameType):
    """``--parents``' colon-separated names: TAB completes the name after the
    last ``:`` (#259). Not re-asked as one name, which it isn't."""

    def shell_complete(
        self, ctx: click.Context, param: click.Parameter, incomplete: str,
    ) -> list[CompletionItem]:
        done, colon, last = incomplete.rpartition(":")
        given = set(done.split(":")) if colon else set()
        typed = last.lower()
        return [
            CompletionItem(f"{done}{colon}{c}") for c in self.choices(ctx)
            if c.lower().startswith(typed) and c not in given
        ]


PARENTS = ParentList(_scenarios, "scenario", check=False)
# `sim explain`'s target: a scenario, or a run file's path, which no list holds.
SCENARIO_OR_RUNFILE = NameType(_scenarios, "scenario", check=False)
ENSEMBLE = NameType(_ensembles, "ensemble")
POOL = NameType(_pools, "pool")
POOL_COMPONENT = PoolComponent(_pool_components, "ensemble", check=False)
ACCOUNT = NameType(_in_scenario(names.account_names), "account")
# `sim invest` makes the account when it's new.
ACCOUNT_OR_NEW = NameType(_in_scenario(names.account_names), "account", check=False)
# A chain command given an account names its one-step leaf chain, which the
# command explains better than a re-ask would.
CHAIN = NameType(_in_scenario(names.chain_names), "chain",
                 known=_in_scenario(names.account_names))
# Either end of a transfer, or a chain step: an account or a chain.
ENDPOINT = NameType(_in_scenario(lambda d, s: names.chain_names(d, s, accounts=True)),
                    "account or chain", plural="accounts and chains")
RULE = NameType(_in_scenario(names.scheduled_names), "scheduled rule")
YTD_ROW = NameType(_in_scenario(names.ytd_names), "year-to-date row")
STRATEGY = NameType(_in_scenario(names.strategy_names), "strategy")
TARGET_SET = NameType(_in_scenario(names.target_set_names), "target set")
TARGET_SET_OR_NEW = NameType(_in_scenario(names.target_set_names), "target set", check=False)
STRATEGY_KIND = NameType(lambda ctx, data_dir: names.strategy_kind_names(data_dir), "strategy kind")  # noqa: ARG005
GUARD_RAIL = NameType(_in_scenario(names.guard_rail_names), "guard rail")
# A row may also be picked by its number or by text in it, so any value goes.
CALENDAR_ROW = NameType(_in_scenario(names.calendar_row_names), "calendar row", check=False)
# A calendar date: a literal one, or relative to a date marker (#281), whose
# names TAB offers.
CALENDAR_DATE = NameType(_in_scenario(names.date_marker_names), "date", check=False)
CORRELATE_WITH = NameType(_in_scenario(_correlate_with), "calendar row or lifespan",
                          plural="calendar rows and lifespans")
# `sim ensemble sensitivity --row|--attribute`: what the batch varied, which
# the command checks against the batch itself.
SENSITIVITY_ROW = NameType(_sensitivity_rows, "varying row", check=False)
# `sim ensemble draws --node`: a batch draws whatever it drew when it ran,
# which the command checks against its draws database, not the scenario now.
DRAW_NODE = NameType(_in_scenario(_correlate_with), "draw node", check=False)


class NodePair(NameType):
    """``--pair A,B``: TAB completes the node after the comma, never the
    one before it (#330). Not re-asked as one name, which it isn't."""

    def shell_complete(
        self, ctx: click.Context, param: click.Parameter, incomplete: str,
    ) -> list[CompletionItem]:
        first, comma, last = incomplete.partition(",")
        if not comma:
            return [CompletionItem(f"{c},") for c in self.choices(ctx)
                    if c.lower().startswith(first.lower())]
        return [CompletionItem(f"{first},{c}") for c in self.choices(ctx)
                if c.lower().startswith(last.lower()) and c != first]


# `sim ensemble draws --pair`: two draw nodes, comma-separated.
DRAW_PAIR = NodePair(_in_scenario(_correlate_with), "draw node", check=False)


def _tag_patterns(data_dir: Path, scenario: str) -> list[str]:
    """``role:*`` for each role in use: a pattern `tags items` takes."""
    return sorted({f"{t.partition(':')[0]}:*" for t in names.tag_tokens(data_dir, scenario)})


# A tag being removed: one in use.
TAG = NameType(_in_scenario(names.tag_tokens), "tag")
# A tag being added: completes from the tags in use, but may be new.
ANY_TAG = NameType(_in_scenario(names.tag_tokens), "tag", check=False)
# The rules a spending strategy owns (#387): a tag some scheduled rule carries.
RULE_TAG = NameType(_in_scenario(names.rule_tags), "rule tag")
# A filter: a tag or a role:* pattern, which may match nothing.
TAG_FILTER = NameType(_in_scenario(lambda d, s: [*names.tag_tokens(d, s), *_tag_patterns(d, s)]),
                      "tag", check=False)
ITEMIZATION = NameType(_in_scenario(names.itemization_names), "itemization",
                       known=_in_scenario(lambda d, s: [f"@{n}" for n in names.itemization_names(d, s)]))
LIFE_TABLE = NameType(
    _in_scenario(lambda d, s: names.series_names(d, s, "life_table")), "life table",
)  # #315: the <name> of an aux life_table__<name>.csv
def _with_joint(ids: list[str]) -> list[str]:
    """The persons and the household, ``joint``, which the loader supplies
    when persons.csv has no row for it."""
    return [*ids, "joint"] if "joint" not in ids else ids


PERSON = NameType(lambda ctx, data_dir: names.person_ids(data_dir), "person")  # noqa: ARG005
# An account's owner: a person, or both of them.
OWNER = NameType(lambda ctx, data_dir: _with_joint(names.person_ids(data_dir)), "owner")  # noqa: ARG005
# A rule's person: one person's lifespan, or the household's (`joint`, #368).
RULE_PERSON = NameType(lambda ctx, data_dir: _with_joint(names.person_ids(data_dir)), "person")  # noqa: ARG005
# A lot may hold a symbol nothing names yet, and prices can fetch one.
SYMBOL = NameType(lambda ctx, data_dir: names.symbols(data_dir), "symbol", check=False)  # noqa: ARG005


def _expense_categories(ctx: click.Context | None, data_dir: Path | None) -> list[str]:  # noqa: ARG001
    from analysis.sim.connection import EXPENSE_CATEGORIES

    return list(EXPENSE_CATEGORIES)


# Unchecked: a batch may hold a category outside the fixed list, which the
# command checks against the batch itself.
EXPENSE_CATEGORY = NameType(_expense_categories, "expense category", check=False)
AUX_KIND = NameType(lambda ctx, data_dir: sorted(registries.SWAPPABLE_AUX_KINDS), "aux kind")  # noqa: ARG005


class LotName(NameType):
    """A lot of the account that ``account_param`` (an argument or option of
    the same command) names."""

    def __init__(self, account_param: str) -> None:
        def lister(ctx: click.Context, data_dir: Path) -> Sequence[str]:
            account = _line_params(ctx).get(account_param)
            if not isinstance(account, str):
                return []
            return _union(lambda s: names.lot_names(data_dir, s, account),
                          _line_scenarios(ctx, data_dir))
        super().__init__(lister, "lot")


class SeriesName(NameType):
    """A series of ``kind`` (``rate``, ``inflation``), or of the kind the
    ``kind_param`` param names, or of any kind. ``fixed`` takes the word
    ``fixed`` too, which a scheduled rule's series may be (no inflation)."""

    def __init__(self, kind: str | None = None, *, kind_param: str | None = None,
                 fixed: bool = False) -> None:
        def lister(ctx: click.Context, data_dir: Path) -> Sequence[str]:
            scenario = _scenario(ctx, data_dir)
            if not scenario:
                return []
            wanted = _line_params(ctx).get(kind_param) if kind_param else kind
            return names.series_names(data_dir, scenario, wanted if isinstance(wanted, str) else None)
        super().__init__(lister, "series", plural="series",
                         known=(lambda ctx, data_dir: ["fixed"]) if fixed else None)  # noqa: ARG005


# The types of the params that the guides' field lists turn into options
# (``scheduled edit``, ``lot add|edit``), by param name.
_FIELD_TYPES: dict[str, NameType] = {
    "from_account": ENDPOINT,
    "to_account": ENDPOINT,
    "from_lot": LotName("from_account"),
    "to_lot": LotName("to_account"),
    "interest_account": ACCOUNT,
    "person": PERSON,
    "owner": OWNER,
    "series": SeriesName(),
    "symbol": SYMBOL,
    "refill_chain": ENDPOINT,
    "refill_lot": LotName("refill_chain"),
    "overflow_chain": ENDPOINT,
    "overflow_lot": LotName("overflow_chain"),
}


# A scheduled rule's series, which may be ``fixed``.
SCHEDULED_SERIES = SeriesName(fixed=True)


def type_for(param: str, *, scheduled: bool = False) -> NameType | None:
    """The completing type of a field's option named ``param`` (a scheduled
    rule's field, when ``scheduled``), or None (a plain string) for a field
    that names nothing."""
    if scheduled and param == "series":
        return SCHEDULED_SERIES
    return _FIELD_TYPES.get(param)
