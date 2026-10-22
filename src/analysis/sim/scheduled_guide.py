"""Guided ``sim scheduled edit`` (issue #183), and the rule kinds ``sim
scheduled add --kind`` writes.

A scheduled rule is one ``scheduled.csv`` row (ADR-003 §4.6). Which of its
columns mean anything depends on its ``amount_rule_kind`` (ADR-003 §2), so
each field here says which kinds read it, and ``calendar_guide.Guide`` asks
only those. Each field's ``param`` is its ``scheduled.csv`` column, so a
rule's answers and its row convert column for column.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from analysis.sim import registries
from analysis.sim.calendar_guide import (
    Answers, Field, GuideContext, KindSpec, from_endpoints, lot_asked, lots_of, to_endpoints,
)

# Every kind's own columns beyond those all rules have, and whether each is
# required: ADR-003 §2 and §4.6. A column no kind lists here is read by every
# kind; one that is listed is blanked on a rule of any other kind.
KIND_COLUMNS: dict[str, dict[str, bool]] = {
    "fixed_amount": {
        "amount": True, "series": False, "base_date": False, "inflation_cadence": False,
        "ytd_preroll": False,
    },
    "percent_of_balance": {"series": True},  # its rate series; the engine has no default
    "rmd": {"person": True},
    "ss_benefit": {"person": True, "claim_age": True, "amount": True, "series": True},
    "tax_accumulator": {"params_json": True},
    "mortgage_payment": {"amount": True, "rate": True, "interest_account": True},
    "on_death": {"person": True},
    # One of rate (a bracket) or amount (a taxable-income target) is required.
    "fill_bracket": {
        "rate": False, "amount": False, "series": False, "base_date": False,
        "inflation_cadence": False,
    },
}
assert set(KIND_COLUMNS) == registries.AMOUNT_RULE_KINDS

KIND_ONLY_COLUMNS: frozenset[str] = frozenset(
    column for columns in KIND_COLUMNS.values() for column in columns
    if column != "params_json"  # every kind may carry extension parameters
)

# What the usual rule of each kind is taxed as (ADR-003 §2); on_death must be
# tax_free.
KIND_TAX_CHARACTER: dict[str, str] = {
    "fixed_amount": "none",
    "percent_of_balance": "ordinary",
    "rmd": "tax_deferred_out",
    "ss_benefit": "ss_benefit",
    "tax_accumulator": "none",
    "mortgage_payment": "none",
    "on_death": "tax_free",
    "fill_bracket": "tax_deferred_out",
}

# Kinds the engine runs only once a year (ADR-003 §2.3, §2.6), and the month
# each usually fires.
ANNUAL_KINDS: dict[str, str] = {"rmd": "12", "tax_accumulator": "12", "fill_bracket": "12"}

# Kinds whose source must be an account, not an interior chain; every kind
# but fixed_amount needs an account as its destination (the engine's rules).
ACCOUNT_SOURCE_KINDS: frozenset[str] = frozenset({"percent_of_balance", "ss_benefit", "on_death"})


# What a rule's amount is, by kind.
AMOUNT_LABEL: dict[str, str] = {
    "fixed_amount": "amount in dollars",
    "ss_benefit": "benefit at full retirement age (PIA) in dollars",
    "mortgage_payment": "monthly payment (principal and interest) in dollars",
    "fill_bracket": "taxable income to fill up to, in today's dollars",
}


def reads(column: str) -> Callable[[GuideContext, Answers], bool]:
    """Whether the answers' rule kind reads ``column``."""
    def applies(ctx: GuideContext, answers: Answers) -> bool:  # noqa: ARG001
        kind = answers.get("amount_rule_kind", "")
        return column not in KIND_ONLY_COLUMNS or column in KIND_COLUMNS.get(kind, {})
    return applies


