"""Click parameter types that tab-complete the names a scenario defines
(#240): ``type=ACCOUNT`` on an argument or option makes ``sim ... <TAB>``
offer the scenario's accounts.

A type here only completes. It takes any text, as ``click.STRING`` does,
because the scenario a name belongs to may come later on the line; the
command checks the name once it has everything.

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
    "ACCOUNT", "AUX_KIND", "CALENDAR_ROW", "CHAIN", "CORRELATE_WITH", "ENDPOINT", "ENSEMBLE",
    "ITEMIZATION", "PERSON", "RULE", "SCENARIO", "SYMBOL", "TAG", "LotName", "NameType",
    "SeriesName", "type_for",
]

# The params, nearest first, whose value is the scenario a name belongs to.
_SCENARIO_PARAMS = ("scenario", "scenario_name", "baseline")


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
    for key in _SCENARIO_PARAMS:
        if isinstance(value := _given(ctx, key), str):
            return value
    return read_current_scenario(data_dir)


class NameType(click.ParamType):
    """Any text, completed from ``lister(ctx, data_dir)``.

    The lister gets the data repo already found; it returns [] when what it
    needs (a scenario, an account) isn't known yet."""

    name = "text"  # so help shows TEXT, as it did for a plain string

    def __init__(self, lister: Callable[[click.Context, Path], Sequence[str]]) -> None:
        self.lister = lister

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        return value

    def shell_complete(
        self, ctx: click.Context, param: click.Parameter, incomplete: str,
    ) -> list[CompletionItem]:
        data_dir = _data_dir(ctx)
        if data_dir is None:
            return []
        try:
            choices = self.lister(ctx, data_dir)
        except Exception:  # noqa: BLE001 - a traceback would land in the user's shell
            return []
        typed = incomplete.lower()
        return [CompletionItem(c) for c in choices if c.lower().startswith(typed)]


def _in_scenario(
    read: Callable[[Path, str], Sequence[str]],
) -> Callable[[click.Context, Path], Sequence[str]]:
    """A lister for names that belong to the line's scenario."""
    def lister(ctx: click.Context, data_dir: Path) -> Sequence[str]:
        scenario = _scenario(ctx, data_dir)
        return read(data_dir, scenario) if scenario else []
    return lister


def _ensembles(ctx: click.Context, data_dir: Path) -> list[str]:  # noqa: ARG001
    import analysis.sim.scenario as sc  # it imports this module's importer, cli
    return [
        name for name in names.scenario_names(data_dir)
        if sc.ensemble_settings(data_dir, name)[1] is not None
    ]


def _correlate_with(data_dir: Path, scenario: str) -> list[str]:
    """What a ``vary_correlate_with`` can name: a calendar row, or a
    person's lifespan node."""
    lifespans = [f"{registries.LIFESPAN_NODE_PREFIX}{p}" for p in names.person_ids(data_dir)]
    return [*names.calendar_row_names(data_dir, scenario), *lifespans]


SCENARIO = NameType(lambda ctx, data_dir: names.scenario_names(data_dir))  # noqa: ARG005
ENSEMBLE = NameType(_ensembles)
ACCOUNT = NameType(_in_scenario(names.account_names))
CHAIN = NameType(_in_scenario(names.chain_names))
# Either end of a transfer, or a chain step: an account or a chain.
ENDPOINT = NameType(_in_scenario(lambda d, s: names.chain_names(d, s, accounts=True)))
RULE = NameType(_in_scenario(names.scheduled_names))
CALENDAR_ROW = NameType(_in_scenario(names.calendar_row_names))
CORRELATE_WITH = NameType(_in_scenario(_correlate_with))
TAG = NameType(_in_scenario(names.tag_tokens))
ITEMIZATION = NameType(_in_scenario(names.itemization_names))
PERSON = NameType(lambda ctx, data_dir: names.person_ids(data_dir))  # noqa: ARG005
SYMBOL = NameType(lambda ctx, data_dir: names.symbols(data_dir))  # noqa: ARG005
AUX_KIND = NameType(lambda ctx, data_dir: sorted(registries.SWAPPABLE_AUX_KINDS))  # noqa: ARG005


class LotName(NameType):
    """A lot of the account that ``account_param`` (an argument or option of
    the same command) names."""

    def __init__(self, account_param: str) -> None:
        def lister(ctx: click.Context, data_dir: Path) -> Sequence[str]:
            scenario = _scenario(ctx, data_dir)
            account = _line_params(ctx).get(account_param)
            if not scenario or not isinstance(account, str):
                return []
            return names.lot_names(data_dir, scenario, account)
        super().__init__(lister)


class SeriesName(NameType):
    """A series of ``kind`` (``rate``, ``inflation``), or of the kind the
    ``kind_param`` param names, or of any kind."""

    def __init__(self, kind: str | None = None, *, kind_param: str | None = None) -> None:
        def lister(ctx: click.Context, data_dir: Path) -> Sequence[str]:
            scenario = _scenario(ctx, data_dir)
            if not scenario:
                return []
            wanted = _line_params(ctx).get(kind_param) if kind_param else kind
            return names.series_names(data_dir, scenario, wanted if isinstance(wanted, str) else None)
        super().__init__(lister)


# The types of the params that the guides' field lists turn into options
# (``scheduled edit``, ``lot add|edit``), by param name.
_FIELD_TYPES: dict[str, NameType] = {
    "from_account": ENDPOINT,
    "to_account": ENDPOINT,
    "from_lot": LotName("from_account"),
    "to_lot": LotName("to_account"),
    "interest_account": ACCOUNT,
    "person": PERSON,
    "owner": PERSON,
    "series": SeriesName(),
    "symbol": SYMBOL,
    "refill_chain": ENDPOINT,
    "refill_lot": LotName("refill_chain"),
    "overflow_chain": ENDPOINT,
    "overflow_lot": LotName("overflow_chain"),
}


def type_for(param: str) -> NameType | None:
    """The completing type of a field's option named ``param``, or None
    (a plain string) for a field that names nothing."""
    return _FIELD_TYPES.get(param)
