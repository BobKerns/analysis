"""Guard rails (#305): lines a scenario's spending, or its withdrawals from
the portfolio, should stay on one side of, and where each run stands
against them, year by year and cumulatively.

A rail is a row of a scenario's ``guard_rails.csv`` (ADR-003 §4.11),
inherited along the lineage like the other files. Rails are a lens on
results, not an input to a run: nothing in the engine reads them, the file
is left out of ``scenario_tree_id`` (ADR-004 §1), and they are evaluated
from a run when a report asks, so editing one reruns nothing. The loader
still checks them, so a bad rail fails ``sim check`` and the edit commands.

The two measures:

- ``spending``: the income report's spending (#249), every expense but
  income tax, business expenses and depreciation, optionally only those
  whose tags ``select`` picks (a rule's tag in a role over its account's).
- ``withdrawals``: what the portfolio pays out, net. Money leaving a
  portfolio account for anything outside the portfolio, less money
  arriving from outside other than the portfolio's own returns (interest,
  dividends, gains). Moves between portfolio accounts are not withdrawals.
  This is what the 4% rule and Guyton-Klinger's rails limit: Social
  Security or a pension landing in checking pays part of the spending.

The portfolio is every asset account but real estate, vehicles and other
assets, unless ``portfolio``'s tags say otherwise. The yearly arithmetic is
``measures``'s.

An ensemble's members are evaluated the same way, from what the series store
keeps of each (``RunPieces``: its transfer totals, asset balances, accounts,
date_markers and household's end), then summarized across members: the
share past each rail, the share ever past it, the RMS overshoot (ADR-006 §8).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from analysis.frames import index_years
from analysis.sim import measures, registries, units
from analysis.sim.dates import month_end_iso, parse_relative_date, place_month
from analysis.sim.lineage import reading_order
from analysis.sim.lineage import scenario_dir as _scenario_dir
from analysis.sim.types import DollarMode

if TYPE_CHECKING:
    from analysis.sim.connection import RunConnection
    from analysis.sim.series_db import EnsembleSeries

__all__ = [
    "GUARD_RAILS_FILE", "GuardRail", "GuardRailError", "RailResult", "References",
    "EnsembleRailResult", "RunPieces", "TagFilter", "check_references", "comparison_frames",
    "ensemble_comparison_frames", "ensemble_rail_frame", "ensemble_rails", "ensemble_results",
    "ensemble_summary_frame",
    "episode_lines", "evaluate_ensemble", "evaluate_run", "run_pieces",
    "margins_frame", "parse_cells", "parse_rows",
    "rail_frame", "read_rows", "references_from_rows", "references_from_run",
    "run_rails", "scenario_rails", "summary_frame",
]

GUARD_RAILS_FILE = "guard_rails.csv"

DEFAULT_PORTFOLIO_EXCLUDED_SUBKINDS = frozenset({"real_estate", "vehicle", "other_asset"})
"""Asset subkinds the default portfolio leaves out: what a household lives
in or drives, and what can't be drawn on, isn't what a withdrawal rate is
taken from."""

RETURN_INCOME_SUBKINDS = frozenset({"interest", "dividend", "capital_gain"})
"""Income subkinds whose money arriving in the portfolio is the portfolio's
own return, not a contribution, so it doesn't offset withdrawals."""

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_PERCENT_BASES = frozenset({"portfolio", "initial_portfolio"})


