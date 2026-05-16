import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

from analysis.amazon.categorizer import categorize_amazon_csv
from analysis.storage import JsonStateStore


def test_categorize_amazon_csv_creates_outputs_and_persists_mapping(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon_jan.csv"
    state_dir = tmp_path / "state"
    out_dir = tmp_path / "out"

    frame = pd.DataFrame(
        {
            "Description": ["Coffee Pods", "Paper Towels", "Coffee Pods"],
            "Amount": [14.5, 10.0, 14.5],
        }
    )
    frame.to_csv(input_csv, index=False)

    assigned = {"coffee pods": "Groceries", "paper towels": "Household"}

    def ask_for_category(item: str, _known: list[str]) -> str:
        return assigned[item]

    result = categorize_amazon_csv(
        input_csv_path=input_csv,
        state_dir=state_dir,
        output_dir=out_dir,
        ask_for_category=ask_for_category,
    )

    assert result.annotated_csv_path.exists()
    assert result.summary_csv_path.exists()
    assert result.uncategorized_count == 0

    annotated = pd.read_csv(result.annotated_csv_path)
    assert annotated["category"].tolist() == ["Groceries", "Household", "Groceries"]

    summary = pd.read_csv(result.summary_csv_path)
    totals = dict(zip(summary["category"], summary["amount_total"], strict=False))
    assert totals["Groceries"] == 29.0
    assert totals["Household"] == 10.0

    store = JsonStateStore(state_dir)
    mapping = store.load_mapping()
    store.close()
    assert mapping == {"coffee pods": "Groceries", "paper towels": "Household"}


def test_categorize_amazon_csv_uses_uncategorized_when_non_interactive(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon_feb.csv"
    frame = pd.DataFrame({"Description": ["Unknown Item"], "Amount": [9.99]})
    frame.to_csv(input_csv, index=False)

    result = categorize_amazon_csv(
        input_csv_path=input_csv,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "out",
        ask_for_category=None,
    )

    assert result.uncategorized_count == 1
    annotated = pd.read_csv(result.annotated_csv_path)
    assert annotated["category"].tolist() == ["UNCATEGORIZED"]


def test_categorize_amazon_csv_supports_amazon_business_headers(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon_business.csv"
    frame = pd.DataFrame(
        {
            "Order Date": ["2026-05-01", "2026-05-01"],
            "Order ID": ["123-1234567-1234567", "123-1234567-1234567"],
            "Title": ["Coffee Pods 36ct", "Paper Towels 12-Pack"],
            "Item Net Total": [28.00, 18.49],
            "Total Amount": [46.49, 46.49],
        }
    )
    frame.to_csv(input_csv, index=False)

    assigned = {
        "coffee pods 36ct": "Groceries",
        "paper towels 12-pack": "Household",
    }

    def ask_for_category(item: str, _known: list[str]) -> str:
        return assigned[item]

    result = categorize_amazon_csv(
        input_csv_path=input_csv,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "out",
        ask_for_category=ask_for_category,
    )

    summary = pd.read_csv(result.summary_csv_path)
    totals = dict(zip(summary["category"], summary["amount_total"], strict=False))

    assert totals == {"Groceries": 28.0, "Household": 18.49}


def test_categorize_amazon_csv_writes_cutoff_monthly_outputs(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon_business_range.csv"
    pd.DataFrame(
        {
            "Order Date": ["2026-03-03", "2026-03-04", "2026-04-02"],
            "Title": ["Coffee Pods", "Paper Towels", "USB Cable"],
            "Item Net Total": [10.0, 20.0, 30.0],
        }
    ).to_csv(input_csv, index=False)

    result = categorize_amazon_csv(
        input_csv_path=input_csv,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "out",
        ask_for_category=None,
    )

    months = [monthly.month for monthly in result.monthly_outputs]
    assert months == ["2026-03", "2026-04"]

    march_summary = pd.read_csv(tmp_path / "out" / "Amazon-2026-03_summary.csv")
    april_summary = pd.read_csv(tmp_path / "out" / "Amazon-2026-04_summary.csv")

    assert float(march_summary.loc[0, "amount_total"]) == 10.0
    assert float(april_summary.loc[0, "amount_total"]) == 50.0


def test_categorize_amazon_csv_marks_open_billing_month_as_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from analysis.amazon import categorizer as categorizer_module
    monkeypatch.setattr(
        categorizer_module,
        "_today_date",
        lambda: date(2026, 5, 15),
    )

    input_csv = tmp_path / "amazon_current.csv"
    pd.DataFrame(
        {
            "Order Date": ["2026-05-05"],
            "Title": ["Coffee Pods"],
            "Item Net Total": [12.0],
        }
    ).to_csv(input_csv, index=False)

    result = categorize_amazon_csv(
        input_csv_path=input_csv,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "out",
        ask_for_category=None,
    )

    assert len(result.monthly_outputs) == 1
    monthly = result.monthly_outputs[0]
    assert monthly.month == "2026-06"
    assert monthly.is_pending is True
    assert monthly.annotated_csv_path.name == "Amazon-2026-06_pending_annotated.csv"
    assert monthly.summary_csv_path.name == "Amazon-2026-06_pending_summary.csv"


def test_categorize_amazon_csv_does_not_write_pending_when_open_interval_is_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from analysis.amazon import categorizer as categorizer_module

    monkeypatch.setattr(categorizer_module, "_today_date", lambda: date(2026, 5, 15))

    input_csv = tmp_path / "amazon_closed_only.csv"
    pd.DataFrame(
        {
            "Order Date": ["2026-05-02"],
            "Title": ["Coffee Pods"],
            "Item Net Total": [12.0],
        }
    ).to_csv(input_csv, index=False)

    result = categorize_amazon_csv(
        input_csv_path=input_csv,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "out",
        ask_for_category=None,
    )

    assert len(result.monthly_outputs) == 1
    monthly = result.monthly_outputs[0]
    assert monthly.month == "2026-05"
    assert monthly.is_pending is False
    assert monthly.annotated_csv_path.name == "Amazon-2026-05_annotated.csv"
    assert monthly.summary_csv_path.name == "Amazon-2026-05_summary.csv"
    assert not list((tmp_path / "out").glob("Amazon-*_pending_annotated.csv"))
    assert not list((tmp_path / "out").glob("Amazon-*_pending_summary.csv"))


def test_categorize_amazon_csv_persists_progress_before_interruption(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon_interrupt.csv"
    frame = pd.DataFrame(
        {
            "Description": ["Coffee Pods", "Paper Towels"],
            "Amount": [14.5, 10.0],
        }
    )
    frame.to_csv(input_csv, index=False)

    def ask_for_category(item: str, _known: list[str]) -> str:
        if item == "coffee pods":
            return "Groceries"
        raise KeyboardInterrupt()

    try:
        categorize_amazon_csv(
            input_csv_path=input_csv,
            state_dir=tmp_path / "state",
            output_dir=tmp_path / "out",
            ask_for_category=ask_for_category,
        )
    except KeyboardInterrupt:
        pass

    store = JsonStateStore(tmp_path / "state")
    mapping = store.load_mapping()
    categories = store.load_categories()
    store.close()

    assert mapping == {"coffee pods": "Groceries"}
    assert categories == ["Groceries"]


def test_categorize_amazon_csv_persists_items_and_monthly_summary(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon_business.csv"
    pd.DataFrame(
        {
            "Order Date": ["2026-03-02", "2026-03-15", "2026-04-01"],
            "Title": ["Coffee Pods", "Paper Towels", "Coffee Pods"],
            "Item Net Total": [12.0, 8.0, 5.0],
        }
    ).to_csv(input_csv, index=False)

    categorize_amazon_csv(
        input_csv_path=input_csv,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "out",
        ask_for_category=None,
    )

    conn = sqlite3.connect(tmp_path / "state" / "state.sqlite3")
    item_rows = conn.execute("SELECT COUNT(*) FROM items").fetchone()
    summary_rows = conn.execute(
        """
        SELECT summary_month_start, category, amount_total
        FROM monthly_summaries
        ORDER BY summary_month_start
        """
    ).fetchall()
    conn.close()

    assert item_rows is not None
    assert item_rows[0] == 3
    assert summary_rows == [
        ("2026-03-01", "UNCATEGORIZED", 12.0),
        ("2026-04-01", "UNCATEGORIZED", 13.0),
    ]
