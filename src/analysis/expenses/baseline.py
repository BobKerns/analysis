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
_EXCLUDED_PAYEE_RE = re.compile(r"amazon|state\s+farm|mortgage|paylease", re.IGNORECASE)
_EXCLUDED_CATEGORY_RE = re.compile(r"mortgage", re.IGNORECASE)
_CATEGORY_MAP_HEADER = ("quicken_category", "target_category")
_EXPENSE_ACCOUNT_MAP_HEADER = ("category", "target_account")
BASELINE_GENERATED_TAG = "generated:expense_baseline"
SCHEDULED_HEADER = SCHEDULED_CSV_HEADER

type SourceCategory = str
type CanonicalCategory = str
type ExpenseAccountName = str
type AccountName = str
type CategoryCanonicalization = Mapping[SourceCategory, CanonicalCategory]
type CategoryAccountMap = Mapping[CanonicalCategory, ExpenseAccountName]


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
    food_owner_account: str | None = None,
) -> BaselineResult:
    """Build baseline expenses, substituting Amazon summaries for Amazon rows.

    ``category_merges`` maps source category names to the desired baseline
    category for both Quicken and Amazon data. ``excluded_categories`` contains
    category names to remove after merges are applied. Quicken rows are
    retained only when they are negative, non-transfer
    transactions outside the explicitly modeled mortgage, State Farm, and
    Amazon exclusions. When ``food_owner_account`` is supplied, canonical
    ``Food`` rows from that Quicken account become ``Hiromi Food`` and all
    other Food, including Amazon summaries, becomes ``Bob Food``. Amazon
    summaries must contain ``summary_month_start``,
    ``category``, and ``amount_total`` columns and use positive expense totals.
    """
    raw = load_quicken_transactions(quicken_path).copy()
    raw["category_raw"] = raw["category"]
    merges = {str(source): str(target) for source, target in (category_merges or {}).items()}
    raw["category"] = raw["category"].replace(merges)
    if food_owner_account is not None:
        raw.loc[
            raw["category"].eq("Food") & raw["account"].eq(food_owner_account),
            "category",
        ] = "Hiromi Food"
        raw.loc[raw["category"].eq("Food"), "category"] = "Bob Food"
    categories_to_exclude = {str(category) for category in (excluded_categories or ())}
    raw["exclude_reason"] = ""
    raw.loc[raw["amount_cents"] >= 0, "exclude_reason"] = "non_expense"
    raw.loc[raw["transfer"].ne(""), "exclude_reason"] = "transfer"
    raw.loc[
        raw["payee"].str.contains(_EXCLUDED_PAYEE_RE, na=False),
        "exclude_reason",
    ] = "modeled_separately"
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
    if food_owner_account is not None:
        amazon.loc[amazon["category"].eq("Food"), "category"] = "Bob Food"
    amazon = amazon.loc[~amazon["category"].isin(categories_to_exclude)].copy()
    observed_categories = set(included["category_raw"]) | set(amazon["category"])
    explicitly_mapped = set(merges) | categories_to_exclude
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