"""The pieces the built-in tax calculators share (#507): the year's income
summed from its items, capital-loss netting, a Schedule C, an HSA a state
taxes, Social Security taxation, the brackets' tax, a state's retirement income
exclusions, and its own deductions and credits."""

from __future__ import annotations

import functools
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, NamedTuple

from analysis.sim import ownership, registries, states
from analysis.sim.tax_api import Account, IncomeItem, TaxError, TaxYear


def income_buckets(ty: TaxYear) -> tuple[int, int, int]:
    """The year's ordinary income, Social Security benefits and qualified
    dividends (#468). Capital gains and losses are :func:`capital_gains`'s."""
    by_char: dict[str, int] = {}
    for item in ty.year_items():
        by_char[item.character] = by_char.get(item.character, 0) + item.amount
    ordinary = (
        by_char.get("ordinary", 0)
        + by_char.get("tax_deferred_out", 0)
        + by_char.get("roth_nonqualified", 0)
        - by_char.get("tax_deferred_in", 0)
    )
    return ordinary, by_char.get("ss_benefit", 0), by_char.get(registries.QUALIFIED_DIVIDEND, 0)


class BusinessResult(NamedTuple):
    """One tax year's Schedule C result (#231): receipts less deductible
    expenses, a loss negative, and the expenses not deducted because the
    business is a hobby."""
    net: int
    disallowed: int


def _business_legs(ty: TaxYear, items: Iterable[IncomeItem]) -> Iterator[tuple[IncomeItem, Account, int, int]]:
    """Each ``business_income`` or ``business_expense`` item with its
    ``business`` account, what it adds to the net (receipts in, expenses
    out), and what it adds to the expenses a hobby can't deduct."""
    for item in items:
        if item.character not in (registries.BUSINESS_INCOME, registries.BUSINESS_EXPENSE):
            continue
        income = item.character == registries.BUSINESS_INCOME
        account = item.payee if income else item.payer
        if account.subkind != registries.BUSINESS_SUBKIND:
            # The loader checks the rows it can; this catches the rest.
            side = "into" if income else "out of"
            raise TaxError(
                f"taxes/{ty.jurisdiction}.csv: {item.year}: {item.character} must be paid {side} a "
                f"{registries.BUSINESS_SUBKIND} account, not '{account.name}'"
            )
        if income:
            yield item, account, item.amount, 0
        elif account.hobby:
            yield item, account, 0, item.amount
        else:
            yield item, account, -item.amount, 0


def business_result(ty: TaxYear) -> BusinessResult:
    """The year's ``business_income`` into, less its ``business_expense``
    out of, ``business`` accounts. A business whose account's meta_json says
    ``"hobby": true`` isn't engaged in for profit (IRC §183): its receipts
    are still income, but none of its expenses are deductible."""
    net = disallowed = 0
    for _item, _account, added, not_deducted in _business_legs(
            ty, ty.year_items(registries.BUSINESS_INCOME, registries.BUSINESS_EXPENSE)):
        net += added
        disallowed += not_deducted
    return BusinessResult(net, disallowed)


CAPITAL_LINES = ("capital_loss_used", "capital_loss_carryover", "capital_loss_lapsed")
"""The return lines :func:`capital_lines` fills: a jurisdiction that nets its
own gains replaces them as a group."""


def capital_lines(capital: CapitalGains) -> dict[str, int]:
    """A year's capital-loss lines: the deduction, the carryover, and what
    of it ended with a death, a line only in that year (#277)."""
    return {
        "capital_loss_used": capital.loss_deduction,
        "capital_loss_carryover": capital.carryover_st + capital.carryover_lt,
        **({"capital_loss_lapsed": capital.lapsed} if capital.lapsed else {}),
    }


class CapitalGains(NamedTuple):
    """One tax year's capital gains after netting (IRC §1211(b), §1212(b)):
    the short- and long-term gains taxed, the loss deducted from ordinary
    income, and the loss carried into the next year, by its character."""
    stcg: int
    ltcg: int
    loss_deduction: int
    carryover_st: int
    carryover_lt: int
    lapsed: int = 0
    """The carryover that ended with a death in the year (#277), already
    taken out of ``carryover_st`` and ``carryover_lt``."""


def net_capital(st: int, lt: int, limit: Callable[[], int]) -> CapitalGains:
    """Net a year's short- and long-term results, each already less its own
    carryover: a loss in one offsets a gain in the other, then up to the
    limit of what's left offsets ordinary income, short-term loss first, and
    the rest carries forward keeping its character."""
    if st < 0 < lt or lt < 0 < st:
        net = st + lt
        if net >= 0:
            st, lt = (net, 0) if st > 0 else (0, net)
        else:
            st, lt = (net, 0) if st < 0 else (0, net)
    loss_st, loss_lt = max(0, -st), max(0, -lt)
    deduction = min(limit(), loss_st + loss_lt) if loss_st + loss_lt else 0
    used_st = min(loss_st, deduction)
    return CapitalGains(
        max(0, st), max(0, lt), deduction,
        loss_st - used_st, loss_lt - (deduction - used_st),
    )