def needs(column: str) -> Callable[[Answers], bool]:
    """Whether the answers' rule kind requires ``column``."""
    def required(answers: Answers) -> bool:
        return KIND_COLUMNS.get(answers.get("amount_rule_kind", ""), {}).get(column, False)
    return required


def _prioritized(ctx: GuideContext, answers: Answers) -> bool:  # noqa: ARG001
    """A fill_bracket has no priority: it runs last in its month (#252)."""
    return answers.get("amount_rule_kind", "") in registries.DEFAULT_PRIORITIES


def _inflated(ctx: GuideContext, answers: Answers) -> bool:
    """A fixed amount grown by an inflation series, which measures from a
    base date at a cadence."""
    return reads("base_date")(ctx, answers) and answers.get("series", "") not in ("", "fixed")


def _annual(ctx: GuideContext, answers: Answers) -> bool:  # noqa: ARG001
    return answers.get("cadence") == "annual"


def _series_choices(ctx: GuideContext, answers: Answers) -> Sequence[str]:
    """Rate series for percent_of_balance; inflation series for the rest."""
    if answers.get("amount_rule_kind") == "percent_of_balance":
        return list(ctx.series.get("rate", ()))
    return list(ctx.series.get("inflation", ()))


def _no_ytd_amount(ctx: GuideContext, answers: Answers) -> bool:
    return reads("ytd_preroll")(ctx, answers) and not answers.get("ytd_amount")


def _start_month(ctx: GuideContext, answers: Answers) -> str | None:  # noqa: ARG001
    start = answers.get("start", "")
    return str(int(start[5:7])) if len(start) >= 7 and start[5:7].isdigit() else None


