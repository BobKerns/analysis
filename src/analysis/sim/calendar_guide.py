"""Guided entry for ``sim calendar add`` (issue #32), and for ``sim account
lot add|edit`` (issue #124, in ``lot_guide``).

``sim calendar add`` takes up to six kinds of row and a dozen ``--vary-*``
options whose meaning depends on each other. This module describes each
kind's inputs as data (``KINDS``) and walks them as questions, asking only
what the answers so far make relevant.

The descriptions are deliberately free of prompting code: a field says what
it is, when it applies and what it may contain, so another front end (a
full-screen form) can present the same fields without restating the rules.
The commands themselves still build and validate the row; the guide only
collects their arguments.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import click

from analysis.sim import registries

type Answers = dict[str, str]

# What a field holds, which decides how its answer is checked and converted:
# "text", "date" (YYYY-MM or YYYY-MM-DD), "dollars", "percent", "number",
# "integer", "tags", "yesno", or "value" -- a date or an amount in the row's
# own unit, whichever the row is varying.
type FieldType = str

CLEAR = "-"  # typed at a prompt to blank an optional field that has a value


@dataclass(frozen=True)
class GuideContext:
    """What the scenario offers as completions."""

    accounts: Sequence[str] = ()
    # the asset accounts among them, the only ones a balance can move out of
    asset_accounts: Sequence[str] = ()
    # account -> (is an investment account, its lot labels)
    lots: Mapping[str, tuple[bool, Sequence[str]]] = field(default_factory=dict)
    # interior chains, which a transfer may name in place of an account
    chains: Sequence[str] = ()
    # each chain's role (ADR-003 §4.7); a chain left out fits either side
    chain_roles: Mapping[str, str] = field(default_factory=dict)
    scheduled_names: Sequence[str] = ()
    # aux_kind -> the series names of that kind
    series: Mapping[str, Sequence[str]] = field(default_factory=dict)
    # named calendar rows and latent factors, for --vary-correlate-with
    row_names: Sequence[str] = ()
    # The scenario's other calendar rows (when editing, all but the row being
    # edited), as answers, by command: what a row must not duplicate.
    other_rows: Mapping[str, Sequence[Answers]] = field(default_factory=dict)
    # names those other rows already use
    taken_names: frozenset[str] = frozenset()
    # account -> its subkind, and -> its tax treatment
    subkinds: Mapping[str, str] = field(default_factory=dict)
    tax_treatments: Mapping[str, str] = field(default_factory=dict)
    securities: Sequence[str] = ()
    persons: Sequence[str] = ()  # person ids, for a scheduled rule's person
    # symbols and series that make a lot grow: a symbol with weights, or one
    # whose name is a rate series
    growing: frozenset[str] = frozenset()
    # account -> the tax character of its growth, blank when it has none yet
    growth_characters: Mapping[str, str] = field(default_factory=dict)


def _always(ctx: GuideContext, answers: Answers) -> bool:  # noqa: ARG001
    return True


@dataclass(frozen=True)
class Field:
    """One input of a ``sim calendar add`` subcommand."""

    param: str  # the command callback's parameter; a leading "_" marks a
    #             question that only steers the guide and is never passed on
    label: str
    type: FieldType = "text"
    option: str | None = None  # its flag, or None for a positional argument
    help: str = ""
    # Whether a blank answer is refused; a function when that depends on the
    # answers so far.
    required: bool | Callable[[Answers], bool] = True
    default: str | None = None  # what the command uses when the option is omitted
    choices: Callable[[GuideContext, Answers], Sequence[str]] | None = None
    strict: bool = False  # the answer must be one of ``choices``
    unique: bool = False  # the answer must not be a name another row uses
    applies: Callable[[GuideContext, Answers], bool] = _always
    chain_role: str | None = None  # the role a chain named here must have (#143)
    # The default offered when it depends on the answers so far (a rule
    # kind's usual tax character, say); ``default`` when it gives None.
    suggest: Callable[[GuideContext, Answers], str | None] | None = None

    @property
    def name(self) -> str:
        """What the user types to pick this field for editing."""
        if self.option:
            return self.option.lstrip("-")
        return self.param.lstrip("_").replace("_", "-")

    @property
    def is_question_only(self) -> bool:
        return self.param.startswith("_")

    @property
    def is_argument(self) -> bool:
        """A positional argument of the command, which it cannot run without."""
        return self.option is None and not self.is_question_only

    def is_required(self, answers: Answers) -> bool:
        return self.required(answers) if callable(self.required) else self.required


@dataclass(frozen=True)
class KindSpec:
    """One ``sim calendar add`` subcommand (or another guided command): its
    inputs, in asking order."""

    command: str
    summary: str
    fields: tuple[Field, ...]
    amount_unit: FieldType | None = None  # what ``--vary-target amount`` varies
    # The fields that identify the row: two rows alike in all of them are one
    # event to the loader (registries.CALENDAR_KEY_COLUMNS), so a second is
    # refused.
    key: tuple[str, ...] = ()
    words: tuple[str, ...] = ("sim", "calendar", "add")  # the command line before ``command``
    duplicate: str = "another row"  # what an answer duplicating a key would copy

    def field_named(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)


def _yes(param: str) -> Callable[[GuideContext, Answers], bool]:
    return lambda ctx, answers: answers.get(param) == "y"  # noqa: ARG005


def _no(param: str) -> Callable[[GuideContext, Answers], bool]:
    return lambda ctx, answers: answers.get(param) == "n"  # noqa: ARG005


_TAIL_DEPENDENCE_HELP = (
    "Only on a correlation tree's root: rows under it tend to hit extremes together. "
    "Lower is stronger, about 3 to 30; blank keeps the Gaussian copula."
)


def _tail_dependence_field(applies: Callable[[GuideContext, Answers], bool]) -> Field:
    return Field(
        "vary_tail_dependence", "tail dependence (degrees of freedom)", type="number",
        option="--vary-tail-dependence", required=False, applies=applies,
        help=_TAIL_DEPENDENCE_HELP,
    )


def _uses(column: str) -> Callable[[GuideContext, Answers], bool]:
    """Applies when the chosen distribution reads ``column``."""
    def applies(ctx: GuideContext, answers: Answers) -> bool:  # noqa: ARG001
        distribution = answers.get("vary_distribution", "")
        return column in registries.DISTRIBUTION_PARAMS.get(distribution, frozenset())
    return applies


def _needs(column: str) -> Callable[[Answers], bool]:
    """Required when the chosen distribution cannot do without ``column``."""
    def required(answers: Answers) -> bool:
        distribution = answers.get("vary_distribution", "")
        return column in registries.DISTRIBUTION_REQUIRED_PARAMS.get(distribution, ())
    return required


def _distributions(ctx: GuideContext, answers: Answers) -> Sequence[str]:  # noqa: ARG001
    """The distributions that can draw the chosen target. A truncated normal's
    spread is a number, not a span of days, so it cannot draw a date."""
    unusable = {"truncated_normal"} if answers.get("vary_target") == "date" else set()
    return sorted(registries.ENSEMBLE_DISTRIBUTIONS - unusable)


_DISTRIBUTION_HELP = (
    "uniform: anywhere between low and high, equally likely. "
    "triangular: a most likely value, with a low and a high. "
    "truncated_normal: a bell curve around a median, clipped to low and high "
    "(amounts only). "
    "discrete: a few separate outcomes with weights, such as an exact zero."
)


def _vary_fields(*, targets: Sequence[str], ramp: bool) -> tuple[Field, ...]:
    """The ensemble-variation questions (ADR-006, ADR-008) shared by every
    kind that can vary. ``targets`` are the columns this kind can draw."""
    varying = _yes("_vary")
    fields = [
        Field(
            "_vary", "vary this row across ensemble members?", type="yesno", default="n",
            help="Draw this row's value from a range for each member of an ensemble, "
                 "instead of using one fixed value.",
        ),
        Field(
            "row_name", "name for this row", option="--name", unique=True,
            required=lambda answers: answers.get("_vary") == "y",
            # Asked when the row varies; a name an existing row already has is
            # kept (and can be changed) either way.
            applies=lambda ctx, answers: (  # noqa: ARG005
                answers.get("_vary") == "y" or bool(answers.get("row_name"))
            ),
            help="A unique name, needed when the row varies; reports and other rows "
                 "refer to the row by it.",
        ),
        Field(
            "vary_target", "what varies", option="--vary-target", applies=varying,
            choices=lambda ctx, answers: list(targets), strict=True,  # noqa: ARG005
            default=targets[0] if len(targets) == 1 else None,
            help="amount: the row's value. date: when it takes effect.",
        ),
        Field(
            "vary_distribution", "distribution", option="--vary-distribution", applies=varying,
            choices=_distributions, strict=True, help=_DISTRIBUTION_HELP,
        ),
        Field(
            "vary_median", "median", type="value", option="--vary-median", required=False,
            applies=_uses("vary_median"),
            help="The base case. May be left blank if both low and high are given.",
        ),
        Field(
            "vary_sd", "spread (sd)", type="value", option="--vary-sd", required=False,
            applies=_uses("vary_sd"),
            help="Standard deviation before clipping. May be left blank if the median "
                 "and a bound are given.",
        ),
        Field(
            "vary_low", "low", type="value", option="--vary-low",
            required=_needs("vary_low"), applies=_uses("vary_low"),
        ),
        Field(
            "vary_high", "high", type="value", option="--vary-high",
            required=_needs("vary_high"), applies=_uses("vary_high"),
        ),
        Field(
            "vary_mode", "most likely value", type="value", option="--vary-mode",
            required=False, applies=_uses("vary_mode"),
            help="Defaults to midway between low and high.",
        ),
        Field(
            "vary_outcomes", "outcomes", option="--vary-outcomes",
            required=_needs("vary_outcomes"), applies=_uses("vary_outcomes"),
            help="Weighted outcomes, each a value or a low..high range, weights adding "
                 "to 100%: e.g. '30%: 0; 25%: 1000..6000; 45%: 6000..20000'.",
        ),
        Field(
            "vary_skew", "skew", type="number", option="--vary-skew", required=False,
            applies=_uses("vary_skew"),
            help="0 is symmetric; positive gives a longer high tail. Needs a spread.",
        ),
        Field(
            "vary_tailweight", "tail weight", type="number", option="--vary-tailweight",
            required=False, applies=_uses("vary_tailweight"),
            help="1 is normal; below 1 gives heavier tails. Needs a spread.",
        ),
    ]
    if ramp:
        fields.append(Field(
            "vary_ramp_months", "ramp months", type="integer", option="--vary-ramp-months",
            required=False,
            applies=lambda ctx, answers: (  # noqa: ARG005
                answers.get("_vary") == "y" and answers.get("vary_target") == "amount"
            ),
            help="Reach the drawn rate gradually over this many months.",
        ))
        amount_varies = lambda ctx, answers: (  # noqa: E731, ARG005
            answers.get("_vary") == "y" and answers.get("vary_target") == "amount"
            and not answers.get("vary_ramp_months")
        )
        process_set = lambda ctx, answers: bool(answers.get("vary_process"))  # noqa: E731, ARG005
        fields += [
            Field(
                "vary_process", "year-by-year process", option="--vary-process",
                required=False, applies=amount_varies, strict=True,
                choices=lambda ctx, answers: sorted(registries.VARY_PROCESSES),  # noqa: ARG005
                help="ar1: the rate moves year by year around the drawn level, as a return "
                     "does. Blank holds the drawn rate, as suits an inflation rate or a yield.",
            ),
            Field(
                "vary_persistence", "persistence (0 to below 1)", type="number",
                option="--vary-persistence", applies=process_set,
                help="How much of one year's departure from the level carries into the next; "
                     "0 makes every year independent.",
            ),
            Field(
                "vary_shock_sd", "yearly shock (sd)", type="value",
                option="--vary-shock-sd", applies=process_set,
                help="The size of one year's shock, in the rate's own unit.",
            ),
            Field(
                "vary_process_start", "start", option="--vary-process-start",
                required=False, applies=process_set, strict=True,
                choices=lambda ctx, answers: sorted(registries.VARY_PROCESS_STARTS),  # noqa: ARG005
                help="current: start at the rate in effect just before this date and "
                     "revert to the drawn level at the persistence above. Blank starts "
                     "with a year already drawn around the level.",
            ),
        ]
    fields += [
        Field(
            "_correlate", "correlate with another varying row or factor?", type="yesno",
            default="n", applies=varying,
            help="Make this row tend to move with another one (ADR-008).",
        ),
        Field(
            "vary_correlate_with", "correlate with", option="--vary-correlate-with",
            applies=_yes("_correlate"),
            choices=lambda ctx, answers: list(ctx.row_names),  # noqa: ARG005
        ),
        Field(
            "vary_correlation", "correlation (-1 to 1)", type="number",
            option="--vary-correlation", applies=_yes("_correlate"),
        ),
        # Asked of a factor (the usual root), but of a varying row only when
        # it already has one: most varying rows without a parent are lone
        # rows, where nu does nothing. --vary-tail-dependence sets it anyway.
        _tail_dependence_field(
            lambda ctx, answers: (
                varying(ctx, answers) and _no("_correlate")(ctx, answers)
                and bool(answers.get("vary_tail_dependence"))
            ),
        ),
    ]
    return tuple(fields)


_DATE = Field("date", "date (YYYY-MM or YYYY-MM-DD)", type="date")
_DESCRIPTION = Field("description", "description", option="--description", required=False)
_TAX_CHARACTER_HELP = (
    "Leave blank to have it follow from the accounts: a rollover is untaxed, "
    "a distribution taxed by its lot (ADR-003 §3.4)."
)
_TAGS = Field(
    "tags", "tags", type="tags", option="--tags", required=False,
    help="Space-separated <role>:<name> tokens, e.g. expense:discretionary.",
)
_SCHEDULED_NAME = Field(
    "scheduled_name", "scheduled rule", strict=True,
    choices=lambda ctx, answers: list(ctx.scheduled_names),  # noqa: ARG005
)


def lot_needed(lots: tuple[bool, Sequence[str]] | None) -> bool:
    """Whether a transfer must name the lot of an account with these ``lots``
    (``None`` for a chain, which takes no lot). Only when ``default`` is an
    ordinary account's one lot does a blank lot say which lot is meant; an
    investment account always names it (ADR-005)."""
    if lots is None:
        return False
    investment, labels = lots
    return investment or list(labels) != [registries.DEFAULT_LOT]


def lot_asked(account_param: str) -> Callable[[GuideContext, Answers], bool]:
    def applies(ctx: GuideContext, answers: Answers) -> bool:
        return lot_needed(ctx.lots.get(answers.get(account_param, "")))
    return applies


def lots_of(account_param: str) -> Callable[[GuideContext, Answers], Sequence[str]]:
    def choices(ctx: GuideContext, answers: Answers) -> Sequence[str]:
        return list(ctx.lots.get(answers.get(account_param, ""), (False, ()))[1])
    return choices


def chains_of(ctx: GuideContext, role: str) -> list[str]:
    """The chains that may stand where a ``role`` chain is needed."""
    return [c for c in ctx.chains if ctx.chain_roles.get(c, role) == role]


def _chain_problem(ctx: GuideContext, answers: Answers, field_: Field, answer: str) -> str | None:
    """Why ``answer``, a chain left out of ``field_``'s choices, doesn't fit:
    a chain refused for its role or for the other side being one should say
    so, not just list the rest, as the CLI's pickers do."""
    if field_.chain_role is None or answer not in ctx.chains:
        return None
    if field_.param == "to_account" and answers.get("from_account", "") in ctx.chains:
        return f"{answer!r} is a chain, and a transfer can't run from one chain into another"
    role = ctx.chain_roles.get(answer, field_.chain_role)
    if role == field_.chain_role:
        return None
    return f"{answer!r} is a {role} chain; only a {field_.chain_role} chain or an account fits here"


