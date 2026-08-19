from pathlib import Path

import pandas as pd

from analysis.amazon.notebook import (
    load_categorization_state,
    load_events_dataframe,
    load_monthly_summaries,
    run_amazon_categorization,
    summarize_over_all_time,
    top_category_trend,
)
from analysis.storage import JsonStateStore

DATA_DIR = Path(__file__).resolve().parent / "data" / "amazon"


def test_load_categorization_state_reads_categories_and_mapping(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "state")
    store.save_categories(["Groceries"])
    store.save_mapping({"coffee pods": "Groceries"})

    categories, mapping = load_categorization_state(tmp_path / "state")

    assert categories == ["Groceries"]
    assert mapping == {"coffee pods": "Groceries"}


def test_load_events_dataframe_returns_empty_when_no_log(tmp_path: Path) -> None:
    df = load_events_dataframe(tmp_path / "state")
    assert df.empty


def test_run_amazon_categorization_returns_dataframes(tmp_path: Path) -> None:
    input_csv = DATA_DIR / "personal_coffee.csv"

    result, annotated, summary = run_amazon_categorization(
        input_csv_path=input_csv,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "out",
        ask_for_category=None,
    )

    assert result.annotated_csv_path.exists()
    assert result.summary_csv_path.exists()
    assert annotated["category"].tolist() == ["UNCATEGORIZED"]
    assert set(summary.columns) == {"category", "amount_total"}


def test_summarize_over_all_time_reads_monthly_summaries(tmp_path: Path) -> None:
    input_csv = DATA_DIR / "personal_two_items.csv"

    run_amazon_categorization(
        input_csv_path=input_csv,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "out",
        ask_for_category=None,
    )

    df = summarize_over_all_time(tmp_path / "state")

    assert set(df.columns) == {"category", "amount_total_all_time"}
    assert "UNCATEGORIZED" in set(df["category"].tolist())


def test_load_monthly_summaries_returns_empty_when_no_state(tmp_path: Path) -> None:
    df = load_monthly_summaries(tmp_path / "state")
    assert df.empty
    assert list(df.columns) == ["summary_month_start", "category", "amount_total"]


def test_top_category_trend_pivots_top_n_categories(tmp_path: Path) -> None:
    input_csv = DATA_DIR / "personal_two_items.csv"

    run_amazon_categorization(
        input_csv_path=input_csv,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "out",
        ask_for_category=None,
    )

    monthly = load_monthly_summaries(tmp_path / "state")
    trend = top_category_trend(monthly, top_n=1)

    assert len(trend.columns) <= 1
    assert trend.index.name == "summary_month_start"