RULE = KindSpec(
    "add", "a scheduled rule: a transfer that recurs, or happens once",
    words=("sim", "scheduled"), duplicate="another rule",
    fields=(
        Field(
            "amount_rule_kind", "rule kind", option="--kind", default="fixed_amount",
            choices=lambda ctx, answers: sorted(registries.AMOUNT_RULE_KINDS),  # noqa: ARG005
            strict=True, help="How the amount is worked out (ADR-003 §2).",
        ),
        Field("start", "start (YYYY-MM or YYYY-MM-DD)", type="date", option="--start",
              help="The first month the rule may fire."),
        Field("from_account", "from account or chain", option="--from-account",
              choices=from_endpoints, strict=True, chain_role="source"),
        Field(
            "from_lot", "from lot", option="--from-lot", strict=True,
            choices=lots_of("from_account"), applies=lot_asked("from_account"),
        ),
        Field("to_account", "to account or chain", option="--to-account",
              choices=to_endpoints, strict=True, chain_role="destination"),
        Field(
            "to_lot", "to lot", option="--to-lot", strict=True,
            choices=lots_of("to_account"), applies=lot_asked("to_account"),
        ),
        Field(
            "amount", "amount in dollars", type="dollars", option="--amount",
            required=needs("amount"), applies=reads("amount"),
        ),
        Field(
            "cadence", "cadence", option="--cadence", default="monthly", strict=True,
            choices=lambda ctx, answers: sorted(registries.CADENCES),  # noqa: ARG005
            suggest=lambda ctx, answers: "annual" if answers.get(  # noqa: ARG005
                "amount_rule_kind") in ANNUAL_KINDS else None,
        ),
        Field(
            "month_of_year", "month of the year it fires (1-12)", type="month",
            option="--month-of-year", applies=_annual,
            suggest=lambda ctx, answers: ANNUAL_KINDS.get(
                answers.get("amount_rule_kind", "")) or _start_month(ctx, answers),
        ),
        Field(
            "person", "person", option="--person", strict=True,
            required=needs("person"), applies=reads("person"),
            choices=lambda ctx, answers: list(ctx.persons),  # noqa: ARG005
            help="Whose age or lifespan the rule follows.",
        ),
        Field(
            "claim_age", "claim age (whole years)", type="integer", option="--claim-age",
            required=needs("claim_age"), applies=reads("claim_age"),
        ),
        Field(
            "series", "series", option="--series", required=needs("series"),
            applies=reads("series"), choices=_series_choices,
            help="fixed_amount, fill_bracket: an inflation series, or blank or 'fixed' for none. "
                 "ss_benefit: the COLA series. percent_of_balance: the rate series.",
        ),
        Field(
            "base_date", "base date (YYYY-MM)", type="date", option="--base-date",
            required=False, applies=_inflated,
            suggest=lambda ctx, answers: answers.get("start") or None,  # noqa: ARG005
            help="The date inflation is measured from.",
        ),
        Field(
            "inflation_cadence", "inflation cadence", option="--inflation-cadence",
            required=False, applies=_inflated, strict=True,
            choices=lambda ctx, answers: ["monthly", "annual"],  # noqa: ARG005
            help="annual steps once each January, as a COLA does; blank compounds monthly.",
        ),
        Field(
            "rate", "rate in percent", type="percent", option="--rate",
            required=needs("rate"), applies=reads("rate"),
            help="mortgage_payment: the annual interest rate (e.g. 6%). fill_bracket: "
                 "the bracket to fill to its top (e.g. 22%), instead of an amount.",
        ),
        Field(
            "interest_account", "interest expense account", option="--interest-account",
            required=needs("interest_account"), applies=reads("interest_account"), strict=True,
            choices=lambda ctx, answers: list(ctx.accounts),  # noqa: ARG005
        ),
        Field(
            "tax_character", "tax character", option="--tax-character", default="none",
            choices=lambda ctx, answers: sorted(registries.TAX_CHARACTERS),  # noqa: ARG005
            strict=True,
            suggest=lambda ctx, answers: KIND_TAX_CHARACTER.get(  # noqa: ARG005
                answers.get("amount_rule_kind", "")),
        ),
        Field(
            "tax_year", "tax year", type="year", option="--tax-year", required=False,
            help="Only when the transfer counts toward another year, as an IRA "
                 "contribution made the next spring does.",
        ),
        Field("end", "end (YYYY-MM)", type="date", option="--end", required=False,
              help="The last month it may fire; blank for none."),
        Field(
            "priority", "priority", type="integer", option="--priority", required=False,
            applies=_prioritized,
            suggest=lambda ctx, answers: str(registries.DEFAULT_PRIORITIES.get(  # noqa: ARG005
                answers.get("amount_rule_kind", ""), 200)),
            help="Lower fires first within a month (ADR-003 §5).",
        ),
        Field(
            "ytd_amount", "amount already paid this year before the scenario starts",
            type="dollars", option="--ytd-amount", required=False,
        ),
        Field(
            "ytd_preroll", "post the months before the start as already paid? (0/1)",
            option="--ytd-preroll", required=False, applies=_no_ytd_amount, strict=True,
            choices=lambda ctx, answers: ["0", "1"],  # noqa: ARG005
        ),
        Field("description", "description", option="--description", required=False),
        Field(
            "params_json", "parameters (JSON object)", type="json", option="--params-json",
            required=needs("params_json"),
            help='tax_accumulator: e.g. {"year":"previous","scope":"federal"}. '
                 'rmd drawing from a chain: {"subject_account":"<account>"}.',
        ),
        Field("tags", "tags", type="tags", option="--tags", required=False,
              help="Space-separated '<role>:<name>' tokens."),
    ),
)

# The columns RULE asks about; the rest of a row (name, meta_json) is kept.
RULE_COLUMNS: tuple[str, ...] = tuple(field_.param for field_ in RULE.fields)
assert set(RULE_COLUMNS) <= set(registries.SCHEDULED_CSV_HEADER)


def answers_of(row: dict[str, str]) -> Answers:
    """A scheduled.csv row as answers to ``RULE``'s fields."""
    return {column: row.get(column, "") for column in RULE_COLUMNS}
