"""The fields of a ``guard_rails.csv`` row (ADR-003 §4.11), for ``sim
guard-rail add`` and the guided ``sim guard-rail edit`` (issue #305).

A rail is a line a scenario's spending, or its withdrawals from the
portfolio, should stay on one side of. Each field's ``param`` is its
``guard_rails.csv`` column, so a row's answers and its cells convert column
for column. ``name`` is not among them: it is the row's key, given to
``add`` and picked by ``edit``.
"""

from __future__ import annotations

from collections.abc import Callable

from analysis.sim import registries
from analysis.sim.calendar_guide import Answers, Field, GuideContext, KindSpec

PERCENT_BASES = frozenset({"portfolio", "initial_portfolio"})

_TAG_FILTER_HELP = ("role:name tags, space-separated: any of them picks; -role:name leaves out; "
                    "role:* is any tag in the role.")


def _spending(ctx: GuideContext, answers: Answers) -> bool:  # noqa: ARG001
    return answers.get("measure") == "spending"


def _reads_portfolio(ctx: GuideContext, answers: Answers) -> bool:  # noqa: ARG001
    return answers.get("measure") == "withdrawals" or answers.get("basis") in PERCENT_BASES


def _tags(ctx: GuideContext, answers: Answers) -> list[str]:  # noqa: ARG001
    return list(ctx.tags)


def _markers(ctx: GuideContext, answers: Answers) -> list[str]:  # noqa: ARG001
    return list(ctx.markers)


def _fixed(values: tuple[str, ...]) -> Callable[[GuideContext, Answers], list[str]]:
    return lambda ctx, answers: list(values)  # noqa: ARG005


ROW = KindSpec(
    "add", "a guard rail",
    words=("sim", "guard-rail"), duplicate="another rail",
    fields=(
        Field("measure", "measure", option="--measure", strict=True,
              choices=_fixed(registries.GUARD_RAIL_MEASURES),
              help="spending: every expense but income tax, business expenses and "
                   "depreciation. withdrawals: what the portfolio pays out, less income "
                   "landing in it other than its own returns."),
        Field("select", "spending to count (optional tags)", type="tag_filter", option="--select",
              required=False, applies=_spending, choices=_tags,
              help="Blank counts all spending. " + _TAG_FILTER_HELP),
        Field("side", "past the rail when the measure is", option="--side", strict=True,
              choices=_fixed(registries.GUARD_RAIL_SIDES),
              help="above: the threshold is a ceiling. below: it is a floor."),
        Field("basis", "threshold basis", option="--basis", strict=True,
              choices=_fixed(registries.GUARD_RAIL_BASES),
              help="nominal: dollars a year. cpi_constant: today's dollars a year, growing "
                   "with CPI. portfolio: a percent of the portfolio at each year's start. "
                   "initial_portfolio: a percent of it when the rail starts, then growing with "
                   "CPI (the 4% rule)."),
        Field("threshold", "threshold", type="threshold", option="--threshold",
              type_of=lambda answers: "percent" if answers.get("basis") in PERCENT_BASES
              else "dollars",
              help="A percent such as 4% for basis portfolio or initial_portfolio; "
                   "otherwise dollars a year."),
        Field("sense", "sense", option="--sense", strict=True, required=False,
              default="negative", choices=_fixed(registries.GUARD_RAIL_SENSES),
              help="negative: past the rail is trouble. positive: past it is good news, "
                   "such as room to spend more."),
        Field("portfolio", "portfolio accounts (optional tags)", type="tag_filter",
              option="--portfolio", required=False, applies=_reads_portfolio, choices=_tags,
              help="Blank is every asset account but real estate, vehicles and other "
                   "assets. " + _TAG_FILTER_HELP),
        Field("start", "start", type="date", option="--start", required=False, choices=_markers,
              help="YYYY-MM, or a date_marker (NAME, NAME+Nm, NAME-Ny). Blank: the "
                   "scenario's start."),
        Field("end", "end", type="date", option="--end", required=False, choices=_markers,
              help="As start. Blank: the scenario's end, or the household's last death."),
        Field("description", "description", option="--description", required=False),
    ),
)

# The columns ROW asks about; with the name, every column of the file.
ROW_COLUMNS: tuple[str, ...] = tuple(field_.param for field_ in ROW.fields)
assert {"name", *ROW_COLUMNS} == set(registries.GUARD_RAILS_CSV_HEADER)


def answers_of(row: dict[str, str]) -> Answers:
    """A guard_rails.csv row as answers to ``ROW``'s fields."""
    return {column: row.get(column, "") for column in ROW_COLUMNS}
