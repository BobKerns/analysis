import csv
from pathlib import Path

import pandas as pd
import pytest

from analysis.expenses.baseline import (
    BaselineResult,
    occasional_payments,
    BASELINE_GENERATED_TAG,
    SCHEDULED_HEADER,
    build_expense_baseline,
    build_scheduled_baseline_rows,
    replace_generated_scheduled_rows,
)
from analysis.sim.imports.mapping import (
    load_category_map,
    load_category_splits,
    load_excluded_payees,
    load_expense_accounts,
)
from analysis.sim.imports.quicken import read_transactions


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


def test_read_transactions_preserves_rows_and_cents(tmp_path: Path) -> None:
    path = tmp_path / "quicken.csv"
    _write_export(path)

    transactions = read_transactions(path).transactions

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
        read_transactions(path), amazon, excluded_payee_patterns=[r"acme\s+insurance"],
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
    result = build_expense_baseline(read_transactions(path), no_amazon)

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
        read_transactions(path),
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
    path = tmp_path / "categories.csv"
    path.write_text(
        "outside_category,category\n"
        "Coffee Shops,Dining\n"
        "Uncategorized,\n"
        "Federal Tax,-\n",
        encoding="utf-8",
    )

    merges, exclusions = load_category_map(path)

    assert merges == {"Coffee Shops": "Dining"}
    assert exclusions == {"Uncategorized", "Federal Tax"}


def test_load_expense_accounts_reads_category_accounts_and_no_rule(tmp_path: Path) -> None:
    path = tmp_path / "expense_accounts.csv"
    path.write_text(
        "category,account\n"
        "Food,food\n"
        "Gifts,-\n",
        encoding="utf-8",
    )

    assert load_expense_accounts(path).accounts == {"Food": "food", "Gifts": "-"}
    assert load_expense_accounts(tmp_path / "none.csv").accounts == {}


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
        read_transactions(path),
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
        read_transactions(path),
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
        read_transactions(path),
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
        read_transactions(path),
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
        read_transactions(path), no_amazon, excluded_payee_patterns=["(?i)nothing here", r"(acme)\s+insurance"],
    )

    assert result.transactions["payee"].tolist() == ["Market"]


def test_load_category_splits_reads_accounts_and_a_blank_default(tmp_path: Path) -> None:
    path = tmp_path / "splits.csv"
    path.write_text(
        "category,outside_account,target_category\n"
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
    path = tmp_path / "splits.csv"
    path.write_text("category,outside_account,target_category\n" + rows, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_category_splits(path)


def test_load_excluded_payees_reads_patterns_and_rejects_bad_ones(tmp_path: Path) -> None:
    path = tmp_path / "excluded_payees.csv"
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
        read_transactions(path),
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
        read_transactions(path), amazon, category_merges={"Groceries": "Food"}, default_category="Other",
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
        read_transactions(path),
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
        provenance={"source": "quicken"},
    )
    assert rows[0]["amount"] == "36.00"
    assert '"source":"quicken"' in rows[0]["meta_json"]
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

def _paid(*payments: tuple[str, str, float]) -> BaselineResult:
    """A baseline of ``payments`` (date, payee, dollars), all of one category."""
    used = pd.DataFrame(payments, columns=["date", "payee", "amount"])
    used["date"] = pd.to_datetime(used["date"])
    used["summary_month_start"] = used["date"].dt.to_period("M").dt.to_timestamp()
    used["amount_cents"] = (-used["amount"] * 100).round().astype(int)
    used["category"] = "Insurance"
    empty = pd.DataFrame()
    return BaselineResult(used, empty, empty, empty, ())


def _monthly(payee: str, start: str, months: int, amount: float = 80.0) -> list[tuple[str, str, float]]:
    return [(f"{each:%Y-%m-%d}", payee, amount) for each in pd.date_range(start, periods=months, freq="MS")]


def _found(*payments: tuple[str, str, float]) -> list[tuple[str, int, bool]]:
    found = occasional_payments(_paid(*payments))
    assert found is not None
    return [(each.payee, len(each.paid), each.repeats) for each in found]


def test_occasional_payments_are_a_month_or_two_of_each_year_and_say_whether_they_came_round() -> None:
    years = (2024, 2025, 2026)
    yearly = [(f"{year}-03-01", "Mutual Insurance", 900.0) for year in years]
    twice = [(f"{year}-{month}-01", "County Tax", 2000.0) for year in years for month in ("04", "12")]
    small = [(f"{year}-06-01", "Web Host", 120.0) for year in years]

    # Three years of a yearly bill are three months paid; 360 in three years is under 600 a year.
    assert _found(*_monthly("Gym", "2024-01-01", 36), *yearly, *twice, *small) == [
        ("County Tax", 6, True), ("Mutual Insurance", 3, True)]


def test_occasional_payments_leave_out_a_bill_that_began_and_mark_a_one_time_expense() -> None:
    year = _monthly("Gym", "2026-01-01", 13)
    # A car payment that began in November is monthly; furniture bought once may never come again.
    assert _found(*year, *_monthly("Auto Loan", "2026-11-01", 2, 500.0),
                  ("2026-05-10", "Furniture Store", 2000.0)) == [("Furniture Store", 1, False)]
    # A summer's program is a season, and a quarterly bill is neither monthly nor yearly.
    two_years = _monthly("Gym", "2025-01-01", 24)
    assert _found(*two_years, *_monthly("Summer Camp", "2025-06-01", 4, 400.0),
                  *[(f"2025-{month:02d}-01", "Water", 300.0) for month in (1, 4, 7, 10)]) == []


def test_occasional_payments_need_most_of_a_year_first_day_to_last() -> None:
    # Twelve calendar months, and ten of data: a yearly bill can't be told from a new monthly one.
    payments = [("2025-01-31", "Gym", 80.0), ("2025-12-01", "Gym", 80.0), ("2025-06-01", "County Tax", 2000.0)]
    assert occasional_payments(_paid(*payments)) is None
    assert _found(*payments, ("2026-01-30", "Gym", 80.0)) == [("County Tax", 1, False)]


def test_occasional_payments_say_where_a_payee_came_from_and_whether_it_is_paid_elsewhere() -> None:
    baseline = _paid(*_monthly("Corner Store 114", "2025-01-01", 13),
                     ("2025-03-01", "Corner Store 220", 900.0), ("2025-09-01", "Mutual Insurance", 900.0))
    used = baseline.transactions
    # The store is paid monthly for food, and once for something large in another category.
    used.loc[used["payee"].eq("Corner Store 114"), "category"] = "Food"
    used["origin"] = ["bank"] * 13 + ["card", "card"]

    found = occasional_payments(baseline)

    assert found is not None
    assert [(each.payee, each.category, each.sources, each.elsewhere) for each in found] == [
        ("Corner Store 220", "Insurance", ("card",), True), ("Mutual Insurance", "Insurance", ("card",), False)]
    # A frame read without `sim import` has no source to name.
    plain = occasional_payments(_paid(*_monthly("Gym", "2025-01-01", 13), ("2025-09-01", "Mutual Insurance", 900.0)))
    assert plain is not None and [each.sources for each in plain] == [()]
