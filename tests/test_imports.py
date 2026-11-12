"""The import framework (issue #527): sources, common forms, mapping, appliers."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from analysis.sim.imports import flows, holdings, mapping, quicken, sources
from analysis.sim.imports.forms import Flows, Holding, Snapshot

_NET_WORTH = """\
Net Worth Export
Month to Date: 2026-08-01 - 2026-08-18
41 Accounts


"",,"2026-08-18"
-------------------------,-------------------------,-------------------------
"Assets",,
,"Checking",
," - Business","304.17"
," - Bank CA","20.11"
," - Total Checking","324.28"
,"Investments",
," - Retirement",
," -  - Bill Brokerage","10,000.38"
," -  - Total Retirement","10,000.38"
," - Total Investments","10,000.38"
,"Total Assets","10,324.66"
"Liabilities",,
,"Credit Card",
," - Card","-8,378.91"
," - Total Credit Card","-8,378.91"
,"Total Liabilities","-8,378.91"
-------------------------,-------------------------,-------------------------
Net Worth,,"1,945.75"
"""

_TRANSACTIONS = (
    '"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount\n'
    '"Checking",,,,,,,,,,,\n'
    ',,"2026-01-02","2026-01-03","2026-01-02","","Market","Groceries","","","","-12.34"\n'
    '"Total Checking",,,,,,,,,,,"-12.34"\n'
)


@pytest.fixture
def net_worth_file(tmp_path: Path) -> Path:
    path = tmp_path / "net_worth.csv"
    path.write_text(_NET_WORTH, encoding="utf-8")
    return path


@pytest.fixture
def transactions_file(tmp_path: Path) -> Path:
    path = tmp_path / "transactions.csv"
    path.write_text(_TRANSACTIONS, encoding="utf-8")
    return path


# ---------- the Quicken source ----------

def test_read_account_tree_extracts_leaf_accounts_and_the_report_date(net_worth_file: Path) -> None:
    snapshot = quicken.read_account_tree(net_worth_file)

    assert snapshot.source == "quicken"
    assert snapshot.as_of == dt.date(2026, 8, 18)
    assert {h.account: h.value_cents for h in snapshot.holdings} == {
        "Business": 30417,
        "Bank CA": 2011,
        "Bill Brokerage": 1000038,
        "Card": -837891,
    }


def test_read_transactions_gives_the_common_columns(transactions_file: Path) -> None:
    found = quicken.read_transactions(transactions_file)

    assert isinstance(found, Flows)
    assert found.transactions[["account", "payee", "category", "amount_cents"]].to_dict("records") == [
        {"account": "Checking", "payee": "Market", "category": "Groceries", "amount_cents": -1234},
    ]


def test_flows_refuse_a_frame_without_the_common_columns(transactions_file: Path) -> None:
    frame = quicken.read_transactions(transactions_file).transactions.drop(columns=["payee"])
    with pytest.raises(ValueError, match="payee"):
        Flows("quicken", frame)


# ---------- sources ----------

def test_detect_tells_the_two_quicken_layouts_apart(
    net_worth_file: Path, transactions_file: Path, tmp_path: Path,
) -> None:
    other = tmp_path / "other.csv"
    other.write_text("a,b\n1,2\n", encoding="utf-8")

    assert [(s.name, form) for s, form in sources.detect(net_worth_file)] == [("quicken", "holdings")]
    assert [(s.name, form) for s, form in sources.detect(transactions_file)] == [
        ("quicken", "transactions")]
    assert sources.detect(other) == []


def test_read_holdings_finds_the_source_and_refuses_the_wrong_layout(
    net_worth_file: Path, transactions_file: Path,
) -> None:
    assert sources.read_holdings([net_worth_file]).source == "quicken"
    assert len(sources.read_flows([transactions_file], "quicken").transactions) == 1
    with pytest.raises(sources.SourceError, match="looks like quicken transactions"):
        sources.read_holdings([transactions_file])
    with pytest.raises(sources.SourceError, match="unknown source 'nowhere'"):
        sources.read_holdings([net_worth_file], "nowhere")


def test_read_holdings_refuses_files_of_different_dates(net_worth_file: Path, tmp_path: Path) -> None:
    later = tmp_path / "later.csv"
    later.write_text(_NET_WORTH.replace('"",,"2026-08-18"', '"",,"2026-09-18"'), encoding="utf-8")

    with pytest.raises(sources.SourceError, match="different dates"):
        sources.read_holdings([net_worth_file, later])
    assert len(sources.read_holdings([net_worth_file, net_worth_file]).holdings) == 8


# ---------- mapping ----------

def test_account_map_round_trips_and_falls_back_to_the_whole_account(tmp_path: Path) -> None:
    path = tmp_path / "imports" / "quicken" / "accounts.csv"
    found = mapping.load_account_map(path)
    assert found.entries == {}

    found.set("Business", "", mapping.AccountTarget("map", "Asset_Checking"))
    found.set("Bill Brokerage", "VTI", mapping.AccountTarget("map", "Asset_Brokerage_Joint", "vti"))
    found.set("Home Repair Reserve", "", mapping.AccountTarget("skip", note="not tracked"))
    found.save()

    assert path.read_text(encoding="utf-8").splitlines()[0] == \
        "outside_account,asset,action,account,lot,note"
    again = mapping.load_account_map(path)
    assert again.entries == found.entries
    assert again.lookup("Business", "CASH") == mapping.AccountTarget("map", "Asset_Checking")
    assert again.lookup("Bill Brokerage", "VTI") == \
        mapping.AccountTarget("map", "Asset_Brokerage_Joint", "vti")
    assert again.lookup("Bill Brokerage") is None
    assert again.lookup("Home Repair Reserve") == mapping.AccountTarget("skip", note="not tracked")


@pytest.mark.parametrize(("text", "message"), [
    ("wrong,header\n", "expected header"),
    ("outside_account,asset,action,account,lot,note\nA,,move,X,,\n", "action must be"),
    ("outside_account,asset,action,account,lot,note\nA,,map,,,\n", "needs an account"),
    ("outside_account,asset,action,account,lot,note\nA,,map,X,,\nA,,skip,,,\n", "duplicate"),
])
def test_load_account_map_rejects_bad_files(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "accounts.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        mapping.load_account_map(path)


def test_rename_account_follows_in_every_saved_mapping(tmp_path: Path) -> None:
    accounts = mapping.load_account_map(mapping.source_dir(tmp_path, "quicken") / mapping.ACCOUNTS)
    accounts.set("Business", "", mapping.AccountTarget("map", "Asset_Checking"))
    accounts.set("Card", "", mapping.AccountTarget("map", "Liability_Card"))
    accounts.save()
    expenses = mapping.load_expense_accounts(mapping.expense_accounts_path(tmp_path))
    expenses.accounts.update({"Food": "Expense_Living", "Gifts": mapping.NO_RULE})
    expenses.save()

    assert mapping.rename_account(tmp_path, "Asset_Checking", "Asset_Bank") == [
        "imports/quicken/accounts.csv"]
    assert mapping.rename_account(tmp_path, "Expense_Living", "Expense_Home") == [
        "imports/expense_accounts.csv"]
    assert mapping.rename_account(tmp_path, "Nothing", "Else") == []

    assert mapping.load_account_map(accounts.path).lookup("Business") == \
        mapping.AccountTarget("map", "Asset_Bank")
    assert mapping.load_expense_accounts(expenses.path).accounts == {
        "Food": "Expense_Home", "Gifts": "-"}


# ---------- the holdings applier ----------

_ACCOUNTS = {
    "Asset_Checking": ("asset", "cash"),
    "Asset_Brokerage_Joint": ("asset", "brokerage_taxable"),
    "Liability_Card": ("liability", "credit_card"),
}


def _lots() -> list[dict[str, str]]:
    def lot(account: str, name: str, opening: str, prior: str = "") -> dict[str, str]:
        return {"account_name": account, "lot": name, "opening_balance": opening,
                "prior_year_end_balance": prior}
    return [
        lot("Asset_Checking", "", "5.00"),
        lot("Asset_Brokerage_Joint", "vti", "100.00"),
        lot("Asset_Brokerage_Joint", "bnd", "50.00", "40.00"),
        lot("Liability_Card", "", "0.00"),
    ]


def _map(tmp_path: Path, **targets: mapping.AccountTarget) -> mapping.AccountMap:
    found = mapping.AccountMap(tmp_path / "accounts.csv")
    for outside, target in targets.items():
        found.set(outside, "", target)
    return found


def _snapshot(**cents: int) -> Snapshot:
    return Snapshot("quicken", tuple(Holding(name, value) for name, value in cents.items()))


def test_plan_sets_balances_sums_shared_lots_and_negates_debts(tmp_path: Path) -> None:
    account_map = _map(
        tmp_path,
        Bank=mapping.AccountTarget("map", "Asset_Checking"),
        Savings=mapping.AccountTarget("map", "Asset_Checking"),
        Funds=mapping.AccountTarget("map", "Asset_Brokerage_Joint", "vti"),
        Card=mapping.AccountTarget("map", "Liability_Card"),
        Old=mapping.AccountTarget("skip"),
    )
    result = holdings.plan(
        _snapshot(Bank=100, Savings=250, Funds=10000, Card=-4200, Old=1), account_map,
        _ACCOUNTS, _lots())

    assert result.complete
    assert [(c.account, c.lot, c.column, c.before, c.after, c.note) for c in result.changes] == [
        ("Asset_Checking", "", "opening_balance", "5.00", "3.50", ""),
        ("Asset_Brokerage_Joint", "vti", "prior_year_end_balance", "", "100.00", "seeded from opening"),
        ("Liability_Card", "", "opening_balance", "0.00", "42.00", ""),
    ]
    assert (result.lots_changed, result.lots_unchanged, result.skipped) == (2, 1, ["Old"])
    assert result.rows[2] == _lots()[2]


def test_plan_year_end_overwrites_investment_accounts_only(tmp_path: Path) -> None:
    account_map = _map(
        tmp_path,
        Bank=mapping.AccountTarget("map", "Asset_Checking"),
        Bonds=mapping.AccountTarget("map", "Asset_Brokerage_Joint", "bnd"),
    )
    result = holdings.plan(_snapshot(Bank=100, Bonds=6000), account_map, _ACCOUNTS, _lots(),
                           year_end=True)

    assert result.ineligible == ["Asset_Checking"]
    assert [(c.lot, c.column, c.before, c.after) for c in result.changes] == [
        ("bnd", "prior_year_end_balance", "40.00", "60.00")]
    assert result.rows[2]["opening_balance"] == "50.00"


def test_plan_reports_what_the_mapping_leaves_open(tmp_path: Path) -> None:
    account_map = _map(
        tmp_path,
        Gone=mapping.AccountTarget("map", "Asset_Closed"),
        NoLot=mapping.AccountTarget("map", "Asset_Brokerage_Joint", "voo"),
        Funds=mapping.AccountTarget("map", "Asset_Brokerage_Joint"),
    )
    result = holdings.plan(_snapshot(New=1, Gone=2, NoLot=3, Funds=4), account_map,
                           _ACCOUNTS, _lots())

    assert not result.complete
    assert [h.account for h in result.unmapped] == ["New"]
    assert [h.account for h, _ in result.unknown] == ["Gone", "NoLot"]
    assert len(result.warnings) == 1 and "has 2 lots" in result.warnings[0]
    assert result.changes == []


# ---------- the flows applier ----------

def test_flow_plan_classifies_each_generated_rule(tmp_path: Path) -> None:
    scheduled = tmp_path / "scheduled.csv"
    columns = ("name", "from_account", "from_lot", "to_account", "amount", "series", "start",
               "cadence", "tags")

    def row(name: str, amount: str, tags: str = "generated:x") -> dict[str, str]:
        return dict(zip(columns, (name, "Asset_Checking", "", "Expense_Living", amount, "cpi",
                                  "2026-05-31", "monthly", tags), strict=True))

    lines = [row("same", "1.00"), row("differs", "2.00"), row("stale", "3.00"),
             row("manual", "4.00", "")]
    scheduled.write_text(
        ",".join(columns) + "\n" + "".join(",".join(r.values()) + "\n" for r in lines),
        encoding="utf-8")

    changes = flows.plan(scheduled, [row("same", "1.00"), row("differs", "2.50"), row("new", "9.00")],
                         "generated:x")

    assert [(c.name, c.before, c.after, c.status) for c in changes] == [
        ("same", "1.00", "1.00", "unchanged"),
        ("differs", "2.00", "2.50", "changed"),
        ("new", "", "9.00", "added"),
        ("stale", "3.00", "", "removed"),
    ]
    assert [c.status for c in flows.plan(tmp_path / "none.csv", [row("new", "9.00")], "generated:x")] \
        == ["added"]
