import csv
from pathlib import Path

import pandas as pd
import pytest

from analysis.expenses.baseline import (
    BASELINE_GENERATED_TAG,
    SCHEDULED_HEADER,
    build_expense_baseline,
    build_scheduled_baseline_rows,
    load_category_map,
    load_category_splits,
    load_excluded_payees,
    load_expense_account_map,
    load_quicken_transactions,
    replace_generated_scheduled_rows,
)


def _write_export(path: Path) -> None:
    path.write_text(
        '"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount\n'
        '"Checking",,,,,,,,,,,\n'
        ',,"2026-01-02","2026-01-03","2026-01-02","","Market","Groceries","","","","-12.34"\n'
        ',,"2026-01-03","2026-01-04","2026-01-03","","Amazon","Shopping","","","","-20.00"\n'
        ',,"2026-01-04","2026-01-05","2026-01-04","","Mortgage",'
        '"Mortgage Interest","","","","-800.00"\n'
        ',,"2026-01-05","2026-01-06","2026-01-05","","Acme Insurance",'
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

    result = build_expense_baseline(
        path, amazon, excluded_payee_patterns=[r"acme\s+insurance"],
    )

    assert result.transactions["payee"].tolist() == ["Market"]
    assert set(result.excluded["exclude_reason"]) == {
        "modeled_separately", "transfer", "non_expense",
    }
    assert result.category_totals.to_dict("records") == [
        {"category": "Household", "amount_total": 20.0},
        {"category": "Groceries", "amount_total": 12.34},
    ]


def test_build_expense_baseline_keeps_household_payees_unless_a_pattern_excludes_them(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quicken.csv"
    _write_export(path)
    no_amazon = pd.DataFrame(columns=["summary_month_start", "category", "amount_total"])

    # Amazon and mortgage payees are always excluded; the insurer is not.
    result = build_expense_baseline(path, no_amazon)

    assert result.transactions["payee"].tolist() == ["Market", "Acme Insurance"]


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


def test_load_expense_account_map_reads_category_accounts(tmp_path: Path) -> None:
    path = tmp_path / "expense_account_map.csv"
    path.write_text(
        "category,target_account\n"
        "Food,food\n",
        encoding="utf-8",
    )

    assert load_expense_account_map(path) == {"Food": "food"}


def test_build_expense_baseline_canonicalizes_and_combines_amazon_categories(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quicken.csv"
    path.write_text(
        '"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount\n'
        '"Checking",,,,,,,,,,,\n'
        ',,"2026-01-02","2026-01-03","2026-01-02","","Cafe","Coffee Shops","","","","-5.00"\n',
        encoding="utf-8",
    )
    amazon = pd.DataFrame(
        {
            "summary_month_start": ["2026-01-01", "2026-01-01"],
            "category": ["Coffee Shops", "Uncategorized"],
            "amount_total": [7.00, 9.00],
        }
    )

    result = build_expense_baseline(
        path,
        amazon,
        category_merges={"Coffee Shops": "Dining"},
        excluded_categories={"Uncategorized"},
    )

    assert result.monthly.to_dict("records") == [
        {
            "summary_month_start": pd.Timestamp("2026-01-01"),
            "category": "Dining",
            "amount": 7.0,
            "source": "Amazon summary",
        },
        {
            "summary_month_start": pd.Timestamp("2026-01-01"),
            "category": "Dining",
            "amount": 5.0,
            "source": "Quicken",
        },
    ]
    assert result.category_totals.to_dict("records") == [
        {"category": "Dining", "amount_total": 12.0},
    ]


def _write_two_account_export(path: Path) -> None:
    path.write_text(
        '"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount\n'
        '"Alice Checking",,,,,,,,,,,\n'
        ',,"2026-01-02","2026-01-03","2026-01-02","","Market","Groceries","","","","-12.34"\n'
        '"Bill Checking",,,,,,,,,,,\n'
        ',,"2026-01-03","2026-01-04","2026-01-03","","Cafe","Fast Food","","","","-5.00"\n',
        encoding="utf-8",
    )


def test_build_expense_baseline_splits_a_category_by_quicken_account(tmp_path: Path) -> None:
    path = tmp_path / "quicken.csv"
    _write_two_account_export(path)
    amazon = pd.DataFrame(
        {
            "summary_month_start": ["2026-01-01"],
            "category": ["Groceries"],
            "amount_total": [20.00],
        }
    )

    result = build_expense_baseline(
        path,
        amazon,
        category_merges={"Groceries": "Food", "Fast Food": "Food"},
        category_splits={"Food": {"Alice Checking": "Alice Food", "": "Bill Food"}},
    )

    # The default takes the other account's Food and Amazon's, which has no account.
    assert result.category_totals.to_dict("records") == [
        {"category": "Bill Food", "amount_total": 25.0},
        {"category": "Alice Food", "amount_total": 12.34},
    ]


def test_build_expense_baseline_split_without_a_default_leaves_the_rest_alone(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quicken.csv"
    _write_two_account_export(path)

    result = build_expense_baseline(
        path,
        pd.DataFrame(columns=["summary_month_start", "category", "amount_total"]),
        category_merges={"Groceries": "Food", "Fast Food": "Food"},
        # "Alice Food" is itself a split category, but a target is never re-split.
        category_splits={
            "Food": {"Alice Checking": "Alice Food"},
            "Alice Food": {"": "Never"},
        },
    )

    assert result.category_totals.to_dict("records") == [
        {"category": "Alice Food", "amount_total": 12.34},
        {"category": "Food", "amount_total": 5.0},
    ]


def test_build_expense_baseline_split_categories_are_not_defaulted_to_other(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quicken.csv"
    path.write_text(
        '"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount\n'
        '"Alice Checking",,,,,,,,,,,\n'
        # Categorized as Food directly in Quicken, so no merge row names it.
        ',,"2026-01-02","2026-01-03","2026-01-02","","Market","Food","","","","-12.34"\n'
        ',,"2026-01-03","2026-01-04","2026-01-03","","Store","Mystery","","","","-7.00"\n',
        encoding="utf-8",
    )
    amazon = pd.DataFrame(
        {
            "summary_month_start": ["2026-01-01"],
            "category": ["Groceries"],
            "amount_total": [20.00],
        }
    )

    result = build_expense_baseline(
        path,
        amazon,
        category_merges={"Groceries": "Food"},
        category_splits={"Food": {"Alice Checking": "Alice Food", "": "Bill Food"}},
        default_category="Other",
    )

    assert result.categories_defaulted_to_other == ("Mystery",)
    assert result.category_totals.to_dict("records") == [
        {"category": "Bill Food", "amount_total": 20.0},
        {"category": "Alice Food", "amount_total": 12.34},
        {"category": "Other", "amount_total": 7.0},
    ]


def test_build_expense_baseline_applies_each_payee_pattern_independently(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quicken.csv"
    _write_export(path)
    no_amazon = pd.DataFrame(columns=["summary_month_start", "category", "amount_total"])

    # An inline flag and a group are both valid alone; neither may disturb the other.
    result = build_expense_baseline(
        path, no_amazon, excluded_payee_patterns=["(?i)nothing here", r"(acme)\s+insurance"],
    )

    assert result.transactions["payee"].tolist() == ["Market"]


def test_load_category_splits_reads_accounts_and_a_blank_default(tmp_path: Path) -> None:
    path = tmp_path / "expense_category_split.csv"
    path.write_text(
        "category,account,target_category\n"
        "Food,Alice Checking,Alice Food\n"
        "Food,,Bill Food\n",
        encoding="utf-8",
    )

    assert load_category_splits(path) == {
        "Food": {"Alice Checking": "Alice Food", "": "Bill Food"},
    }


@pytest.mark.parametrize(("rows", "message"), [
    ("Food,Alice Checking,Alice Food\nFood,Alice Checking,Other\n", "duplicate split"),
    ("Food,Alice Checking,\n", "target category is required"),
])
def test_load_category_splits_rejects_bad_rows(tmp_path: Path, rows: str, message: str) -> None:
    path = tmp_path / "expense_category_split.csv"
    path.write_text("category,account,target_category\n" + rows, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_category_splits(path)


def test_load_excluded_payees_reads_patterns_and_rejects_bad_ones(tmp_path: Path) -> None:
    path = tmp_path / "expense_excluded_payees.csv"
    path.write_text("payee_pattern\nacme\\s+insurance\n\ngarden club\n", encoding="utf-8")

    assert load_excluded_payees(path) == (r"acme\s+insurance", "garden club")

    path.write_text("payee_pattern\nacme (insurance\n", encoding="utf-8")
    with pytest.raises(ValueError, match="row 2: invalid payee pattern"):
        load_excluded_payees(path)


def test_build_expense_baseline_defaults_unmapped_categories_to_other(tmp_path: Path) -> None:
    path = tmp_path / "quicken.csv"
    path.write_text(
        '"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount\n'
        '"Checking",,,,,,,,,,,\n'
        ',,"2026-01-02","2026-01-03","2026-01-02","","Cafe","Coffee Shops","","","","-5.00"\n'
        ',,"2026-01-03","2026-01-04","2026-01-03","","Store","Mystery","","","","-7.00"\n',
        encoding="utf-8",
    )

    result = build_expense_baseline(
        path,
        pd.DataFrame(columns=["summary_month_start", "category", "amount_total"]),
        category_merges={"Coffee Shops": "Dining"},
        default_category="Other",
    )

    assert result.categories_defaulted_to_other == ("Mystery",)
    assert set(result.transactions["category"]) == {"Dining", "Other"}


def test_build_expense_baseline_does_not_default_merge_targets_to_other(tmp_path: Path) -> None:
    path = tmp_path / "quicken.csv"
    path.write_text(
        '"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount\n'
        '"Checking",,,,,,,,,,,\n'
        # Already carries the merge target's name, so no merge row names it as a source.
        ',,"2026-01-02","2026-01-03","2026-01-02","","Market","Food","","","","-12.00"\n',
        encoding="utf-8",
    )
    amazon = pd.DataFrame(
        {
            "summary_month_start": ["2026-01-01", "2026-01-01"],
            "category": ["Groceries", "Gadgets"],
            "amount_total": [20.00, 9.00],
        }
    )

    result = build_expense_baseline(
        path, amazon, category_merges={"Groceries": "Food"}, default_category="Other",
    )

    assert result.categories_defaulted_to_other == ("Gadgets",)
    assert result.category_totals.to_dict("records") == [
        {"category": "Food", "amount_total": 32.0},
        {"category": "Other", "amount_total": 9.0},
    ]


def test_scheduled_baseline_rows_are_rerunnable_and_remove_stale_rows(tmp_path: Path) -> None:
    path = tmp_path / "quicken.csv"
    path.write_text(
        '"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount\n'
        '"Checking",,,,,,,,,,,\n'
        ',,"2026-01-02","2026-01-03","2026-01-02","","Market","Food","","","","-12.00"\n',
        encoding="utf-8",
    )
    baseline = build_expense_baseline(
        path,
        pd.DataFrame(
            {
                "summary_month_start": ["2026-01-01"],
                "category": ["Food"],
                "amount_total": [24.00],
            }
        ),
    )
    rows = build_scheduled_baseline_rows(
        baseline,
        {"Food": "food"},
        "2026-05-31",
        "Asset_Checking",
    )
    assert rows[0]["amount"] == "36.00"
    assert rows[0]["tags"] == BASELINE_GENERATED_TAG
    assert '"generated_by":"expense_baseline"' in rows[0]["meta_json"]

    scheduled = tmp_path / "scheduled.csv"
    keep = dict.fromkeys(SCHEDULED_HEADER, "")
    keep.update(name="keep_me", tags="manual:keep")
    stale = dict(rows[0])
    stale["amount"] = "99.00"
    replace_generated_scheduled_rows(scheduled, [stale])
    replace_generated_scheduled_rows(scheduled, rows)

    with scheduled.open(newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
    assert [row["name"] for row in written] == ["baseline_expense_food"]
    assert written[0]["amount"] == "36.00"