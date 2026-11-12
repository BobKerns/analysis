# Tax calculators: your own state's rules

> To read a formatted `.md` file:
> - In Jupyter: right-click on it and select `Open With / Markdown Preview`
> - In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

The simulator computes each income tax, federal, state and local, with a
**tax calculator**. It ships three: `federal`, `state_tables` (a state
that starts from federal AGI and reads its brackets and other tables for
the state you live in) and `local_bands`. When your state has a rule they
don't model (a renter deduction, a public pension exclusion, a credit for
long-term care premiums), first see whether a row of the state's
`state_adjustments` table can say it: a share of tagged spending or
income, up to a cap, for filers from an age, below an income, as a
subtraction, deduction or credit (the code repository's
`templates/aux/README.md` lists the columns). Most renter's, 529 and
premium rules fit. When one doesn't (a circuit breaker figured from
property value, say), you can write a calculator of your own, a small
Python class in your data repo, and have a scenario use it. A calculator
reads what the year's money did and returns the lines of a return; it
never touches the ledger, and a run records the exact code it ran.

This guide works one example through for Alice and Bill, the prototype
scenario's household: a state that takes part of the rent they pay off
their income. The design and its rules are in
[ADR-011](../architecture/ADR-011-tax-calculators.md).

## 1. Say what counts: a tag

A calculator can't tell rent from a repair by itself: both are payments
into `Expense_Housing`. A **tag** says which is which. Tag the rule that
pays the rent (in the prototype scenario, `housing`):

```sh
sim scheduled edit housing --tags tax:rent
```

or, when everything paid into an account counts, the account:

```sh
sim account tag add Expense_Housing tax:rent
```

A rule's tag refines its account's: in an account tagged `tax:rent`, a
rule tagged `tax:repair` is a repair. Income works the same way: tag the
income account a public pension pays from `tax:pension:public`.

## 2. Write the calculator

A calculator lives in the data repo's top-level `taxes/` folder, one
module or more (`taxes/renters.py`), shared by every scenario. (A
scenario's own `taxes/` folder holds its `federal.csv`, `state.csv` and
`local.csv`, which say which calculator each tax uses.) This one builds on
the built-in state, `state_tables`, and adds two subtractions:

```python
"""A state income tax with a renter deduction (the worked example in
docs/guides/tax-calculators.md): the built-in ``state_tables`` state, which
starts from federal AGI, less part of the rent the household paid, up to a
cap, and, if asked, the pension a public employer paid."""

from dataclasses import dataclass

from analysis.sim.tax_api import Dollars, Percent, Tag, percent, register
from analysis.sim.taxes.state_base import Adjustment
from analysis.sim.taxes.state_tables import StateTables, StateTablesYear

API_VERSION = 1


class RentersYear(StateTablesYear):
    """One year of the state: the built-in state's, with two more
    subtractions."""

    def subtractions(self, income, basis):
        params = self.ty.params
        # The year's spending carrying the rent tag: a rule tagged tax:rent,
        # or any payment into an expense account tagged so.
        rent = self.ty.spent(params.rent)
        deduction = min(rent * params.rent_share // 1_000_000, params.rent_cap)
        lines = [*super().subtractions(income, basis),
                 Adjustment("renter_deduction", ordinary=min(deduction, max(0, income.ordinary)))]
        if params.public_pension is not None:
            # Income out of an account (or by a rule) tagged as a public pension.
            pension = sum(item.amount for item in self.ty.tagged(params.public_pension))
            lines.append(Adjustment("public_pension", ordinary=min(pension, max(0, income.ordinary))))
        return lines


@register
class Renters(StateTables):
    """A state like ``state_tables``, less a renter deduction: ``rent_share``
    of the year's rent (spending tagged ``rent``), up to ``rent_cap``; and,
    with ``public_pension``, the pension income tagged so."""
    kind = "state_renters"
    jurisdiction = "state"
    year_class = RentersYear

    @dataclass(frozen=True)
    class Params(StateTables.Params):
        rent: Tag = "tax:rent"
        """The tag on the rent the deduction counts."""
        rent_share: Percent = percent("50%")
        """How much of the rent comes off."""
        rent_cap: Dollars = 4000_00
        """The most the deduction takes off."""
        public_pension: Tag | None = None
        """The tag on a public pension's income, which the state doesn't tax."""
```