class GuardRailError(ValueError):
    """A scenario's guard rails don't read or don't fit the scenario."""

    def __init__(self, issues: Sequence[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = list(issues)


@dataclass(frozen=True)
class TagFilter:
    """Tags picking accounts or expenses: any of ``include`` (all, when
    there are none), then none of ``exclude``. ``role:*`` matches a role."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> TagFilter:
        """``role:name`` tokens, a ``-`` before each one to leave out.
        Raises ``ValueError`` for a malformed token."""
        include: list[str] = []
        exclude: list[str] = []
        for token in text.split():
            negated = token.startswith("-")
            tag = token[1:] if negated else token
            role, _, name = tag.partition(":")
            if name != "*":
                registries.parse_tags(tag)
            elif not re.fullmatch(r"[a-z0-9_]+", role):
                raise ValueError(f"invalid tag {tag!r}; expected '<role>:<name>' or '<role>:*'")
            (exclude if negated else include).append(tag)
        return cls(tuple(include), tuple(exclude))

    def __str__(self) -> str:
        return " ".join([*self.include, *(f"-{t}" for t in self.exclude)])

    def __bool__(self) -> bool:
        return bool(self.include or self.exclude)

    @staticmethod
    def _hits(tags: frozenset[str], tokens: Iterable[str]) -> bool:
        roles = {t.partition(":")[0] for t in tags}
        return any(t in tags if not t.endswith(":*") else t[:-2] in roles for t in tokens)

    def keeps(self, tags: frozenset[str], *, by_default: bool = True) -> bool:
        """Whether something tagged ``tags`` is picked; ``by_default`` is
        whether it would be with no ``include`` tokens."""
        kept = self._hits(tags, self.include) if self.include else by_default
        return kept and not self._hits(tags, self.exclude)


@dataclass(frozen=True)
class GuardRail:
    """One row of ``guard_rails.csv``, parsed."""

    name: str
    measure: str  # registries.GUARD_RAIL_MEASURES
    side: measures.Side
    threshold: int  # cents a year, or a percent in micros (``is_percent``)
    basis: str  # registries.GUARD_RAIL_BASES
    sense: str = "negative"  # registries.GUARD_RAIL_SENSES
    select: TagFilter = field(default_factory=TagFilter)
    portfolio: TagFilter = field(default_factory=TagFilter)
    start: str = ""  # a month end, a date_marker (maybe offset), or blank
    end: str = ""
    description: str = ""
    defined_in: str = ""  # the scenario whose file the row (as merged) last came from

    @property
    def is_percent(self) -> bool:
        return self.basis in _PERCENT_BASES

    @property
    def positive(self) -> bool:
        return self.sense == "positive"

    def threshold_text(self) -> str:
        """The threshold as authored: ``4%``, or ``85000.00``."""
        if self.is_percent:
            return units.micros_to_percent(self.threshold)
        return units.cents_to_dollars(self.threshold)

    def describe(self) -> str:
        """One line: ``withdrawals above 4% of the portfolio at the rail's start, CPI-indexed``."""
        what = self.measure
        if self.select:
            what += f" [{self.select}]"
        basis = {
            "nominal": "a year",
            "cpi_constant": "a year in CPI-constant dollars",
            "portfolio": "of the portfolio at each year's start",
            "initial_portfolio": "of the portfolio at the rail's start, CPI-indexed",
        }[self.basis]
        amount = self.threshold_text() if self.is_percent else f"${self.threshold / 100:,.0f}"
        text = f"{what} {self.side} {amount} {basis}"
        if self.portfolio and (self.measure == "withdrawals" or self.is_percent):
            text += f" (portfolio: {self.portfolio})"
        return text


# ---------- reading guard_rails.csv along the lineage ----------


@dataclass(frozen=True)
class _Row:
    cells: dict[str, str]
    where: str  # "<scenario>/guard_rails.csv line N" of the row last merged in
    defined_in: str


def read_rows(scenarios_dir: Path, scenario_name: str) -> tuple[list[_Row], list[str]]:
    """The scenario's rails as rows, merged down its load order by ``name``
    the way the loader merges ``scheduled.csv``: a later scenario's row
    replaces the cells its file has. A pool's are its base component's,
    under its own (``lineage.reading_order``). Also the issues of reading
    them."""
    issues: list[str] = []
    merged: dict[str, _Row] = {}
    for name in reading_order(scenarios_dir, scenario_name):
        path = _scenario_dir(scenarios_dir, name) / GUARD_RAILS_FILE
        if not path.exists():
            continue
        label = f"{name}/{GUARD_RAILS_FILE}"
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = [h.strip() for h in next(reader, [])]
            unknown = [h for h in header if h not in registries.GUARD_RAILS_CSV_HEADER]
            if unknown or "name" not in header:
                issues.append(
                    f"{label}: unknown column(s) {', '.join(unknown)}" if unknown
                    else f"{label}: no name column"
                )
                continue
            last = reader.line_num
            seen: dict[str, int] = {}  # a name's line in this file
            for values in reader:
                line, last = last + 1, reader.line_num
                cells = [v.strip() for v in values]
                if not any(cells):
                    continue
                where = f"{label} line {line}"
                if len(cells) > len(header):
                    issues.append(f"{where}: {len(cells)} cells but the header has {len(header)} columns")
                row = dict(zip(header, (cells + [""] * len(header))[:len(header)], strict=True))
                if not row["name"]:
                    issues.append(f"{where}: name is blank")
                    continue
                if (first := seen.setdefault(row["name"], line)) != line:
                    # A child's row replaces its parent's; one file naming a
                    # rail twice is a mistake, not an override.
                    issues.append(f"{where}: rail {row['name']!r} is already on line {first}")
                    continue
                prior = merged.get(row["name"])
                cells_now = {**prior.cells, **row} if prior is not None else row
                merged[row["name"]] = _Row(cells_now, where, name)
    return list(merged.values()), issues


def parse_rows(rows: Iterable[_Row]) -> tuple[list[GuardRail], list[str]]:
    """``rows`` as rails, and what's wrong with them on their own (the
    scenario's tags and markers are ``check_references``'s)."""
    rails: list[GuardRail] = []
    issues: list[str] = []
    for row in rows:
        rail, problems = parse_cells(row.cells, defined_in=row.defined_in)
        issues.extend(f"{row.where} ({row.cells.get('name', '')}): {problem}" for problem in problems)
        if rail is not None:
            rails.append(rail)
    return rails, issues


def parse_cells(row: Mapping[str, str], *, defined_in: str = "") -> tuple[GuardRail | None, list[str]]:
    """One row's cells as a rail, or None and what's wrong with it."""
    cells = {column: row.get(column, "").strip() for column in registries.GUARD_RAILS_CSV_HEADER}
    problems: list[str] = []

    def pick(column: str, choices: Sequence[str], default: str = "") -> str:
        value = cells[column] or default
        if value not in choices:
            problems.append(
                f"{column} must be one of {', '.join(choices)}"
                + (f" (got {value!r})" if value else "")
            )
        return value

    if not _NAME.fullmatch(cells["name"]):
        problems.append("name must start with a letter or _, then letters, digits, _, . or -")
    measure = pick("measure", registries.GUARD_RAIL_MEASURES)
    side = pick("side", registries.GUARD_RAIL_SIDES)
    basis = pick("basis", registries.GUARD_RAIL_BASES)
    sense = pick("sense", registries.GUARD_RAIL_SENSES, default="negative")
    threshold = 0
    if not cells["threshold"]:
        problems.append("threshold is blank")
    elif basis in _PERCENT_BASES:
        try:
            threshold = units.percent_to_micros(cells["threshold"])
        except ValueError as exc:
            problems.append(f"threshold: {exc} (basis {basis} is a percent of the portfolio)")
    elif basis:
        if cells["threshold"].endswith("%"):
            problems.append(f"threshold {cells['threshold']!r} is a percent, but basis {basis} "
                            "takes dollars a year (portfolio or initial_portfolio take a percent)")
        else:
            try:
                threshold = units.dollars_to_cents(cells["threshold"])
            except ValueError as exc:
                problems.append(f"threshold: {exc}")
    if cells["threshold"] and threshold <= 0 and not problems:
        problems.append("threshold must be more than zero")
    filters: dict[str, TagFilter] = {}
    for column in ("select", "portfolio"):
        try:
            filters[column] = TagFilter.parse(cells[column])
        except ValueError as exc:
            problems.append(f"{column}: {exc}")
            filters[column] = TagFilter()
    if filters["select"] and measure == "withdrawals":
        problems.append("select picks expenses, so it applies only to measure spending")
    bounds: dict[str, str] = {}
    for column in ("start", "end"):
        raw = cells[column]
        if raw and parse_relative_date(raw) is None:
            try:
                raw = month_end_iso(raw)
            except ValueError:
                problems.append(f"{column} {raw!r} is not YYYY-MM, YYYY-MM-DD, or a "
                                "date_marker name (NAME, NAME+Nm, NAME-Ny)")
        bounds[column] = raw
    if problems:
        return None, problems
    return GuardRail(
        name=cells["name"], measure=measure, side="above" if side == "above" else "below",
        threshold=threshold, basis=basis, sense=sense,
        select=filters["select"], portfolio=filters["portfolio"],
        start=bounds["start"], end=bounds["end"],
        description=cells["description"], defined_in=defined_in,
    ), []


# ---------- what a scenario's rails may refer to ----------


@dataclass(frozen=True)
class References:
    """The tags and markers a scenario's rails may name."""

    spending_tags: frozenset[str]  # on expense accounts and scheduled rules
    portfolio_tags: frozenset[str]  # on asset accounts
    markers: frozenset[str]  # date_marker names


def _tag_set(cell: str) -> frozenset[str]:
    return frozenset((cell or "").split())


def references_from_rows(
    accounts: Iterable[Mapping[str, str]], scheduled: Iterable[Mapping[str, str]],
    calendar: Iterable[Mapping[str, str]],
) -> References:
    """References from a load's merged CSV rows."""
    accounts = list(accounts)
    return References(
        spending_tags=frozenset().union(
            *(_tag_set(a.get("tags", "")) for a in accounts if a.get("kind") == "expense"),
            *(_tag_set(s.get("tags", "")) for s in scheduled),
        ),
        portfolio_tags=frozenset().union(
            *(_tag_set(a.get("tags", "")) for a in accounts if a.get("kind") == "asset")),
        markers=frozenset(c.get("name", "") for c in calendar if c.get("kind") == "date_marker"),
    )


def references_from_run(rc: RunConnection) -> References:
    """References from a run's own accounts, rules and calendar."""
    events = rc.items("calendar_event")
    return references_from_rows(
        rc.items("account").to_dict("records"), rc.items("scheduled").to_dict("records"),
        events.to_dict("records"),
    )


def check_references(rails: Iterable[GuardRail], refs: References) -> list[str]:
    """What ``rails`` name that the scenario doesn't have: a tag no
    expense, rule or asset account carries, a date_marker no row defines."""
    issues: list[str] = []

    def unknown(tags: Iterable[str], known: frozenset[str]) -> list[str]:
        roles = {t.partition(":")[0] for t in known}
        return [t for t in tags if (t[:-2] not in roles if t.endswith(":*") else t not in known)]

    for rail in rails:
        where = f"{rail.defined_in}/{GUARD_RAILS_FILE} ({rail.name})"
        for tag in unknown([*rail.select.include, *rail.select.exclude], refs.spending_tags):
            issues.append(f"{where}: select names {tag}, which no expense account or "
                          "scheduled rule carries")
        for tag in unknown([*rail.portfolio.include, *rail.portfolio.exclude], refs.portfolio_tags):
            issues.append(f"{where}: portfolio names {tag}, which no asset account carries")
        for column in ("start", "end"):
            rel = parse_relative_date(getattr(rail, column))
            if rel is not None and rel.anchor not in refs.markers:
                issues.append(f"{where}: {column} names {rel.anchor!r}, which is no date_marker")
    return issues


def scenario_rails(
    scenarios_dir: Path, scenario_name: str, refs: References | None = None,
) -> list[GuardRail]:
    """The scenario's rails, checked (against ``refs`` when given). Raises
    :class:`GuardRailError` with every issue."""
    rows, issues = read_rows(scenarios_dir, scenario_name)
    rails, more = parse_rows(rows)
    issues += more
    if refs is not None:
        issues += check_references(rails, refs)
    if issues:
        raise GuardRailError(issues)
    return rails


def run_scenario_name(rc: RunConnection) -> str:
    """Whose rails a run is judged by: its scenario's, except a pool
    member's (ADR-009), whose run is a component's but whose rails are
    the pool's."""
    from analysis.sim.pool import pool_record

    batch = Path(rc.run_path).parent
    if pool_record(batch) is not None:
        return batch.parent.name
    return str(rc.items("scenario")["name"].iloc[0])


def run_rails(rc: RunConnection, scenarios_dir: Path) -> list[GuardRail]:
    """The rails of the run's scenario (:func:`run_scenario_name`), read
    from the data repo now (they are a lens, not part of the run) and
    checked against the run."""
    return scenario_rails(scenarios_dir, run_scenario_name(rc), references_from_run(rc))


# ---------- a run's monthly series ----------


@dataclass
class _RunInputs:
    """What every rail of one run reads, gathered once."""

    months: pd.DatetimeIndex
    axis: measures.YearAxis
    index: np.ndarray  # the run's cumulative CPI index (micros) by month
    expenses: pd.DataFrame  # spending events: pos, amount (dollars), tags
    transfers: pd.DataFrame  # every transfer: pos, amount, sides' names/kinds/subkinds
    balances: pd.DataFrame  # month x account, nominal dollars
    accounts: pd.DataFrame  # name -> kind, subkind, tags (frozenset)
    markers: dict[str, date]
    last_month: pd.Timestamp  # the household's last death, or the run's end

    def month_ends(self, events: pd.DataFrame) -> np.ndarray:
        """Each month's total of ``events``' amounts, over the run's months
        (``pos``, an event's month's place among them; -1 outside them)."""
        pos = events["pos"].to_numpy()
        inside = pos >= 0
        return np.bincount(pos[inside], weights=events["amount"].to_numpy(dtype=float)[inside],
                           minlength=len(self.months))


def _effective_tags(rule_tags: frozenset[str], account_tags: frozenset[str]) -> frozenset[str]:
    """An expense's tags: its rule's in each role the rule tags, its
    account's in the others, as ``sim report expenses --by tag:ROLE`` reads
    them."""
    rule_roles = {t.partition(":")[0] for t in rule_tags}
    return rule_tags | frozenset(t for t in account_tags if t.partition(":")[0] not in rule_roles)


def _timestamp(value: str | date) -> pd.Timestamp:
    """``value`` as a Timestamp; never NaT (a date already checked)."""
    stamp = pd.Timestamp(value)
    if not isinstance(stamp, pd.Timestamp):
        raise ValueError(f"not a date: {value!r}")
    return stamp


@dataclass(frozen=True)
class RunPieces:
    """The raw pieces of one run that every rail reads: from a run file
    (``run_pieces``) or an ensemble member in the series store
    (``EnsembleSeries.rail_pieces``), so the two evaluate alike."""

    months: pd.DatetimeIndex
    index: np.ndarray  # the cumulative CPI index (micros) by month
    flows: pd.DataFrame  # RunConnection.monthly_flows: month, from_name, to_name, rule_tags, cents
    balances: pd.DataFrame  # month x asset account, nominal dollars (0 where none)
    accounts: pd.DataFrame  # name -> kind, subkind, tags (space-separated)
    markers: dict[str, date]
    household_end: date | None


def run_pieces(rc: RunConnection) -> RunPieces:
    nw = rc.net_worth_frame()
    months = pd.DatetimeIndex(nw.index)
    accounts = rc.items("account").set_index("name")[["kind", "subkind", "tags"]]
    assets = list(accounts.index[accounts["kind"] == "asset"])
    return RunPieces(
        months=months,
        index=nw["cumulative_index_micros"].astype(float).to_numpy(),
        flows=rc.monthly_flows(),
        balances=rc.balance_pivot().reindex(index=months, columns=assets).fillna(0.0),
        accounts=accounts,
        markers=rc.date_markers(),
        household_end=rc.household_end(),
    )


def _inputs(pieces: RunPieces) -> _RunInputs:
    from analysis.sim.connection import EXPENSE_SUBKIND_TO_CATEGORY, NON_CASH_EXPENSE_ACCOUNTS

    months = pieces.months
    accounts = pieces.accounts.copy()
    accounts["tag_set"] = accounts["tags"].map(_tag_set)
    kind, subkind = accounts["kind"], accounts["subkind"]
    flows = pieces.flows
    place = pd.Series(np.arange(len(months)), index=months)
    pos = place.reindex(pd.DatetimeIndex(flows["month"])).fillna(-1).to_numpy(dtype=np.int64)
    transfers = pd.DataFrame({
        "pos": pos, "amount": flows["cents"].to_numpy(dtype=float) / 100,
        "from_name": flows["from_name"], "from_kind": flows["from_name"].map(kind),
        "from_subkind": flows["from_name"].map(subkind),
        "to_name": flows["to_name"], "to_kind": flows["to_name"].map(kind),
    })
    to_subkind = flows["to_name"].map(subkind)
    category = to_subkind.map(EXPENSE_SUBKIND_TO_CATEGORY)
    spent = ((transfers["to_kind"] == "expense") & ~category.isin(["taxes", "business"])
             & ~transfers["to_name"].isin(NON_CASH_EXPENSE_ACCOUNTS))
    empty: frozenset[str] = frozenset()
    account_tags = dict(zip(accounts.index, accounts["tag_set"], strict=True))
    tags_of: dict[tuple[str, str], frozenset[str]] = {}
    for key in zip(flows.loc[spent, "rule_tags"], flows.loc[spent, "to_name"], strict=True):
        if key not in tags_of:
            tags_of[key] = _effective_tags(_tag_set(key[0]), account_tags.get(key[1], empty))
    expenses = transfers.loc[spent, ["pos", "amount"]].copy()
    expenses["tags"] = [tags_of[key] for key in zip(flows.loc[spent, "rule_tags"],
                                                    flows.loc[spent, "to_name"], strict=True)]
    end = months.max() if len(months) else pd.Timestamp.max
    death = None if pieces.household_end is None else _timestamp(pieces.household_end)
    return _RunInputs(
        months=months, axis=measures.year_axis(np.asarray(index_years(pd.Series(0.0, index=months)))),
        index=pieces.index, expenses=expenses, transfers=transfers,
        balances=pieces.balances, accounts=accounts[["kind", "subkind", "tag_set"]],
        markers=pieces.markers,
        last_month=min(end, death) if death is not None else end,
    )


def _run_inputs(rc: RunConnection) -> _RunInputs:
    return _inputs(run_pieces(rc))


def _portfolio(inputs: _RunInputs, rail: GuardRail) -> list[str]:
    """The asset accounts that are the rail's portfolio."""
    assets = inputs.accounts[inputs.accounts["kind"] == "asset"]
    return [
        name for name, subkind, tags in zip(assets.index, assets["subkind"], assets["tag_set"], strict=True)
        if rail.portfolio.keeps(tags, by_default=subkind not in DEFAULT_PORTFOLIO_EXCLUDED_SUBKINDS)
    ]


def _withdrawals(inputs: _RunInputs, portfolio: Sequence[str]) -> np.ndarray:
    """The portfolio's net payout by month (see the module docstring)."""
    t = inputs.transfers
    inside_from = t["from_name"].isin(portfolio)
    inside_to = t["to_name"].isin(portfolio)
    out = t[inside_from & ~inside_to & (t["to_kind"] != "income")]
    returns = (t["from_kind"] == "income") & t["from_subkind"].isin(RETURN_INCOME_SUBKINDS)
    into = t[~inside_from & inside_to & ~returns]
    return inputs.month_ends(out) - inputs.month_ends(into)


def _bound(inputs: _RunInputs, text: str) -> pd.Timestamp | None:
    """A rail's start or end as a month end, None when blank."""
    if not text:
        return None
    rel = parse_relative_date(text)
    if rel is None:
        return _timestamp(text)
    anchor = inputs.markers.get(rel.anchor)
    if anchor is None:
        raise GuardRailError([f"no date_marker {rel.anchor!r} in this run"])
    return _timestamp(place_month(anchor, rel.months))


def _window(inputs: _RunInputs, rail: GuardRail) -> np.ndarray:
    months = inputs.months
    start, end = _bound(inputs, rail.start), _bound(inputs, rail.end)
    on = months <= inputs.last_month
    if start is not None:
        on &= months >= start
    if end is not None:
        on &= months <= end
    return np.asarray(on)


def _allowance(inputs: _RunInputs, rail: GuardRail, values: np.ndarray, active: np.ndarray) -> np.ndarray:
    """The rail's threshold for each month, nominal dollars."""
    if rail.basis == "nominal":
        return np.full(len(inputs.months), rail.threshold / 100 / 12)
    if rail.basis == "cpi_constant":
        # The dollars CPI-constant reports show (``to_cpi_constant``).
        return rail.threshold / 100 / 12 * inputs.index / 1_000_000
    share = rail.threshold / 1_000_000  # micros of 1
    if rail.basis == "portfolio":
        return share * measures.year_start_values(values, inputs.axis)[0] / 12
    start_value = measures.value_before_window(values, active)[0]
    at = int(np.argmax(active))
    start_index = inputs.index[max(at - 1, 0)]
    return share * start_value / 12 * inputs.index / start_index


@dataclass(frozen=True)
class RailResult:
    """One rail evaluated on one run."""

    rail: GuardRail
    years: measures.RailYears

    @property
    def applies(self) -> bool:
        return bool(np.isfinite(self.years.margin).any())


def _carry_returns(inputs: _RunInputs, values: np.ndarray, net_out: np.ndarray,
                   factor: np.ndarray) -> np.ndarray:
    return measures.dietz_returns(values * factor, net_out * factor, inputs.axis)


def evaluate_run(
    rc: RunConnection, rails: Sequence[GuardRail], mode: DollarMode = DollarMode.CPI_CONSTANT,
) -> list[RailResult]:
    """Each rail on the run, in ``mode`` dollars."""
    return _evaluate(_run_inputs(rc), rails, mode)


def _evaluate(inputs: _RunInputs, rails: Sequence[GuardRail], mode: DollarMode) -> list[RailResult]:
    factor = (1_000_000 / inputs.index if mode == DollarMode.CPI_CONSTANT
              else np.ones(len(inputs.months)))
    results: list[RailResult] = []
    # Rails often share a portfolio (the default one) or a selection.
    portfolios: dict[tuple[str, ...], tuple[np.ndarray, np.ndarray]] = {}
    for rail in rails:
        portfolio = tuple(_portfolio(inputs, rail))
        if portfolio not in portfolios:
            portfolios[portfolio] = (
                inputs.balances.reindex(columns=list(portfolio), fill_value=0.0).sum(axis=1).to_numpy(),
                _withdrawals(inputs, portfolio))
        values, withdrawn = portfolios[portfolio]
        if rail.measure == "spending":
            keeps: dict[frozenset[str], bool] = {}
            for tags in inputs.expenses["tags"]:
                if tags not in keeps:
                    keeps[tags] = rail.select.keeps(tags)
            picked = inputs.expenses[[keeps[tags] for tags in inputs.expenses["tags"]]]
            measure = inputs.month_ends(picked)
        else:
            measure = withdrawn
        active = _window(inputs, rail)
        allowance = _allowance(inputs, rail, values, active) if active.any() else np.zeros(len(active))
        years = measures.evaluate(
            measure, allowance, factor, active, inputs.axis, rail.side,
            returns=_carry_returns(inputs, values, withdrawn, factor),
        )
        results.append(RailResult(rail, years))
    return results


@dataclass(frozen=True)
class EnsembleRailResult:
    """One rail evaluated on every member of an ensemble: ``years``'s arrays
    have a row per member (``stems``, ``member_indexes``), NaN in a year
    the rail doesn't cover in that member (after its household's last
    death, say)."""

    rail: GuardRail
    stems: list[str]
    member_indexes: list[int | None]
    years: measures.RailYears

    @property
    def applies(self) -> bool:
        return bool(np.isfinite(self.years.margin).any())

    def member(self, stem: str) -> RailResult | None:
        """One member's result, as ``evaluate_run`` gives it."""
        if stem not in self.stems:
            return None
        row = self.stems.index(stem)
        y = self.years
        return RailResult(self.rail, measures.RailYears(
            y.years, *(getattr(y, f)[row:row + 1] for f in
                       ("measure", "threshold", "margin", "cumulative", "carried"))))


_YEAR_FIELDS = ("measure", "threshold", "margin", "cumulative", "carried")


def evaluate_ensemble(
    members: Iterable[tuple[str, int | None, RunPieces]], rails: Sequence[GuardRail],
    mode: DollarMode = DollarMode.CPI_CONSTANT,
) -> list[EnsembleRailResult]:
    """Each rail on every member (``EnsembleSeries.rail_pieces``), in
    ``mode`` dollars. A member whose run lacks a date_marker a rail is
    bounded by has no years for that rail; a tag it lacks matches nothing
    there (a pool's members come from different components)."""
    stems: list[str] = []
    indexes: list[int | None] = []
    per_rail: list[list[RailResult | None]] = [[] for _ in rails]
    for stem, member_index, pieces in members:
        inputs = _inputs(pieces)
        stems.append(stem)
        indexes.append(member_index)
        try:
            found: list[RailResult | None] = list(_evaluate(inputs, rails, mode))
        except GuardRailError:  # a date_marker this member lacks: find which rail
            found = []
            for rail in rails:
                try:
                    found += _evaluate(inputs, [rail], mode)
                except GuardRailError:
                    found.append(None)
        for i, result in enumerate(found):
            per_rail[i].append(result)
    out: list[EnsembleRailResult] = []
    for rail, results in zip(rails, per_rail, strict=True):
        years = sorted({int(y) for r in results if r is not None for y in r.years.years})
        stacked = {f: np.full((len(results), len(years)), np.nan) for f in _YEAR_FIELDS}
        column = {year: i for i, year in enumerate(years)}
        for row, r in enumerate(results):
            if r is None:
                continue
            at = [column[int(y)] for y in r.years.years]
            for f in _YEAR_FIELDS:
                stacked[f][row, at] = getattr(r.years, f)[0]
        out.append(EnsembleRailResult(
            rail, stems, indexes, measures.RailYears(np.array(years, dtype=int), **stacked)))
    return out


def ensemble_results(
    store: EnsembleSeries, rails: Sequence[GuardRail], mode: DollarMode = DollarMode.CPI_CONSTANT,
) -> list[EnsembleRailResult]:
    """``evaluate_ensemble`` over the store's members, held by the store, so
    a redraw or another report on the same batch costs nothing."""
    pieces = store.memo(("guard_rail_pieces",), lambda: list(store.rail_pieces()))
    return store.memo(("guard_rails", tuple(rails), mode),
                      lambda: evaluate_ensemble(pieces, rails, mode))


def ensemble_rails(scenarios_dir: Path, recipe: str) -> list[GuardRail]:
    """An ensemble's rails: its recipe's, read once for the whole batch and
    not checked against one member's run, since a pool's members come from
    different components (``sim check`` has checked them against the
    scenario). A tag a member lacks matches nothing there."""
    return scenario_rails(scenarios_dir, recipe)


ENSEMBLE_FORMS = (("annual", "margin"), ("cumulative", "cumulative"))


def _ever_share(margin: np.ndarray) -> np.ndarray:
    """Per year, the share of members past the rail in some year so far,
    among those the rail has covered so far."""
    covered = np.logical_or.accumulate(~np.isnan(margin), axis=1).sum(axis=0)
    ever = measures.ever_past(margin).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(covered > 0, ever / np.maximum(covered, 1), np.nan)


def ensemble_rail_frame(result: EnsembleRailResult) -> pd.DataFrame:
    """One rail by year across members, for each form (``annual``, then
    ``cumulative`` with a `` cum`` suffix): ``past``, the share of members
    past it that year; ``ever_past``, the share past it in some year so
    far; ``rms_over``, the root mean square of how far past, members inside
    counting as zero. Shares are of the members the rail covers."""
    columns: dict[str, np.ndarray] = {}
    for form, field_ in ENSEMBLE_FORMS:
        margin = getattr(result.years, field_)
        suffix = "" if form == "annual" else CUMULATIVE_SUFFIX
        columns["past" + suffix] = measures.share_past(margin)
        columns["ever_past" + suffix] = _ever_share(margin)
        columns["rms_over" + suffix] = np.asarray(measures.rms_overshoot(margin, axis=0))
    frame = pd.DataFrame(columns, index=pd.Index(result.years.years, name="year"))
    return frame.dropna(how="all")


def ensemble_summary_frame(results: Sequence[EnsembleRailResult]) -> pd.DataFrame:
    """Per rail and form, over the whole run: ``members``; ``past``, the
    share of member-years past it; ``ever_past``, the share of members past
    it in some year; ``past_at_end``, the share past it in each member's
    last covered year (for the cumulative form, the members who end the plan
    overdrawn); ``rms_over`` over every member-year, inside counting as
    zero."""
    rows: list[dict[str, object]] = []
    for r in results:
        if not r.applies:
            continue
        for form, field_ in ENSEMBLE_FORMS:
            margin = getattr(r.years, field_)
            covered = ~np.isnan(margin)
            members = covered.any(axis=1)
            last = np.array([row[c][-1] if c.any() else np.nan
                             for row, c in zip(margin, covered, strict=True)])
            rows.append({
                "rail": r.rail.name, "form": form, "sense": r.rail.sense,
                "members": int(members.sum()),
                "past": float(measures.past(margin).sum() / covered.sum()),
                "ever_past": float(measures.past(margin).any(axis=1).sum() / members.sum()),
                "past_at_end": float(measures.past(last[members]).mean()),
                "rms_over": float(measures.rms_overshoot(margin)),
            })
    return pd.DataFrame(rows, columns=["rail", "form", "sense", "members", "past", "ever_past",
                                       "past_at_end", "rms_over"])


def ensemble_comparison_frames(
    baseline: Sequence[EnsembleRailResult], variant: Sequence[EnsembleRailResult],
) -> dict[str, pd.DataFrame]:
    """Each rail both ensembles have, matched by name: by year, the
    variant's change in each ``ensemble_rail_frame`` column (variant less
    baseline, ``Δ`` prefixed)."""
    base = {r.rail.name: r for r in baseline if r.applies}
    frames: dict[str, pd.DataFrame] = {}
    for r in variant:
        if not r.applies or r.rail.name not in base:
            continue
        delta = ensemble_rail_frame(r) - ensemble_rail_frame(base[r.rail.name])
        frames[r.rail.name] = delta.add_prefix("Δ").dropna(how="all")
    return frames


# ---------- frames for reports ----------

CUMULATIVE_SUFFIX = " cum"
CARRIED_SUFFIX = " carried"


def rail_frame(result: RailResult) -> pd.DataFrame:
    """One rail by year: ``measure``, ``threshold``, ``margin``,
    ``cumulative``, ``carried``; only the years it covers."""
    y = result.years
    frame = pd.DataFrame({
        "measure": y.measure[0], "threshold": y.threshold[0], "margin": y.margin[0],
        "cumulative": y.cumulative[0], "carried": y.carried[0],
    }, index=pd.Index(y.years, name="year"))
    return frame.dropna(how="all")


def margins_frame(results: Sequence[RailResult], *, carried: bool = False) -> pd.DataFrame:
    """Year by rail: each rail's annual margin and its cumulative one
    (``<name> cum``), and with ``carried`` the carried-at-return one
    (``<name> carried``). Only years some rail covers."""
    columns: dict[str, np.ndarray] = {}
    years = results[0].years.years if results else np.array([], dtype=int)
    for r in results:
        if not r.applies:
            continue
        columns[r.rail.name] = r.years.margin[0]
        columns[r.rail.name + CUMULATIVE_SUFFIX] = r.years.cumulative[0]
        if carried:
            columns[r.rail.name + CARRIED_SUFFIX] = r.years.carried[0]
    frame = pd.DataFrame(columns, index=pd.Index(years, name="year"))
    return frame.dropna(how="all")


def summary_frame(results: Sequence[RailResult]) -> pd.DataFrame:
    """Per rail and form (``annual``, ``cumulative``): the years past it, the
    worst margin and its year, the margin in the last year, and the RMS
    overshoot over the years (inside counting as zero)."""
    rows: list[dict[str, object]] = []
    for r in results:
        if not r.applies:
            continue
        for form, margin in (("annual", r.years.margin[0]), ("cumulative", r.years.cumulative[0])):
            covered = np.isfinite(margin)
            worst = int(np.nanargmin(margin))
            rows.append({
                "rail": r.rail.name, "form": form, "sense": r.rail.sense,
                "years": int(covered.sum()), "years_past": int(measures.past(margin).sum()),
                "worst": float(margin[worst]), "worst_year": int(r.years.years[worst]),
                "end": float(margin[covered][-1]),
                "rms_over": float(measures.rms_overshoot(margin)),
            })
    return pd.DataFrame(rows, columns=["rail", "form", "sense", "years", "years_past",
                                       "worst", "worst_year", "end", "rms_over"])


def rail_episodes(result: RailResult) -> dict[str, list[measures.Episode]]:
    """The rail's episodes past it, ``annual`` and ``cumulative``."""
    y = result.years
    return {
        "annual": measures.episodes(y.margin[0], y.years),
        "cumulative": measures.episodes(y.cumulative[0], y.years),
    }


def _episode_text(episode: measures.Episode, side: str, *, total: bool) -> str:
    years = (str(episode.first) if episode.first == episode.last
             else f"{episode.first}-{episode.last}")
    amount = f"${-episode.worst:,.0f} {'over' if side == 'above' else 'under'}"
    if total:
        amount += " in all"
    if episode.first == episode.last:
        return f"{years} ({amount})"
    return f"{years} (worst {episode.worst_year}, {amount})"


def episode_lines(results: Sequence[RailResult]) -> list[str]:
    """One line per rail a run went past, either form: ``four_percent
    (negative): past 2033-2035 (worst 2034, $23,912 over); cumulative past
    2036-2040 (worst 2040, $47,800 over in all)``. Rails never past are
    left out."""
    lines: list[str] = []
    for r in results:
        if not r.applies:
            continue
        found = rail_episodes(r)
        parts = []
        if found["annual"]:
            parts.append("past " + ", ".join(
                _episode_text(e, r.rail.side, total=False) for e in found["annual"]))
        if found["cumulative"]:
            parts.append("cumulative past " + ", ".join(
                _episode_text(e, r.rail.side, total=True) for e in found["cumulative"]))
        if parts:
            lines.append(f"{r.rail.name} ({r.rail.sense}): {'; '.join(parts)}")
    return lines


def comparison_frames(
    results: Mapping[str, Sequence[RailResult]], baseline: str, variants: Sequence[str],
) -> dict[str, pd.DataFrame]:
    """Each rail some scenario defines, matched by name: year by scenario
    annual margins, then a ``Δ <variant>`` column per variant (its margin
    less the baseline's), then the same for the cumulative margins
    (``<scenario> cum``, ``Δ <variant> cum``). A scenario without the rail
    has no columns, and no Δ when it or the baseline lacks it."""
    order: list[str] = []
    by_scenario: dict[str, dict[str, RailResult]] = {}
    for name in (baseline, *variants):
        by_scenario[name] = {r.rail.name: r for r in results.get(name, ()) if r.applies}
        order += [rail for rail in by_scenario[name] if rail not in order]
    frames: dict[str, pd.DataFrame] = {}
    for rail in order:
        columns: dict[str, pd.Series] = {}
        for suffix, form in (("", "margin"), (CUMULATIVE_SUFFIX, "cumulative")):
            margins = {
                name: pd.Series(getattr(found[rail].years, form)[0], index=found[rail].years.years)
                for name, found in by_scenario.items() if rail in found
            }
            columns |= {name + suffix: margin for name, margin in margins.items()}
            if baseline in margins:
                columns |= {f"Δ {name}{suffix}": margins[name] - margins[baseline]
                            for name in variants if name in margins}
        frame = pd.DataFrame(columns)
        frame.index.name = "year"
        frames[rail] = frame.dropna(how="all")
    return frames