def to_endpoints(ctx: GuideContext, answers: Answers) -> Sequence[str]:
    """Accounts and destination chains for the to side; no chain when the
    from side is one, since a transfer can't run from one chain into another."""
    if answers.get("from_account", "") in ctx.chains:
        return list(ctx.accounts)
    return [*ctx.accounts, *chains_of(ctx, "destination")]


def from_endpoints(ctx: GuideContext, answers: Answers) -> Sequence[str]:  # noqa: ARG001
    return [*ctx.accounts, *chains_of(ctx, "source")]


KINDS: tuple[KindSpec, ...] = (
    KindSpec(
        "set-rate", "a series starts earning a new rate", amount_unit="percent",
        key=("date", "series", "rate_percent"),
        fields=(
            _DATE,
            Field(
                "aux_kind", "kind of series", option="--aux-kind", default="rate",
                choices=lambda ctx, answers: ["rate", "inflation"], strict=True,  # noqa: ARG005
                help="rate: an asset's own growth series. inflation: an inflation or "
                     "COLA series such as cpi.",
            ),
            Field(
                "series", "series",
                choices=lambda ctx, answers: list(ctx.series.get(answers.get("aux_kind", ""), ())),
            ),
            Field("rate_percent", "rate in percent (e.g. 4.5)", type="percent"),
            _DESCRIPTION, _TAGS,
            *_vary_fields(targets=("amount", "date"), ramp=True),
        ),
    ),
    KindSpec(
        "set-amount", "a scheduled rule's amount changes", amount_unit="dollars",
        key=("date", "scheduled_name", "amount"),
        fields=(
            _DATE, _SCHEDULED_NAME,
            Field("amount", "new amount in dollars", type="dollars"),
            Field(
                "param_date", "new base date", type="date", option="--param-date",
                required=False, help="The date inflation is measured from, if it changes.",
            ),
            _DESCRIPTION, _TAGS,
            *_vary_fields(targets=("amount", "date"), ramp=False),
        ),
    ),
    KindSpec(
        "retire-scheduled", "a scheduled rule stops", key=("date", "scheduled_name"),
        fields=(
            _DATE, _SCHEDULED_NAME, _DESCRIPTION, _TAGS,
            *_vary_fields(targets=("date",), ramp=False),
        ),
    ),
    KindSpec(
        "transfer", "a one-time transfer between accounts", amount_unit="dollars",
        key=("date", "from_account", "from_lot", "to_account", "to_lot", "amount", "series"),
        fields=(
            _DATE,
            Field("from_account", "from account or chain", choices=from_endpoints, strict=True,
                  chain_role="source"),
            Field(
                "from_lot", "from lot", option="--from-lot", strict=True,
                choices=lots_of("from_account"), applies=lot_asked("from_account"),
            ),
            Field("to_account", "to account or chain", choices=to_endpoints, strict=True,
                  chain_role="destination"),
            Field(
                "to_lot", "to lot", option="--to-lot", strict=True,
                choices=lots_of("to_account"), applies=lot_asked("to_account"),
            ),
            Field("amount", "amount in dollars", type="dollars"),
            Field(
                "tax_character", "tax character", option="--tax-character", default="",
                choices=lambda ctx, answers: sorted(registries.TAX_CHARACTERS),  # noqa: ARG005
                strict=True, required=False, help=_TAX_CHARACTER_HELP,
            ),
            _DESCRIPTION,
            Field("series", "series tag", option="--series", required=False),
            *_vary_fields(targets=("amount", "date"), ramp=False),
        ),
    ),
    KindSpec(
        "transfer-balance", "an account's whole balance moves, as in a rollover",
        key=("date", "from_account", "from_lot", "to_account", "to_lot"),
        fields=(
            _DATE,
            Field(
                "from_account", "from account", strict=True,
                choices=lambda ctx, answers: list(ctx.asset_accounts),  # noqa: ARG005
                help="An asset account; its whole balance (one lot's) moves.",
            ),
            Field(
                "from_lot", "from lot", option="--from-lot", strict=True,
                choices=lots_of("from_account"), applies=lot_asked("from_account"),
            ),
            Field("to_account", "to account or chain", choices=to_endpoints, strict=True,
                  chain_role="destination"),
            Field(
                "to_lot", "to lot", option="--to-lot", strict=True,
                choices=lots_of("to_account"), applies=lot_asked("to_account"),
            ),
            Field(
                "tax_character", "tax character", option="--tax-character", default="",
                choices=lambda ctx, answers: sorted(registries.TAX_CHARACTERS),  # noqa: ARG005
                strict=True, required=False, help=_TAX_CHARACTER_HELP,
            ),
            _DESCRIPTION, _TAGS,
            *_vary_fields(targets=("date",), ramp=False),
        ),
    ),
    KindSpec(
        "swap-aux-table", "an aux table is replaced by another, as in a move",
        key=("date", "aux_kind", "series"),
        fields=(
            _DATE,
            Field(
                "aux_kind", "kind of table", strict=True,
                choices=lambda ctx, answers: sorted(registries.SWAPPABLE_AUX_KINDS),  # noqa: ARG005
                help="state_brackets for a move to another state.",
            ),
            Field(
                "source_series", "replacement series",
                choices=lambda ctx, answers: list(ctx.series.get(answers.get("aux_kind", ""), ())),
                help="The table that takes over, aux/<kind>__<series>.csv or "
                     "series/<kind>__<series>.csv.",
            ),
            Field(
                "series", "series replaced", option="--series", required=False,
                choices=lambda ctx, answers: list(ctx.series.get(answers.get("aux_kind", ""), ())),
                help="Blank for a kind without a series, such as federal_brackets.",
            ),
            _DESCRIPTION, _TAGS,
            *_vary_fields(targets=("date",), ramp=False),
        ),
    ),
    KindSpec(
        "latent-factor", "a shared influence that varying rows correlate with",
        fields=(
            Field("name", "factor name", unique=True),
            Field(
                "description", "what this factor stands for", option="--description",
                required=False,
            ),
            Field(
                "_correlate", "does this factor depend on another factor or row?",
                type="yesno", default="n",
            ),
            Field(
                "vary_correlate_with", "depends on", option="--vary-correlate-with",
                applies=_yes("_correlate"),
                choices=lambda ctx, answers: list(ctx.row_names),  # noqa: ARG005
            ),
            Field(
                "vary_correlation", "correlation (-1 to 1)", type="number",
                option="--vary-correlation", applies=_yes("_correlate"),
            ),
            _tail_dependence_field(_no("_correlate")),
        ),
    ),
)