def capital_gains(
    ty: TaxYear, *, loss_limit: Callable[[int], int], hsa_years: Collection[int] = (),
) -> CapitalGains:
    """The year's capital gains: its ``stcg``/``ltcg`` items (a withdrawal at
    a loss posts a negative one) less its capital losses, netted with the
    carryover from every earlier year, each of which used up to its own
    year's ``loss_limit``. In ``hsa_years``, the years a state taxes an HSA
    (#497), an HSA's gains (:func:`hsa_gains`) are capital gains too.

    A carryover is the taxpayer's whose losses made it, and their share ends
    after the return for the year they die (#277; Rev. Rul. 74-175). Their
    share is their part of the losses since the carryover was last empty:
    which year's losses a deduction used first is more than a household
    plan needs. A loss is its year's, so one taken after the death in the
    same year still counts as the decedent's if their account made it."""
    people = {p.id for p in ty.persons}
    totals: dict[int, list[int]] = {}
    # Each year's net result by whose it was (None: the household's); a net
    # loss is what that owner added to the carryover.
    nets: dict[int, dict[str | None, int]] = {}

    def add(year: int, character: str, owner: str | None, amount: int) -> None:
        totals.setdefault(year, [0, 0])[1 if character == "ltcg" else 0] += amount
        by_owner = nets.setdefault(year, {})
        owner = None if owner in ownership.JOINT_OWNERS else owner
        by_owner[owner] = by_owner.get(owner, 0) + amount

    for item in ty.items:
        if item.year > ty.year:
            continue
        if item.character in ("stcg", "ltcg"):
            owner = _owner(item.payer.owner, item.payee.owner, people, payer_kind=item.payer.kind)
            add(item.year, item.character, owner, item.amount)
    for year, character, owner, amount in hsa_gains(ty, hsa_years):
        add(year, character, owner, amount)
    for loss in ty.losses:
        if loss.character in ("stcg", "ltcg") and loss.year <= ty.year:
            add(loss.year, loss.character, loss.owner, -loss.amount)
    household = ty.household
    carry_st = carry_lt = 0
    building: dict[str | None, int] = {}
    result = CapitalGains(0, 0, 0, 0, 0)
    for year in range(min(totals, default=ty.year), ty.year + 1):
        st, lt = totals.get(year, (0, 0))
        result = net_capital(st - carry_st, lt - carry_lt, functools.partial(loss_limit, year))
        carry_st, carry_lt = result.carryover_st, result.carryover_lt
        for owner, net in nets.get(year, {}).items():
            if net < 0:
                building[owner] = building.get(owner, 0) - net
        if carry_st + carry_lt and household.died_in(year):
            ended = _ended(household, year, building)
            share = _share(building, ended)
            lapsed_st, lapsed_lt = round(carry_st * share), round(carry_lt * share)
            carry_st, carry_lt = carry_st - lapsed_st, carry_lt - lapsed_lt
            result = result._replace(carryover_st=carry_st, carryover_lt=carry_lt,
                                     lapsed=lapsed_st + lapsed_lt)
            building = _left(building, ended)
        if not carry_st + carry_lt:
            building = {}
    return result


_GAIN_DISTRIBUTIONS = registries.PAYOUT_ACCOUNTS["ltcg"].name


def gain_distributions(ty: TaxYear) -> int:
    """The year's reinvested long-term gain distributions (#547): gain it
    was paid, not gain it drew out of a lot, which they add basis to."""
    return sum(i.amount for i in ty.year_items("ltcg") if i.payer.name == _GAIN_DISTRIBUTIONS)


_HOLDING_KINDS = frozenset({"asset", "liability"})
"""The kinds of account that hold money for someone: one with no person
for its owner is the household's."""


def _owner(payer: str | None, payee: str | None, people: Collection[str], *,
           payer_kind: str = "asset") -> str | None:
    """Whose an item is: its paying account's owner's, or else, when the
    payer holds nothing for anyone (an income account), its receiving
    account's. None is the household's: a joint account's (owner ``joint``
    or none), or an item neither account's owner holds alone."""
    if payer in people:
        return payer
    if payer_kind in _HOLDING_KINDS:
        return None
    return payee if payee in people else None


def _ended(household: ownership.Household, year: int,
           losses: Mapping[str | None, int]) -> dict[str | None, float]:
    """The part of each owner's ``losses`` that ends with the deaths in
    ``year`` (#277), by :meth:`ownership.Household.decedent_part`: the
    decedent's own accounts', an account's they inherited, and half a joint
    account's while another of the household lives on (all of it once no
    one does). A community loss is half each spouse's too, so no
    ``community`` here."""
    decedents = household.died_in(year)

    def part(decedent: str, owner: str | None) -> float:
        found = household.decedent_part(decedent, owner)
        return 1 / found[0] if found else 0.0

    return {owner: min(1.0, sum(part(d, owner) for d in decedents)) for owner in losses}


def _share(losses: Mapping[str | None, int], ended: Mapping[str | None, float]) -> float:
    """The part of a carryover built from ``losses`` that ``ended``."""
    total = sum(losses.values())
    return sum(amount * ended[owner] for owner, amount in losses.items()) / total if total else 0.0


def _left(losses: Mapping[str | None, int], ended: Mapping[str | None, float]) -> dict[str | None, int]:
    """``losses`` less what ``ended``, so a later death's share is of what's left."""
    return {owner: round(amount * (1 - ended[owner])) for owner, amount in losses.items()}