What it does:

- `StateTables` is the built-in state calculator; `RentersYear` is one year
  of it, and overrides `subtractions`, one of the steps a state year goes
  through (`additions`, `subtractions`, `deductions`, `itemized_lines`,
  `other_deductions`, `tax`, `credits`, `credit_lines`). The built-in
  `itemized_lines` figures property tax, mortgage interest, charity and
  medical from tagged spending and adds them to what `deductions` returns;
  a year whose `deductions` figures its own itemized total overrides
  `itemized_lines` to return `None`, or those count twice.
  Each `Adjustment` is a line of the return, written to the run as
  `state_<line>`.
- `self.ty` is the tax year as the calculator sees it. `ty.spent(tag)` is
  the year's spending carrying a tag; `ty.tagged(tag)` the year's income
  items carrying one; `ty.params` the calculator's parameters.
- `Params` extends the built-in state's. Each field has a unit: `Tag`, a
  tag; `Percent`, written `"50%"` and received as millionths; `Dollars`,
  written `"4000.00"` and received as cents; or a plain string or number.
- `@register` makes the class a kind a scenario can name.
  `API_VERSION` says which version of the calculator API it was written
  for.

`sim tax kinds state_renters` shows it, its parameters and their defaults.

## 3. Use it

Tell the scenario's state tax to use the new kind, with any parameters:

```sh
sim tax edit state --kind state_renters --params-json '{"rent_cap": "3000.00"}'
```

Loading the scenario checks it: the kind exists and computes the state
tax, each parameter is one it takes, in its unit, and some account, rule
or calendar row carries each tag it reads, `tax:rent` here. Without the
tag, the load stops, rather than letting the deduction come out zero.
`sim tax edit state --kind ''` goes back to the built-in one.

Run the scenario, and see the state's return, its new lines included:

```sh
sim run
sim report tax --return state --start 2027 --end 2029
```

## 4. Check it

A calculator must give the same answer every time it is asked: no clock,
files, network or random numbers, nothing kept from one year to the next,
and no reliance on the order of a set. Otherwise two runs of one scenario
can differ. `sim tax check` runs the scenario twice, each in a fresh
Python process, and compares every line of every return:

```sh
sim tax check
```

Editing a module changes the scenario's data version like any other data
edit, so the runs it affects are rebuilt. A module that fails to import
stops the load of a scenario that names one of its kinds, with its
traceback.

## 5. What a calculator can read

`self.ty`, a tax year, gives:

| | |
| --- | --- |
| `params` | the calculator's `Params`, in cents and millionths |
| `year_items(*characters)`, `total(character)` | the year's income, item by item or summed, by tax character |
| `tagged(tag)` | the year's income items carrying a tag |
| `year_spending(*tags)`, `spent(tag)` | the year's payments into expense accounts, item by item or summed |
| `filing_status`, `persons`, `residence` | the household, and where it lives on December 31 |
| `table(kind)`, `indexed(kind)` | an aux table as of the year's end, or grown to January by its series |
| `prior(line)` | this tax's line from last year's return |
| `upstream("federal")` | the federal return for the same year and the same what-if |

Each income and spending item has its accounts (name, kind, subkind,
owner, tags), its amount in cents, its tax character, its rule's tags and,
for spending, the rule's `person`.

A state calculator usually builds on `StateTables`, as above, or on
`StateFromFederal` or `StateFromIncome` in `analysis.sim.taxes.state_base`
for one that reads its own tables. Its `compute` must be quick: the
simulator asks it many times a year, with more income on top, to price a
Roth conversion or a withdrawal.
