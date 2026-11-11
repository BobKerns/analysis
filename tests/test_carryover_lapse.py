"""A decedent's loss carryovers end at their death (#277): their share of a
capital-loss carryover and of a net operating loss can be used on the return
for the year they die, and then lapses. The survivor's share carries on."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from analysis.sim.tax_api import NO_EXTRA, Account, CapitalLoss, IncomeItem, Person, Return, TaxYear
from analysis.sim import registries
from analysis.sim.taxes import common
from analysis.sim.taxes.state_base import Basis
from analysis.sim.taxes.state_tables import StateTablesYear

CHECKING = Account("Asset_Checking", "asset", "cash", None)
ALICE_OWN = Account("Asset_Brokerage_Alice", "asset", "brokerage", "alice")
BILL_OWN = Account("Asset_Brokerage_Bill", "asset", "brokerage", "bill")
JOINT = Account("Asset_Brokerage_Joint", "asset", "brokerage", "joint")
ALICE_DIES = "2030-03-31"
BILL_DIES = "2034-08-31"


def _year(year: int, items: Sequence[IncomeItem] = (), losses: Sequence[CapitalLoss] = (), *,
          alice: str | None = ALICE_DIES, bill: str | None = None,
          persons: Sequence[Person] | None = None, nol: Mapping[int, int] | None = None) -> TaxYear:
    """A tax year with only what the carryovers read: the items, the losses,
    the people (Alice and Bill with their final months, unless ``persons``),
    and ``nol``, the NOL carried out of earlier years."""
    def unused(*_args: object) -> list:
        return []

    return TaxYear(
        year=year, jurisdiction="federal", params=None, items=list(items), losses=list(losses),
        persons=list(persons) if persons is not None else [
            Person("alice", "1965-03-15", "primary", alice),
            Person("bill", "1967-08-20", "primary", bill)],
        _table=unused, _indexed=unused, _dated=unused, _keyed=lambda *_a: None,
        _filing_status_in=lambda _y: "MFJ", _prior=lambda _line: 0, _series_of=unused,
        _line_in=lambda line, y: (nol or {}).get(y, 0) if line == "nol_carryover" else 0,
    )


def _sold_at_a_loss(year: int, account: Account, amount: int) -> IncomeItem:
    return IncomeItem(year, "ltcg", account, CHECKING, -amount)


def _carry(year: int, *items: IncomeItem, alice: str | None = ALICE_DIES,
           bill: str | None = None) -> common.CapitalGains:
    return common.capital_gains(_year(year, items, alice=alice, bill=bill),
                                loss_limit=lambda _y: 3_000_00)


def test_a_loss_on_the_decedents_own_account_ends_after_their_final_return() -> None:
    loss = _sold_at_a_loss(2028, ALICE_OWN, 20_000_00)
    # 2028 and 2029 use $3,000 each; 2030, the year she dies, uses $3,000 more.
    assert _carry(2029, loss).carryover_lt == 14_000_00
    died = _carry(2030, loss)
    assert (died.loss_deduction, died.carryover_lt, died.lapsed) == (3_000_00, 0, 11_000_00)
    assert _carry(2031, loss).loss_deduction == 0


def test_the_survivors_own_loss_carries_on() -> None:
    loss = _sold_at_a_loss(2028, BILL_OWN, 20_000_00)
    assert (_carry(2030, loss).carryover_lt, _carry(2030, loss).lapsed) == (11_000_00, 0)
    assert _carry(2031, loss).loss_deduction == 3_000_00


def test_half_a_joint_loss_ends_at_the_first_death_and_the_rest_at_the_second() -> None:
    loss = _sold_at_a_loss(2028, JOINT, 20_000_00)
    first = _carry(2030, loss, bill=BILL_DIES)
    assert (first.carryover_lt, first.lapsed) == (5_500_00, 5_500_00)
    # Bill uses his half at $3,000 a year from 2031, and runs out in 2032.
    second = _carry(2032, loss, bill=BILL_DIES)
    assert (second.carryover_lt, second.lapsed) == (0, 0)
    assert _carry(2031, loss, bill=BILL_DIES).carryover_lt == 2_500_00


def test_a_carryover_from_both_spouses_losses_ends_in_proportion() -> None:
    own, theirs = _sold_at_a_loss(2029, ALICE_OWN, 15_000_00), _sold_at_a_loss(2029, BILL_OWN, 5_000_00)
    died = _carry(2030, own, theirs)
    # 20,000 less two years' 3,000: 14,000, three-quarters of it Alice's.
    assert (died.carryover_lt, died.lapsed) == (3_500_00, 10_500_00)


def test_losses_before_the_carryover_last_ran_out_dont_count() -> None:
    # Alice's 2026 loss is used up by 2027; Bill's 2028 loss builds the carryover she dies with.
    early = _sold_at_a_loss(2026, ALICE_OWN, 5_000_00)
    later = _sold_at_a_loss(2028, BILL_OWN, 20_000_00)
    assert _carry(2030, early, later).lapsed == 0


def test_a_loss_on_an_account_inherited_from_a_spouse_is_the_survivors() -> None:
    later = _sold_at_a_loss(2031, ALICE_OWN, 20_000_00)  # Bill holds Alice's account after 2030
    assert _carry(2034, later, bill=BILL_DIES).lapsed == 20_000_00 - 4 * 3_000_00


def test_a_recorded_capital_loss_is_its_lots_owners() -> None:
    ty = _year(2030, losses=[CapitalLoss(2029, "ltcg", "alice", 10_000_00)])
    result = common.capital_gains(ty, loss_limit=lambda _y: 3_000_00)
    assert (result.carryover_lt, result.lapsed) == (0, 4_000_00)


def test_no_lifespan_nothing_lapses() -> None:
    assert _carry(2030, _sold_at_a_loss(2028, ALICE_OWN, 20_000_00), alice=None).lapsed == 0


SHOP = Account("Asset_Shop", "asset", "business", "alice")
BILL_SHOP = Account("Asset_Workshop", "asset", "business", "bill")
SUPPLIES = Account("Expense_Supplies", "expense", "business", None)


def _business_loss(year: int, shop: Account, amount: int) -> IncomeItem:
    return IncomeItem(year, "business_expense", shop, SUPPLIES, amount)


def test_a_proprietors_operating_loss_ends_with_them() -> None:
    ty = _year(2030, [_business_loss(2029, SHOP, 10_000_00)], nol={2029: 10_000_00})
    assert common.nol_lapse_share(ty) == 1
    assert common.nol_carried(8_000_00, 1.0) == {"nol_carryover": 0, "nol_lapsed": 8_000_00}


def test_the_survivors_business_loss_carries_on() -> None:
    ty = _year(2030, [_business_loss(2029, SHOP, 3_000_00), _business_loss(2029, BILL_SHOP, 1_000_00)],
               nol={2029: 4_000_00})
    assert common.nol_lapse_share(ty) == 0.75
    assert common.nol_carried(8_000_00, 0.75) == {"nol_carryover": 2_000_00, "nol_lapsed": 6_000_00}


def test_no_death_in_the_year_no_nol_lapses() -> None:
    assert common.nol_lapse_share(_year(2029, [_business_loss(2029, SHOP, 10_000_00)])) == 0
    assert common.nol_carried(8_000_00, 0.0) == {"nol_carryover": 8_000_00}


def test_the_state_reports_its_own_lapse_not_the_federal_one() -> None:
    """The state nets its own losses (an HSA's growth is gain to it), so a
    federal lapse it doesn't share stays off its return."""
    federal = Return({"capital_loss_carryover": 0, "capital_loss_lapsed": 11_000_00})
    year = object.__new__(StateTablesYear)
    year._capital = common.CapitalGains(0, 0, 3_000_00, 0, 0)
    assert "capital_loss_lapsed" not in year.fixed_lines(Basis(NO_EXTRA, federal))
    year._capital = common.CapitalGains(0, 0, 3_000_00, 0, 0, lapsed=4_000_00)
    assert year.fixed_lines(Basis(NO_EXTRA, federal))["capital_loss_lapsed"] == 4_000_00


def test_business_losses_from_before_the_nol_last_ran_out_dont_count() -> None:
    # Alice's 2015 loss was used up by 2018; Bill's 2029 loss is the NOL she dies with.
    ty = _year(2030, [_business_loss(2015, SHOP, 50_000_00), _business_loss(2029, BILL_SHOP, 10_000_00)],
               nol={2015: 40_000_00, 2016: 20_000_00, 2017: 5_000_00, 2029: 8_000_00})
    assert common.nol_lapse_share(ty) == 0


def test_hobby_receipts_offset_its_owners_business_loss() -> None:
    """As in business_result: a hobby's receipts are income, its expenses
    aren't deducted."""
    hobby = Account("Asset_Pottery", "asset", "business", "bill", hobby=True)
    items = [_business_loss(2029, SHOP, 10_000_00), _business_loss(2029, BILL_SHOP, 5_000_00),
             IncomeItem(2029, "business_income", SUPPLIES, hobby, 5_000_00),
             _business_loss(2029, hobby, 2_000_00)]
    ty = _year(2030, items, alice=None, bill="2030-06-30", nol={2029: 10_000_00})
    assert common.nol_lapse_share(ty) == 0  # Bill's businesses netted to nothing


INCOME = Account("Income_Other", "income", "other", None)


def test_a_loss_is_the_receiving_accounts_owners_when_the_payer_has_none() -> None:
    loss = IncomeItem(2029, "ltcg", INCOME, ALICE_OWN, -10_000_00)
    assert _carry(2030, loss).lapsed == 10_000_00 - 2 * 3_000_00


def test_a_fall_in_an_hsa_is_its_owners_loss_where_the_state_taxes_it() -> None:
    hsa = Account("Asset_HSA_Alice", "asset", "hsa", "alice")
    growth = registries.GROWTH_COUNTERPARTY_ACCOUNTS["deferred"].name
    fall = IncomeItem(2029, "deferred", hsa, Account(growth, "income", "", None), 10_000_00)
    result = common.capital_gains(_year(2030, [fall]), loss_limit=lambda _y: 3_000_00, hsa_years={2029})
    assert (result.carryover_lt, result.lapsed) == (0, 4_000_00)


def test_a_later_death_shares_only_what_the_earlier_one_left() -> None:
    """With no primary persons the household is everyone with a role, so a
    joint loss is half Alice's at her death and half Bill's at his, while
    Carol lives on; Carol's own loss never lapses."""
    persons = [Person("alice", None, "member", ALICE_DIES), Person("bill", None, "member", "2031-05-31"),
               Person("carol", None, "member", None)]
    carols = Account("Asset_Brokerage_Carol", "asset", "brokerage", "carol")
    items = (_sold_at_a_loss(2029, JOINT, 10_000_00), _sold_at_a_loss(2029, carols, 10_000_00))

    def carry(year: int) -> common.CapitalGains:
        return common.capital_gains(_year(year, items, persons=persons), loss_limit=lambda _y: 0)

    assert (carry(2030).carryover_lt, carry(2030).lapsed) == (15_000_00, 5_000_00)
    # Bill's half of the joint loss Alice's death left: 2,500 of the 15,000.
    assert (carry(2031).carryover_lt, carry(2031).lapsed) == (12_500_00, 2_500_00)