_HSA_DIVIDENDS = registries.HSA_PAYOUT_ACCOUNTS["ordinary"].name
_HSA_GAIN_DISTRIBUTIONS = registries.HSA_PAYOUT_ACCOUNTS["ltcg"].name


def state_taxes_hsa(ty: TaxYear, state: str, year: int) -> bool:
    """Whether ``state``'s ``state_hsa`` table, as of ``year``'s end, says it
    taxes an HSA (#490); without a table it follows the federal rules."""
    rows = ty.table_as_of(registries.STATE_HSA, state, year)
    if not rows:
        return False
    return rows[0].get("treatment") == "taxable"


def hsa_flow_years(ty: TaxYear) -> list[int]:
    """The years up to this one with money moving into or out of an HSA, or
    a withdrawal's gain (#497): one untaxed federally has no income item."""
    return sorted({i.year for i in ty.items
                   if i.year <= ty.year and "hsa" in (i.payer.subkind, i.payee.subkind)}
                  | {g.year for g in ty.hsa_gains if g.year <= ty.year})


def _hsa_payouts(items: Iterable[IncomeItem], payer_name: str) -> Iterator[IncomeItem]:
    """The ``items`` that ``payer_name``, an HSA payout account, paid into an
    HSA lot."""
    return (item for item in items if item.payer.name == payer_name and item.payee.subkind == "hsa")


def hsa_dividends(ty: TaxYear) -> dict[str | None, int]:
    """The year's dividends and interest reinvested in HSA lots (#497), by
    the HSA's owner: untaxed federally, ordinary income to a state that
    taxes an HSA."""
    out: dict[str | None, int] = {}
    for item in _hsa_payouts(ty.year_items(), _HSA_DIVIDENDS):
        out[item.payee.owner] = out.get(item.payee.owner, 0) + item.amount
    return out


def hsa_gains(ty: TaxYear, years: Collection[int]) -> Iterator[tuple[int, str, str | None, int]]:
    """An HSA's capital gains in each of ``years`` up to this one (#497), as
    ``(year, character, owner, amount)``: its withdrawals' gains over basis
    (a loss negative), and its reinvested long-term gain distributions."""
    years = {year for year in years if year <= ty.year}
    for gain in ty.hsa_gains:
        if gain.year in years:
            yield gain.year, gain.character, gain.owner, gain.amount
    for item in _hsa_payouts(ty.items, _HSA_GAIN_DISTRIBUTIONS):
        if item.year in years:
            yield item.year, "ltcg", item.payee.owner, item.amount


def hsa_gain_distributions(ty: TaxYear) -> int:
    """The year's long-term gain distributions reinvested in HSA lots
    (#497): gain to a state that taxes an HSA, but none drawn from a lot."""
    return sum(i.amount for i in _hsa_payouts(ty.year_items(), _HSA_GAIN_DISTRIBUTIONS))


def hsa_deferred_flows(ty: TaxYear) -> tuple[int, int]:
    """The year's HSA contributions (``tax_deferred_in``), which the federal
    rules deduct, and withdrawals (``tax_deferred_out``), which they tax: a
    state that taxes an HSA undoes both."""
    contributed = sum(i.amount for i in ty.year_items("tax_deferred_in") if i.payee.subkind == "hsa")
    withdrawn = sum(i.amount for i in ty.year_items("tax_deferred_out") if i.payer.subkind == "hsa")
    return contributed, withdrawn


