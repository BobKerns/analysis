from pathlib import Path

import pandas as pd

from analysis.amazon.categorizer import categorize_amazon_csv


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

    mapping_path = state_dir / "item_category_map.json"
    assert mapping_path.exists()


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
