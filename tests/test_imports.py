"""The import framework (issue #527): sources, common forms, mapping, appliers."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.imports import flows, holdings, mapping, profiles, quicken, sources
from analysis.sim.imports.forms import Flows, Holding, Snapshot

# ---------- the Quicken source ----------

def test_read_account_tree_extracts_leaf_accounts_and_the_report_date(
    quicken_net_worth_export: Path,
) -> None:
    snapshot = quicken.read_account_tree(quicken_net_worth_export)

    assert snapshot.source == "quicken"
    assert snapshot.as_of == dt.date(2026, 8, 18)
    # Category headers, subtotals at any depth and the net worth line are not accounts.
    assert {h.account: h.value_cents for h in snapshot.holdings} == {
        "Asset_Checking": 123456,
        "Asset_Brokerage_Joint": 31000000,
        "Asset_TraditionalIRA_Alice": 51000000,
        "Liability_Mortgage": -24000000,
    }


def test_read_transactions_gives_the_common_columns(quicken_transaction_export: Path) -> None:
    found = quicken.read_transactions(quicken_transaction_export)

    assert isinstance(found, Flows)
    assert found.transactions[["account", "payee", "category", "amount_cents"]].to_dict("records")[0] == {
        "account": "Checking", "payee": "Market", "category": "Groceries", "amount_cents": -30000}
    assert len(found.transactions) == 4


def test_an_export_with_no_transactions_still_has_dates(
    quicken_transaction_export: Path, quicken_empty_transaction_export: Path,
) -> None:
    empty = quicken.read_transactions(quicken_empty_transaction_export).transactions
    assert empty.empty and str(empty["date"].dtype).startswith("datetime64")

    both = sources.read_flows([quicken_transaction_export, quicken_empty_transaction_export])
    assert both.transactions["date"].dt.month.tolist() == [1, 1, 2, 2]


def test_flows_refuse_a_frame_without_the_common_columns(quicken_transaction_export: Path) -> None:
    frame = quicken.read_transactions(quicken_transaction_export).transactions.drop(columns=["payee"])
    with pytest.raises(ValueError, match="payee"):
        Flows("quicken", frame)


# ---------- sources ----------

def test_detect_tells_the_two_quicken_layouts_apart(
    quicken_net_worth_export: Path, quicken_transaction_export: Path, prototype_data: Path,
) -> None:
    assert [(s.name, form) for s, form in sources.detect(quicken_net_worth_export)] == [
        ("quicken", "holdings")]
    assert [(s.name, form) for s, form in sources.detect(quicken_transaction_export)] == [
        ("quicken", "transactions")]
    assert sources.detect(prototype_data / "persons.csv") == []


def test_read_holdings_finds_the_source_and_refuses_the_wrong_layout(
    quicken_net_worth_export: Path, quicken_transaction_export: Path,
) -> None:
    assert sources.read_holdings([quicken_net_worth_export]).source == "quicken"
    assert len(sources.read_flows([quicken_transaction_export], "quicken").transactions) == 4
    with pytest.raises(sources.SourceError, match="looks like quicken transactions"):
        sources.read_holdings([quicken_transaction_export])
    with pytest.raises(sources.SourceError, match="unknown source 'nowhere'"):
        sources.read_holdings([quicken_net_worth_export], "nowhere")


def test_read_holdings_refuses_files_of_different_dates_or_with_the_same_account(
    quicken_net_worth_export: Path, quicken_year_end_export: Path,
) -> None:
    with pytest.raises(sources.SourceError, match="different dates"):
        sources.read_holdings([quicken_net_worth_export, quicken_year_end_export])
    # Holdings that land in one lot are added, so a repeat would count twice.
    with pytest.raises(sources.SourceError, match="'Asset_Checking' is in more than one file"):
        sources.read_holdings([quicken_net_worth_export, quicken_net_worth_export])


# ---------- mapping ----------

def test_account_map_round_trips_and_falls_back_to_the_whole_account(test_data_dir: Path) -> None:
    path = mapping.source_dir(test_data_dir, "quicken") / mapping.ACCOUNTS
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
    assert (found.entries["Bill Brokerage", "VTI"].label, found.entries["Business", ""].label) == \
        ("Asset_Brokerage_Joint:vti", "Asset_Checking")


@pytest.mark.parametrize(("rows", "message"), [
    ("A,,move,X,,\n", "action must be"),
    ("A,,map,,,\n", "needs an account"),
    ("A,,map,X,,\nA,,skip,,,\n", "duplicate"),
])
def test_load_account_map_rejects_bad_rows(
    write_import_map: Callable[[str, str], Path], rows: str, message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        mapping.load_account_map(write_import_map("quicken/accounts.csv", rows))


def test_load_account_map_rejects_another_header(quicken_account_map: Path) -> None:
    quicken_account_map.write_text("quicken_name,account_name,action,note\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected header"):
        mapping.load_account_map(quicken_account_map)


def test_renames_follow_in_every_saved_mapping(
    test_data_dir: Path, write_import_map: Callable[[str, str], Path],
) -> None:
    accounts = write_import_map(
        "quicken/accounts.csv",
        "Business,,map,Asset_Checking,,\n"
        "Funds,,map,Asset_Brokerage_Joint,vti,\n"
        "Bonds,,map,Asset_Brokerage_Joint,bnd,\n"
        "Card,,skip,,,\n")
    expenses = write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,-\n")

    assert mapping.rename_account(test_data_dir, "Asset_Checking", "Asset_Bank") == [
        "imports/quicken/accounts.csv"]
    assert mapping.rename_account(test_data_dir, "Expense_Living", "Expense_Home") == [
        "imports/expense_accounts.csv"]
    assert mapping.rename_lot(test_data_dir, "Asset_Brokerage_Joint", "vti", "total_market") == [
        "imports/quicken/accounts.csv"]
    assert mapping.rename_account(test_data_dir, "Nothing", "Else") == []
    assert mapping.rename_lot(test_data_dir, "Asset_Checking", "vti", "x") == []

    found = mapping.load_account_map(accounts)
    assert found.lookup("Business") == mapping.AccountTarget("map", "Asset_Bank")
    assert found.entries["Funds", ""].label == "Asset_Brokerage_Joint:total_market"
    assert found.entries["Bonds", ""].label == "Asset_Brokerage_Joint:bnd"
    assert mapping.load_expense_accounts(expenses).accounts == {
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


def _map(**targets: mapping.AccountTarget) -> mapping.AccountMap:
    found = mapping.AccountMap(Path("accounts.csv"))
    for outside, target in targets.items():
        found.set(outside, "", target)
    return found


def _snapshot(**cents: int) -> Snapshot:
    return Snapshot("quicken", tuple(Holding(name, value) for name, value in cents.items()))


def test_plan_sets_balances_sums_shared_lots_and_negates_debts() -> None:
    account_map = _map(
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
    assert result.cells == {
        0: {"opening_balance": "3.50"}, 1: {"prior_year_end_balance": "100.00"},
        3: {"opening_balance": "42.00"}}
    assert result.rows[2] == _lots()[2]


def test_plan_nets_a_credit_against_the_debt_it_shares_a_lot_with() -> None:
    account_map = _map(
        CardA=mapping.AccountTarget("map", "Liability_Card"),
        CardB=mapping.AccountTarget("map", "Liability_Card"),
    )
    owed = holdings.plan(_snapshot(CardA=-10000, CardB=2000), account_map, _ACCOUNTS, _lots())
    assert owed.rows[3]["opening_balance"] == "80.00" and owed.warnings == []

    # Only a net credit is unusual: its size is kept, with a warning.
    credit = holdings.plan(_snapshot(CardA=-1000, CardB=2500), account_map, _ACCOUNTS, _lots())
    assert credit.rows[3]["opening_balance"] == "15.00"
    assert len(credit.warnings) == 1 and "credit balance" in credit.warnings[0]


def test_plan_year_end_overwrites_investment_accounts_only() -> None:
    account_map = _map(
        Bank=mapping.AccountTarget("map", "Asset_Checking"),
        Funds=mapping.AccountTarget("map", "Asset_Brokerage_Joint", "vti"),
        Bonds=mapping.AccountTarget("map", "Asset_Brokerage_Joint", "bnd"),
    )
    result = holdings.plan(_snapshot(Bank=100, Funds=9000, Bonds=6000), account_map, _ACCOUNTS,
                           _lots(), year_end=True)

    assert result.ineligible == ["Asset_Checking"]
    assert [(c.lot, c.column, c.before, c.after) for c in result.changes] == [
        ("vti", "prior_year_end_balance", "", "90.00"),
        ("bnd", "prior_year_end_balance", "40.00", "60.00")]
    assert result.rows[2]["opening_balance"] == "50.00"


def test_plan_leaves_an_account_whole_in_its_prior_year_balances() -> None:
    """The loader wants the prior-year balance on every lot of an account or
    on none, so one lot alone is neither seeded nor set."""
    account_map = _map(Bonds=mapping.AccountTarget("map", "Asset_Brokerage_Joint", "bnd"))
    lots = _lots()
    lots[2]["prior_year_end_balance"] = ""

    seeded = holdings.plan(_snapshot(Bonds=6000), account_map, _ACCOUNTS, lots)
    assert seeded.cells == {2: {"opening_balance": "60.00"}}

    year_end = holdings.plan(_snapshot(Bonds=6000), account_map, _ACCOUNTS, lots, year_end=True)
    assert year_end.cells == {} and year_end.changes == []
    assert len(year_end.warnings) == 1 and "every lot of an account or on none" in year_end.warnings[0]

    # A Roth lot beside it needn't have one.
    lots[1]["subkind"] = "roth_ira"
    assert holdings.plan(_snapshot(Bonds=6000), account_map, _ACCOUNTS, lots, year_end=True).cells == {
        2: {"prior_year_end_balance": "60.00"}}


def test_plan_compares_balances_as_money() -> None:
    account_map = _map(Bank=mapping.AccountTarget("map", "Asset_Checking"))
    lots = _lots()
    lots[0]["opening_balance"] = "5"

    same = holdings.plan(_snapshot(Bank=500), account_map, _ACCOUNTS, lots)
    assert same.cells == {} and same.lots_unchanged == 1
    assert same.rows[0]["opening_balance"] == "5"

    lots[0]["opening_balance"] = ""
    assert holdings.plan(_snapshot(Bank=0), account_map, _ACCOUNTS, lots).cells == {
        0: {"opening_balance": "0.00"}}


def test_plan_reports_what_the_mapping_leaves_open() -> None:
    account_map = _map(
        Gone=mapping.AccountTarget("map", "Asset_Closed"),
        NoLot=mapping.AccountTarget("map", "Asset_Brokerage_Joint", "voo"),
        Funds=mapping.AccountTarget("map", "Asset_Brokerage_Joint"),
        Card=mapping.AccountTarget("map", "Liability_Card"),
    )
    lots = [lot for lot in _lots() if lot["account_name"] != "Liability_Card"]
    result = holdings.plan(_snapshot(New=1, Gone=2, NoLot=3, Funds=4, Card=-5), account_map,
                           _ACCOUNTS, lots)

    assert not result.complete
    assert [h.account for h in result.unmapped] == ["New"]
    assert [h.account for h, _ in result.unknown] == ["Gone"]
    # An account with no lot at all has nowhere to hold a balance either.
    assert [h.account for h, _ in result.no_lot] == ["NoLot", "Card"]
    assert len(result.warnings) == 1 and "has 2 lots" in result.warnings[0]
    assert result.changes == [] and result.cells == {}


# ---------- the flows applier ----------

def test_flow_plan_classifies_each_generated_rule() -> None:
    def row(name: str, amount: str, tags: str = "generated:x", **more: str) -> dict[str, str]:
        return {"name": name, "from_account": "Asset_Checking", "to_account": "Expense_Living",
                "amount": amount, "series": "cpi", "start": "2026-05-31", "cadence": "monthly",
                "end": "", "tags": tags, "meta_json": "{}", **more}

    existing = {each["name"]: each for each in (
        row("same", "1.00", meta_json='{"files_sha256": "old"}'), row("differs", "2.00"),
        row("ended", "5.00", end="2030-01-31"), row("stale", "3.00"), row("parents", "6.00"),
        row("manual", "4.00", ""))}

    changes = flows.plan(
        existing,
        [row("same", "1.00"), row("differs", "2.50"), row("ended", "5.00"), row("new", "9.00")],
        "generated:x", inherited={"parents"})

    assert [(c.name, c.before, c.after, c.status) for c in changes] == [
        ("same", "1.00", "1.00", "unchanged"),
        ("differs", "2.00", "2.50", "changed"),
        # Edited by hand since: the write puts the generated row back.
        ("ended", "5.00", "5.00", "changed"),
        ("new", "", "9.00", "added"),
        ("stale", "3.00", "", "removed"),
        # A parent scenario's rule stays in force whatever this one writes.
        ("parents", "6.00", "6.00", "kept"),
    ]
    assert [c.status for c in flows.plan({}, [row("new", "9.00")], "generated:x")] == ["added"]


def test_read_flows_refuses_files_that_cover_the_same_days_of_an_account(
    quicken_transaction_export: Path,
) -> None:
    text = quicken_transaction_export.read_text(encoding="utf-8")
    later = quicken_transaction_export.with_name("later.csv")
    later.write_text(text.replace("2026-01", "2026-03").replace("2026-02", "2026-04"), encoding="utf-8")
    assert len(sources.read_flows([quicken_transaction_export, later]).transactions) == 8

    # The same file twice, and a file that overlaps another by a month.
    with pytest.raises(sources.SourceError, match="both have transactions of 'Checking'"):
        sources.read_flows([quicken_transaction_export, quicken_transaction_export])
    later.write_text(text.replace("2026-01", "2026-03"), encoding="utf-8")
    with pytest.raises(sources.SourceError, match="between 2026-02-02 and 2026-02-14"):
        sources.read_flows([quicken_transaction_export, later])


def test_read_transactions_reads_a_signed_dollar_amount_and_names_a_bad_one(
    quicken_transaction_export: Path,
) -> None:
    text = quicken_transaction_export.read_text(encoding="utf-8")
    quicken_transaction_export.write_text(text.replace('"-300.00"', '"-$300.00"'), encoding="utf-8")
    assert quicken.read_transactions(quicken_transaction_export).transactions["amount_cents"][0] == -30000

    quicken_transaction_export.write_text(text.replace('"-300.00"', '"n/a"'), encoding="utf-8")
    with pytest.raises(ValueError, match="row 3: 'n/a' is not an amount"):
        quicken.read_transactions(quicken_transaction_export)


# ---------- sources described by a profile ----------

def test_a_profile_reads_money_dates_and_where_the_holdings_stop(
    fidelity_positions_export: Path, vanguard_holdings_export: Path,
) -> None:
    fidelity = sources.read_holdings([fidelity_positions_export])
    assert (fidelity.source, fidelity.as_of) == ("fidelity", dt.date(2026, 1, 5))
    # The notes after the empty row are not holdings; '**' is Fidelity's mark.
    assert [(h.account, h.asset, h.value_cents, h.cost_basis_cents) for h in fidelity.holdings] == [
        ("111111111", "FXAIX", 20010000, 15000000), ("111111111", "FSKAX", 7500000, 8000000),
        ("111111111", "SPAXX", 4490000, None), ("222222222", "FXAIX", 9000000, 6000000)]
    assert fidelity.holdings[0].hints["account_name"] == "Joint brokerage"

    vanguard = sources.read_holdings([vanguard_holdings_export])
    assert vanguard.as_of == dt.date(2026, 1, 5)
    # The account is the file's name; the basis is the balance less the gain,
    # and a dash is no gain to take off.
    assert [(h.account, h.asset, h.cost_basis_cents) for h in vanguard.holdings] == [
        ("Bill Roth IRA", "VTSAX", 30000000), ("Bill Roth IRA", "VBTLX", 11642362),
        ("Bill Roth IRA", "VMFXX", None)]


@pytest.mark.parametrize(("text", "problem"), [
    ("- a list\n", "a profile is a mapping"),
    ("description: x\n", "needs 'holdings'"),
    ("holdings: {columns: {asset: A}}\n", "must name value"),
    ("holdings: {columns: {asset: A, value: V}}\n", "say where the account is, once"),
    ("holdings: {columns: {asset: A, value: V, account: C}, account: {file_name: x}}\n",
     "say where the account is, once"),
    ("holdings: {columns: {asset: A, value: V, account: C}, as_of: {heading: '(', format: x}}\n",
     "is not a pattern"),
    ("holdings: {columns: {asset: A, value: V, account: C}, stop: never}\n", "unknown holdings key(s)"),
    ("name: other\nholdings: {columns: {asset: A, value: V, account: C}}\n", "its folder says 'mine'"),
])
def test_a_profile_that_cant_be_used_says_why(
    write_import_profile: Callable[[str, str], Path], text: str, problem: str,
) -> None:
    path = write_import_profile("mine", text)
    with pytest.raises(profiles.ProfileError, match=re.escape(problem)) as error:
        profiles.load(path, "mine")
    assert str(path) in str(error.value)


def test_a_profile_names_the_cell_it_cant_read(
    write_import_profile: Callable[[str, str], Path], tmp_path: Path, test_data_dir: Path,
) -> None:
    write_import_profile("mine", "holdings: {columns: {account: Acct, asset: Sym, value: Worth}}\n")
    imports = test_data_dir / "imports"
    export = tmp_path / "mine.csv"
    export.write_text("Acct,Sym,Worth\nA1,AAA,lots\n", encoding="utf-8")

    with pytest.raises(profiles.ProfileError, match=r"line 2, Worth: 'lots' is not an amount of money"):
        sources.read_holdings([export], imports=imports)
    # A file without the profile's columns is not the profile's.
    export.write_text("Acct,Sym\nA1,AAA\n", encoding="utf-8")
    assert sources.detect(export, imports=imports) == []


def test_a_profile_may_not_take_a_coded_sources_name(
    write_import_profile: Callable[[str, str], Path], test_data_dir: Path,
) -> None:
    write_import_profile("quicken", "holdings: {columns: {account: A, asset: S, value: V}}\n")
    with pytest.raises(sources.SourceError, match="give the profile's folder another name"):
        sources.sources(test_data_dir / "imports")


# ---------- lots by symbol ----------

_BROKERAGE = {"Brokerage": ("asset", "brokerage_taxable"), "Roth": ("asset", "retirement")}


def _symbol_snapshot() -> Snapshot:
    from decimal import Decimal
    return Snapshot("broker", (
        Holding("B1", 12000_00, asset="AAA", quantity=Decimal("100"), cost_basis_cents=9000_00),
        Holding("B1", 5000_00, asset="BBB", quantity=Decimal("50.5"), cost_basis_cents=6000_00),
    ))


def _mapped(test_data_dir: Path, *rows: tuple[str, str, mapping.AccountTarget]) -> mapping.AccountMap:
    found = mapping.AccountMap(test_data_dir / "imports" / "broker" / "accounts.csv")
    for outside, asset, target in rows:
        found.set(outside, asset, target)
    return found


def test_plan_sends_each_holding_to_the_lot_of_its_symbol(test_data_dir: Path) -> None:
    account_map = _mapped(test_data_dir, ("B1", "", mapping.AccountTarget("map", "Brokerage")))
    lots = [
        # Kept in shares: only the share count is the import's to set.
        {"account_name": "Brokerage", "lot": "a", "symbol": "AAA", "quantity": "90", "opening_balance": "",
         "opening_cost_basis": "8000.00"},
        {"account_name": "Brokerage", "lot": "b", "symbol": "BBB", "quantity": "", "opening_balance": "4000.00"},
        {"account_name": "Brokerage", "lot": "gone", "symbol": "CCC", "quantity": "5", "opening_balance": "70.00"},
    ]

    result = holdings.plan(_symbol_snapshot(), account_map, _BROKERAGE, lots)

    assert result.complete
    assert result.cells == {
        0: {"quantity": "100", "opening_cost_basis": "9000.00"},
        1: {"opening_balance": "5000.00", "opening_cost_basis": "6000.00"},
    }
    assert [w for w in result.warnings if "'gone'" in w and "sim account lot delete Brokerage gone" in w]


def test_plan_leaves_open_a_symbol_with_no_lot_and_fills_a_fresh_one(test_data_dir: Path) -> None:
    account_map = _mapped(test_data_dir, ("B1", "", mapping.AccountTarget("map", "Roth")))
    one = {"account_name": "Roth", "lot": "default", "subkind": "roth_ira", "opening_balance": "100.00"}

    # One lot with no symbol: together, or a lot each?
    asked = holdings.plan(_symbol_snapshot(), account_map, _BROKERAGE, [one])
    assert [h.asset for h, _ in asked.undecided] == ["AAA", "BBB"] and not asked.new
    # A lot each: every holding needs one.
    split = holdings.plan(_symbol_snapshot(), account_map, _BROKERAGE, [one], split={"Roth"})
    assert [h.asset for h, _ in split.new] == ["AAA", "BBB"] and not split.undecided

    fresh = [{"account_name": "Roth", "lot": name, "subkind": "roth_ira", "symbol": name.upper()}
             for name in ("aaa", "bbb")]
    result = holdings.plan(_symbol_snapshot(), account_map, _BROKERAGE, [one, *fresh],
                           split={"Roth"}, fresh={1, 2})
    assert result.complete
    # Shares and balance both, so the lot needs no price; never a Roth's basis.
    assert result.cells[1] == {"quantity": "100", "opening_balance": "12000.00",
                               "prior_year_end_balance": "12000.00"}
    assert {"quantity": "50.5", "opening_balance": "5000.00"}.items() <= result.cells[2].items()
    assert not any("opening_cost_basis" in cells for cells in result.cells.values())
    assert len(result.notes) == 2 and "sim account lot edit Roth aaa --opening-cost-basis" in result.notes[0]
    assert [w for w in result.warnings if "'default'" in w and "now has lots of its own" in w]
