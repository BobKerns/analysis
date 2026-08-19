"""Build a partial expense baseline from Quicken and Amazon summary data."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd

_DATE_FORMAT = "%Y-%m-%d"
_EXCLUDED_PAYEE_RE = re.compile(r"amazon|state\s+farm|mortgage|paylease", re.IGNORECASE)
_EXCLUDED_CATEGORY_RE = re.compile(r"mortgage", re.IGNORECASE)


@dataclass(frozen=True)
class BaselineResult:
    """Parsed rows and derived expense tables for one baseline run."""

    transactions: pd.DataFrame
    excluded: pd.DataFrame
    monthly: pd.DataFrame
    category_totals: pd.DataFrame


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
) -> BaselineResult:
    """Build baseline expenses, substituting Amazon summaries for Amazon rows.

    Quicken rows are retained only when they are negative, non-transfer
    transactions outside the explicitly modeled mortgage, State Farm, and
    Amazon exclusions. Amazon summaries must contain ``summary_month_start``,
    ``category``, and ``amount_total`` columns and use positive expense totals.
    """
    raw = load_quicken_transactions(quicken_path).copy()
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
    )