from pathlib import Path

import pandas as pd

from analysis.expenses.baseline import (
    build_expense_baseline,
    load_category_map,
    load_quicken_transactions,
)


def _write_export(path: Path) -> None:
    path.write_text(
        '"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount\n'
        '"Checking",,,,,,,,,,,\n'
        ',,"2026-01-02","2026-01-03","2026-01-02","","Market","Groceries","","","","-12.34"\n'
        ',,"2026-01-03","2026-01-04","2026-01-03","","Amazon","Shopping","","","","-20.00"\n'
        ',,"2026-01-04","2026-01-05","2026-01-04","","Mortgage",'
        '"Mortgage Interest","","","","-800.00"\n'
        ',,"2026-01-05","2026-01-06","2026-01-05","","State Farm",'
        '"Auto Insurance","","","","-100.00"\n'
        ',,"2026-01-06","2026-01-07","2026-01-06","","Transfer","Uncategorized","","checking","","-50.00"\n'
        ',,"2026-01-07","2026-01-08","2026-01-07","","Paycheck","Income","","","","100.00"\n'
        '"Total Checking",,,,,,,,,,,"-882.34"\n',
        encoding="utf-8",
    )


def test_load_quicken_transactions_preserves_rows_and_cents(tmp_path: Path) -> None:
    path = tmp_path / "quicken.csv"
    _write_export(path)

    transactions = load_quicken_transactions(path)

    assert transactions["account"].tolist() == ["Checking"] * 6
    assert transactions["amount_cents"].tolist() == [-1234, -2000, -80000, -10000, -5000, 10000]


def test_build_expense_baseline_excludes_modeled_rows_and_adds_amazon_summary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quicken.csv"
    _write_export(path)
    amazon = pd.DataFrame(
        {
            "summary_month_start": ["2026-01-01"],
            "category": ["Household"],
            "amount_total": [20.00],
        }
    )

    result = build_expense_baseline(path, amazon)

    assert result.transactions["payee"].tolist() == ["Market"]
    assert set(result.excluded["exclude_reason"]) == {
        "modeled_separately", "transfer", "non_expense",
    }
    assert result.category_totals.to_dict("records") == [
        {"category": "Household", "amount_total": 20.0},
        {"category": "Groceries", "amount_total": 12.34},
    ]


def test_build_expense_baseline_merges_and_excludes_quicken_categories(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quicken.csv"
    path.write_text(
        '"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount\n'
        '"Checking",,,,,,,,,,,\n'
        ',,"2026-01-02","2026-01-03","2026-01-02","","Market","Groceries","","","","-12.34"\n'
        ',,"2026-01-03","2026-01-04","2026-01-03","","Cafe","Coffee Shops","","","","-5.00"\n'
        ',,"2026-01-04","2026-01-05","2026-01-04","","Gym","Gym","","","","-25.00"\n',
        encoding="utf-8",
    )

    result = build_expense_baseline(
        path,
        pd.DataFrame(columns=["summary_month_start", "category", "amount_total"]),
        category_merges={"Coffee Shops": "Dining"},
        excluded_categories={"Gym"},
    )

    assert result.transactions["category"].tolist() == ["Groceries", "Dining"]
    assert result.transactions["category_raw"].tolist() == ["Groceries", "Coffee Shops"]
    excluded_reason = result.excluded.loc[
        result.excluded["payee"].eq("Gym"), "exclude_reason"
    ].iloc[0]
    assert excluded_reason == "category_excluded"
    assert result.category_totals.to_dict("records") == [
        {"category": "Groceries", "amount_total": 12.34},
        {"category": "Dining", "amount_total": 5.0},
    ]


def test_load_category_map_reads_merges_and_blank_or_dash_exclusions(tmp_path: Path) -> None:
    path = tmp_path / "expense_category_map.csv"
    path.write_text(
        "quicken_category,target_category\n"
        "Coffee Shops,Dining\n"
        "Uncategorized,\n"
        "Federal Tax,-\n",
        encoding="utf-8",
    )

    merges, exclusions = load_category_map(path)

    assert merges == {"Coffee Shops": "Dining"}
    assert exclusions == {"Uncategorized", "Federal Tax"}