def kind_named(command: str) -> KindSpec:
    return next(spec for spec in KINDS if spec.command == command)


def value_type(spec: KindSpec, field_: Field, answers: Answers) -> FieldType:
    """``field_``'s type, with "value" resolved to what the row is varying."""
    if field_.type != "value":
        return field_.type
    if answers.get("vary_target") == "date":
        return "date"
    return spec.amount_unit or "number"


def command_params(spec: KindSpec, answers: Answers) -> dict[str, Any]:
    """Keyword arguments for the subcommand's callback: every answered field
    that is passed on, with blanks left to the command's own defaults."""
    params: dict[str, Any] = {}
    for field_ in spec.fields:
        value = answers.get(field_.param, "")
        if field_.is_question_only or value == "":
            continue
        params[field_.param] = int(value) if field_.type == "integer" else value
    return params


def equivalent_command(
    spec: KindSpec, answers: Answers,
    *, scenario: str | None = None, data_dir: str | None = None,
) -> str:
    """The non-interactive command line that writes the same row.
    ``scenario`` and ``data_dir`` are included when the guide was given them,
    so the command acts on the same scenario wherever it is run."""
    words = [*spec.words, spec.command]
    options: list[str] = []
    for field_ in spec.fields:
        value = answers.get(field_.param, "")
        if field_.is_question_only or value == "":
            continue
        if field_.option is None:  # a positional argument: question-only fields were skipped
            words.append(value)
        elif value != field_.default:
            options += [field_.option, value]
    if scenario:
        options += ["--scenario", scenario]
    if data_dir:
        options += ["--data-dir", data_dir]
    return " ".join(shlex.quote(word) for word in [*words, *options])


