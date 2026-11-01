"""Context-managed access to scenario (intent) and run SQLite DBs.

`ScenarioConnection` opens a fresh intent DB built from the CSV data dir, and
exposes read-only queries plus write operations that mutate the underlying
CSVs and then reload the intent DB to keep reads consistent.

`RunConnection` opens an existing run snapshot DB read-only and shares the
read methods.

Today's surface is intentionally narrow: tags. Other tables will be layered
on as notebook/CLI needs grow.
"""

from __future__ import annotations

from abc import abstractmethod
import json
import sqlite3
import tempfile
import threading
import warnings
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from datetime import date
from datetime import date as _Date  # a RunConnection has its own `date`
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

import pandas as pd
import traitlets

from analysis.frames import index_years
from analysis.sim import accounts
from analysis.sim.types import INCOME_CATEGORIES, AccountKindAny, Death, DollarMode, ItemType

from . import registries
from .loader import latest_run_path, load_scenario, resolve_scenario_name
from .schema import connect_sim_file

if TYPE_CHECKING:
    from analysis.sim.lifespan_curves import PersonLifespan
    from analysis.sim.shortfalls import NegativeBalance

# Category-bucketing defaults for RunConnection's derived-data methods below.
# Notebooks may pass their own mapping/set to override any of these.
INCOME_SUBKIND_TO_CATEGORY: dict[str, str] = {
    "salary": "salary",
    "interest": "investment",
    "dividend": "investment",
    "capital_gain": "investment",
    "ss_benefit": "social security",
}
EXPENSE_SUBKIND_TO_CATEGORY: dict[str, str] = {
    "housing": "housing",
    "interest_expense": "housing",
    "taxes_property": "housing",
    "healthcare": "healthcare",
    "taxes_federal": "taxes",
    "taxes_state": "taxes",
    "living": "living",
    "food": "living",
    "business": "business",
    "gift": "gifts",
}
EXPENSE_CATEGORIES = ["housing", "healthcare", "taxes", "living", "business", "gifts", "other"]
# depreciation is a book entry, not spending -- see ADR-002 gains/losses.
NON_CASH_EXPENSE_ACCOUNTS = {"depreciation"}

LIQUID_SUBKINDS = registries.CASH_SUBKINDS
# ss_benefit is handled separately
TAXABLE_GAIN_CHARS = {"ordinary", "ltcg", "stcg", "tax_deferred_out", "roth_nonqualified"}
NONTAXABLE_GAIN_CHARS = {"tax_free", "excluded_gain"}
GAIN_CATEGORIES = ["taxable", "non-taxable", "unrealized"]

DEFAULT_SETTLE_TAX_BUCKETS = ["federal_tax_due", "state_tax_due"]
DEFAULT_TAX_SUMMARY_BUCKETS: dict[str, str] = {
    "federal_tax_liability": "federal tax",
    "state_tax_liability": "state tax",
    "federal_ss_income": "SS gross",
    "federal_taxable_ss": "SS taxed",
}
_TAX_RECONCILIATION_BUCKETS = [
    "federal_tax_liability", "state_tax_liability",
    "federal_tax_paid", "state_tax_paid",
    "federal_tax_due", "state_tax_due",
    "federal_tax_overpaid", "state_tax_overpaid",
]
_TAX_RECONCILIATION_RENAME = {
    "federal_tax_liability": "federal_liability",
    "state_tax_liability": "state_liability",
    "federal_tax_paid": "federal_withheld_or_paid",
    "state_tax_paid": "state_withheld_or_paid",
    "federal_tax_due": "federal_reconciliation_due",
    "state_tax_due": "state_reconciliation_due",
    "federal_tax_overpaid": "federal_estimated_refund",
    "state_tax_overpaid": "state_estimated_refund",
}


