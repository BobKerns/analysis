"""What a budget's import does before its rules are written (issue #617):
gathers the budget's categories into the plan's, and says how each is paid,
every month or in the month or two of the year it falls in.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from analysis.sim.imports.forms import Budget, BudgetLine

__all__ = ["YEARLY_MONTHS", "Gathered", "Planned", "gather", "plan"]

YEARLY_MONTHS = 2
"""A category budgeted in no more months of a year than this is paid in
those months, once a year each; in more, every month at its average."""
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


def _target(line: BudgetLine, merges: Mapping[str, str], exclusions: Collection[str]) -> str | None:
    """The plan's category for ``line``, or None when the map leaves it out.

    The map's row for the line's whole path wins, then one for each category
    it is under, nearest first: a row for ``Food`` covers all that is under
    it. A row names a category by its path from the top, so one for the top
    category ``Insurance`` says nothing of ``Auto:Insurance``. With no row, a
    line is its top category's, since a budget's top level is about what a
    plan wants.
    """
    parts = line.category.split(":")
    for depth in range(len(parts), 0, -1):
        name = ":".join(parts[:depth])
        if name in exclusions:
            return None
        if name in merges:
            return merges[name]
    return parts[0]


def gather(budget: Budget, merges: Mapping[str, str], exclusions: Collection[str]) -> Gathered:
    """``budget``'s lines added up by the plan's categories (``merges`` and
    ``exclusions`` are the source's ``categories.csv``)."""
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
        target = _target(line, merges, exclusions)
        if target is None:
            found.excluded.append(line.category)
            continue
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
