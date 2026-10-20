"""Guided entry for ``sim account lot add|edit`` (issue #124).

A lot is one ``account_lots.csv`` row (ADR-003 §4.3, ADR-005). Its fields
are described here as data for ``calendar_guide.Guide`` to ask, as the
calendar kinds are, so the questions follow the same rules: only what applies
to the account and the answers so far is asked.

Each field's ``param`` is its ``account_lots.csv`` column, except
``account`` (``account_name``), so a lot's answers and its row convert
column for column.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from analysis.sim import registries
from analysis.sim.calendar_guide import Answers, Field, GuideContext, KindSpec, chains_of, lot_needed

MANAGE = "_manage"  # the question that opens the balance-management fields


def _accounts(ctx: GuideContext, answers: Answers) -> Sequence[str]:  # noqa: ARG001
    return list(ctx.accounts)


def _refill_chains(ctx: GuideContext, answers: Answers) -> Sequence[str]:  # noqa: ARG001
    return [*ctx.accounts, *chains_of(ctx, "source")]


def _overflow_chains(ctx: GuideContext, answers: Answers) -> Sequence[str]:  # noqa: ARG001
    return [*ctx.accounts, *chains_of(ctx, "destination")]


def _has_basis(ctx: GuideContext, answers: Answers) -> bool:
    """A taxable account's lot, or a Roth lot, whose basis comes out tax-free
    before its five-year clock has run (#217)."""
    return (
        ctx.tax_treatments.get(answers.get("account", "")) == "taxable"
        or registries.RETIREMENT_LOT_SUBKINDS.get(answers.get("subkind", "")) == "tax_free"
    )


def _retirement(ctx: GuideContext, answers: Answers) -> bool:
    return ctx.subkinds.get(answers.get("account", "")) == registries.RETIREMENT_SUBKIND


def _rmd_lot(ctx: GuideContext, answers: Answers) -> bool:
    """A lot that may need a prior Dec 31 balance, for a first-year RMD."""
    return _retirement(ctx, answers) and answers.get("subkind") in registries.RMD_LOT_SUBKINDS


# The usual tax character of an account's growth, by subkind: the built-in
# account the growth comes from (ADR-003 §2.2). A retirement account's follows
# each lot's subkind instead.
GROWTH_CHARACTER = {
    "cash": "ordinary", registries.BUSINESS_SUBKIND: "ordinary", "brokerage_taxable": "gains",
    "hsa": "deferred",
    "real_estate": "gains", "vehicle": "depreciation", "other_asset": "gains",
}


def needs_growth_character(ctx: GuideContext, answers: Answers) -> bool:
    """Whether this lot grows in an account with no tax character for its
    growth yet, which the loader would refuse (#233): the lot is the first
    of the account to grow, so the account's character must be set with it."""
    account = answers.get("account", "")
    subkind = ctx.subkinds.get(account, "")
    if subkind not in registries.ACCOUNT_SUBKINDS["asset"] or subkind == registries.RETIREMENT_SUBKIND:
        return False
    if ctx.growth_characters.get(account):
        return False
    return bool(answers.get("series", "").strip()) or answers.get("symbol", "").strip().upper() in ctx.growing


def _usual_growth_character(ctx: GuideContext, answers: Answers) -> str:
    return GROWTH_CHARACTER.get(ctx.subkinds.get(answers.get("account", ""), ""), "gains")


def _managed(ctx: GuideContext, answers: Answers) -> bool:  # noqa: ARG001
    return answers.get(MANAGE) == "y"


def _chain_lot(chain_param: str) -> tuple[
    Callable[[GuideContext, Answers], bool], Callable[[GuideContext, Answers], Sequence[str]],
]:
    """When a refill or overflow chain's lot is asked, and its choices: as a
    transfer's, only where a blank lot would not say which."""
    def applies(ctx: GuideContext, answers: Answers) -> bool:
        return _managed(ctx, answers) and lot_needed(ctx.lots.get(answers.get(chain_param, "")))

    def choices(ctx: GuideContext, answers: Answers) -> Sequence[str]:
        return list(ctx.lots.get(answers.get(chain_param, ""), (False, ()))[1])

    return applies, choices


def _balance_required(answers: Answers) -> bool:
    """A blank opening balance is derived from quantity times price, so it
    needs both."""
    return not (answers.get("quantity") and answers.get("symbol"))


_refill_lot_applies, _refill_lots = _chain_lot("refill_chain")
_overflow_lot_applies, _overflow_lots = _chain_lot("overflow_chain")

LOT = KindSpec(
    "add", "a lot: one position inside an account",
    words=("sim", "account", "lot"), key=("account", "lot"), duplicate="an existing lot",
    fields=(
        Field("account", "account", choices=_accounts, strict=True),
        Field(
            "lot", "lot", type="lot", default=registries.DEFAULT_LOT,
            help="A label unique within the account, without ':'.",
        ),
        Field(
            "subkind", "lot subkind", option="--subkind", applies=_retirement, strict=True,
            choices=lambda ctx, answers: sorted(registries.RETIREMENT_LOT_SUBKINDS),  # noqa: ARG005
            help="A retirement account's lot: Roth or traditional, IRA or 401k.",
        ),
        Field(
            "symbol", "symbol", option="--symbol", required=False,
            choices=lambda ctx, answers: list(ctx.securities),  # noqa: ARG005
            help="The security held; names its rate series and its price.",
        ),
        Field(
            "acquired", "acquired (YYYY-MM-DD)", type="day", option="--acquired",
            required=False, help="Starts the holding period; blank means long-term.",
        ),
        Field(
            "quantity", "quantity (shares)", type="number", option="--quantity",
            required=False, help="Leave blank for a dollar-denominated lot.",
        ),
        Field(
            "series", "growth-rate series", option="--series", required=False,
            choices=lambda ctx, answers: list(ctx.series.get("rate", ())),  # noqa: ARG005
            help="Overrides the symbol's and the account's series.",
        ),
        Field(
            "tax_character", "tax character of the account's growth", option="--tax-character",
            applies=needs_growth_character, strict=True, suggest=_usual_growth_character,
            choices=lambda ctx, answers: sorted(registries.GROWTH_COUNTERPARTY_ACCOUNTS),  # noqa: ARG005
            help="The account's first lot to grow sets how its growth is taxed (accounts.csv).",
        ),
        Field(
            "opening_balance", "opening balance in dollars", type="money",
            option="--opening-balance", required=_balance_required,
            help="May be blank when quantity and symbol are given and the symbol has a price.",
        ),
        Field(
            "opening_cost_basis", "opening cost basis in dollars", type="money",
            option="--opening-cost-basis", required=False, applies=_has_basis,
            help="For a Roth lot, its contributions and conversions; blank means all of it.",
        ),
        Field(
            "prior_year_end_balance", "balance on the Dec 31 before the scenario starts",
            type="money", option="--prior-year-end-balance", required=False,
            applies=_rmd_lot,
            help="Needed when the owner reaches RMD age in the first year; "
                 "set on every lot of the account or on none.",
        ),
        Field(
            MANAGE, "set balance management (minimum, refill, overflow)?",
            type="yesno", default="n",
        ),
        Field(
            "minimum_balance", "minimum balance in dollars", type="money",
            option="--minimum-balance", required=False, applies=_managed,
            help="A debit that would go below it first draws on the refill chain.",
        ),
        Field(
            "refill_chain", "refill from (account or source chain)", option="--refill-chain",
            required=False, applies=_managed, choices=_refill_chains, strict=True, chain_role="source",
        ),
        Field(
            "refill_lot", "refill lot", option="--refill-lot", strict=True,
            applies=_refill_lot_applies, choices=_refill_lots,
        ),
        Field(
            "refill_target", "refill up to, in dollars", type="money",
            option="--refill-target", required=False, applies=_managed,
            help="Not with a refill amount; neither refills to the minimum.",
        ),
        Field(
            "refill_amount", "refill by, in dollars", type="money",
            option="--refill-amount", required=False, applies=_managed,
        ),
        Field(
            "maximum_balance", "maximum balance in dollars", type="money",
            option="--maximum-balance", required=False, applies=_managed,
            help="Any excess at month end is swept into the overflow chain.",
        ),
        Field(
            "overflow_chain", "overflow into (account or destination chain)", option="--overflow-chain",
            required=False, applies=_managed, choices=_overflow_chains, strict=True,
            chain_role="destination",
        ),
        Field(
            "overflow_lot", "overflow lot", option="--overflow-lot", strict=True,
            applies=_overflow_lot_applies, choices=_overflow_lots,
        ),
    ),
)

# account_lots.csv columns the lot's fields write, beside account_name/lot.
VALUE_COLUMNS: tuple[str, ...] = tuple(
    column for column in registries.ACCOUNT_LOTS_CSV_HEADER
    if column not in ("account_name", "lot")
)


def answers_of(row: dict[str, str]) -> Answers:
    """An ``account_lots.csv`` row as answers to ``LOT``'s fields."""
    answers = {"account": row["account_name"], "lot": row["lot"] or registries.DEFAULT_LOT}
    answers |= {column: row.get(column, "") for column in VALUE_COLUMNS}
    managed = any(row.get(column) for column in registries.CASH_MANAGEMENT_COLUMNS)
    answers[MANAGE] = "y" if managed else "n"
    return answers