@overload
def year_end_values(frame: pd.DataFrame) -> pd.DataFrame: ...
@overload
def year_end_values(frame: pd.Series) -> pd.Series: ...
def year_end_values(frame: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Last row of each calendar year from a month-indexed frame/series.

    Re-indexed by year (int).
    """
    year_end = frame.groupby(index_years(frame)).tail(1)
    year_end.index = index_years(year_end)
    return year_end


def event_month(events: pd.DataFrame) -> pd.Series:
    """The month end of each transfer event's own date. Not its step's month:
    a year-to-date catch-up posts in the first step but is dated before the
    scenario starts, a prior year's one at that year's Dec 31."""
    return pd.to_datetime(events["date"]) + pd.offsets.MonthEnd(0)


def _cpi_factor(index: pd.Series, at: pd.Index):
    """``1e6 / index`` at each of ``at``, clamped to ``index``'s first and last
    entries: CPI-constant dollars for a period outside the run use its
    nearest one."""
    index = index.dropna()
    if index.empty:
        return pd.Series(float("nan"), index=at)
    lookup = pd.Series(at).clip(index.index.min(), index.index.max())
    return pd.Series(1_000_000 / index.reindex(lookup).to_numpy(), index=at)


def to_cpi_constant(monthly: pd.DataFrame | pd.Series, cpi_index: pd.Series):
    """``monthly`` dollars in CPI-constant dollars by ``cpi_index`` (a run's
    month-indexed cumulative index, micros). ``RunConnection`` converts with
    this, and so does an ensemble's series store (#298), which keeps each
    member's index rather than its run file open."""
    return monthly.mul(_cpi_factor(cpi_index, monthly.index), axis=0)


def account_balance_cpi(balance: pd.Series, kind: str | None, cpi_index: pd.Series) -> pd.Series:
    """One account's nominal balance in CPI-constant dollars: an income or
    expense account's balance is a running total of flows, so each month's
    flow is converted before they're summed again (see
    ``RunConnection.cpi_balance_pivot``)."""
    if kind not in accounts.CASHFLOW_KINDS:
        return to_cpi_constant(balance, cpi_index)
    return to_cpi_constant(balance.diff().fillna(balance), cpi_index).cumsum()


def yearly_totals(monthly: pd.DataFrame) -> pd.DataFrame:
    """Sum a month-indexed frame (e.g. from ``income_by_category``) by calendar year."""
    yearly = monthly.groupby(index_years(monthly)).sum()
    yearly.index.name = "year"
    return yearly


def _normalize_tag_collection(
    tags: str | Iterable[str] | Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Accept str, iterable of strings, or iterable of (role, name) pairs."""
    if isinstance(tags, str):
        return registries.parse_tags(tags)
    out: list[tuple[str, str]] = []
    for item in tags:
        if isinstance(item, str):
            out.extend(registries.parse_tags(item))
        else:
            role, name = item
            out.extend(registries.parse_tags(f"{role}:{name}"))
    # de-dupe, preserve order
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str]] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def _split_tag_selectors(
    tags: str | Iterable[str] | Iterable[tuple[str, str]],
) -> tuple[set[str], set[str]]:
    """Split selectors into exact ``role:name`` tokens and wildcard roles.

    ``"estimate:*"`` (or ``("estimate", "*")``) selects every tag in that role,
    so a review pass can ask for all estimates without naming each one.
    """
    tokens = [tags] if isinstance(tags, str) else list(tags)
    roles: set[str] = set()
    exact: list[str] = []
    for token in tokens:
        raw = token if isinstance(token, str) else f"{token[0]}:{token[1]}"
        role, _, name = raw.partition(":")
        if name == "*":
            roles.add(role)
        else:
            exact.append(raw)
    pairs = _normalize_tag_collection(exact) if exact else []
    return {f"{role}:{name}" for role, name in pairs}, roles


# ---------- read mixin (shared by Scenario + Run) ----------

_ITEM_SQL: dict[str, str] = {
    "account": (
        "SELECT a.*, "
        "  COALESCE(GROUP_CONCAT(t.role || ':' || t.name, ' '), '') AS tags "
        "FROM account a "
        "LEFT JOIN account_tag x ON x.account_id = a.id "
        "LEFT JOIN tag t ON t.id = x.tag_id "
        "GROUP BY a.id ORDER BY a.id"
    ),
    "scheduled": (
        "SELECT s.*, fc.name AS from_name, tc.name AS to_name, "
        "  COALESCE(GROUP_CONCAT(t.role || ':' || t.name, ' '), '') AS tags "
        "FROM scheduled_transaction s "
        "JOIN chain fc ON fc.id = s.from_account "
        "JOIN chain tc ON tc.id = s.to_account "
        "LEFT JOIN scheduled_transaction_tag x "
        "  ON x.scheduled_transaction_id = s.id "
        "LEFT JOIN tag t ON t.id = x.tag_id "
        "GROUP BY s.id ORDER BY s.id"
    ),
    "person": "SELECT * FROM person ORDER BY id",
    "security": "SELECT symbol, name FROM security ORDER BY symbol",
    "security_allocation": (
        "SELECT symbol, as_of, asset_class, weight_micros FROM security_allocation "
        "ORDER BY symbol, as_of, asset_class"
    ),
    "person_lifespan": "SELECT * FROM person_lifespan ORDER BY person",
    "chain": (
        "SELECT c.*, a.name AS account_name, a.kind AS account_kind, m.lot AS lot "
        "FROM chain c "
        "LEFT JOIN account a ON a.id = c.account_id "
        "LEFT JOIN account_lot m ON m.id = c.lot_id "
        "ORDER BY c.id"
    ),
    "chain_steps": (
        "SELECT cs.*, c.name AS chain_name, df.name AS draw_from_chain_name "
        "FROM chain_steps cs "
        "JOIN chain c ON c.id = cs.chain_id "
        "JOIN chain df ON df.id = cs.draw_from_chain_id "
        "ORDER BY c.name, cs.step_order"
    ),
    "calendar_event": (
        "SELECT c.*, "
        "  COALESCE(GROUP_CONCAT(t.role || ':' || t.name, ' '), '') AS tags "
        "FROM calendar_event c "
        "LEFT JOIN calendar_event_tag x ON x.calendar_event_id = c.id "
        "LEFT JOIN tag t ON t.id = x.tag_id "
        "GROUP BY c.id ORDER BY c.id"
    ),
    "scenario": "SELECT * FROM scenario",
    # Run-file tables/views. No tag join: these aren't user-tagged today.
    "transfer_event": (
        "SELECT te.id, te.step_id, s.month, te.date, te.amount, "
        "       te.requested_cents, te.remainder_cents, "
        "       te.tax_character, te.reason_kind, te.reason_id, "
        "       te.parent_event_id, te.from_account, te.to_account, "
        "       fa.name AS from_name, fa.kind AS from_kind, "
        "       fa.subkind AS from_subkind, fm.lot AS from_lot, "
        "       ta.name AS to_name, ta.kind AS to_kind, "
        "       ta.subkind AS to_subkind, tm.lot AS to_lot, "
        "       rule.name AS rule_name "
        "FROM transfer_event te "
        "JOIN simulation_step s ON s.id = te.step_id "
        "JOIN account fa ON fa.id = te.from_account "
        "JOIN account ta ON ta.id = te.to_account "
        "JOIN account_lot fm ON fm.id = te.from_lot_id "
        "JOIN account_lot tm ON tm.id = te.to_lot_id "
        "LEFT JOIN scheduled_transaction rule "
        "  ON te.reason_kind = 'rule' AND rule.id = te.reason_id "
        "ORDER BY te.id"
    ),
    "simulation_run": "SELECT * FROM simulation_run",
    "net_worth": "SELECT * FROM net_worth ORDER BY month",
    "net_worth_constant_cpi": (
        "SELECT * FROM net_worth_constant_cpi ORDER BY month"
    ),
    "year_to_date": (
        "SELECT y.*, fa.name AS from_name, fm.lot AS from_lot, "
        "       ta.name AS to_name, tm.lot AS to_lot "
        "FROM year_to_date y "
        "JOIN account fa ON fa.id = y.from_account "
        "JOIN account_lot fm ON fm.id = y.from_lot_id "
        "JOIN account ta ON ta.id = y.to_account "
        "JOIN account_lot tm ON tm.id = y.to_lot_id"
    ),
    "tax_year_summary": "SELECT * FROM tax_year_summary ORDER BY year, bucket",
    "capital_loss": (
        "SELECT cl.*, s.month, a.name AS account, m.lot "
        "FROM capital_loss cl "
        "JOIN simulation_step s ON s.id = cl.step_id "
        "JOIN account_lot m ON m.id = cl.lot_id "
        "JOIN account a ON a.id = m.account_id "
        "ORDER BY cl.id"
    ),
    "basis_step_up": (
        "SELECT b.*, a.name AS account, m.lot "
        "FROM basis_step_up b "
        "JOIN account_lot m ON m.id = b.lot_id "
        "JOIN account a ON a.id = m.account_id "
        "ORDER BY b.id"
    ),
    "account_balance": (
        "SELECT s.month, a.id AS account_id, a.name AS account, "
        "       a.kind, a.subkind, ast.balance "
        "FROM account_state ast "
        "JOIN simulation_step s ON s.id = ast.step_id "
        "JOIN account a         ON a.id = ast.account_id "
        "ORDER BY s.month, a.name"
    ),
    "lot_balance": "SELECT * FROM balance_by_lot ORDER BY month, account, lot_id",
    "account_lot": (
        "SELECT a.name AS account, m.lot, m.symbol, m.acquired, m.quantity_micros, "
        "       m.series, a.kind, a.subkind, m.subkind AS lot_subkind, m.tax_treatment, "
        "       m.opening_balance, m.opening_cost_basis, "
        "       m.prior_year_end_balance, "
        "       m.minimum_balance_cents, m.maximum_balance_cents, "
        "       m.refill_target_cents, m.refill_amount_cents, "
        "       CASE WHEN rc.account_id IS NULL THEN rc.name ELSE ra.name END "
        "         AS refill_chain, rm.lot AS refill_lot, "
        "       CASE WHEN oc.account_id IS NULL THEN oc.name ELSE oa.name END "
        "         AS overflow_chain, om.lot AS overflow_lot "
        "FROM account_lot m "
        "JOIN account a ON a.id = m.account_id "
        "LEFT JOIN chain rc ON rc.id = m.refill_chain_id "
        "LEFT JOIN account ra ON ra.id = rc.account_id "
        "LEFT JOIN account_lot rm ON rm.id = rc.lot_id "
        "LEFT JOIN chain oc ON oc.id = m.overflow_chain_id "
        "LEFT JOIN account oa ON oa.id = oc.account_id "
        "LEFT JOIN account_lot om ON om.id = oc.lot_id "
        "ORDER BY a.name, m.id"
    ),
    "aux_table": "SELECT * FROM aux_table ORDER BY kind, series, effective_date",
    # Intent-file-only: not copied into run files (SNAPSHOT_TABLES), so this
    # only works on a ScenarioConnection, not a RunConnection.
    "intent_manifest": "SELECT * FROM intent_manifest",
}


class _TagReader:
    """Read API against ``self._con``. Per-item-type DataFrames are cached.

    ``self._con`` is opened on one thread (typically the notebook kernel's main
    thread) and SQLite connections are bound to it. A widget's query can come
    from another thread: JupyterLab sends widget messages to an ipykernel
    subshell (#375), whose thread is named ``subshell-<id>``. ``_read_sql``
    notices sqlite3's thread-mismatch error and falls back to a temporary
    read-only connection opened on the calling thread instead of raising, since
    every query here is read-only. It warns when the thread isn't a subshell,
    so an unexplained case still leaves a trace to diagnose from.
    """

    _con: sqlite3.Connection
    _item_cache: dict[str, pd.DataFrame]
    _derived_cache: dict[str, object]

    @property
    @abstractmethod
    def label(self) -> str:
        """Return a human-readable label for the scenario connection."""
        ...

    @property
    @abstractmethod
    def _db_path(self) -> Path:
        """Path of the SQLite file ``self._con`` is open on."""
        ...

    def _read_sql(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        """``pd.read_sql_query`` against ``self._con``, falling back to a
        temporary same-file connection if called from a different thread."""
        try:
            return pd.read_sql_query(sql, self._con, params=list(params))
        except sqlite3.ProgrammingError as exc:
            if "same thread" not in str(exc):
                raise
            # self.label is avoided here: on some subclasses it runs its own query
            # (e.g. RunConnection reads its metadata for its finish date), which would
            # hit this same cross-thread error and recurse back into this handler.
            if not threading.current_thread().name.startswith("subshell-"):
                warnings.warn(
                    f"{type(self).__name__} {self._db_path}: query reached from thread "
                    f"{threading.current_thread().name!r}, not the thread that opened "
                    "the connection; using a temporary read-only connection",
                    RuntimeWarning, stacklevel=2,
                )
            temp = connect_sim_file(f"file:{self._db_path}?mode=ro", uri=True)
            try:
                return pd.read_sql_query(sql, temp, params=list(params))
            finally:
                temp.close()

    def _ensure_fresh(self) -> None:
        """Hook for subclasses; default no-op (run files don't mutate)."""

    def list_tags(self, role: str | None = None) -> pd.DataFrame:
        """Return distinct tags with per-item-type usage counts."""
        self._ensure_fresh()
        sql = (
            "SELECT t.role, t.name, "
            "  (SELECT COUNT(*) FROM account_tag WHERE tag_id=t.id) AS n_account, "
            "  (SELECT COUNT(*) FROM scheduled_transaction_tag "
            "      WHERE tag_id=t.id) AS n_scheduled, "
            "  (SELECT COUNT(*) FROM calendar_event_tag "
            "      WHERE tag_id=t.id) AS n_calendar_event "
            "FROM tag t"
        )
        params: tuple = ()
        if role is not None:
            sql += " WHERE t.role = ?"
            params = (role,)
        sql += " ORDER BY t.role, t.name"
        return self._read_sql(sql, params)

    def items(
        self,
        item_type: ItemType,
        *,
        tags: str | Iterable[str] | Iterable[tuple[str, str]] | None = None,
        tags_match: Literal["any", "all"] = "any",
        **filters,
    ) -> pd.DataFrame:
        """Return rows of ``item_type``, optionally filtered.

        ``**filters`` keys must be columns on the underlying table; scalar
        values match by equality, iterables (list/tuple/set) by ``isin``.
        ``tags`` accepts a single ``"role:name"`` string, an iterable of
        strings, or an iterable of ``(role, name)`` pairs; ``"role:*"`` matches
        every tag in that role. ``tags_match`` is ``"any"`` (default) or
        ``"all"``.
        """
        if item_type not in _ITEM_SQL:
            raise ValueError(f"unknown item_type {item_type!r}")
        self._ensure_fresh()
        df = self._items_cache(item_type)
        mask = pd.Series(True, index=df.index)

        for col, val in filters.items():
            if col not in df.columns:
                raise KeyError(
                    f"{col!r} is not a column of {item_type} "
                    f"(have: {list(df.columns)})"
                )
            if isinstance(val, str) or not hasattr(val, "__iter__"):
                mask &= df[col] == val
            else:
                mask &= df[col].isin(list(val))

        if tags is not None:
            if "_tag_set" not in df.columns:
                raise ValueError(
                    f"{item_type!r} does not support tag filtering"
                )
            wanted, roles = _split_tag_selectors(tags)
            if not wanted and not roles:
                mask &= False
            else:
                def has_role(tag_set: set[str]) -> bool:
                    return {t.partition(":")[0] for t in tag_set} >= roles

                if tags_match == "all":
                    mask &= df["_tag_set"].apply(
                        lambda s: wanted.issubset(s) and has_role(s)
                    )
                else:
                    mask &= df["_tag_set"].apply(
                        lambda s: bool(s & wanted)
                        or bool({t.partition(":")[0] for t in s} & roles)
                    )
        drop = ["_tag_set"] if "_tag_set" in df.columns else []
        return df.loc[mask].drop(columns=drop).reset_index(drop=True)

    def tags_for_item(self, item_type: ItemType, item_id: int) -> pd.DataFrame:
        """Return the tags attached to one item."""
        self._ensure_fresh()
        link = {
            "account": ("account_tag", "account_id"),
            "scheduled": ("scheduled_transaction_tag", "scheduled_transaction_id"),
            "calendar_event": ("calendar_event_tag", "calendar_event_id"),
        }[item_type]
        sql = (
            f"SELECT t.role, t.name FROM tag t "
            f"JOIN {link[0]} x ON x.tag_id=t.id "
            f"WHERE x.{link[1]}=? ORDER BY t.role, t.name"
        )
        return self._read_sql(sql, (item_id,))

    # -- internal --

    def _items_cache(self, item_type: str) -> pd.DataFrame:
        cache = getattr(self, "_item_cache", None)
        if cache is None:
            cache = {}
            self._item_cache = cache
        if item_type not in cache:
            df = self._read_sql(_ITEM_SQL[item_type])
            if "tags" in df.columns:
                df["_tag_set"] = df["tags"].apply(
                    lambda s: set(s.split()) if s else set()
                )
            cache[item_type] = df
        return cache[item_type]

    def _invalidate_items_cache(self) -> None:
        if getattr(self, "_item_cache", None) is not None:
            self._item_cache.clear()
        if getattr(self, "_derived_cache", None) is not None:
            self._derived_cache.clear()

    def _cached(self, key: str, builder):
        """Memoize a derived-data ``builder()`` call for the life of this connection.

        Not keyed on ``builder``'s arguments -- callers with parameters (e.g.
        a ``kind`` filter) should key on those explicitly, or leave the result
        uncached if it's cheap relative to the ``items()`` call it rests on.
        """
        cache = getattr(self, "_derived_cache", None)
        if cache is None:
            cache = {}
            self._derived_cache = cache
        if key not in cache:
            cache[key] = builder()
        return cache[key]

    def __repr__(self) -> str:
        clsname = type(self).__name__
        try:
            return f'<{clsname} "{self.label}">'
        except Exception as ex:
            return f'<{clsname} ({ex})>'


class Connection(_TagReader):
    """
    Abstract base class for a connection to a scenario or run.

    Subclasses must implement the abstract properties and methods to provide
    access to the underlying data and metadata.
    """

    @property
    def name(self) -> str:
        """The name of the connection."""
        raise NotImplementedError

    @property
    @abstractmethod
    def label(self) -> str:
        """A human-readable label for this connection."""
        pass

    @property
    @abstractmethod
    def data_dir(self) -> Path:
        """The data directory associated with this connection."""
        pass

    @property
    @abstractmethod
    def scenario(self) -> ScenarioConnection:
        """The scenario connection associated with this connection."""
        pass

    @property
    @abstractmethod
    def scenario_name(self) -> str:
        """The name of the scenario associated with this connection."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close the connection and release any associated resources."""
        pass


# ---------- run (read-only) ----------

class RunConnection(Connection):
    """Read-only access to a finalized run snapshot DB.

    Calling :meth:`close` (or using the context manager) frees the SQLite
    handle, but is optional: leaking a connection only costs a file handle
    until interpreter shutdown, so notebooks can construct a bare
    ``RunConnection(path)`` and use it across cells without ceremony.
    """

    @property
    def name(self) -> str:
        return self.metadata().scenario_name[0]
    run_path: Path
    _con: sqlite3.Connection
    @property
    def date(self) -> pd.Timestamp:
        return pd.to_datetime(self.metadata().finished_at)[0]

    @property
    def label(self) -> str:
        return f"{self.name}@{self.date.strftime('%Y-%m-%d %H:%M')}"

    _scenario: ScenarioConnection | None
    @property
    def scenario(self) -> ScenarioConnection:
        if self._scenario is None:
            # A run file doesn't record its data repo, so there is no
            # scenario to open here; open_run() supplies one.
            raise RuntimeError(
                f"run {self.run_path} was opened without its scenario; use open_run() "
                "or pass a ScenarioConnection"
            )
        return self._scenario

    _scenario_name: str | None
    @property
    def scenario_name(self) -> str:
        if self._scenario_name is None:
            self._scenario_name = self.scenario.name
        return self._scenario_name

    @property
    def data_dir(self) -> Path:
        return self.scenario.data_dir

    def __init__(self, run_path: Path | str,
                  scenario: ScenarioConnection | str | None = None) -> None:
        if isinstance(scenario, str):
            self._scenario_name = scenario
            self._scenario = None
        else:
            self._scenario_name = None
            self._scenario = scenario

        self.run_path = Path(run_path)
        self._con = connect_sim_file(self.run_path)
        self._con.row_factory = sqlite3.Row

    @property
    def _db_path(self) -> Path:
        return self.run_path

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> RunConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- derived data (cached per instance; see _TagReader._cached) --

    def net_worth_frame(self) -> pd.DataFrame:
        """Month-indexed nominal + CPI-constant net worth, in dollars."""

        def _build() -> pd.DataFrame:
            nw = self.items("net_worth").rename(columns={"net_worth": "nominal_cents"})
            cpi = self.items("net_worth_constant_cpi")[
                ["month", "constant_cents", "cumulative_index_micros"]
            ]
            nw = nw.merge(cpi, on="month", how="left")
            nw["month"] = pd.to_datetime(nw["month"])
            nw["nominal"] = nw["nominal_cents"] / 100
            nw["cpi_constant"] = nw["constant_cents"].astype("Float64") / 100
            return nw.set_index("month")

        return self._cached("net_worth_frame", _build)

    def after_tax_net_worth_frame(self) -> pd.DataFrame:
        """Month-indexed after-tax net worth (#251), ``nominal`` and
        ``cpi_constant``, in dollars, with the two deductions from net worth:
        ``deferred_tax``, the tax still owed on traditional balances and (unless
        the scenario's ``after_tax`` says ``step_up``) on taxable lots'
        unrealized gains, at each year's drawdown rate from the engine; and
        ``unpaid_tax``, a finished year's balance due until its April payment.
        """

        def _build() -> pd.DataFrame:
            nw = self.net_worth_frame()
            months = nw.index
            base = self._drawdown_base_by_month().reindex(months, fill_value=0)
            rates = self._drawdown_rate_by_year()
            years = pd.Index(index_years(nw))
            # A year with no rate computed (no tax rule in force) takes off
            # nothing, rather than an earlier year's rate.
            rate = rates.reindex(years).fillna(0.0).to_numpy() if len(rates) else 0.0
            frame = pd.DataFrame(index=months)
            frame["deferred_tax"] = base * rate
            frame["unpaid_tax"] = self._unpaid_tax_by_month(months)
            frame["nominal"] = nw["nominal"] - frame["deferred_tax"] - frame["unpaid_tax"]
            frame["cpi_constant"] = self.to_cpi_constant(frame["nominal"]).where(
                nw["cpi_constant"].notna())
            return frame

        return self._cached("after_tax_net_worth_frame", _build)

    def net_worth_series(self, mode: DollarMode = DollarMode.NOMINAL, *,
                         after_tax: bool = False) -> pd.Series:
        """Month-indexed net worth in ``mode`` dollars, after tax with
        ``after_tax`` (#251)."""
        column = "cpi_constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
        frame = self.after_tax_net_worth_frame() if after_tax else self.net_worth_frame()
        return frame[column]

    def _drawdown_base_by_month(self) -> pd.Series:
        """Each month's money an after-tax drawdown taxes, as the engine's
        ``_drawdown_tax`` counts it at a year-end, in dollars."""
        step_up = bool((self.scenario_meta().get("after_tax") or {}).get("step_up"))
        df = self._read_sql(
            "SELECT s.month, m.tax_treatment, a.subkind, ls.balance, ls.cost_basis "
            "FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
            "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
            "WHERE a.kind = 'asset' AND (m.tax_treatment = 'tax_deferred' "
            "  OR (m.tax_treatment = 'taxable' AND ls.cost_basis IS NOT NULL))"
        )
        deferred = df["tax_treatment"] == "tax_deferred"
        gain = ~deferred & ~df["subkind"].isin(registries.CASH_SUBKINDS)
        if step_up:
            gain = gain & False
        df["base"] = (df["balance"].clip(lower=0).where(deferred, 0)
                      + (df["balance"] - df["cost_basis"]).clip(lower=0).where(gain, 0))
        base = df.groupby("month")["base"].sum() / 100
        base.index = pd.to_datetime(base.index)
        return base

    def _drawdown_rate_by_year(self) -> pd.Series:
        """Each computed year's extra drawdown tax (federal plus state) over
        the money it taxes."""
        summary = self.items("tax_year_summary").pivot_table(
            index="year", columns="bucket", values="amount", aggfunc="sum")
        tax_columns = [c for c in summary.columns if str(c).endswith("_drawdown_tax")]
        base_columns = [c for c in summary.columns if str(c).endswith("_drawdown_base")]
        if not tax_columns:
            return pd.Series(dtype=float)
        base = summary[base_columns].max(axis=1)
        return (summary[tax_columns].sum(axis=1) / base.where(base > 0)).fillna(0.0)

    def _unpaid_tax_by_month(self, months: pd.Index) -> pd.Series:
        """A finished tax year's balance due, from its year-end until the
        month its payment posts (the April after), in dollars."""
        due = self.outstanding_tax_by_year()
        unpaid = pd.Series(0.0, index=months)
        if due.empty:
            return unpaid
        # Only a tax rule's payment settles the year: other rows can carry an
        # earlier tax_year too (a next-spring IRA contribution, say).
        paid = self._read_sql(
            "SELECT tax_year, MIN(date) AS date FROM transfer_event "
            "WHERE tax_year IS NOT NULL AND CAST(substr(date, 1, 4) AS INTEGER) > tax_year "
            "  AND reason_kind = 'rule' AND reason_id IN ("
            "    SELECT id FROM scheduled_transaction WHERE amount_rule_kind = 'tax_accumulator') "
            "GROUP BY tax_year"
        )
        settled = dict(zip(paid["tax_year"].astype(int),
                           pd.to_datetime(paid["date"]) + pd.offsets.MonthEnd(0), strict=True))
        for year, amount in due[due > 0].items():
            start = pd.Timestamp(f"{int(year)}-12-31")
            end = settled.get(int(year))
            owed = (months >= start) & ((months < end) if end is not None else True)
            unpaid[owed] += amount
        return unpaid

    def cpi_index(self) -> pd.Series:
        """Month-indexed cumulative CPI index (micros), for CPI-constant conversion."""
        return self._cached(
            "cpi_index", lambda: self.net_worth_frame()["cumulative_index_micros"]
        )

    def to_cpi_constant(self, monthly: pd.DataFrame | pd.Series):
        """Convert a month-indexed dollar series/frame to this run's CPI-constant dollars.

        A month before the run's first (a year-to-date catch-up dated before
        the scenario starts) takes the first month's CPI, and one after its
        last the last's: the run has no index for them.
        """
        return to_cpi_constant(monthly, self.cpi_index())

    def to_cpi_constant_annual(self, yearly: pd.DataFrame | pd.Series):
        """Convert a year-indexed dollar series/frame using each year's year-end CPI,
        the first or last year's for a year outside the run."""
        cpi = self.cpi_index()
        year_end_index = cpi.groupby(index_years(cpi)).last()
        return yearly.mul(_cpi_factor(year_end_index, yearly.index), axis=0)

    def account_kinds(self) -> pd.Series:
        """Account name -> kind, from this run's account-balance history."""

        def _build() -> pd.Series:
            df = self.items("account_balance")
            return df.drop_duplicates("account").set_index("account")["kind"]

        return self._cached("account_kinds", _build)

    def balance_pivot(self, kind: AccountKindAny | None = None) -> pd.DataFrame:
        """Month x account balance pivot (dollars), optionally filtered to one ``kind``.

        For income/expense accounts the balance is already the engine's own
        running cumulative total (``account_state`` tracks every account, not
        just assets/liabilities), so this needs no separate flow/cumsum pass.
        Income accounts are stored as credits (negative balances), but are
        displayed as positive cumulative inflows.
        """

        def _build() -> pd.DataFrame:
            df = self.items("account_balance").copy()
            df["month"] = pd.to_datetime(df["month"])
            pivot = df.pivot_table(
                index="month", columns="account", values="balance", aggfunc="last"
            )
            return pivot / 100

        full = self._cached("balance_pivot", _build)
        if kind is None:
            return full
        kinds = self.account_kinds()
        cols = [
            c 
            for c in full.columns
            if kind == "any"
            or kinds.get(c) == kind
        ]
        pivot = full[cols]
        return -pivot if kind == "income" else pivot

    def cpi_balance_pivot(self, kind: AccountKindAny | None = None) -> pd.DataFrame:
        """``balance_pivot(kind)`` converted to CPI-constant dollars.

        Income and expense balances are cumulative flows. Converting their
        monthly changes before accumulating avoids a false negative change
        after a nominal flow has stopped while CPI continues increasing.
        """
        balances = self.balance_pivot(kind)
        if kind not in accounts.CASHFLOW_KINDS:
            return self.to_cpi_constant(balances)
        monthly_flows = balances.diff().fillna(balances)
        return self.to_cpi_constant(monthly_flows).cumsum()

    def account_balance(self, account: str, mode: DollarMode = DollarMode.NOMINAL) -> pd.Series:
        """One account's month-indexed balance in dollars, signed and
        converted as ``balance_pivot``/``cpi_balance_pivot`` would show it,
        read with its own query rather than the whole ``account_balance``
        table: an ensemble reads one account from hundreds of member files
        (#163). Empty when this run has no such account."""
        df = self._read_sql(
            "SELECT s.month, a.kind, ast.balance "
            "FROM account_state ast "
            "JOIN simulation_step s ON s.id = ast.step_id "
            "JOIN account a         ON a.id = ast.account_id "
            "WHERE a.name = ? ORDER BY s.month",
            (account,),
        )
        balance = pd.Series(
            df["balance"].to_numpy() / 100, index=pd.to_datetime(df["month"]), name=account,
        )
        kind = df["kind"].iloc[0] if len(df) else None
        if kind == "income":
            balance = -balance
        if mode != DollarMode.CPI_CONSTANT or balance.empty:
            return balance
        return account_balance_cpi(balance, kind, self.cpi_index())

    def lot_balance(
        self, account: str, lot: str, mode: DollarMode = DollarMode.NOMINAL,
    ) -> pd.Series:
        """One lot's month-indexed balance in dollars, like ``account_balance``
        for one of the account's lots (#170): its own query, so an ensemble
        reads one lot cheaply from every member. Empty when this run has no
        such lot."""
        df = self._read_sql(
            "SELECT month, balance FROM balance_by_lot "
            "WHERE account = ? AND lot = ? ORDER BY month",
            (account, lot),
        )
        balance = pd.Series(
            df["balance"].to_numpy() / 100, index=pd.to_datetime(df["month"]),
            name=f"{account}/{lot}",
        )
        if mode != DollarMode.CPI_CONSTANT or balance.empty:
            return balance
        return to_cpi_constant(balance, self.cpi_index())

    def lots_by_account(self, *, single: bool = False) -> dict[str, list[str]]:
        """Account name -> its lot labels in definition order, for the asset
        and liability accounts that have more than one lot: the ones a lot
        picker can drill into (#170). Income and expense accounts are flows,
        tracked as a single lot. ``single`` keeps single-lot accounts too,
        for a union across runs: an account split into lots in one run may
        have one lot of its own in another."""

        def _build() -> dict[str, list[str]]:
            df = self._read_sql(
                "SELECT a.name AS account, m.lot FROM account_lot m "
                "JOIN account a ON a.id = m.account_id "
                "WHERE a.kind IN ('asset', 'liability') ORDER BY a.name, m.id"
            )
            lots = df.groupby("account", sort=True)["lot"].agg(list)
            return {account: names for account, names in lots.items()
                    if single or len(names) > 1}

        return self._cached(f"lots_by_account_{single}", _build)

    def ensemble_member(self) -> tuple[int, int | None] | None:
        """``(member_index, seed)`` from this run's ``ensemble_manifest``, or
        None for a run that isn't an ensemble member."""
        row = self._read_sql("SELECT member_index, seed FROM ensemble_manifest")
        if row.empty:
            return None
        seed = row["seed"].iloc[0]
        return int(row["member_index"].iloc[0]), None if pd.isna(seed) else int(seed)

    def negative_balances(self) -> list[NegativeBalance]:
        """The run's negative-balance warnings (#431), as ``sim explain``
        lists them: see ``shortfalls.negative_balances``."""
        from analysis.sim import shortfalls

        # Its own read-only connection, opened on the calling thread: a
        # widget redraw can come from a kernel subshell (see _read_sql).
        return self._cached("negative_balances",
                            lambda: shortfalls.run_negative_balances(self.run_path))

    def deaths(self) -> list[Death]:
        """Each person's death in this run (#255), from ``lifespan.csv``: the
        display name and the final month, earliest first. A chart marks them."""
        rows = self._read_sql(
            "SELECT p.display, l.final_month FROM person_lifespan l "
            "JOIN person p ON p.id = l.person ORDER BY l.final_month, p.display"
        )
        return [Death(str(display), date.fromisoformat(str(month)))
                for display, month in zip(rows["display"], rows["final_month"], strict=True)]

    def household_end(self) -> _Date | None:
        """The household's last death, when every member of it has a lifespan
        (the month a ``joint`` rule stops at, ``engine._rule_lifespans``)."""
        persons = self.items("person")
        household = list(persons.loc[persons["role"] == "primary", "id"])
        if not household:
            household = list(persons.loc[persons["role"].notna(), "id"])
        lifespans = self.items("person_lifespan").set_index("person")["final_month"]
        if not household or not all(p in lifespans.index for p in household):
            return None
        return date.fromisoformat(max(str(lifespans[p]) for p in household)[:10])

    def date_markers(self) -> dict[str, _Date]:
        """Each named ``date_marker`` calendar row's date in this run."""
        events = self.items("calendar_event")
        return {
            str(name): date.fromisoformat(str(when)[:10])
            for name, kind, when in zip(events["name"], events["kind"], events["date"], strict=True)
            if kind == "date_marker" and name
        }

    def monthly_flows(self) -> pd.DataFrame:
        """Every transfer summed by its own date's month end (``event_month``),
        its from and to accounts and, for a scheduled rule's expense, the
        rule's tags (sorted, space-separated; '' otherwise): columns
        ``month``, ``from_name``, ``to_name``, ``rule_tags``, ``cents``.
        Enough to rebuild any guard rail's spending or withdrawals (#305), so
        the series store keeps it for each ensemble member."""
        events = self.items("transfer_event")
        rules = self.items("scheduled").set_index("id")["tags"]
        by_rule = (events["reason_kind"].isin(["rule", "ytd_rule"]) & events["reason_id"].notna()
                   & (events["to_kind"] == "expense"))
        tags = {int(rid): " ".join(sorted(str(cell or "").split())) for rid, cell in rules.items()}
        flows = pd.DataFrame({
            "month": event_month(events),
            "from_name": events["from_name"].astype(str),
            "to_name": events["to_name"].astype(str),
            "rule_tags": [tags.get(int(rid), "") if rule else ""
                          for rid, rule in zip(events["reason_id"], by_rule, strict=True)],
            "cents": events["amount"].astype("int64"),
        })
        return (flows.groupby(["month", "from_name", "to_name", "rule_tags"], as_index=False)["cents"]
                .sum())

    def scenario_meta(self) -> dict[str, Any]:
        """The run's merged ``scenario.meta_json`` (ADR-003 §4.5)."""
        row = self._read_sql("SELECT meta_json FROM scenario")
        if row.empty or not row["meta_json"].iloc[0]:
            return {}
        return json.loads(row["meta_json"].iloc[0])

    LOT_MEASURES = ("balance", "cost_basis", "quantity")

    def lot_balance_pivot(self, measure: str = "balance") -> pd.DataFrame:
        """Month x ``Account/lot`` pivot of asset lots: ``balance`` and ``cost_basis``
        in dollars, ``quantity`` in shares (NaN where a lot's basis or quantity
        is not tracked).
        """
        if measure not in self.LOT_MEASURES:
            raise ValueError(f"measure must be one of {self.LOT_MEASURES}, got {measure!r}")

        def _build() -> pd.DataFrame:
            df = self.items("lot_balance")
            df = df[df["kind"] == "asset"].copy()
            df["month"] = pd.to_datetime(df["month"])
            df["label"] = df["account"] + "/" + df["lot"]
            df["value"] = (
                df["quantity_micros"] / 1_000_000 if measure == "quantity"
                else df[measure] / 100
            )
            return df.pivot(index="month", columns="label", values="value")

        return self._cached(f"lot_balance_pivot_{measure}", _build)

    def cpi_lot_balance_pivot(self, measure: str = "balance") -> pd.DataFrame:
        """``lot_balance_pivot(measure)`` in CPI-constant dollars (shares are unchanged)."""
        pivot = self.lot_balance_pivot(measure)
        return pivot if measure == "quantity" else self.to_cpi_constant(pivot)

    def income_by_category(
        self, subkind_to_category: dict[str, str] = INCOME_SUBKIND_TO_CATEGORY,
    ) -> pd.DataFrame:
        """Monthly income by category, plus a ``total`` column, in dollars.

        Rows whose subkind isn't in ``subkind_to_category`` are computed as
        "other" but -- matching this method's notebook origin -- dropped from
        the result along with ``total``, since only the categories in
        :data:`INCOME_CATEGORIES` are kept.
        """

        def _build() -> pd.DataFrame:
            xfer = self.items("transfer_event", from_kind="income").copy()
            xfer["month"] = event_month(xfer)
            xfer["category"] = xfer["from_subkind"].map(subkind_to_category).fillna("other")
            income = (
                xfer.groupby(["month", "category"])["amount"].sum()
                .unstack("category", fill_value=0)
                .reindex(columns=INCOME_CATEGORIES, fill_value=0)
                / 100
            )
            income["total"] = income.sum(axis=1)
            return income

        return self._cached("income_by_category", _build)

    def expenses_by_category(
        self,
        subkind_to_category: dict[str, str] = EXPENSE_SUBKIND_TO_CATEGORY,
        non_cash_accounts: set[str] = NON_CASH_EXPENSE_ACCOUNTS,
    ) -> pd.DataFrame:
        """Monthly expenses by category, plus a ``total`` column, in dollars."""

        def _build() -> pd.DataFrame:
            xfer_exp = self.items("transfer_event", to_kind="expense").copy()
            xfer_exp = xfer_exp[~xfer_exp["to_name"].isin(non_cash_accounts)]
            xfer_exp["month"] = event_month(xfer_exp)
            xfer_exp["category"] = (
                xfer_exp["to_subkind"].map(subkind_to_category).fillna("other")
            )
            expenses = (
                xfer_exp.groupby(["month", "category"])["amount"].sum()
                .unstack("category", fill_value=0)
                .reindex(columns=EXPENSE_CATEGORIES, fill_value=0)
                / 100
            )
            expenses["total"] = expenses.sum(axis=1)
            return expenses

        return self._cached("expenses_by_category", _build)

    def gains_by_category(self) -> pd.DataFrame:
        """Year-indexed realized (taxable/non-taxable) vs. unrealized gains, in dollars.

        Social Security is split into taxable/non-taxable by this run's own
        ``federal_taxable_ss`` (the provisional-income test), not a flat
        assumption. Also includes ``expenses``/``distribution_coverage``
        (realized cash vs. that year's spending). A run whose strategies
        harvested gains (#421: sold and rebought, so no cash moved) adds a
        ``harvested`` column, outside the realized cash total.
        """

        def _build() -> pd.DataFrame:
            gains = self.items("transfer_event")
            gains = gains[gains["reason_kind"] != "initial"].copy()
            gains["month"] = pd.to_datetime(gains["month"])
            # The event's own date, not its step: a prior year's year-to-date
            # row posts in the first step but belongs to that year.
            gains["year"] = pd.to_datetime(gains["date"]).dt.year

            reaches_cash = gains["to_subkind"].isin(LIQUID_SUBKINDS)
            is_unrealized = (gains["from_kind"] == "income") & ~reaches_cash
            is_taxable = reaches_cash & gains["tax_character"].isin(TAXABLE_GAIN_CHARS)
            is_nontaxable = reaches_cash & gains["tax_character"].isin(NONTAXABLE_GAIN_CHARS)
            is_ss = gains["tax_character"] == "ss_benefit"
            is_harvest = (
                (gains["reason_kind"] == "strategy") & (gains["from_lot"] == gains["to_lot"])
                & (gains["from_account"] == gains["to_account"])
                & gains["tax_character"].isin(["ltcg", "stcg"])
            )

            gains["gain_category"] = pd.NA
            gains.loc[is_unrealized, "gain_category"] = "unrealized"
            gains.loc[is_taxable, "gain_category"] = "taxable"
            gains.loc[is_nontaxable, "gain_category"] = "non-taxable"
            gains.loc[is_ss, "gain_category"] = "social_security"
            gains.loc[is_harvest, "gain_category"] = "harvested"
            gains = gains.dropna(subset=["gain_category"])

            gains_yearly = (
                gains.groupby(["year", "gain_category"])["amount"].sum()
                .unstack("gain_category", fill_value=0)
                .reindex(columns=[*GAIN_CATEGORIES, "social_security", "harvested"], fill_value=0)
                / 100
            )
            gains_yearly.index.name = "year"

            tax_summary = self.items("tax_year_summary")
            federal_taxable_ss = (
                tax_summary.loc[
                    tax_summary["bucket"] == "federal_taxable_ss", ["year", "amount"]
                ].set_index("year")["amount"]
                / 100
            )
            ss_total = gains_yearly["social_security"]
            ss_taxable = (
                federal_taxable_ss.reindex(gains_yearly.index).fillna(0).clip(upper=ss_total)
            )
            ss_nontaxable = ss_total - ss_taxable

            gains_yearly["taxable"] += ss_taxable
            gains_yearly["non-taxable"] += ss_nontaxable
            gains_yearly = gains_yearly.drop(columns="social_security")
            harvested = gains_yearly.pop("harvested")

            gains_yearly["realized_total"] = (
                gains_yearly["taxable"] + gains_yearly["non-taxable"]
            )
            expenses_yearly = self.expenses_by_category().groupby(
                lambda month: month.year
            ).sum()
            gains_yearly["expenses"] = (
                expenses_yearly["total"].reindex(gains_yearly.index).fillna(0)
            )
            gains_yearly["distribution_coverage"] = (
                gains_yearly["realized_total"] - gains_yearly["expenses"]
            )
            if harvested.any():
                gains_yearly["harvested"] = harvested
            return gains_yearly

        return self._cached("gains_by_category", _build)

    def tax_liability_by_year(self) -> pd.Series:
        """Year-indexed total (federal + state) tax liability, in dollars."""

        def _build() -> pd.Series:
            tax_summary = self.items("tax_year_summary")
            return (
                tax_summary.loc[
                    tax_summary["bucket"].isin(
                        ["federal_tax_liability", "state_tax_liability"]
                    ),
                    ["year", "amount"],
                ]
                .groupby("year")["amount"]
                .sum()
                / 100
            )

        return self._cached("tax_liability_by_year", _build)

    def tax_reconciliation_by_year(self) -> pd.DataFrame:
        """Year-indexed liability/withheld/due/overpaid, federal and state, in dollars.

        Adds ``reconciliation_year`` (year + 1): the annual reconciliation
        payment or refund for year Y settles in April of Y+1.
        """

        def _build() -> pd.DataFrame:
            tax_summary = self.items("tax_year_summary")
            reconciliation = (
                tax_summary.loc[
                    tax_summary["bucket"].isin(_TAX_RECONCILIATION_BUCKETS),
                    ["year", "bucket", "amount"],
                ]
                .pivot_table(
                    index="year", columns="bucket", values="amount",
                    aggfunc="sum", fill_value=0,
                )
                .rename(columns=_TAX_RECONCILIATION_RENAME)
                / 100
            )
            reconciliation["reconciliation_year"] = reconciliation.index + 1
            return reconciliation

        return self._cached("tax_reconciliation_by_year", _build)

    def outstanding_tax_by_year(
        self, buckets: list[str] = DEFAULT_SETTLE_TAX_BUCKETS,
    ) -> pd.Series:
        """Year X's tax liability still unpaid at year end; it settles the April after.

        Only ``*_tax_due`` buckets are used. ``*_tax_overpaid`` looks like a
        refund but also fires when the *previous* April's settlement lands in
        a zero-liability year, so crediting it here would double-count that
        payment.
        """
        df = self.items("tax_year_summary")
        df = df[df["bucket"].isin(buckets)]
        wide = (
            df.pivot(index="year", columns="bucket", values="amount")
            .reindex(columns=buckets)
            .fillna(0)
            / 100
        )
        return wide.sum(axis=1)

    def tax_summary_by_year(
        self, buckets: dict[str, str] = DEFAULT_TAX_SUMMARY_BUCKETS,
    ) -> pd.DataFrame:
        """Yearly federal/state tax and SS income/taxed-portion, plus a total-tax column."""
        df = self.items("tax_year_summary")
        df = df[df["bucket"].isin(buckets)]
        wide = df.pivot(index="year", columns="bucket", values="amount") / 100
        wide = wide.rename(columns=buckets).reindex(columns=list(buckets.values()))
        wide["total tax"] = wide["federal tax"] + wide["state tax"]
        return wide

    def ss_gross_by_month(self) -> pd.Series:
        """Gross Social Security received per month (before Medicare withholding)."""

        def _build() -> pd.Series:
            te = self.items("transfer_event")
            te = te[te["tax_character"] == "ss_benefit"].copy()
            te["month"] = pd.to_datetime(te["month"])
            return te.groupby("month")["amount"].sum() / 100

        return self._cached("ss_gross_by_month", _build)

    def metadata(self) -> pd.DataFrame:
        """Return basic metadata about the run, including its name and path."""
        def _build() -> pd.DataFrame:
            meta = self.items("simulation_run")
            return meta
        return self._cached("metadata", _build)


@contextmanager
def open_run(run_path: Path | str,
             scenario: ScenarioConnection | str | None = None,
             scenarios_dir: Path | str | None = None,
             data_dir: Path | str | None = None) -> Generator[RunConnection]:
    data_dir = Path(data_dir or Path(run_path).parent).resolve()
    if scenarios_dir is None:
        scenarios_dir = data_dir / "scenarios"
    else:
        scenarios_dir = Path(scenarios_dir)
    scenarios_dir = scenarios_dir.resolve()
    match scenario:
        case ScenarioConnection():
            rc = RunConnection(run_path, scenario)
            try:
                yield rc
            finally:
                rc.close()
        case str() | None:
            with open_scenario(data_dir, scenario,
                               scenarios_dir=scenarios_dir) as scenario_conn:
                rc = RunConnection(run_path, scenario_conn)
                try:
                    yield rc   
                finally:
                    rc.close()
        case _:
            raise TypeError(f"Unexpected type for scenario: {type(scenario)}")

def find_latest_run(runs_dir: Path | str, scenario: str | None = None) -> Path:
    """Return the most recent run snapshot file under ``runs_dir``.

    Raises ``FileNotFoundError`` if there is none; when ``scenario`` is given,
    only that scenario's runs are considered.
    """
    run_file = latest_run_path(runs_dir, scenario)
    if run_file is None:
        where = f"{runs_dir} for scenario {scenario!r}" if scenario else str(runs_dir)
        raise FileNotFoundError(f"no run files under {where}")
    return run_file


# ---------- scenario (read + write through CSVs) ----------

_TAG_CSV_KEY: dict[ItemType, tuple[str, str]] = {
    # item_type -> (relative-csv-path-template, key column)
    # All scenario-centric: accounts.csv and scheduled.csv live in scenarios/<name>/
    "account": ("scenarios/{scenario}/accounts.csv", "name"),
    "scheduled": ("scenarios/{scenario}/scheduled.csv", "name"),
}


class ScenarioConnection(Connection, traitlets.HasTraits):
    """Read+write access to a scenario.

    Reads run against a freshly built intent DB; writes go to the CSV source
    of truth and mark the connection dirty so the next read transparently
    rebuilds the intent DB.

    Closing is optional. The only resources held are a SQLite handle and a
    temporary directory for the intent DB; both are reclaimed at interpreter
    shutdown, so notebooks can keep a bare ``ScenarioConnection(...)`` alive
    across cells without a ``with`` block.
    """

    @property
    def label(self) -> str:
        """
        Return a human-readable label for the scenario connection.
        """
        return f"{self.name}"

    _scenario_name: str

    @property
    def scenario_name(self) -> str:
        """
        Return the name of the scenario associated with this connection.
        """
        return self._scenario_name

    @property
    def name(self) -> str:
        return self._scenario_name

    _data_dir: Path
    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def scenario(self) -> ScenarioConnection:
        """A ScenarioConnection already is one -- returns itself, so
        polymorphic code written against `Connection` (``conn.scenario...``)
        works the same whether `conn` started as a RunConnection or a
        ScenarioConnection."""
        return self

    scenarios_dir: Path

    def __init__(self, data_dir: Path | str,
                 scenario: str | None = None,
                 scenarios_dir: Path | str | None = None,
                 intent_dir: Path | str | None = None,
                 state_dir: Path | str | None = None,
                 runs_dir: Path | str | None = None,
                 run_path: Path | str | None = None) -> None:
        self._data_dir = Path(data_dir).resolve()
        self.scenarios_dir = Path(scenarios_dir or (self.data_dir / "scenarios")).resolve()
        self.ref = resolve_scenario_name(self.data_dir, scenario,
                                         scenarios_dir=self.scenarios_dir,
                                         intent_dir=intent_dir,
                                         state_dir=state_dir,
                                         runs_dir=runs_dir,
                                         run_path=run_path)
        self._scenario_name = self.ref.name
        self._tmp = tempfile.TemporaryDirectory()
        if intent_dir is None:
            intent_dir = Path(self._tmp.name)
        self._intent_dir = Path(intent_dir).resolve()
        self._dirty = False
        self._reload()

    # -- lifecycle --

    def _reload(self) -> None:
        # Unset until the first load, and again once closed.
        old = getattr(self, "_con", None)
        if old is not None:
            old.close()
        load_scenario(
            self.data_dir, self._scenario_name,
            intent_dir=self._intent_dir,
            scenarios_dir=self.scenarios_dir,
            compute_stamps=False, allow_dirty_data=True,
        )
        con = connect_sim_file(self._intent_dir / f"{self._scenario_name}.sqlite3")
        con.row_factory = sqlite3.Row
        self._con = con
        self._invalidate_items_cache()
        self._dirty = False

    def _ensure_fresh(self) -> None:
        if self._dirty:
            self._reload()

    @property
    def _db_path(self) -> Path:
        return self._intent_dir / f"{self._scenario_name}.sqlite3"

    def close(self) -> None:
        con = getattr(self, "_con", None)
        if con is not None:
            con.close()
            del self._con  # closed: any later use is an AttributeError
        self._tmp.cleanup()

    def __enter__(self) -> ScenarioConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- lifespans --

    def lifespans(self) -> tuple[list[PersonLifespan], list[str]]:
        """Each person with a sex, with the age-at-death distribution the
        life table gives (#312), and a note for each one it can't draw:
        ``lifespan_curves.person_lifespans`` on this scenario's intent."""
        from analysis.sim.lifespan_curves import person_lifespans

        self._ensure_fresh()
        return person_lifespans(self._con)

    # -- tag writes --

    def _tag_csv(self, item_type: ItemType) -> tuple[Path, str]:
        if item_type == "calendar_event":
            raise ValueError(
                "calendar_event tag writes are unsupported via ScenarioConnection "
                "(no natural unique key in calendar.csv)"
            )
        rel, key = _TAG_CSV_KEY[item_type]
        return self.data_dir / rel.format(scenario=self._scenario_name), key

    def _mutate_tags(
        self,
        item_type: ItemType,
        item_key: str,
        mutate,  # (list[tuple[str,str]]) -> list[tuple[str,str]]
    ) -> str:
        csv_path, key_col = self._tag_csv(item_type)
        if not csv_path.exists():
            raise FileNotFoundError(csv_path)
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        if "tags" not in df.columns:
            raise ValueError(f"{csv_path} has no 'tags' column; run v7 migration")
        mask = df[key_col] == item_key
        if not mask.any():
            raise KeyError(f"no row with {key_col}={item_key!r} in {csv_path}")
        if mask.sum() > 1:
            raise ValueError(f"multiple rows with {key_col}={item_key!r}")
        idx = df.index[mask][0]
        existing = registries.parse_tags(df.at[idx, "tags"])
        new_val = registries.format_tags(mutate(existing))
        df.at[idx, "tags"] = new_val
        df.to_csv(csv_path, index=False)
        self._dirty = True
        self._invalidate_items_cache()
        return new_val

    def add_tags(
        self,
        item_type: ItemType,
        item_key: str,
        tokens: Iterable[str] | str,
    ) -> str:
        """Add tag tokens (idempotent). Returns the new tags string."""
        raw = tokens if isinstance(tokens, str) else " ".join(tokens)
        new_pairs = registries.parse_tags(raw)
        return self._mutate_tags(
            item_type, item_key, lambda old: old + new_pairs,
        )

    def remove_tags(
        self,
        item_type: ItemType,
        item_key: str,
        tokens: Iterable[str] | str,
    ) -> str:
        raw = tokens if isinstance(tokens, str) else " ".join(tokens)
        drop = set(registries.parse_tags(raw))
        return self._mutate_tags(
            item_type, item_key, lambda old: [p for p in old if p not in drop],
        )

    def set_tags(
        self,
        item_type: ItemType,
        item_key: str,
        tokens: Iterable[str] | str,
    ) -> str:
        raw = tokens if isinstance(tokens, str) else " ".join(tokens)
        pairs = registries.parse_tags(raw)
        return self._mutate_tags(item_type, item_key, lambda _old: pairs)


@contextmanager
def open_scenario(
    data_dir: Path | str, scenario: str | None = None,
    scenarios_dir: Path | str | None = None,
) -> Generator[ScenarioConnection]:
    sc = ScenarioConnection(data_dir, scenario,
                            scenarios_dir=scenarios_dir)
    try:
        yield sc
    finally:
        sc.close()


__all__ = [
    "ItemType",
    "RunConnection",
    "ScenarioConnection",
    "Connection",
    "open_run",
    "open_scenario",
]
