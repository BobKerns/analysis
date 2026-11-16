"""Build a partial expense baseline from imported transactions and Amazon summary data.

The transactions arrive in the import framework's common form
(``analysis.sim.imports.forms.Flows``), whatever source they were read from;
the category maps are read by ``analysis.sim.imports.mapping``.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import pandas as pd

from analysis.sim.imports.forms import ORIGIN, Flows
from analysis.sim.registries import SCHEDULED_CSV_HEADER

# Excluded for every household: Amazon rows are replaced by the Amazon
# summaries, and a mortgage is modeled in the scenario. Household-specific
# payees come from the data repo (``load_excluded_payees``).
_ALWAYS_EXCLUDED_PAYEE_PATTERNS = ("amazon", "mortgage")
_EXCLUDED_CATEGORY_RE = re.compile(r"mortgage", re.IGNORECASE)
BASELINE_GENERATED_TAG = "generated:expense_baseline"
CARD_PAYMENT_TAG = "generated:card_payment"
"""On the rule an import writes to pay a card it charged spending to (#641).
It is written once and then the household's: no rerun replaces it."""
INCOME_GENERATED_TAG = "generated:income_baseline"
"""On the income rules a budget's import writes: a rerun that uses the
budget's income replaces them, and nothing else does."""
SCHEDULED_HEADER = SCHEDULED_CSV_HEADER

type SourceCategory = str
type CanonicalCategory = str
type ExpenseAccountName = str
type AccountName = str
type CategoryCanonicalization = Mapping[SourceCategory, CanonicalCategory]
type OutsideAccount = str
type Payer = tuple[AccountName, str]
"""A scenario account that pays generated rules, and its lot (blank for its only one)."""
type Cards = Mapping[tuple[str, OutsideAccount], Payer]
"""The outside accounts that are credit cards of the scenario (#641), each
by its source and its name there: what they paid is charged to the card."""
type CategoryAccountMap = Mapping[CanonicalCategory, ExpenseAccountName]
# Per canonical category: outside account -> replacement category. The ``""``
# account is the category's default, for rows no named account claims.
type CategorySplits = Mapping[CanonicalCategory, Mapping[OutsideAccount, CanonicalCategory]]


@dataclass(frozen=True)
class BaselineResult:
    """Parsed rows and derived expense tables for one baseline run."""

    transactions: pd.DataFrame
    excluded: pd.DataFrame
    monthly: pd.DataFrame
    category_totals: pd.DataFrame
    categories_defaulted_to_other: tuple[SourceCategory, ...]

    @property
    def observed_months(self) -> int:
        """The months with any spending, imported or Amazon's: what a
        category's total is divided by for its monthly rule."""
        return self.monthly["summary_month_start"].nunique() or 1


def _apply_category_splits(
    frame: pd.DataFrame, splits: CategorySplits, *, has_accounts: bool,
) -> None:
    """Rewrite ``frame["category"]`` in place per ``splits``.

    Matches against the categories as they stood on entry, so one split's
    target is never re-split by another.
    """
    original = frame["category"].copy()
    for category, targets in splits.items():
        unclaimed = original.eq(category)
        if has_accounts:
            for account, target in targets.items():
                if not account:
                    continue
                claimed = unclaimed & frame["account"].eq(account)
                frame.loc[claimed, "category"] = target
                unclaimed &= ~claimed
        if "" in targets:
            frame.loc[unclaimed, "category"] = targets[""]


def build_expense_baseline(
    flows: Flows,
    amazon_monthly_summaries: pd.DataFrame,
    category_merges: CategoryCanonicalization | None = None,
    excluded_categories: Collection[CanonicalCategory] | None = None,
    default_category: CanonicalCategory | None = None,
    category_splits: CategorySplits | None = None,
    excluded_payee_patterns: Collection[str] = (),
    *,
    source_label: str | None = None,
) -> BaselineResult:
    """Build baseline expenses, substituting Amazon summaries for Amazon rows.

    ``category_merges`` maps source category names to the desired baseline
    category for both the imported and the Amazon data. ``excluded_categories``
    contains category names to remove after merges are applied. Imported rows are
    retained only when they are negative, non-transfer transactions whose
    payee is not Amazon, a mortgage, or a match for one of
    ``excluded_payee_patterns`` (case-insensitive regular expressions for
    expenses the scenario models separately). ``category_splits`` divides a
    canonical category by the outside account that paid (see
    ``mapping.load_category_splits``); it is applied after the merges. Amazon
    summaries must contain ``summary_month_start``,
    ``category``, and ``amount_total`` columns and use positive expense totals.
    ``source_label`` names the imported rows in the monthly table's ``source``
    column (default: the flows' source, capitalized).
    """
    label = source_label or flows.source.capitalize()
    raw = flows.transactions.copy()
    raw["category_raw"] = raw["category"]
    merges = {str(source): str(target) for source, target in (category_merges or {}).items()}
    raw["category"] = raw["category"].replace(merges)
    splits = category_splits or {}
    _apply_category_splits(raw, splits, has_accounts=True)
    # Each pattern is searched for by itself, exactly as load_excluded_payees
    # validated it: joined into one expression, inline flags and group
    # references in one pattern would break or change another.
    payee_excluded = pd.Series(False, index=raw.index)
    for pattern in (*_ALWAYS_EXCLUDED_PAYEE_PATTERNS, *excluded_payee_patterns):
        search = re.compile(pattern, re.IGNORECASE).search
        payee_excluded |= raw["payee"].map(lambda payee, search=search: bool(search(payee)))
    categories_to_exclude = {str(category) for category in (excluded_categories or ())}
    raw["exclude_reason"] = ""
    raw.loc[raw["amount_cents"] >= 0, "exclude_reason"] = "non_expense"
    raw.loc[raw["transfer"].ne(""), "exclude_reason"] = "transfer"
    raw.loc[payee_excluded, "exclude_reason"] = "modeled_separately"
    raw.loc[
        raw["category"].str.contains(_EXCLUDED_CATEGORY_RE, na=False),
        "exclude_reason",
    ] = "modeled_separately"
    raw.loc[
        raw["category"].isin(categories_to_exclude),
        "exclude_reason",
    ] = "category_excluded"

    included = raw.loc[raw["exclude_reason"].eq("")].copy()
    excluded = raw.loc[raw["exclude_reason"].ne("")].copy()
    included["amount"] = -included["amount_cents"] / 100
    included["summary_month_start"] = included["date"].dt.to_period("M").dt.to_timestamp()

    amazon = amazon_monthly_summaries.copy()
    required = {"summary_month_start", "category", "amount_total"}
    missing = required - set(amazon.columns)
    if missing:
        raise ValueError(f"Amazon summaries missing required columns: {sorted(missing)}")
    amazon = amazon.loc[:, ["summary_month_start", "category", "amount_total"]].copy()
    amazon["category"] = amazon["category"].replace(merges)
    _apply_category_splits(amazon, splits, has_accounts=False)
    amazon = amazon.loc[~amazon["category"].isin(categories_to_exclude)].copy()
    observed_categories = set(included["category_raw"]) | set(amazon["category"])
    # A merge target, a split's category and a split's targets are configured,
    # not unmapped, so the default category must not swallow them. (Amazon
    # rows are observed after merging, so a merge target is what they carry.)
    split_categories = set(splits) | {
        target for targets in splits.values() for target in targets.values()
    }
    explicitly_mapped = (
        set(merges) | set(merges.values()) | categories_to_exclude | split_categories
    )
    defaulted_categories = tuple(sorted(
        category for category in observed_categories
        if category not in explicitly_mapped and category not in {default_category, ""}
    ))
    if default_category is not None:
        raw.loc[raw["category_raw"].isin(defaulted_categories), "category"] = default_category
        included.loc[
            included["category_raw"].isin(defaulted_categories), "category"
        ] = default_category
        amazon.loc[amazon["category"].isin(defaulted_categories), "category"] = default_category
    amazon["summary_month_start"] = pd.to_datetime(amazon["summary_month_start"])
    amazon["amount"] = pd.to_numeric(amazon["amount_total"], errors="raise")
    amazon["source"] = "Amazon summary"

    included["source"] = label
    imported_monthly = (
        included.groupby(["summary_month_start", "category"], as_index=False)["amount"]
        .sum()
    )
    imported_monthly["source"] = label
    monthly = pd.concat(
        [imported_monthly, amazon[["summary_month_start", "category", "amount", "source"]]],
        ignore_index=True,
    )
    monthly = monthly.sort_values(["summary_month_start", "category", "source"])
    category_totals = (
        monthly.groupby("category", as_index=False)["amount"]
        .sum()
        .rename(columns={"amount": "amount_total"})
        .sort_values(["amount_total", "category"], ascending=[False, True])
    )
    return BaselineResult(
        transactions=included,
        excluded=excluded,
        monthly=monthly.reset_index(drop=True),
        category_totals=category_totals.reset_index(drop=True),
        categories_defaulted_to_other=defaulted_categories,
    )


YEARLY_MONTHS = 2
"""A budget's category budgeted in no more months of a year than this is
paid in those months, once a year each, and not monthly
(``analysis.sim.imports.budget``). A year's transactions are read by
``OCCASIONAL_GAP_MONTHS`` instead."""
OCCASIONAL_LEAST_CENTS = 60_000
"""The least a payee paid now and then comes to in a year to be worth rules
of its own: under 50 dollars a month, spreading it changes little."""
OCCASIONAL_BASELINE_DAYS = 360
"""The least days, first transaction to last, that payments now and then
are looked for in: in less, a yearly bill can't be told from a monthly one
that just began. Days and not calendar months, which the last day of one
month to the first of another would fill with far less."""
OCCASIONAL_GAP_MONTHS = 3
"""Payments in months closer than this are a monthly bill that began or
ended in the period, or a season's, and not a bill that comes round. It is
also what bounds a candidate's months: four of any twelve, a quarterly
bill's, are the most that months this far apart allow."""


@dataclass(frozen=True)
class Occasional:
    """A payee the baseline spreads over every month, though it was paid
    in only a few months of a year: a candidate for a yearly rule for each."""

    category: CanonicalCategory
    payee: str
    """The payee as one of its transactions has it."""
    paid: tuple[tuple[date, int], ...]
    """Each month it was paid in, and the cents paid then."""
    repeats: bool
    """Whether a payment came round again about a year after another. Seen
    only once, it may as well be a one-time expense as a yearly bill."""

    sources: tuple[str, ...] = ()
    """The sources its transactions were read from, where they say."""
    accounts: tuple[tuple[str, OutsideAccount], ...] = ()
    """The outside accounts that paid it, each with its source."""
    elsewhere: bool = False
    """Whether the payee is paid in other categories too: leaving the
    payee out would then drop more than these payments."""

    @property
    def total_cents(self) -> int:
        return sum(cents for _, cents in self.paid)


def months_covered(baseline: BaselineResult) -> int:
    """The calendar months from the first imported transaction used to the
    last, both counted."""
    months = baseline.transactions["summary_month_start"]
    if months.empty:
        return 0
    first, last = months.min(), months.max()
    return (last.year - first.year) * 12 + last.month - first.month + 1


def occasional_payments(
    baseline: BaselineResult, *, least_cents: int = OCCASIONAL_LEAST_CENTS,
) -> list[Occasional] | None:
    """The large payments that may be yearly and not monthly, the largest
    first; None when the transactions cover too little time to tell
    (``OCCASIONAL_BASELINE_DAYS``).

    One is a payee of a category never paid in months closer than
    ``OCCASIONAL_GAP_MONTHS`` (so in at most four of any twelve), for
    ``least_cents`` or more a year. That leaves out a monthly bill that
    began or ended in the period, and a season's. It can't leave out a one-time expense, which
    looks like a yearly bill seen once: ``Occasional.repeats`` says which
    were seen to come round.
    """
    from analysis.sim.imports.mapping import payee_key

    used = baseline.transactions
    if used.empty:
        return None
    first, last = used["date"].min(), used["date"].max()
    days = (last - first).days
    if days < OCCASIONAL_BASELINE_DAYS:
        return None
    months = used["summary_month_start"]
    keyed = pd.DataFrame({
        "category": used["category"], "payee": used["payee"], "key": used["payee"].map(payee_key),
        "at": (months.dt.year - first.year) * 12 + months.dt.month - first.month,
        "cents": -used["amount_cents"],
        # Rows read by `sim import` say which source each came from.
        "origin": used[ORIGIN] if ORIGIN in used.columns else "",
        "account": used["account"] if "account" in used.columns else "",
    })
    keyed = keyed[keyed["key"].ne("")]
    in_categories = keyed.groupby("key")["category"].nunique()
    by_month = keyed.groupby(["category", "key", "at"], as_index=False).agg(
        cents=("cents", "sum"), payee=("payee", "first"))
    large = by_month.groupby(["category", "key"])["cents"].transform("sum") * 365 >= least_cents * days
    origins = keyed.groupby(["category", "key"])["origin"].agg(
        lambda each: tuple(sorted({str(origin) for origin in each if origin})))
    paid_by = keyed.groupby(["category", "key"])[["origin", "account"]].apply(
        lambda each: tuple(sorted(set(zip(each["origin"].astype(str), each["account"].astype(str),
                                          strict=True)))))
    found: list[Occasional] = []
    for (category, key), rows in by_month[large].groupby(["category", "key"], sort=True):
        at = [int(each) for each in rows["at"]]
        if any(later - earlier < OCCASIONAL_GAP_MONTHS for earlier, later in zip(at, at[1:], strict=False)):
            continue
        # About a year on, give or take a month: a bill's date moves a little.
        # Not eleven months on, though a bill may come a month early: in one
        # year of transactions that is a January and a December payment,
        # which a job's deposit and its last payment are as likely to be.
        repeats = any(later - earlier >= 12 and (later - earlier + 1) % 12 <= 2
                      for index, earlier in enumerate(at) for later in at[index + 1:])
        found.append(Occasional(
            str(rows["category"].iloc[0]), str(rows["payee"].iloc[0]),
            tuple((date(first.year + (first.month - 1 + each) // 12, (first.month - 1 + each) % 12 + 1, 1),
                   int(cents)) for each, cents in zip(at, rows["cents"], strict=True)),
            repeats,
            sources=origins[(category, key)], accounts=paid_by[(category, key)],
            elsewhere=int(in_categories[key]) > 1))
    return sorted(found, key=lambda each: (-each.total_cents, each.category, each.payee))


def card_spending(baseline: BaselineResult, cards: Cards) -> dict[CanonicalCategory, dict[Payer, int]]:
    """The cents of each category that each card of ``cards`` paid (#641).
    What no card paid, Amazon's summaries among it, is not here: it is the
    rest of the category's total."""
    used = baseline.transactions
    found: dict[CanonicalCategory, dict[Payer, int]] = {}
    if not cards or used.empty or ORIGIN not in used.columns:
        return found
    for origin, account, category, cents in zip(
            used[ORIGIN], used["account"], used["category"], used["amount_cents"], strict=True):
        if (card := cards.get((str(origin), str(account)))) is not None:
            by_card = found.setdefault(str(category), {})
            by_card[card] = by_card.get(card, 0) - int(cents)
    return found


def payer_slug(payer: Payer) -> str:
    """``payer`` as part of a rule's name."""
    return category_slug("_".join(filter(None, payer)))


def build_scheduled_baseline_rows(
    baseline: BaselineResult,
    account_map: CategoryAccountMap,
    start: str,
    from_account: AccountName,
    ignored_categories: Collection[CanonicalCategory] = (),
    *,
    from_lot: str = "",
    series: str = "cpi",
    priority: int = 200,
    provenance: Mapping[str, str] | None = None,
    cards: Cards | None = None,
) -> list[dict[str, str]]:
    """Create monthly fixed-amount scheduled rows from baseline category totals.

    What a card of ``cards`` paid of a category is a rule of its own, from
    the card and named for it after ``__``; the rest of the category is paid
    from ``from_account``, and has no rule when the cards paid it all.
    ``provenance`` (the source, a hash of its files) is recorded in each
    row's ``meta_json``."""
    if baseline.monthly.empty:
        return []
    observed_months = baseline.observed_months
    rows: list[dict[str, str]] = []
    ignored = set(ignored_categories)
    charged = card_spending(baseline, cards or {})
    for record in baseline.category_totals.to_dict("records"):
        category = str(record["category"])
        if category in ignored:
            continue
        to_account = account_map.get(category)
        if to_account is None:
            raise ValueError(f"no scheduled account mapping for category {category!r}")
        by_card = charged.get(category, {})
        name = f"baseline_expense_{category_slug(category)}"
        rest = round(float(record["amount_total"]) * 100) - sum(by_card.values())
        parts = [(name, (from_account, from_lot), rest)] if rest > 0 or not by_card else []
        parts += [(f"{name}__{payer_slug(card)}", card, cents) for card, cents in sorted(by_card.items())]
        for rule, (payer, lot), cents in parts:
            rows.append(generated_row(
                rule, category, f"{round(cents / observed_months) / 100:.2f}",
                from_account=payer, from_lot=lot, to_account=to_account, start=start,
                series=series, priority=priority, provenance=provenance))
    return rows


def card_payment_row(
    card: Payer, *, from_account: AccountName, from_lot: str = "", start: str,
) -> dict[str, str]:
    """The rule that pays ``card`` each month what it was owed when the
    month opened (a blank amount, ADR-003 §2.1): in full, so the card carries
    no balance until the household says otherwise."""
    row = generated_row(
        f"card_payment_{payer_slug(card)}", "", "", from_account=from_account, from_lot=from_lot,
        to_account=card[0], to_lot=card[1], start=start, series="")
    return {
        **row, "base_date": "", "description": f"Generated card payment: {card[0]} in full",
        "meta_json": json.dumps({"generated_by": "card_payment"}, separators=(",", ":")),
        "tags": CARD_PAYMENT_TAG,
    }


def category_slug(category: str) -> str:
    """``category`` as part of a rule's name."""
    return re.sub(r"[^a-z0-9]+", "_", category.casefold()).strip("_")


def generated_row(
    name: str, category: str, amount: str, *, from_account: AccountName, to_account: AccountName,
    start: str, from_lot: str = "", series: str = "cpi", priority: int = 200,
    month_of_year: int | None = None, person: str = "", tax_character: str = "none",
    to_lot: str = "", producer: Literal["expense_baseline", "income_baseline"] = "expense_baseline",
    provenance: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """One generated fixed-amount rule, monthly, or yearly in ``month_of_year``.
    ``producer`` says which generated rows it is one of: its tag is
    ``generated:<producer>``. ``provenance`` (the source, a hash of its
    files) goes in its ``meta_json``."""
    income = producer == "income_baseline"
    metadata = {"generated_by": producer, "category": category, **(provenance or {})}
    return {
        "name": name,
        "from_account": from_account,
        "from_lot": from_lot,
        "to_account": to_account,
        "to_lot": to_lot,
        "cadence": "monthly" if month_of_year is None else "annual",
        "month_of_year": "" if month_of_year is None else str(month_of_year),
        "amount_rule_kind": "fixed_amount",
        "interest_account": "",
        "person": person,
        "series": series,
        "rate": "",
        "claim_age": "",
        "amount": amount,
        "base_date": start,
        "inflation_cadence": "",
        "tax_character": tax_character,
        "start": start,
        "end": "",
        "priority": str(priority),
        "ytd_amount": "",
        "ytd_preroll": "",
        "description": f"Generated {'income' if income else 'expense'} baseline: {category}",
        "meta_json": json.dumps(metadata, separators=(",", ":")),
        "params_json": "{}",
        "tags": f"generated:{producer}",
    }


def replace_generated_scheduled_rows(
    path: Path | str,
    generated_rows: list[dict[str, str]],
    tags: Collection[str] = (BASELINE_GENERATED_TAG,),
) -> int:
    """Replace the rows carrying one of ``tags`` (the generated expense
    baseline's), preserving other rows."""
    scheduled_path = Path(path)
    existing: list[dict[str, str]] = []
    if scheduled_path.exists() and scheduled_path.stat().st_size:
        with scheduled_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != SCHEDULED_HEADER:
                raise ValueError(f"{scheduled_path}: scheduled.csv header mismatch")
            existing = [
                row for row in reader
                if not set(tags) & set((row.get("tags") or "").split())
            ]
    scheduled_path.parent.mkdir(parents=True, exist_ok=True)
    with scheduled_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SCHEDULED_HEADER)
        writer.writeheader()
        writer.writerows(existing)
        writer.writerows(generated_rows)
    return len(generated_rows)