class Guide:
    """Asks a kind's fields as questions.

    ``ask(text, choices, default)`` reads one line; ``checkers`` maps a field
    type to a function that raises ``click.ClickException`` for a bad answer.
    """

    def __init__(
        self,
        ask: Callable[[str, list[str] | None, str | None], str],
        checkers: Mapping[FieldType, Callable[[str], object]],
        echo: Callable[[str], None] = click.echo,
        canonical_date: Callable[[KindSpec, str], str] | None = None,
    ) -> None:
        self._ask = ask
        self._checkers = checkers
        self._echo = echo
        # How a kind's date reads once loaded, so that two spellings of one
        # date compare equal when looking for a duplicate row.
        self._canonical_date = canonical_date

    def ask_field(
        self, spec: KindSpec, field_: Field, ctx: GuideContext, answers: Answers,
        *, current: str | None = None,
    ) -> str:
        """Ask one field until its answer is acceptable. ``current`` is the
        answer being changed, offered as the default."""
        choices = list(field_.choices(ctx, answers)) if field_.choices else []
        if field_.type == "yesno":
            choices = ["y", "n"]
        choices = [
            choice for choice in choices
            if not self._duplicates(spec, ctx, {**answers, field_.param: choice}, field_)
        ]
        required = field_.is_required(answers)
        if field_.help:
            self._echo(f"  {field_.help}")
        if choices and len(choices) <= 12:
            self._echo(f"  choices: {', '.join(choices)}")
        elif choices:
            self._echo(f"  {len(choices)} choices; press Tab to complete")
        default = current
        if default is None and field_.suggest is not None:
            default = field_.suggest(ctx, answers)
        if default is None:
            default = field_.default
        text = field_.label
        if field_.type == "yesno":
            text += " (y/n)"
        elif not required:
            text += f" (optional; {CLEAR} to clear)" if current else " (optional)"
        if default is None and not required:
            default = ""
        while True:
            answer = self._ask(text, choices or None, default).strip()
            if answer == CLEAR and not required:
                answer = ""  # judged like any other answer: a blank can duplicate too
            problem = self._problem(spec, field_, ctx, answers, answer, choices)
            if problem is None:
                return answer.lower() if field_.type == "yesno" else answer
            self._echo(f"  {problem}")

    def _key(self, spec: KindSpec, ctx: GuideContext, answers: Answers) -> tuple[object, ...]:
        """``answers``' key fields, each in a form that compares equal however
        it was typed (``100`` and ``100.00``, ``2030-12`` and ``2030-12-31``).
        A field that does not apply counts as blank, whatever answer it still
        holds: the row will be written without it."""
        key: list[object] = []
        for param in spec.key:
            field_ = next(f for f in spec.fields if f.param == param)
            # Trimmed, as the loader trims every cell: another row's value is
            # compared as authored, spaces and all.
            value = answers.get(param, "").strip() if field_.applies(ctx, answers) else ""
            type_ = value_type(spec, field_, answers)
            checker = self._checkers.get(type_)
            if value and type_ == "date" and self._canonical_date is not None:
                try:
                    key.append(self._canonical_date(spec, value))
                    continue
                except (click.ClickException, ValueError):
                    pass
            if value and checker is not None:
                try:
                    key.append(checker(value))
                    continue
                except (click.ClickException, ValueError):
                    pass
            key.append(value)
        return tuple(key)

    def _duplicates(
        self, spec: KindSpec, ctx: GuideContext, answers: Answers, field_: Field,
    ) -> bool:
        """Whether ``answers`` would be a second copy of another row. Only
        judged once every argument in the key has an answer."""
        if field_.param not in spec.key:
            return False
        arguments = {f.param for f in spec.fields if f.is_argument}
        if any(param in arguments and not answers.get(param) for param in spec.key):
            return False
        key = self._key(spec, ctx, answers)
        others = ctx.other_rows.get(spec.command, ())
        return any(self._key(spec, ctx, other) == key for other in others)

    def _problem(
        self, spec: KindSpec, field_: Field, ctx: GuideContext, answers: Answers,
        answer: str, choices: list[str],
    ) -> str | None:
        if answer == "":
            if field_.is_required(answers):
                return "an answer is required"
            if self._duplicates(spec, ctx, {**answers, field_.param: ""}, field_):
                return f"left blank, that would duplicate {spec.duplicate}"
            return None
        if field_.type == "yesno":
            return None if answer.lower() in ("y", "n") else "answer y or n"
        # Before the choices check: a choice left out for this reason should say so.
        if self._duplicates(spec, ctx, {**answers, field_.param: answer}, field_):
            return f"that would duplicate {spec.duplicate}"
        if field_.strict and answer not in choices and (wrong := _chain_problem(ctx, answers, field_, answer)):
            return wrong
        if field_.strict and answer not in choices:
            return f"not one of: {', '.join(choices)}" if choices else "there is nothing to choose from"
        if field_.unique and answer in ctx.taken_names:
            return f"another row is already named {answer!r}"
        checker = self._checkers.get(value_type(spec, field_, answers))
        if checker is None:
            return None
        try:
            checker(answer)
        except click.ClickException as error:
            return error.format_message()
        except ValueError as error:
            return str(error) or "not a valid value"
        return None

    def complete(
        self, spec: KindSpec, ctx: GuideContext, answers: Answers,
        *, arguments_only: bool = False,
    ) -> Answers:
        """Ask every applicable field ``answers`` lacks, in order, and drop
        answers to fields that no longer apply. With ``arguments_only``, ask
        only the positional arguments -- what the command cannot run without,
        leaving everything else for the command itself to judge.

        Otherwise an answer that a later change has made unacceptable (a
        distribution that cannot draw the target now chosen, a dollar bound
        on what is now a date) is asked again."""
        for field_ in spec.fields:
            if not field_.applies(ctx, answers):
                answers.pop(field_.param, None)
                continue
            if arguments_only and not field_.is_argument:
                continue
            answer = answers.get(field_.param)
            if answer and not arguments_only:
                choices = list(field_.choices(ctx, answers)) if field_.choices else []
                if self._problem(spec, field_, ctx, answers, answer, choices) is not None:
                    self._echo(f"  {field_.name} can no longer be {answer!r}")
                    answer = None
            if answer is None:
                answers[field_.param] = self.ask_field(spec, field_, ctx, answers)
        return answers

    def review(
        self, spec: KindSpec, ctx: GuideContext, answers: Answers,
        submit: Callable[[dict[str, Any]], None],
        *, scenario: str | None = None, data_dir: str | None = None,
        show_command: bool = True,
    ) -> bool:
        """Show the answers, then write, change a field, or cancel -- as often
        as needed. A row the command rejects comes back here with its answers
        intact. Returns whether a row was written. ``show_command`` is off
        when no single command would do the same, as when changing a row."""
        while True:
            shown = [f for f in spec.fields if f.param in answers]
            width = max(len(f.name) for f in shown)
            self._echo("")
            for field_ in shown:
                self._echo(f"  {field_.name:<{width}}  {answers[field_.param] or '(blank)'}")
            self._echo("")
            if show_command:
                self._echo("Equivalent command:")
                command = equivalent_command(spec, answers, scenario=scenario, data_dir=data_dir)
                self._echo(f"  {command}")
            names = [f.name for f in shown]
            action = self._ask(
                "write this row, cancel, or name a field to change it",
                ["write", "cancel", *names], "write",
            ).strip()
            if action == "write":
                try:
                    submit(command_params(spec, answers))
                except click.ClickException as error:
                    self._echo(f"Not written: {error.format_message()}")
                    continue
                return True
            if action == "cancel":
                self._echo("Cancelled; nothing was written.")
                return False
            field_ = spec.field_named(action)
            if field_ is None or field_.param not in answers:
                self._echo(f"  not a field here: {action!r}")
                continue
            current = answers.pop(field_.param)
            answers[field_.param] = self.ask_field(spec, field_, ctx, answers, current=current)
            self.complete(spec, ctx, answers)
