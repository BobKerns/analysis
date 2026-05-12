from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from analysis.storage import JsonStateStore

_DESCRIPTION_CANDIDATES = ["description", "item", "item title", "title", "product", "name"]
_AMOUNT_CANDIDATES = ["amount", "total", "price", "item total", "charge"]


@dataclass(frozen=True)
class CategorizationResult:
    annotated_csv_path: Path
    summary_csv_path: Path
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
    ask_for_category: Callable[[str, list[str]], str] | None = None,
) -> CategorizationResult:
    input_path = Path(input_csv_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    frame = pd.read_csv(input_path)
    description_col = _find_column(frame.columns.tolist(), _DESCRIPTION_CANDIDATES)
    amount_col = _find_column(frame.columns.tolist(), _AMOUNT_CANDIDATES)

    store = JsonStateStore(state_dir)
    categories = store.load_categories()
    known_mapping = store.load_mapping()

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
            chosen = ask_for_category(item, categories).strip()

        if not chosen:
            chosen = "UNCATEGORIZED"
            uncategorized += 1
        else:
            if chosen not in categories:
                categories.append(chosen)
            known_mapping[item] = chosen

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
    summary.to_csv(summary_path, index=False)

    store.save_categories(categories)
    store.save_mapping(known_mapping)
    store.append_history(
        {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "input_csv": str(input_path),
            "annotated_csv": str(annotated_path),
            "summary_csv": str(summary_path),
            "row_count": int(len(working)),
            "uncategorized_count": uncategorized,
        }
    )

    return CategorizationResult(
        annotated_csv_path=annotated_path,
        summary_csv_path=summary_path,
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
