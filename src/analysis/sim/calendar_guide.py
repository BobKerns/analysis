"""Guided entry for ``sim calendar add`` (issue #32).

``sim calendar add`` takes up to five kinds of row and a dozen ``--vary-*``
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
    # account -> (is an investment account, its lot labels)
    lots: Mapping[str, tuple[bool, Sequence[str]]] = field(default_factory=dict)
    scheduled_names: Sequence[str] = ()
    # aux_kind -> the series names of that kind
    series: Mapping[str, Sequence[str]] = field(default_factory=dict)
    # named calendar rows and latent factors, for --vary-correlate-with
    row_names: Sequence[str] = ()


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
    applies: Callable[[GuideContext, Answers], bool] = _always

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
    """One ``sim calendar add`` subcommand: its inputs, in asking order."""

    command: str
    summary: str
    fields: tuple[Field, ...]
    amount_unit: FieldType | None = None  # what ``--vary-target amount`` varies

    def field_named(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)


def _yes(param: str) -> Callable[[GuideContext, Answers], bool]:
    return lambda ctx, answers: answers.get(param) == "y"  # noqa: ARG005


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


_DISTRIBUTION_HELP = (
    "uniform: anywhere between low and high, equally likely. "
    "triangular: a most likely value, with a low and a high. "
    "truncated_normal: a bell curve around a median, clipped to low and high."
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
            "row_name", "name for this varying row", option="--name", applies=varying,
            help="A unique name; reports and other rows refer to the row by it.",
        ),
        Field(
            "vary_target", "what varies", option="--vary-target", applies=varying,
            choices=lambda ctx, answers: list(targets), strict=True,  # noqa: ARG005
            default=targets[0] if len(targets) == 1 else None,
            help="amount: the row's value. date: when it takes effect.",
        ),
        Field(
            "vary_distribution", "distribution", option="--vary-distribution", applies=varying,
            choices=lambda ctx, answers: sorted(registries.ENSEMBLE_DISTRIBUTIONS),  # noqa: ARG005
            strict=True, help=_DISTRIBUTION_HELP,
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
    ]
    return tuple(fields)


_DATE = Field("date", "date (YYYY-MM or YYYY-MM-DD)", type="date")
_DESCRIPTION = Field("description", "description", option="--description", required=False)
_TAGS = Field(
    "tags", "tags", type="tags", option="--tags", required=False,
    help="Space-separated <role>:<name> tokens, e.g. expense:discretionary.",
)
_SCHEDULED_NAME = Field(
    "scheduled_name", "scheduled rule", strict=True,
    choices=lambda ctx, answers: list(ctx.scheduled_names),  # noqa: ARG005
)


def _is_investment(account_param: str) -> Callable[[GuideContext, Answers], bool]:
    def applies(ctx: GuideContext, answers: Answers) -> bool:
        return ctx.lots.get(answers.get(account_param, ""), (False, ()))[0]
    return applies


def _lots_of(account_param: str) -> Callable[[GuideContext, Answers], Sequence[str]]:
    def choices(ctx: GuideContext, answers: Answers) -> Sequence[str]:
        return list(ctx.lots.get(answers.get(account_param, ""), (False, ()))[1])
    return choices


def _accounts(ctx: GuideContext, answers: Answers) -> Sequence[str]:  # noqa: ARG001
    return list(ctx.accounts)


KINDS: tuple[KindSpec, ...] = (
    KindSpec(
        "set-rate", "a series starts earning a new rate", amount_unit="percent",
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
        "retire-scheduled", "a scheduled rule stops",
        fields=(
            _DATE, _SCHEDULED_NAME, _DESCRIPTION, _TAGS,
            *_vary_fields(targets=("date",), ramp=False),
        ),
    ),
    KindSpec(
        "transfer", "a one-time transfer between accounts", amount_unit="dollars",
        fields=(
            _DATE,
            Field("from_account", "from account", choices=_accounts, strict=True),
            Field(
                "from_lot", "from lot", option="--from-lot", strict=True,
                choices=_lots_of("from_account"), applies=_is_investment("from_account"),
            ),
            Field("to_account", "to account", choices=_accounts, strict=True),
            Field(
                "to_lot", "to lot", option="--to-lot", strict=True,
                choices=_lots_of("to_account"), applies=_is_investment("to_account"),
            ),
            Field("amount", "amount in dollars", type="dollars"),
            Field(
                "tax_character", "tax character", option="--tax-character", default="none",
                choices=lambda ctx, answers: sorted(registries.TAX_CHARACTERS),  # noqa: ARG005
                strict=True,
            ),
            _DESCRIPTION,
            *_vary_fields(targets=("amount", "date"), ramp=False),
        ),
    ),
    KindSpec(
        "latent-factor", "a shared influence that varying rows correlate with",
        fields=(
            Field("name", "factor name"),
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
    spec: KindSpec, answers: Answers, *, scenario: str | None = None,
) -> str:
    """The non-interactive command line that writes the same row."""
    words = ["sim", "calendar", "add", spec.command]
    options: list[str] = []
    for field_ in spec.fields:
        value = answers.get(field_.param, "")
        if field_.is_question_only or value == "":
            continue
        if field_.is_argument:
            words.append(value)
        elif value != field_.default:
            options += [field_.option, value]
    if scenario:
        options += ["--scenario", scenario]
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
    ) -> None:
        self._ask = ask
        self._checkers = checkers
        self._echo = echo

    def ask_field(
        self, spec: KindSpec, field_: Field, ctx: GuideContext, answers: Answers,
        *, current: str | None = None,
    ) -> str:
        """Ask one field until its answer is acceptable. ``current`` is the
        answer being changed, offered as the default."""
        choices = list(field_.choices(ctx, answers)) if field_.choices else []
        if field_.type == "yesno":
            choices = ["y", "n"]
        required = field_.is_required(answers)
        if field_.help:
            self._echo(f"  {field_.help}")
        if choices and len(choices) <= 12:
            self._echo(f"  choices: {', '.join(choices)}")
        elif choices:
            self._echo(f"  {len(choices)} choices; press Tab to complete")
        default = current if current is not None else field_.default
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
                return ""
            problem = self._problem(spec, field_, answers, answer, choices)
            if problem is None:
                return answer.lower() if field_.type == "yesno" else answer
            self._echo(f"  {problem}")

    def _problem(
        self, spec: KindSpec, field_: Field, answers: Answers, answer: str, choices: list[str],
    ) -> str | None:
        if answer == "":
            return "an answer is required" if field_.is_required(answers) else None
        if field_.type == "yesno":
            return None if answer.lower() in ("y", "n") else "answer y or n"
        if field_.strict and answer not in choices:
            return f"not one of: {', '.join(choices)}" if choices else "there is nothing to choose from"
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
        only the positional arguments -- what the command cannot run without."""
        for field_ in spec.fields:
            if not field_.applies(ctx, answers):
                answers.pop(field_.param, None)
            elif field_.param not in answers and (field_.is_argument or not arguments_only):
                answers[field_.param] = self.ask_field(spec, field_, ctx, answers)
        return answers

    def review(
        self, spec: KindSpec, ctx: GuideContext, answers: Answers,
        submit: Callable[[dict[str, Any]], None], *, scenario: str | None = None,
    ) -> bool:
        """Show the answers, then write, change a field, or cancel -- as often
        as needed. A row the command rejects comes back here with its answers
        intact. Returns whether a row was written."""
        while True:
            shown = [f for f in spec.fields if f.param in answers]
            width = max(len(f.name) for f in shown)
            self._echo("")
            for field_ in shown:
                self._echo(f"  {field_.name:<{width}}  {answers[field_.param] or '(blank)'}")
            self._echo("")
            self._echo("Equivalent command:")
            self._echo(f"  {equivalent_command(spec, answers, scenario=scenario)}")
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