def taxable_social_security(
    *, ordinary_income: int, ltcg_income: int, ss_income: int, tier1: int, tier2: int,
    exempt_interest: int = 0,
) -> int:
    """Approximate taxable Social Security amount under provisional-income
    rules, which count tax-exempt interest (IRC §86(b)(2)(B), #265)."""
    provisional = ordinary_income + ltcg_income + exempt_interest + (ss_income // 2)
    if provisional <= tier1:
        return 0
    if provisional <= tier2:
        return min(ss_income // 2, (provisional - tier1) // 2)
    base = min(ss_income // 2, (tier2 - tier1) // 2)
    addl = ((provisional - tier2) * 85) // 100
    return min((ss_income * 85) // 100, base + addl)


def state_ss_exclusion(row: Mapping[str, Any], taxable_ss: int, agi: int) -> int:
    """How much of ``taxable_ss``, the federally taxable Social Security, a
    state leaves out by its ``state_social_security`` ``row`` (#484), at a
    federal AGI of ``agi``: ``exclusion_percent`` of it, phased out above
    ``phaseout_start`` to nothing at ``phaseout_end`` (at once when they are
    equal), or less ``phaseout_rate`` of each dollar over."""
    full = taxable_ss * int(row["exclusion_micros"]) // 1_000_000
    start = row.get("phaseout_start")
    if start is None or agi <= int(start):
        return full
    over = agi - int(start)
    rate = row.get("phaseout_rate_micros")
    if rate is not None:
        return max(0, full - over * int(rate) // 1_000_000)
    span = int(row["phaseout_end"]) - int(start)
    if over >= span:
        return 0
    return full * (span - over) // span


def progressive_tax(taxable_income: int, brackets: Sequence[tuple[int, int]]) -> int:
    """The tax on ``taxable_income`` by ``brackets``, (floor, rate micros)
    pairs in floor order."""
    if taxable_income <= 0 or not brackets:
        return 0
    tax = 0
    for i, (floor, rate_micros) in enumerate(brackets):
        if taxable_income <= floor:
            continue
        next_floor = brackets[i + 1][0] if i + 1 < len(brackets) else None
        upper = taxable_income if next_floor is None else min(taxable_income, next_floor)
        taxable_slice = max(0, upper - floor)
        if taxable_slice > 0:
            tax += (taxable_slice * rate_micros) // 1_000_000
    return tax


def split_cents(amount: int, ways: int) -> list[int]:
    """``amount`` in ``ways`` shares that differ by at most a cent."""
    share, extra = divmod(amount, ways)
    return [share + (i < extra) for i in range(ways)]


class ExclusionGrant(NamedTuple):
    """One person's exclusion (#486), before the household income is known."""
    person: str
    income: tuple[str, ...]
    """The ``STATE_EXCLUSION_INCOME`` kinds it covers, in the order it uses them."""
    cap: int | None
    """The most it excludes; None for all of it."""
    income_limit: int | None
    phaseout: int | None

    def after_income_test(self, amount: int, income: int) -> int:
        """How much of the ``amount`` it covers it grants, once household
        ``income`` is known: all of it up to ``income_limit``, then nothing
        (a cliff) or less in a line to nothing ``phaseout`` above it."""
        if self.income_limit is None or income <= self.income_limit:
            return amount
        if not self.phaseout:
            return 0
        return amount * max(0, self.income_limit + self.phaseout - income) // self.phaseout


class RetirementExclusion(NamedTuple):
    """A tax year's state retirement income exclusions (#486): each person's
    income by kind, the grants in the order they use it, and the year's net
    capital gain, which caps what they leave out as gain."""
    pools: Mapping[str, Mapping[str, int]] = {}
    grants: tuple[ExclusionGrant, ...] = ()
    net_gain: int = 0

    def excluded(self, ordinary: int, ltcg: int) -> tuple[int, int]:
        """How much of ``ordinary`` income and ``ltcg`` the exclusions take
        off (``split``, without its short-term part)."""
        return self.split(ordinary, ltcg)[:2]

    def split(self, ordinary: int, ltcg: int,
              net_gain: int | None = None) -> tuple[int, int, int]:
        """How much of ``ordinary`` income and ``ltcg`` the exclusions take
        off, with household income ``ordinary + ltcg`` for their income
        tests, and how much of the ordinary part is short-term gain;
        ``net_gain`` stands in for the year's net gain (an estimate with
        less gain than was posted). A grant uses up only the income it
        actually leaves out, so a later one can cover what an earlier one's
        test denied. Gain beyond the net capital gain (a loss offset it)
        isn't left out at all; the rest comes off the long-term gain first,
        and never more than there is."""
        if not self.grants:
            return 0, 0, 0
        income = ordinary + ltcg
        pools = {person: dict(by_kind) for person, by_kind in self.pools.items()}
        total = gains = 0
        gain_limit = max(0, self.net_gain if net_gain is None else net_gain)
        for grant in self.grants:
            left = pools[grant.person]
            # Gain past what losses left of the net gain isn't income to
            # cover, so it mustn't use up a grant's cap.
            net_left = max(0, gain_limit - gains)
            covered = [(kind, min(left[kind], net_left) if kind == "capital_gain" else left[kind])
                       for kind in grant.income]
            eligible = sum(amount for _, amount in covered)
            if grant.cap is not None:
                eligible = min(eligible, grant.cap)
            granted = grant.after_income_test(eligible, income)
            total += granted
            for kind, amount in covered:
                take = min(amount, granted)
                left[kind] -= take
                granted -= take
                gains += take if kind == "capital_gain" else 0
        total = min(total, max(0, income))
        # When income is below the gain left out (a business loss), the
        # total is all gain and none of it comes off ordinary income.
        off_ordinary = max(0, min(total - min(gains, max(0, ltcg)), max(0, ordinary)))
        off_ltcg = min(total - off_ordinary, max(0, ltcg))
        # Gain left out past the long-term gain is short-term gain, which is
        # part of ordinary income: a state's short-term rates (#485) skip it.
        return off_ordinary, off_ltcg, min(max(0, gains - off_ltcg), off_ordinary)


_EXCLUSION_POOL_BY_CHARACTER: dict[str, tuple[str, int]] = {
    "tax_deferred_out": ("retirement_account", 1),
    "roth_nonqualified": ("retirement_account", 1),
    "stcg": ("capital_gain", 1),
    "ltcg": ("capital_gain", 1),
    registries.QUALIFIED_DIVIDEND: ("dividend", 1),
    "tax_deferred_in": ("other", -1),
    registries.BUSINESS_INCOME: ("other", 1),
    registries.BUSINESS_EXPENSE: ("other", -1),
}
"""Which ``STATE_EXCLUSION_INCOME`` kind a taxed character's items are, and
their sign. ``ordinary`` goes by its income account's subkind instead."""


def _shares(owner: str | None, amount: int, people: Sequence[str],
            pools: Mapping[str, Any]) -> list[tuple[str, int]]:
    """``amount`` as its owner's, or split evenly among ``people`` when no
    one of them owns it alone."""
    if owner is not None and owner in pools:
        return [(owner, amount)]
    return list(zip(people, split_cents(amount, len(people)), strict=True))


def person_income(
    ty: TaxYear, people: Sequence[str], *, hsa_taxed: bool = False,
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """Each person's income this year by ``STATE_EXCLUSION_INCOME`` kind
    (never below zero), and their Social Security. An item is whose
    :func:`_owner` says; the household's is split evenly among ``people``. An
    HSA's flows are ``other`` income, not a retirement account's, and none
    at all when the state taxes the HSA as a brokerage account
    (``hsa_taxed``, #490); then its dividends are its owner's, and its
    gains their capital gain (#497)."""
    pools = {p: dict.fromkeys(registries.STATE_EXCLUSION_INCOME, 0) for p in people}
    ss = dict.fromkeys(people, 0)
    # Items of the same kind of account and owners are summed before they
    # are split, so each sum loses at most a cent to the split.
    groups: dict[tuple[Any, ...], int] = {}
    for item in ty.year_items("ordinary", "ss_benefit", *_EXCLUSION_POOL_BY_CHARACTER):
        key = (item.character, item.payer.kind, item.payer.subkind, item.payer.owner,
               item.payee.owner, item.payer.hobby, item.payee.subkind)
        groups[key] = groups.get(key, 0) + item.amount
    for (character, kind, subkind, payer, payee, hobby, payee_subkind), amount in groups.items():
        hsa = (subkind if character == "tax_deferred_out" else payee_subkind) == "hsa"
        if hsa and character in ("tax_deferred_out", "tax_deferred_in") and hsa_taxed:
            continue  # the state left these out of its income (#490)
        if character == registries.BUSINESS_EXPENSE and hobby:
            continue  # a hobby's expenses aren't deducted (#231), so its receipts stay income
        if character == "ordinary":
            pool, sign = (subkind if kind == "income" and subkind in
                          ("pension", "interest", "dividend") else "other"), 1
        elif character == "ss_benefit":
            pool, sign = "ss", 1
        else:
            pool, sign = _EXCLUSION_POOL_BY_CHARACTER[character]
            if hsa and character == "tax_deferred_out":
                pool = "other"
        for person, share in _shares(_owner(payer, payee, pools, payer_kind=kind), amount, people, pools):
            if pool == "ss":
                ss[person] += share
            else:
                pools[person][pool] += sign * share
    if hsa_taxed:
        # The state taxes an HSA like a brokerage account (#497): its
        # dividends and interest, and its gains at withdrawal, are its
        # owner's, a loss offsetting their other gains.
        for owner, amount in hsa_dividends(ty).items():
            for person, share in _shares(owner, amount, people, pools):
                pools[person]["dividend"] += share
        for _year, _character, owner, amount in hsa_gains(ty, [ty.year]):
            for person, share in _shares(owner, amount, people, pools):
                pools[person]["capital_gain"] += share
    # A realized loss recorded apart from its sale (#199) offsets its lot
    # owner's gains, as the household's netting does.
    losses: dict[str | None, int] = {}
    for loss in ty.losses:
        if loss.year == ty.year:
            losses[loss.owner] = losses.get(loss.owner, 0) + loss.amount
    for owner, amount in losses.items():
        for person, share in _shares(owner, amount, people, pools):
            pools[person]["capital_gain"] -= share
    for by_kind in pools.values():
        for pool, amount in by_kind.items():
            by_kind[pool] = max(0, amount)
    return pools, ss


def _exclusion_age_months(row: Mapping[str, Any]) -> int:
    """A ``state_retirement_exclusion`` row's ``min_age`` in months (59.5:
    714); blank is any age."""
    age = row.get("min_age")
    return 0 if age in (None, "") else round(float(age) * 12)


def _exclusion_income(row: Mapping[str, Any]) -> list[str]:
    """The income kinds a ``state_retirement_exclusion`` row covers, in the
    order it uses them up; ``all`` is every kind."""
    kinds: list[str] = []
    for word in str(row.get("income") or "").replace(",", ";").split(";"):
        word = word.strip()
        for income in registries.STATE_EXCLUSION_INCOME if word == "all" else (word,):
            if income and income not in kinds:
                kinds.append(income)
    return kinds


def _yes(value: Any) -> bool:
    """A yes/no cell (``less_ss``, ``per_person``): ``yes`` or ``true``."""
    return str(value or "").strip().lower() in ("yes", "true")


def _exclusion_less_ss(row: Mapping[str, Any]) -> bool:
    return _yes(row.get("less_ss"))


def age_months(born: date | None, year: int) -> int | None:
    """Whole months of age on December 31 of ``year``; None without a birthdate."""
    return None if born is None else (year - born.year) * 12 + 12 - born.month


def household(ty: TaxYear) -> dict[str, date | None]:
    """The household's people and birthdates: role ``primary`` or
    ``secondary``, as a strategy's household is, or everyone with a
    birthdate if no one is marked."""
    return {
        p.id: (date.fromisoformat(p.birthdate) if p.birthdate else None)
        for p in ty.persons if p.role in registries.PERSON_ROLES
    } or {
        p.id: date.fromisoformat(p.birthdate) for p in ty.persons if p.birthdate
    }


def retirement_exclusion(
    ty: TaxYear, kind: str, state: str, *, net_gain: int, hsa_taxed: bool = False,
) -> RetirementExclusion:
    """``state``'s retirement income exclusions for the year (#486), from
    the ``kind`` table in force (``state_retirement_exclusion__<state>``).

    Each of the household's people gets, of each exclusion (rows sharing a
    ``name``, applied in file order), the age tier with the highest
    ``min_age`` they reach by December 31. It covers their income of the
    kinds it lists, in that order, up to its ``amount`` (all of it when
    blank, and less their Social Security when ``less_ss``).
    ``RetirementExclusion.excluded`` applies the household income tests and
    uses the income up."""
    rows = [
        r for r in ty.indexed(kind, state)
        if not r.get("filing_status") or r.get("filing_status") == ty.filing_status
    ]
    if not rows:
        return RetirementExclusion()
    birthdates = household(ty)
    people = sorted(birthdates)
    if not people:
        return RetirementExclusion()
    pools, ss = person_income(ty, people, hsa_taxed=hsa_taxed)
    tiers: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        tiers.setdefault(str(row["name"]), []).append(row)
    grants: list[ExclusionGrant] = []
    for name_rows in tiers.values():
        for person in people:
            born = birthdates[person]
            # Whole months of age on December 31.
            months = age_months(born, ty.year)
            reached = [
                r for r in name_rows
                if months is not None and months >= _exclusion_age_months(r)
                or _exclusion_age_months(r) == 0
            ]
            if not reached:
                continue
            row = max(reached, key=_exclusion_age_months)
            cap = row.get("amount")
            if cap is not None and _exclusion_less_ss(row):
                cap = max(0, int(cap) - ss[person])
            limit, phaseout = row.get("income_limit"), row.get("phaseout")
            grants.append(ExclusionGrant(
                person, tuple(_exclusion_income(row)),
                None if cap is None else int(cap),
                None if limit is None else int(limit),
                None if phaseout is None else int(phaseout)))
    return RetirementExclusion(pools, tuple(grants), net_gain)


# -- Tables every jurisdiction reads the same way (#508) ----------------------

def where(ty: TaxYear) -> str:
    """The jurisdiction's file, for an error."""
    return f"{registries.TAXES_DIR}/{ty.jurisdiction}.csv"


def loss_limit(ty: TaxYear) -> Callable[[int], int]:
    """Each year's capital-loss limit (``capital_loss_limit``), for
    :func:`capital_gains`."""
    status_aux = str(getattr(ty.params, "filing_status_aux", None) or "filing_status")

    def limit(year: int) -> int:
        # A carryover from a year before the filing_status table starts (a
        # year-to-date loss) is limited as if filed with this year's status.
        status = ty.filing_status if year == ty.year or not ty.table_as_of(
            status_aux, None, year) else ty.filing_status_in(year)
        row = ty.keyed("capital_loss_limit", "filing_status", status, year)
        if row is None:
            raise TaxError(
                f"{where(ty)}: {year} has a net capital loss, but no "
                f"capital_loss_limit row for filing_status={status!r} effective on "
                f"or before {year}-12-31 (aux/capital_loss_limit.csv)"
            )
        return int(row["amount"])
    return limit


def bracket_series(ty: TaxYear) -> str | None:
    """The federal ``bracket_series`` param: its brackets' series, if any."""
    series = getattr(ty.params, "bracket_series", None)
    return None if series is None else str(series)


def taxable_muni_interest(ty: TaxYear) -> int:
    """The year's municipal bond interest (#265) a state adds back to
    federal AGI: every state's but its own, by where the household lives on
    December 31, and never a territory's. The District of Columbia adds
    back none."""
    residence = ty.residence_in(ty.year)
    home = None if residence is None else residence.state
    if home in states.TAXES_NO_MUNI_INTEREST:
        return 0
    return sum(i.amount for i in ty.year_items(registries.TAX_EXEMPT)
               if (issuer := registries.MUNI_PAYER_STATE.get(i.payer.name)) != home
               and issuer not in states.MUNI_EXEMPT_EVERYWHERE)


def state_series(ty: TaxYear, year: int | None = None) -> str:
    """The series of the state tables a tax year reads (#509): the state the
    household lives in on December 31 of ``year`` (the tax year's own by
    default), or ``none`` for one without an income tax."""
    residence = ty.residence_in(ty.year if year is None else year)
    if residence is None:
        raise TaxError(
            f"{where(ty)}: the scenario doesn't say where the household lives in "
            f"{ty.year if year is None else year}; set it with `sim tax state` (residence.csv)")
    return states.tax_series(
        residence.state, has_tables=ty.has_table("state_brackets", residence.state))


def rate_table(ty: TaxYear, kind: str | None, series: str | None = None) -> list[tuple[int, int]]:
    """The year's (floor, rate micros) rows of ``kind`` for its filing
    status, in floor order; none for no kind."""
    if kind is None:
        return []
    return sorted(
        (int(r["floor"]), int(r["marginal_rate_micros"]))
        for r in ty.indexed(kind, series)
        if r.get("filing_status") == ty.filing_status
    )


def brackets(ty: TaxYear, kind: str, series: str | None) -> list[tuple[int, int]]:
    """The ordinary brackets, which a jurisdiction can't do without."""
    rows = rate_table(ty, kind, series)
    if not rows:
        raise TaxError(
            f"{where(ty)}: missing brackets kind={kind!r} "
            f"series={series!r} filing_status={ty.filing_status!r}"
        )
    return rows


def standard_deduction(ty: TaxYear, kind: str, series: str | None = None) -> int:
    rows = [r for r in ty.indexed(kind, series) if r.get("filing_status") == ty.filing_status]
    if not rows:
        raise TaxError(f"{where(ty)}: missing standard deduction for filing_status={ty.filing_status!r}")
    return int(rows[0]["amount"])


def itemized_deduction(ty: TaxYear, kind: str) -> int:
    """The itemized total (optional): a dated total, no filing_status split.
    A row is one tax year's total, so it counts for the year it's dated in
    only, never carried into the years after (#370)."""
    rows = [row for effective, row in ty.dated(kind) if effective[:4] == str(ty.year)]
    return int(rows[-1]["amount"]) if rows else 0


def ltcg_exclusion(ty: TaxYear, kind: str | None, series: str | None) -> Callable[[int], int]:
    """A function giving a long-term gain less what the ``kind`` table
    (a state's gain exclusion, #485) leaves out of it."""
    rows = ty.table(kind, series) if kind else []

    def net(gain: int) -> int:
        if not rows or gain <= 0:
            return gain
        row = rows[0]
        excluded = gain * int(row["exclusion_micros"]) // 1_000_000
        if row.get("max_exclusion") is not None:
            excluded = min(excluded, int(row["max_exclusion"]))
        excluded = max(excluded, int(row.get("min_exclusion") or 0))
        return gain - min(excluded, gain)
    return net


def nol_limit_percent(ty: TaxYear, default: int) -> int:
    """How much of taxable income a carried-in NOL may offset (#231)."""
    given = getattr(ty.params, "nol_limit_percent", None)
    percent = default if given is None else int(given)
    if not 0 <= percent <= 100:
        raise TaxError(f"{where(ty)}: nol_limit_percent must be 0 to 100, got {percent}")
    return percent


def nol_new(*, agi: int, business_net: int, capital_loss_used: int, deduction: int) -> int:
    """The year's new NOL: the business loss less the income it absorbed.
    The deduction never adds to it (a nonbusiness deduction offsets only
    nonbusiness income, §172(d)(4)), nor does the capital-loss deduction."""
    nonbusiness_income = agi - business_net + capital_loss_used
    return max(0, -business_net - max(0, nonbusiness_income - deduction))


def nol_lapse_share(ty: TaxYear) -> float:
    """The part of the NOL carried out of the year that ends with a death in
    it (#277; Treas. Reg. §1.172-7): a business's loss is its proprietor's,
    the ``business`` account's ``owner``. Their share is their part of the
    business losses since the NOL carried last ran out, less what earlier
    deaths ended; with one business, that's all of it. Each owner's
    businesses net together as :func:`business_result` nets them."""
    household = ty.household
    if not household.died_in(ty.year):
        return 0.0
    people = {p.id for p in ty.persons}
    nets: dict[int, dict[str | None, int]] = {}
    items = (i for i in ty.items if i.year <= ty.year)
    for item, account, added, _not_deducted in _business_legs(ty, items):
        by_owner = nets.setdefault(item.year, {})
        owner = _owner(account.owner, None, people)
        by_owner[owner] = by_owner.get(owner, 0) + added
    # The years whose losses the NOL carried into this one holds: back to
    # the last that carried none out.
    since = ty.year - 1
    while since >= min(nets, default=ty.year) and ty.line_in("nol_carryover", since) > 0:
        since -= 1
    losses: dict[str | None, int] = {}
    for year in range(since + 1, ty.year + 1):
        for owner, net in nets.get(year, {}).items():
            if net < 0:
                losses[owner] = losses.get(owner, 0) - net
        if year < ty.year and household.died_in(year):
            losses = _left(losses, _ended(household, year, losses))
    return _share(losses, _ended(household, ty.year, losses))


def nol_carried(carryover: int, lapse_share: float) -> dict[str, int]:
    """The NOL lines out of a year: what carries on, and what ended with a
    death (``lapse_share``, :func:`nol_lapse_share`), a line only in that
    year."""
    lapsed = round(carryover * lapse_share)
    return {"nol_carryover": carryover - lapsed, **({"nol_lapsed": lapsed} if lapsed else {})}


def nol_used(nol_in: int, before_nol: int, limit_percent: int) -> int:
    """The carried-in NOL taxable income ``before_nol`` uses. More income
    can use more of it under its limit, so a what-if works it out again."""
    return min(nol_in, max(0, before_nol) * limit_percent // 100)


def stacked_tax(
    taxable: int, ltcg: int, stcg: int, brackets: Sequence[tuple[int, int]],
    ltcg_brackets: Sequence[tuple[int, int]] = (), stcg_brackets: Sequence[tuple[int, int]] = (),
) -> int:
    """The brackets' tax on ``taxable`` income, ``ltcg`` of it long-term
    gain and up to ``stcg`` more of it short-term gain. Long-term gains sit
    on top of ordinary taxable income and are taxed at the preferential
    rates for the band each part lands in; a state's short-term gain rates
    (#485) take the slice just below them. A gain without a table of its
    own keeps its place, at the ordinary rates."""
    if not (ltcg_brackets or stcg_brackets):
        return progressive_tax(taxable, brackets)
    gain_taxable = min(max(0, ltcg), taxable)
    below_gain = taxable - gain_taxable
    short_taxable = min(stcg, below_gain) if stcg_brackets else 0
    ordinary_taxable = below_gain - short_taxable
    tax = progressive_tax(ordinary_taxable, brackets)
    if short_taxable > 0:
        tax += band_tax(ordinary_taxable, below_gain, stcg_brackets)
    if gain_taxable > 0:
        tax += band_tax(below_gain, taxable, ltcg_brackets or brackets)
    return tax


def ordinary_taxable(taxable: int, ltcg: int, stcg: int, separate_stcg: bool) -> int:
    """Taxable income less the long-term gain stacked on top and, when it is
    taxed apart (``separate_stcg``), the short-term gain below it: what the
    ordinary brackets tax."""
    below_gain = taxable - min(ltcg, taxable)
    return below_gain - (min(stcg, below_gain) if separate_stcg else 0)


def band_tax(low: int, high: int, table: Sequence[tuple[int, int]]) -> int:
    return progressive_tax(high, table) - progressive_tax(low, table)


# -- A state's own deductions and credits (#511, #488) ------------------------

_ADJUSTMENT_INCOME_CHARACTERS: tuple[str, ...] = (
    "ordinary", "tax_deferred_out", "roth_nonqualified", registries.BUSINESS_INCOME,
)
"""The characters of the tagged income a ``state_adjustments`` row with
base ``income`` counts: what a state taxes as ordinary income (a pension,
a retirement account's distribution, an early Roth one, business
receipts)."""


@dataclass(frozen=True)
class StateAdjustment:
    """One ``state_adjustments`` line for the year (#511): its figure before
    the household income test, which :meth:`after_income_test` applies."""
    name: str
    step: str
    base: str
    figure: int
    """What the row grants before the income test, from its fixed base
    (spending, income, filers); for a federal base, its rate and cap
    applied by :meth:`from_federal`."""
    rate_micros: int | None
    cap: int | None
    income_limit: int | None
    phaseout: int | None
    phaseout_rate_micros: int | None
    units: int = 1
    """How many exemptions a ``filers`` line is: ``phaseout_rate`` takes its
    share of each dollar over from each, as California's does."""

    def from_federal(self, amount: int) -> int:
        """The figure for a ``federal_tax`` or ``federal_deduction`` base."""
        value = max(0, amount)
        if self.rate_micros is not None:
            value = value * self.rate_micros // 1_000_000
        return value if self.cap is None else min(value, self.cap)

    def after_income_test(self, figure: int, income: int) -> int:
        """``figure`` less what the household's ``income`` takes away: all of
        it over ``income_limit``, in a straight line to nothing over
        ``phaseout`` dollars, or ``phaseout_rate`` of each dollar over."""
        if self.income_limit is None or income <= self.income_limit:
            return figure
        over = income - self.income_limit
        if self.phaseout_rate_micros is not None:
            return max(0, figure - over * self.phaseout_rate_micros * self.units // 1_000_000)
        if self.phaseout:
            return max(0, figure - figure * over // self.phaseout)
        return 0


def state_adjustments(ty: TaxYear, kind: str, state: str) -> list[StateAdjustment]:
    """``state``'s own deductions and credits for the year (#511, #488),
    from the ``kind`` table in force (``state_adjustments__<state>``): one
    per ``name``, the row for the year's filing status (or a blank one).
    Each row's base is figured here, except a federal one, which needs the
    federal return of a what-if."""
    rows = [r for r in ty.indexed(kind, state)
            if not r.get("filing_status") or r.get("filing_status") == ty.filing_status]
    # A row for the filing status wins over a blank one of the same name.
    chosen: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["name"])
        if name not in chosen or (row.get("filing_status") and not chosen[name].get("filing_status")):
            chosen[name] = row
    birthdates = household(ty)
    # A return has two filers only when joint: after a death the survivor
    # files alone (which of the two the model can't tell, so their ages
    # count as the household's).
    filers = 2 if ty.filing_status == "MFJ" else 1
    out = []
    for name, row in chosen.items():
        min_months = _exclusion_age_months(row)
        eligible = min(filers, sum(
            1 for born in birthdates.values()
            if min_months == 0
            or (age_months(born, ty.year) or 0) >= min_months))
        amount = None if row.get("amount") is None else int(row["amount"])
        base, rate = str(row["base"]), row.get("rate_micros")
        cap = amount if amount is None or not _yes(row.get("per_person")) else amount * eligible
        if base == "filers":
            figure = (amount or 0) * eligible
        elif base in ("spending", "income"):
            tag = str(row.get("tag") or "")
            raw = ty.spent(tag) if base == "spending" else sum(
                i.amount for i in ty.tagged(tag) if i.character in _ADJUSTMENT_INCOME_CHARACTERS)
            figure = max(0, raw) if rate is None else max(0, raw) * int(rate) // 1_000_000
            figure = figure if cap is None else min(figure, cap)
        else:
            figure = 0
        if eligible == 0 and birthdates:
            figure, cap = 0, 0  # no filer has reached its age
        limit, phaseout, phaseout_rate = (row.get("income_limit"), row.get("phaseout"),
                                          row.get("phaseout_rate_micros"))
        out.append(StateAdjustment(
            name, str(row["step"]), base, figure,
            None if rate is None else int(rate), cap,
            None if limit is None else int(limit), None if phaseout is None else int(phaseout),
            None if phaseout_rate is None else int(phaseout_rate),
            units=eligible if base == "filers" else 1))
    return out
