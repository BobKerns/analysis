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
premium rules fit. When one doesn't (a circuit breaker, figured from
property tax above a share of income, say), you can write a calculator of your own, a small
Python class in your data repo, and have a scenario use it. A calculator
reads what the year's money did and returns the lines of a return; it
never touches the ledger, and a run records the exact code it ran.

This guide works one example through for Alice and Bill, the prototype
scenario's household: the District of Columbia's property tax credit
(Schedule H), as if they retired to a DC rowhouse. It is a "circuit
breaker": the property tax above a share of federal AGI, up to a cap, for a
household below an income limit, and refundable, so DC pays it even when
no tax is due. The share, the limits that depend on age, and the refund
are what a row can't say. The design and its rules are in
[ADR-011](../architecture/ADR-011-tax-calculators.md), and
video 13, *Tax plugins*, walks through it:
[English](https://www.youtube.com/watch?v=rWfsNvoNnVI),
[Japanese (日本語)](https://www.youtube.com/watch?v=tckwO18axvw).

## 1. Say what counts: a tag

A calculator can't tell property tax from a repair by itself. A **tag**
says which is which. Tag the account the property tax is paid into:

```sh
sim account tag add Expense_Property_Tax tax:property
```

or, when other things are paid into the account too, the rule that pays
the tax:

```sh
sim scheduled edit property_tax --tags tax:property
```

A rule's tag refines its account's: in an account tagged `tax:property`, a
rule tagged `tax:repair` is a repair. Income works the same way: tag the
income account a public pension pays from `tax:pension:public`.

## 2. Write the calculator

A calculator lives in the data repo's top-level `taxes/` folder, one
module or more (`taxes/property_credit.py`), shared by every scenario. (A
scenario's own `taxes/` folder holds its `federal.csv`, `state.csv` and
`local.csv`, which say which calculator each tax uses.) This one builds on
the built-in state, `state_tables`, which already has DC's brackets and
standard deduction, and adds the credit:

```python
"""DC's income tax with its property tax credit (Schedule H), the worked
example in docs/guides/tax-calculators.md and video 13: the built-in
``state_tables``, less a refundable credit for property tax above a share
of federal AGI."""

from dataclasses import dataclass

from analysis.sim.tax_api import Dollars, Tag, percent, register
from analysis.sim.taxes.state_tables import StateTables, StateTablesYear

API_VERSION = 4

# Schedule H's worksheet for 2025: up to each federal AGI, the share of it
# the property tax must exceed. Past the last row, no credit.
UNDER_70 = [(24_999_00, percent("3%")),
            (51_999_00, percent("4%")),
            (66_000_00, percent("5%"))]
AGE_70 = [(90_000_00, percent("3%"))]


class PropertyCreditYear(StateTablesYear):
    """One year of DC's tax, with the credit."""

    def setup(self):
        # Once a year: what a what-if's extra income doesn't change.
        super().setup()
        ty, params = self.ty, self.ty.params
        bands = AGE_70 if self.oldest() >= 70 else UNDER_70
        self.bands = [(ty.grown(top, params.series, 2025), share)
                      for top, share in bands]
        self.most = ty.grown(params.most, params.series, 2025)
        self.paid = (0 if params.property_tax is None
                     else ty.spent(params.property_tax))
        if params.rent is not None:
            self.paid += ty.spent(params.rent) // 5  # 20% of rent counts

    def refundable_credits(self, tax, income, basis):
        # Each return, with the federal AGI it builds on.
        agi = basis.federal.lines["agi"]
        share = next((s for top, s in self.bands if agi <= top), None)
        if share is None:
            return {}
        credit = self.paid - agi * share // 1_000_000
        return {"property_credit": max(0, min(credit, self.most))}

    def oldest(self):
        """The older filer's age on December 31: the primary and secondary
        people, less one who died before the year."""
        year = self.ty.year
        return max((year - int(p.birthdate[:4]) for p in self.ty.persons
                    if p.birthdate and p.role in ("primary", "secondary")
                    and (p.final_month is None
                         or int(p.final_month[:4]) >= year)), default=0)


@register
class PropertyCredit(StateTables):
    """``state_tables`` with DC's property tax credit: the year's property
    tax, or 20% of its rent, above a share of federal AGI, up to ``most``,
    paid even when no tax is due. Its 2025 figures grow with ``series``."""
    kind = "property_credit"
    jurisdiction = "state"
    year_class = PropertyCreditYear

    @dataclass(frozen=True)
    class Params(StateTables.Params):
        property_tax: Tag | None = "tax:property"
        """The tag on the property tax paid; none for a renter."""
        rent: Tag | None = None
        """The tag on rent paid, for a renter."""
        most: Dollars = 1425_00
        """The largest credit, in 2025."""
        series: str = "cpi"
        """The inflation series the 2025 figures grow by."""
```

What it does:

- `StateTables` is the built-in state calculator; `PropertyCreditYear` is
  one year of it. Its `setup` runs once for the year, for what a what-if's
  extra income can't change (the spending, the age band, the grown
  figures), since a return may be asked for thousands of times a year
  (`fill_bracket` steps, the after-tax drawdown). It overrides
  `refundable_credits`, run for each return, one of the steps a
  state year goes through (`additions`, `subtractions`, `deductions`,
  `itemized_lines`, `other_deductions`, `tax`, `credits`, `credit_lines`,
  `excise`, `refundable_credits`). Each line it returns is a line of the return,
  written to the run as `state_<line>`. A refundable credit comes off
  last and can take the tax below zero; the April settlement pays it
  back. A credit that can't, like most, goes in `credit_lines` instead.
- The built-in `itemized_lines` figures property tax, mortgage interest,
  charity and medical from tagged spending and adds them to what
  `deductions` returns; a year whose `deductions` figures its own itemized
  total overrides `itemized_lines` to return `None`, or those count twice.
  (A module that overrides `deductions` and says `API_VERSION = 4`, or
  sets no `API_VERSION`, gets no lines, as before they existed; say
  `API_VERSION = 5` to get them.)
- `self.ty` is the tax year as the calculator sees it. `ty.spent(tag)` is
  the year's spending carrying a tag; `ty.grown(amount, series, since)` a
  figure the law set for year `since`, grown with inflation to this one,
  as DC indexes its limits and cap; `ty.persons` the household, with
  their birthdates, roles and, for one who died, the last month alive;
  `ty.params` the calculator's parameters.
- `basis.federal` is the federal return for the same year, which DC's
  credit reads its AGI from. A what-if (more income on top, such as a
  Roth conversion being priced) moves both returns, so the credit
  follows it, the income limit included.
- `Params` extends the built-in state's. Each field has a unit: `Tag`, a
  tag; `Dollars`, written `"1425.00"` and received as cents; `Percent`,
  written `"3%"` and received as millionths; or a plain string or number.
- `@register` makes the class a kind a scenario can name.
  `API_VERSION` says which version of the calculator API it was written
  for.

`sim tax kinds property_credit` shows it, its parameters and their
defaults.

## 3. Use it

Tell the scenario's state tax to use the new kind. Its defaults fit a
homeowner, whose property tax carries `tax:property`:

```sh
sim tax edit state --kind property_credit
```

A renter counts a fifth of the rent instead, and pays no property tax of
its own, so it names its rent's tag and none for property tax:

```sh
sim tax edit state --kind property_credit --params-json '{"property_tax": null, "rent": "tax:rent"}'
```

Loading the scenario checks it: the kind exists and computes the state
tax, each parameter is one it takes, in its unit, and some account, rule
or calendar row carries each tag it reads, `tax:property` for the
homeowner. Without the tag, the load stops, rather than letting the
credit come out zero.
`sim tax edit state --kind ''` goes back to the built-in one.

Run the scenario, and see the state's return, its new line included:

```sh
sim run
sim report tax --return state --start 2030 --end 2036
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
| `grown(amount, series, since)` | a figure set for year `since`, grown by an inflation series to January of this one |
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
