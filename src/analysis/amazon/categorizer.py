from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from analysis.storage import JsonStateStore

_log = logging.getLogger(__name__)

_DESCRIPTION_CANDIDATES = [
    "title",
    "description",
    "item",
    "item title",
    "product",
    "name",
    "asin",
]
_AMOUNT_CANDIDATES = [
    "item net total",
    "item subtotal",
    "total amount",
    "order net total",
    "amount",
    "total",
    "price",
    "item total",
    "charge",
]
_ORDER_DATE_CANDIDATES = [
    "order date",
    "date",
    "transaction date",
]
_MONTHLY_CUTOFF_DAY = 3


@dataclass(frozen=True)
class MonthlyOutput:
    month: str
    is_pending: bool
    annotated_csv_path: Path
    summary_csv_path: Path


@dataclass(frozen=True)
class CategorizationResult:
    annotated_csv_path: Path
    summary_csv_path: Path
    monthly_outputs: list[MonthlyOutput]
    categories_count: int
    mapped_items_count: int
    uncategorized_count: int


def normalize_item_description(raw: str) -> str:
    normalized = " ".join(str(raw).strip().lower().split())
    return normalized


def categorize_amazon_csv(
    input_csv_path: Path | str,
    state_dir: Path | str,
    output_dir: Path | str,
    cutoff_day: int = _MONTHLY_CUTOFF_DAY,
    ask_for_category: Callable[[str, list[str]], str] | None = None,
) -> CategorizationResult:
    input_path = Path(input_csv_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    frame = pd.read_csv(input_path)
    description_col = _find_column(frame.columns.tolist(), _DESCRIPTION_CANDIDATES)
    amount_col = _find_column(frame.columns.tolist(), _AMOUNT_CANDIDATES)
    order_date_col = _find_optional_column(frame.columns.tolist(), _ORDER_DATE_CANDIDATES)

    store = JsonStateStore(state_dir)
    categories = store.load_categories()
    known_mapping = store.load_mapping()

    _log.info(
        "categorize.run.start",
        extra={
            "event": "categorize.run.start",
            "input_csv": str(input_path),
            "row_count": len(frame),
        },
    )

    working = frame.copy()
    working["normalized_item"] = working[description_col].map(normalize_item_description)

    assigned_categories: list[str] = []
    uncategorized = 0

    for item in working["normalized_item"]:
        existing = known_mapping.get(item)
        if existing:
            assigned_categories.append(existing)
            continue

        chosen = ""
        if ask_for_category is not None:
            _log.info("item.unknown", extra={"event": "item.unknown", "item": item})
            chosen = ask_for_category(item, categories).strip()

        if not chosen:
            chosen = "UNCATEGORIZED"
            uncategorized += 1
        else:
            if chosen not in categories:
                categories.append(chosen)
                store.save_categories(categories)
            known_mapping[item] = chosen
            store.save_mapping(known_mapping)

        assigned_categories.append(chosen)

    working["category"] = assigned_categories

    working[amount_col] = pd.to_numeric(working[amount_col], errors="coerce").fillna(0.0)
    summary = (
        working.groupby("category", dropna=False)[amount_col]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
        .rename(columns={amount_col: "amount_total"})
    )

    stem = input_path.stem
    annotated_path = out_dir / f"{stem}_annotated.csv"
    summary_path = out_dir / f"{stem}_summary.csv"

    working.to_csv(annotated_path, index=False)
    summary.to_csv(summary_path, index=False, float_format="%.2f")

    monthly_outputs = _write_monthly_outputs(
        working=working,
        amount_col=amount_col,
        order_date_col=order_date_col,
        output_dir=out_dir,
        cutoff_day=cutoff_day,
    )

    store.insert_items(working.to_dict(orient="records"), source_file=input_path)

    monthly_summary = (
        working.assign(
            summary_month_start=_billing_month_start_series(
                order_dates=working[order_date_col] if order_date_col is not None else None,
                row_count=len(working),
                index=working.index,
                cutoff_day=cutoff_day,
            ).dt.strftime("%Y-%m-01")
        )
        .groupby(["summary_month_start", "category"], dropna=False)[amount_col]
        .sum()
        .reset_index()
    )

    store.upsert_monthly_summaries(
        [
            (
                str(row["summary_month_start"]),
                str(row["category"]),
                float(row[amount_col]),
            )
            for _, row in monthly_summary.iterrows()
        ]
    )

    store.save_categories(categories)
    store.save_mapping(known_mapping)
    _log.info(
        "categorize.run.complete",
        extra={
            "event": "categorize.run.complete",
            "input_csv": str(input_path),
            "annotated_csv": str(annotated_path),
            "summary_csv": str(summary_path),
            "cutoff_day": cutoff_day,
            "monthly_output_count": len(monthly_outputs),
            "row_count": int(len(working)),
            "uncategorized_count": uncategorized,
            "categories_count": len(categories),
            "mapped_items_count": len(known_mapping),
        },
    )

    return CategorizationResult(
        annotated_csv_path=annotated_path,
        summary_csv_path=summary_path,
        monthly_outputs=monthly_outputs,
        categories_count=len(categories),
        mapped_items_count=len(known_mapping),
        uncategorized_count=uncategorized,
    )


def _find_column(columns: list[str], candidates: list[str]) -> str:
    by_casefold = {column.casefold(): column for column in columns}
    for candidate in candidates:
        match = by_casefold.get(candidate.casefold())
        if match:
            return match

    available = ", ".join(columns)
    expected = ", ".join(candidates)
    raise ValueError(f"Missing required column. Expected one of: {expected}. Found: {available}")


def _find_optional_column(columns: list[str], candidates: list[str]) -> str | None:
    by_casefold = {column.casefold(): column for column in columns}
    for candidate in candidates:
        match = by_casefold.get(candidate.casefold())
        if match:
            return match
    return None


def _billing_month_start_series(
    order_dates: pd.Series | None,
    row_count: int,
    index: pd.Index,
    cutoff_day: int,
) -> pd.Series:
    fallback = pd.Timestamp(_open_billing_period_start(cutoff_day))
    if order_dates is None:
        return pd.Series([fallback] * row_count, index=index)

    parsed = pd.to_datetime(order_dates, errors="coerce")
    month_start = parsed.dt.to_period("M").dt.to_timestamp()
    next_month = month_start + pd.offsets.MonthBegin(1)
    billing_month = month_start.where(parsed.dt.day <= cutoff_day, next_month)
    return billing_month.fillna(fallback)


def _write_monthly_outputs(
    working: pd.DataFrame,
    amount_col: str,
    order_date_col: str | None,
    output_dir: Path,
    cutoff_day: int,
) -> list[MonthlyOutput]:
    today = _today_date()
    month_start = _billing_month_start_series(
        order_dates=working[order_date_col] if order_date_col is not None else None,
        row_count=len(working),
        index=working.index,
        cutoff_day=cutoff_day,
    )

    month_labels = month_start.dt.strftime("%Y-%m")
    with_month = working.assign(_billing_month=month_labels)

    outputs: list[MonthlyOutput] = []
    for month in sorted(with_month["_billing_month"].unique()):
        is_pending = _is_month_pending(month, cutoff_day=cutoff_day, today=today)
        month_rows = with_month[with_month["_billing_month"] == month].drop(
            columns=["_billing_month"]
        )
        month_summary = (
            month_rows.groupby("category", dropna=False)[amount_col]
            .sum()
            .sort_values(ascending=False)
            .reset_index()
            .rename(columns={amount_col: "amount_total"})
        )

        name_prefix = f"Amazon-{month}{'_pending' if is_pending else ''}"
        annotated_path = output_dir / f"{name_prefix}_annotated.csv"
        summary_path = output_dir / f"{name_prefix}_summary.csv"
        month_rows.to_csv(annotated_path, index=False)
        month_summary.to_csv(summary_path, index=False, float_format="%.2f")
        outputs.append(
            MonthlyOutput(
                month=month,
                is_pending=is_pending,
                annotated_csv_path=annotated_path,
                summary_csv_path=summary_path,
            )
        )

    return outputs


def _open_billing_period_start(cutoff_day: int) -> date:
    today = _today_date()
    if today.day <= cutoff_day:
        return today.replace(day=1)

    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


def _is_month_pending(month: str, cutoff_day: int, today: date) -> bool:
    start_date, end_date = _billing_interval_for_month(month, cutoff_day=cutoff_day)
    return start_date <= today <= end_date


def _billing_interval_for_month(month: str, cutoff_day: int) -> tuple[date, date]:
    year = int(month[:4])
    month_num = int(month[5:7])
    end_date = date(year, month_num, cutoff_day)

    if month_num == 1:
        previous_cutoff = date(year - 1, 12, cutoff_day)
    else:
        previous_cutoff = date(year, month_num - 1, cutoff_day)
    start_date = previous_cutoff + timedelta(days=1)
    return start_date, end_date


def _today_date() -> date:
    return date.today()
