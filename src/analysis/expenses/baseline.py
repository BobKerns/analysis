"""Build a partial expense baseline from Quicken and Amazon summary data."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd

from analysis.sim.registries import SCHEDULED_CSV_HEADER

_DATE_FORMAT = "%Y-%m-%d"
# Excluded for every household: Amazon rows are replaced by the Amazon
# summaries, and a mortgage is modeled in the scenario. Household-specific
# payees come from the data repo (``load_excluded_payees``).
_ALWAYS_EXCLUDED_PAYEE_PATTERNS = ("amazon", "mortgage")
_EXCLUDED_CATEGORY_RE = re.compile(r"mortgage", re.IGNORECASE)
_CATEGORY_MAP_HEADER = ("quicken_category", "target_category")
_EXPENSE_ACCOUNT_MAP_HEADER = ("category", "target_account")
_CATEGORY_SPLIT_HEADER = ("category", "account", "target_category")
_EXCLUDED_PAYEE_HEADER = ("payee_pattern",)
BASELINE_GENERATED_TAG = "generated:expense_baseline"
SCHEDULED_HEADER = SCHEDULED_CSV_HEADER

type SourceCategory = str
type CanonicalCategory = str
type ExpenseAccountName = str
type AccountName = str
type CategoryCanonicalization = Mapping[SourceCategory, CanonicalCategory]
type QuickenAccount = str
type CategoryAccountMap = Mapping[CanonicalCategory, ExpenseAccountName]
# Per canonical category: Quicken account -> replacement category. The ``""``
# account is the category's default, for rows no named account claims.
type CategorySplits = Mapping[CanonicalCategory, Mapping[QuickenAccount, CanonicalCategory]]


@dataclass(frozen=True)
class BaselineResult:
    """Parsed rows and derived expense tables for one baseline run."""

    transactions: pd.DataFrame
    excluded: pd.DataFrame
    monthly: pd.DataFrame
    category_totals: pd.DataFrame
    categories_defaulted_to_other: tuple[SourceCategory, ...]


def load_category_map(
    path: Path | str,
) -> tuple[dict[SourceCategory, CanonicalCategory], set[CanonicalCategory]]:
    """Load Quicken category merges and exclusions from a two-column CSV.

    A blank or ``-`` target excludes the source category. All other targets
    merge the source category into that target category.
    """
    merges: dict[SourceCategory, CanonicalCategory] = {}
    exclusions: set[CanonicalCategory] = set()
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _CATEGORY_MAP_HEADER:
            raise ValueError(
                f"{path}: expected header {list(_CATEGORY_MAP_HEADER)}, "
                f"got {list(reader.fieldnames or [])}"
            )
        for row_number, row in enumerate(reader, start=2):
            source = row["quicken_category"].strip()
            target = row["target_category"].strip()
            if not source:
                continue
            if source in merges or source in exclusions:
                raise ValueError(f"{path} row {row_number}: duplicate category {source!r}")
            if not target or target == "-":
                exclusions.add(source)
            else:
                merges[source] = target
    return merges, exclusions


def load_expense_account_map(path: Path | str) -> dict[CanonicalCategory, ExpenseAccountName]:
    """Load optional canonical category to expense-account mappings."""
    mappings: dict[CanonicalCategory, ExpenseAccountName] = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _EXPENSE_ACCOUNT_MAP_HEADER:
            raise ValueError(
                f"{path}: expected header {list(_EXPENSE_ACCOUNT_MAP_HEADER)}, "
                f"got {list(reader.fieldnames or [])}"
            )
        for row_number, row in enumerate(reader, start=2):
            category = row["category"].strip()
            target_account = row["target_account"].strip()
            if not category:
                continue
            if category in mappings:
                raise ValueError(f"{path} row {row_number}: duplicate category {category!r}")
            if not target_account:
                raise ValueError(f"{path} row {row_number}: target account is required")
            mappings[category] = target_account
    return mappings


def load_category_splits(
    path: Path | str,
) -> dict[CanonicalCategory, dict[QuickenAccount, CanonicalCategory]]:
    """Load splits of one canonical category by the Quicken account that paid.

    Each row sends ``category`` expenses charged to ``account`` to
    ``target_category``. A row with a blank ``account`` is that category's
    default: it takes every row no named account claims, including Amazon
    summaries, which have no account.
    """
    splits: dict[CanonicalCategory, dict[QuickenAccount, CanonicalCategory]] = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _CATEGORY_SPLIT_HEADER:
            raise ValueError(
                f"{path}: expected header {list(_CATEGORY_SPLIT_HEADER)}, "
                f"got {list(reader.fieldnames or [])}"
            )
        for row_number, row in enumerate(reader, start=2):
            category = row["category"].strip()
            account = row["account"].strip()
            target = row["target_category"].strip()
            if not category:
                continue
            if not target:
                raise ValueError(f"{path} row {row_number}: target category is required")
            targets = splits.setdefault(category, {})
            if account in targets:
                raise ValueError(
                    f"{path} row {row_number}: duplicate split of {category!r} "
                    f"for account {account!r}"
                )
            targets[account] = target
    return splits


def load_excluded_payees(path: Path | str) -> tuple[str, ...]:
    """Load payee patterns for expenses the scenario models separately.

    Each ``payee_pattern`` is a case-insensitive regular expression searched
    for anywhere in the Quicken payee.
    """
    patterns: list[str] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _EXCLUDED_PAYEE_HEADER:
            raise ValueError(
                f"{path}: expected header {list(_EXCLUDED_PAYEE_HEADER)}, "
                f"got {list(reader.fieldnames or [])}"
            )
        for row_number, row in enumerate(reader, start=2):
            pattern = row["payee_pattern"].strip()
            if not pattern:
                continue
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(
                    f"{path} row {row_number}: invalid payee pattern {pattern!r}: {error}"
                ) from error
            patterns.append(pattern)
    return tuple(patterns)


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


def load_quicken_transactions(path: Path | str) -> pd.DataFrame:
    """Parse transaction rows from a Quicken Transaction Export CSV.

    Account headers, separator rows, and account totals are ignored. The
    returned frame preserves source row numbers and raw Quicken fields.
    """
    rows: list[dict[str, object]] = []
    current_account = ""
    header: list[str] | None = None

    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        for source_row, row in enumerate(csv.reader(handle), start=1):
            if not row:
                continue
            if row[0].strip().casefold() == "account":
                header = [column.strip() for column in row]
                continue
            if header is None:
                continue

            values = row + [""] * (len(header) - len(row))
            first = values[0].strip()
            if first:
                if first.casefold().startswith("total") or first.startswith("-"):
                    continue
                current_account = first
                continue

            record = dict(zip(header, values, strict=False))
            date_raw = str(record.get("Date", "")).strip()
            if not date_raw:
                continue
            try:
                transaction_date = pd.to_datetime(date_raw, format=_DATE_FORMAT)
            except (TypeError, ValueError):
                continue

            amount_raw = str(record.get("Amount", "")).strip().replace(",", "")
            if not amount_raw:
                continue
            amount_cents = int(Decimal(amount_raw) * 100)
            rows.append(
                {
                    "source_row": source_row,
                    "account": current_account,
                    "date": transaction_date,
                    "entered": str(record.get("Entered", "")).strip(),
                    "posted": str(record.get("Posted", "")).strip(),
                    "action": str(record.get("Action", "")).strip(),
                    "payee": str(record.get("Payee", "")).strip(),
                    "category": str(record.get("Category", "")).strip(),
                    "tags": str(record.get("Tags", "")).strip(),
                    "transfer": str(record.get("Transfer", "")).strip(),
                    "memo": str(record.get("Memo/Notes", "")).strip(),
                    "amount_cents": amount_cents,
                }
            )

    columns = [
        "source_row", "account", "date", "entered", "posted", "action",
        "payee", "category", "tags", "transfer", "memo", "amount_cents",
    ]
    return pd.DataFrame(rows, columns=columns)


def build_expense_baseline(
    quicken_path: Path | str,
    amazon_monthly_summaries: pd.DataFrame,
    category_merges: CategoryCanonicalization | None = None,
    excluded_categories: Collection[CanonicalCategory] | None = None,
    default_category: CanonicalCategory | None = None,
    category_splits: CategorySplits | None = None,
    excluded_payee_patterns: Collection[str] = (),
) -> BaselineResult:
    """Build baseline expenses, substituting Amazon summaries for Amazon rows.

    ``category_merges`` maps source category names to the desired baseline
    category for both Quicken and Amazon data. ``excluded_categories`` contains
    category names to remove after merges are applied. Quicken rows are
    retained only when they are negative, non-transfer transactions whose
    payee is not Amazon, a mortgage, or a match for one of
    ``excluded_payee_patterns`` (case-insensitive regular expressions for
    expenses the scenario models separately). ``category_splits`` divides a
    canonical category by the Quicken account that paid (see
    ``load_category_splits``); it is applied after the merges. Amazon
    summaries must contain ``summary_month_start``,
    ``category``, and ``amount_total`` columns and use positive expense totals.
    """
    raw = load_quicken_transactions(quicken_path).copy()
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

    included["source"] = "Quicken"
    quicken_monthly = (
        included.groupby(["summary_month_start", "category"], as_index=False)["amount"]
        .sum()
    )
    quicken_monthly["source"] = "Quicken"
    monthly = pd.concat(
        [quicken_monthly, amazon[["summary_month_start", "category", "amount", "source"]]],
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
) -> list[dict[str, str]]:
    """Create monthly fixed-amount scheduled rows from baseline category totals."""
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
        metadata = {"generated_by": "expense_baseline", "category": category}
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
            "rate_micros": "",
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