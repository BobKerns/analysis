"""The fields of a ``year_to_date.csv`` row (ADR-003 §4.10), for ``sim ytd
add`` and the guided ``sim ytd edit`` (issue #193).

A row is one fact from before the scenario's first simulated month: money
that already moved, or a tax amount carried in. Its two sides are accounts,
never chains, and its amount may be negative (a loss carried forward). Each
field's ``param`` is its ``year_to_date.csv`` column, so a row's answers and
its cells convert column for column. ``name`` is not among them: it is the
row's key, given to ``add`` and picked by ``edit``.
"""

from __future__ import annotations

from analysis.sim import registries
from analysis.sim.calendar_guide import Answers, Field, KindSpec, lot_asked, lots_of

ROW = KindSpec(
    "add", "a fact from before the scenario starts",
    words=("sim", "ytd"), duplicate="another row",
    fields=(
        Field("from_account", "from account", option="--from-account", strict=True,
              choices=lambda ctx, answers: list(ctx.accounts)),  # noqa: ARG005
        Field("from_lot", "from lot", option="--from-lot", strict=True,
              choices=lots_of("from_account"), applies=lot_asked("from_account")),
        Field("to_account", "to account", option="--to-account", strict=True,
              choices=lambda ctx, answers: list(ctx.accounts),  # noqa: ARG005
              help="The same account on both sides only adjusts a tax total, "
                   "as a loss carried forward does."),
        Field("to_lot", "to lot", option="--to-lot", strict=True,
              choices=lots_of("to_account"), applies=lot_asked("to_account")),
        Field("amount", "amount in dollars, or @itemization", type="money", option="--amount",
              help="Negative for a loss carried in."),
        Field("tax_character", "tax character", option="--tax-character", strict=True,
              choices=lambda ctx, answers: sorted(registries.TAX_CHARACTERS)),  # noqa: ARG005
        Field("year", "year", type="year", option="--year", required=False,
              help="Blank for the scenario's first year; an earlier year for a "
                   "fact of a whole prior tax year."),
        Field("description", "description", option="--description", required=False),
    ),
)

# The columns ROW asks about; with the name, every column of the file.
ROW_COLUMNS: tuple[str, ...] = tuple(field_.param for field_ in ROW.fields)
assert ("name", *ROW_COLUMNS) == registries.YEAR_TO_DATE_CSV_HEADER


def answers_of(row: dict[str, str]) -> Answers:
    """A year_to_date.csv row as answers to ``ROW``'s fields."""
    return {column: row.get(column, "") for column in ROW_COLUMNS}
