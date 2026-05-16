from pathlib import Path

import pandas as pd

from analysis.amazon.notebook import (
    load_categorization_state,
    load_events_dataframe,
    run_amazon_categorization,
)
from analysis.storage import JsonStateStore


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
    input_csv = tmp_path / "amazon.csv"
    pd.DataFrame({"Description": ["Coffee Pods"], "Amount": [12.0]}).to_csv(input_csv, index=False)

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