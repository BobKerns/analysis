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
from pathlib import Path

import pandas as pd

from analysis.sim.imports.forms import Flows
from analysis.sim.registries import SCHEDULED_CSV_HEADER

# Excluded for every household: Amazon rows are replaced by the Amazon
# summaries, and a mortgage is modeled in the scenario. Household-specific
# payees come from the data repo (``load_excluded_payees``).
_ALWAYS_EXCLUDED_PAYEE_PATTERNS = ("amazon", "mortgage")
_EXCLUDED_CATEGORY_RE = re.compile(r"mortgage", re.IGNORECASE)
BASELINE_GENERATED_TAG = "generated:expense_baseline"
SCHEDULED_HEADER = SCHEDULED_CSV_HEADER

type SourceCategory = str
type CanonicalCategory = str
type ExpenseAccountName = str
type AccountName = str
type CategoryCanonicalization = Mapping[SourceCategory, CanonicalCategory]
type OutsideAccount = str
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
) -> list[dict[str, str]]:
    """Create monthly fixed-amount scheduled rows from baseline category totals.

    ``provenance`` (the source, a hash of its files) is recorded in each
    row's ``meta_json``."""
    if baseline.monthly.empty:
        return []
    observed_months = baseline.monthly["summary_month_start"].nunique()
    rows: list[dict[str, str]] = []
    ignored = set(ignored_categories)
    for record in baseline.category_totals.to_dict("records"):
        category = str(record["category"])
        if category in ignored:
            continue
        to_account = account_map.get(category)
        if to_account is None:
            raise ValueError(f"no scheduled account mapping for category {category!r}")
        amount = round(float(record["amount_total"]) / observed_months, 2)
        slug = re.sub(r"[^a-z0-9]+", "_", category.casefold()).strip("_")
        metadata = {"generated_by": "expense_baseline", "category": category, **(provenance or {})}
        rows.append({
            "name": f"baseline_expense_{slug}",
            "from_account": from_account,
            "from_lot": from_lot,
            "to_account": to_account,
            "to_lot": "",
            "cadence": "monthly",
            "month_of_year": "",
            "amount_rule_kind": "fixed_amount",
            "interest_account": "",
            "person": "",
            "series": series,
            "rate": "",
            "claim_age": "",
            "amount": f"{amount:.2f}",
            "base_date": start,
            "inflation_cadence": "",
            "tax_character": "none",
            "start": start,
            "end": "",
            "priority": str(priority),
            "ytd_amount": "",
            "ytd_preroll": "",
            "description": f"Generated expense baseline: {category}",
            "meta_json": json.dumps(metadata, separators=(",", ":")),
            "params_json": "{}",
            "tags": BASELINE_GENERATED_TAG,
        })
    return rows


def replace_generated_scheduled_rows(
    path: Path | str,
    generated_rows: list[dict[str, str]],
) -> int:
    """Replace rows tagged as generated baseline rows, preserving other rows."""
    scheduled_path = Path(path)
    existing: list[dict[str, str]] = []
    if scheduled_path.exists() and scheduled_path.stat().st_size:
        with scheduled_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != SCHEDULED_HEADER:
                raise ValueError(f"{scheduled_path}: scheduled.csv header mismatch")
            existing = [
                row for row in reader
                if BASELINE_GENERATED_TAG not in (row.get("tags") or "").split()
            ]
    scheduled_path.parent.mkdir(parents=True, exist_ok=True)
    with scheduled_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SCHEDULED_HEADER)
        writer.writeheader()
        writer.writerows(existing)
        writer.writerows(generated_rows)
    return len(generated_rows)