"""What a budget's import does before its rules are written (issue #617):
gathers the budget's categories into the plan's, and says how each is paid,
every month or in the month or two of the year it falls in.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from analysis.expenses.baseline import YEARLY_MONTHS
from analysis.sim.imports.forms import Budget, BudgetLine
from analysis.sim.imports.mapping import BudgetChoice

__all__ = ["MOVED", "YEARLY_MONTHS", "Compared", "Gathered", "Planned", "compare", "gather", "plan"]

# A budget's rows for money moved between the household's own accounts.
_TRANSFER = re.compile(r"^(FROM|TO)\s")
# The scenario models a mortgage itself, as the transactions' baseline holds.
_MODELED = re.compile(r"mortgage", re.IGNORECASE)
# Income tax, withheld or paid as estimates: the scenario's tax rules work
# it out and pay it, so a rule for it too would pay it twice. Property and
# sales tax are spending, and no rule of the scenario pays the payroll taxes.
_INCOME_TAX = re.compile(
    r"\btax(es)?\b.*\b(fed|federal|state|income|estimated|withholding)\b"
    r"|\b(fed|federal|state|income|estimated|withholding)\b.*\btax(es)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class Planned:
    """One rule a budget makes."""

    category: str
    income: bool
    amount_cents: int
    """What is paid each time: a month's average, or a yearly payment."""
    month: int | None = None
    """The month of the year a yearly payment falls in; None for monthly."""
    several: bool = False
    """Whether the category has another yearly payment, so its rule's name
    needs the month."""


@dataclass
class Gathered:
    """A budget's lines by the plan's categories."""

    spending: dict[str, list[int]] = field(default_factory=dict)
    """Each category's months, or its one total when the budget has no months."""
    income: dict[str, list[int]] = field(default_factory=dict)
    transfers: list[str] = field(default_factory=list)
    """Lines left out as moves between the household's own accounts."""
    modeled: list[str] = field(default_factory=list)
    """Lines left out because the scenario models them itself (a mortgage)."""
    taxes: list[str] = field(default_factory=list)
    """Income tax lines left out: the scenario's tax rules pay it. A line
    with a row of its own in the category map is kept."""
    excluded: list[str] = field(default_factory=list)
    """Lines the category map leaves out."""


def _target(
    line: BudgetLine, merges: Mapping[str, str], exclusions: Collection[str],
    defaulted: Collection[str] = (), default: str | None = None,
) -> str | None:
    """The plan's category for ``line``, or None when the map leaves it out.

    The map's row for the line's whole path wins, then one for each category
    it is under, nearest first: a row for ``Food`` covers all that is under
    it. A row names a category by its path from the top, so one for the top
    category ``Insurance`` says nothing of ``Auto:Insurance``. With no row, a
    line is its top category's, since a budget's top level is about what a
    plan wants; unless it is one of ``defaulted``, the categories that
    transactions set beside the budget put in ``default`` for want of a
    row, when it goes there with them.
    """
    parts = line.category.split(":")
    paths = [":".join(parts[:depth]) for depth in range(len(parts), 0, -1)]
    for name in paths:
        if name in exclusions:
            return None
        if name in merges:
            return merges[name]
    if default is not None and any(name in defaulted for name in paths):
        return default
    return parts[0]


def gather(
    budget: Budget, merges: Mapping[str, str], exclusions: Collection[str], *,
    splits: Mapping[str, Mapping[str, str]] | None = None,
    defaulted: Collection[str] = (), default: str | None = None,
) -> Gathered:
    """``budget``'s lines added up by the plan's categories (``merges`` and
    ``exclusions`` are the source's ``categories.csv``).

    The rest is for a budget set beside transactions, so that the two are
    in the same categories and neither is counted beside the other. A
    budget's line has no account, so a category in ``splits`` (the
    transactions' ``splits.csv``) takes the split's category for a row no
    named account claims, as Amazon's summaries do. A line with no row
    whose category the transactions put in ``default`` (``defaulted``)
    goes there too.
    """
    found = Gathered()
    for line in budget.lines:
        top = line.category.split(":")[0]
        if _TRANSFER.match(top):
            found.transfers.append(line.category)
            continue
        if not line.income and _MODELED.search(line.category):
            found.modeled.append(line.category)
            continue
        if not line.income and _INCOME_TAX.search(line.category) and line.category not in merges:
            found.taxes.append(line.category)
            continue
        target = _target(line, merges, exclusions, defaulted, default)
        if target is None:
            found.excluded.append(line.category)
            continue
        target = (splits or {}).get(target, {}).get("", target)
        amounts = list(line.monthly_cents) or [line.total_cents]
        into = (found.income if line.income else found.spending).setdefault(target, [0] * len(amounts))
        for at, amount in enumerate(amounts):
            into[at] += amount
    return found


def plan(
    amounts: Mapping[str, list[int]], months: tuple[dt.date, ...], *, income: bool = False,
) -> tuple[list[Planned], list[str]]:
    """The rules for each category's ``amounts``, and the categories with
    nothing to pay (a budget of zero, or a refund).

    A whole year's budget that has a category in only a month or two
    (``YEARLY_MONTHS``) is a yearly payment in each; anything else is paid
    every month at the period's average.
    """
    planned: list[Planned] = []
    nothing: list[str] = []
    for category in sorted(amounts):
        each = amounts[category]
        total = sum(each)
        if total <= 0:
            nothing.append(category)
            continue
        paid = [(months[at].month, amount) for at, amount in enumerate(each) if amount] \
            if len(each) == len(months) == 12 else []
        if paid and len(paid) <= YEARLY_MONTHS and all(amount > 0 for _, amount in paid):
            planned.extend(
                Planned(category, income, amount, month, several=len(paid) > 1) for month, amount in paid)
        else:
            planned.append(Planned(category, income, round(total / (len(months) or 12))))
    return planned, nothing


MOVED = 0.05
"""How far either amount may move from what a saved choice was made
between before the choice is asked again: less is one more month's
transactions, more may be a different answer."""


@dataclass(frozen=True)
class Compared:
    """One category as a budget and the transactions each have it, a month."""

    category: str
    budget_cents: int | None
    """None when the budget has no such category."""
    actual_cents: int | None
    """None when no transaction is in it."""
    saved: str | None = None
    """The saved choice, ``budget`` or ``actual``, where both have it."""
    moved: bool = False
    """Whether an amount has moved more than ``MOVED`` since ``saved`` was
    chosen."""

    @property
    def only(self) -> str | None:
        """``budget`` or ``actual`` when just that one has the category."""
        if self.actual_cents is None:
            return "budget"
        return "actual" if self.budget_cents is None else None


def compare(
    budget: Mapping[str, int], actual: Mapping[str, int], saved: Mapping[str, BudgetChoice],
) -> list[Compared]:
    """Every category of ``budget`` or ``actual`` (each a month's cents), by
    name, with its choice in ``saved`` where both have it. An ``actual``
    of zero is a category whose transactions were all left out: both have
    it, and choosing the transactions gives it no rule."""
    found: list[Compared] = []
    for category in sorted({*budget, *actual}):
        ours, theirs = budget.get(category), actual.get(category)
        was = saved.get(category) if ours is not None and theirs is not None else None
        found.append(Compared(
            category, ours, theirs, was.use if was else None,
            was is not None and ours is not None and theirs is not None and (
                abs(ours - was.budget_cents) > MOVED * abs(was.budget_cents)
                or abs(theirs - was.actual_cents) > MOVED * abs(was.actual_cents))))
    return found
