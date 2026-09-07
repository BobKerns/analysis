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

import sqlite3
import tempfile
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import pandas as pd
import traitlets

from . import registries
from .loader import latest_run_path, load_scenario, resolve_scenario_name

# Category-bucketing defaults for RunConnection's derived-data methods below.
# Notebooks may pass their own mapping/set to override any of these.
INCOME_SUBKIND_TO_CATEGORY: dict[str, str] = {
    "salary": "salary",
    "interest": "investment",
    "dividend": "investment",
    "capital_gain": "investment",
    "ss_benefit": "social security",
}
INCOME_CATEGORIES = ["salary", "social security", "investment"]

EXPENSE_SUBKIND_TO_CATEGORY: dict[str, str] = {
    "housing": "housing",
    "interest_expense": "housing",
    "taxes_property": "housing",
    "healthcare": "healthcare",
    "taxes_federal": "taxes",
    "taxes_state": "taxes",
    "living": "living",
    "food": "living",
}
EXPENSE_CATEGORIES = ["housing", "healthcare", "taxes", "living", "other"]
# depreciation is a book entry, not spending -- see ADR-002 gains/losses.
NON_CASH_EXPENSE_ACCOUNTS = {"depreciation"}

LIQUID_SUBKINDS = {"checking", "savings"}
TAXABLE_GAIN_CHARS = {"ordinary", "ltcg", "tax_deferred_out"}  # ss_benefit handled separately
NONTAXABLE_GAIN_CHARS = {"tax_free"}
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

ItemType = Literal[
    "account",
    "scheduled",
    "calendar_event",
    "transfer_event",
    "simulation_run",
    "net_worth",
    "net_worth_constant_cpi",
    "account_balance",
    "account_model_member",
    "aux_table",
    "scenario",
    "tax_year_summary",
    "year_to_date",
    "person",
    "person_lifespan",
    "chain",
    "chain_steps",
    "intent_manifest",
]


def year_end_values(frame: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Last row of each calendar year from a month-indexed frame/series.

    Re-indexed by year (int).
    """
    year_end = frame.groupby(frame.index.year).tail(1)
    year_end.index = year_end.index.year
    return year_end


def yearly_totals(monthly: pd.DataFrame) -> pd.DataFrame:
    """Sum a month-indexed frame (e.g. from ``income_by_category``) by calendar year."""
    yearly = monthly.groupby(monthly.index.year).sum()
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
    "person_lifespan": "SELECT * FROM person_lifespan ORDER BY person",
    "chain": (
        "SELECT c.*, a.name AS account_name, a.kind AS account_kind "
        "FROM chain c "
        "LEFT JOIN account a ON a.id = c.account_id "
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
        "       fa.subkind AS from_subkind, "
        "       ta.name AS to_name, ta.kind AS to_kind, "
        "       ta.subkind AS to_subkind "
        "FROM transfer_event te "
        "JOIN simulation_step s ON s.id = te.step_id "
        "JOIN account fa ON fa.id = te.from_account "
        "JOIN account ta ON ta.id = te.to_account "
        "ORDER BY te.id"
    ),
    "simulation_run": "SELECT * FROM simulation_run",
    "net_worth": "SELECT * FROM net_worth ORDER BY month",
    "net_worth_constant_cpi": (
        "SELECT * FROM net_worth_constant_cpi ORDER BY month"
    ),
    "year_to_date": (
        "SELECT y.*, fa.name AS from_name, ta.name AS to_name "
        "FROM year_to_date y "
        "JOIN account fa ON fa.id = y.from_account "
        "JOIN account ta ON ta.id = y.to_account"
    ),
    "tax_year_summary": "SELECT * FROM tax_year_summary ORDER BY year, bucket",
    "account_balance": (
        "SELECT s.month, a.id AS account_id, a.name AS account, "
        "       a.kind, a.subkind, ast.balance "
        "FROM account_state ast "
        "JOIN simulation_step s ON s.id = ast.step_id "
        "JOIN account a         ON a.id = ast.account_id "
        "ORDER BY s.month, a.name"
    ),
    "account_model_member": (
        "SELECT a.name AS account, a.kind, a.subkind, a.tax_treatment, "
        "       m.opening_balance, m.opening_cost_basis, "
        "       m.prior_year_end_balance "
        "FROM account_model_member m "
        "JOIN account a ON a.id = m.account_id "
        "ORDER BY a.name"
    ),
    "aux_table": "SELECT * FROM aux_table ORDER BY kind, series, effective_date",
    # Intent-file-only: not copied into run files (SNAPSHOT_TABLES), so this
    # only works on a ScenarioConnection, not a RunConnection.
    "intent_manifest": "SELECT * FROM intent_manifest",
}


class _TagReader:
    """Read API against ``self._con``. Per-item-type DataFrames are cached."""

    _con: sqlite3.Connection
    _item_cache: dict[str, pd.DataFrame]
    _derived_cache: dict[str, object]

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
        return pd.read_sql_query(sql, self._con, params=params)

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
        return pd.read_sql_query(sql, self._con, params=(item_id,))

    # -- internal --

    def _items_cache(self, item_type: str) -> pd.DataFrame:
        cache = getattr(self, "_item_cache", None)
        if cache is None:
            cache = {}
            self._item_cache = cache
        if item_type not in cache:
            df = pd.read_sql_query(_ITEM_SQL[item_type], self._con)
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


# ---------- run (read-only) ----------

class RunConnection(_TagReader):
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

    def __init__(self, run_path: Path | str) -> None:
        self.run_path = Path(run_path)
        self._con = sqlite3.connect(self.run_path)
        self._con.row_factory = sqlite3.Row

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

    def cpi_index(self) -> pd.Series:
        """Month-indexed cumulative CPI index (micros), for CPI-constant conversion."""
        return self._cached(
            "cpi_index", lambda: self.net_worth_frame()["cumulative_index_micros"]
        )

    def to_cpi_constant(self, monthly: pd.DataFrame | pd.Series):
        """Convert a month-indexed dollar series/frame to this run's CPI-constant dollars."""
        factor = 1_000_000 / self.cpi_index().reindex(monthly.index)
        return monthly.mul(factor, axis=0)

    def to_cpi_constant_annual(self, yearly: pd.DataFrame | pd.Series):
        """Convert a year-indexed dollar series/frame using each year's year-end CPI."""
        cpi = self.cpi_index()
        year_end_index = cpi.groupby(cpi.index.year).last()
        return yearly.mul(1_000_000 / year_end_index.reindex(yearly.index), axis=0)

    def account_kinds(self) -> pd.Series:
        """Account name -> kind, from this run's account-balance history."""

        def _build() -> pd.Series:
            df = self.items("account_balance")
            return df.drop_duplicates("account").set_index("account")["kind"]

        return self._cached("account_kinds", _build)

    def balance_pivot(self, kind: str | None = None) -> pd.DataFrame:
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
        cols = [c for c in full.columns if kinds.get(c) == kind]
        pivot = full[cols]
        return -pivot if kind == "income" else pivot

    def cpi_balance_pivot(self, kind: str | None = None) -> pd.DataFrame:
        """``balance_pivot(kind)`` converted to CPI-constant dollars.

        Income and expense balances are cumulative flows. Converting their
        monthly changes before accumulating avoids a false negative change
        after a nominal flow has stopped while CPI continues increasing.
        """
        balances = self.balance_pivot(kind)
        if kind not in {"income", "expense"}:
            return self.to_cpi_constant(balances)
        monthly_flows = balances.diff().fillna(balances)
        return self.to_cpi_constant(monthly_flows).cumsum()

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
            xfer["month"] = pd.to_datetime(xfer["month"])
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
            xfer_exp["month"] = pd.to_datetime(xfer_exp["month"])
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
        (realized cash vs. that year's spending).
        """

        def _build() -> pd.DataFrame:
            gains = self.items("transfer_event")
            gains = gains[gains["reason_kind"] != "initial"].copy()
            gains["month"] = pd.to_datetime(gains["month"])
            gains["year"] = gains["month"].dt.year

            reaches_cash = gains["to_subkind"].isin(LIQUID_SUBKINDS)
            is_unrealized = (gains["from_kind"] == "income") & ~reaches_cash
            is_taxable = reaches_cash & gains["tax_character"].isin(TAXABLE_GAIN_CHARS)
            is_nontaxable = reaches_cash & gains["tax_character"].isin(NONTAXABLE_GAIN_CHARS)
            is_ss = gains["tax_character"] == "ss_benefit"

            gains["gain_category"] = pd.NA
            gains.loc[is_unrealized, "gain_category"] = "unrealized"
            gains.loc[is_taxable, "gain_category"] = "taxable"
            gains.loc[is_nontaxable, "gain_category"] = "non-taxable"
            gains.loc[is_ss, "gain_category"] = "social_security"
            gains = gains.dropna(subset=["gain_category"])

            gains_yearly = (
                gains.groupby(["year", "gain_category"])["amount"].sum()
                .unstack("gain_category", fill_value=0)
                .reindex(columns=[*GAIN_CATEGORIES, "social_security"], fill_value=0)
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

    def metadata(self) -> pd.Series:
        """Return basic metadata about the run, including its name and path."""
        def _build() -> pd.Series:
            meta = self.items("simulation_run")
            return meta
        return self._cached("metadata", _build)


@contextmanager
def open_run(run_path: Path | str) -> Generator[RunConnection]:
    rc = RunConnection(run_path)
    try:
        yield rc
    finally:
        rc.close()


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


class ScenarioConnection(_TagReader, traitlets.HasTraits):
    """Read+write access to a scenario.

    Reads run against a freshly built intent DB; writes go to the CSV source
    of truth and mark the connection dirty so the next read transparently
    rebuilds the intent DB.

    Closing is optional. The only resources held are a SQLite handle and a
    temporary directory for the intent DB; both are reclaimed at interpreter
    shutdown, so notebooks can keep a bare ``ScenarioConnection(...)`` alive
    across cells without a ``with`` block.
    """

    def __init__(self, data_dir: Path | str, scenario: str | None = None) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.ref = resolve_scenario_name(self.data_dir, scenario)
        self.scenario = self.ref.name
        self._tmp = tempfile.TemporaryDirectory()
        self._intent_dir = Path(self._tmp.name)
        self._con: sqlite3.Connection | None = None  # type: ignore[assignment]
        self._dirty = False
        self._reload()

    # -- lifecycle --

    def _reload(self) -> None:
        if self._con is not None:
            self._con.close()
        load_scenario(
            self.data_dir, self.scenario,
            intent_dir=self._intent_dir,
            compute_stamps=False, allow_dirty_data=True,
        )
        con = sqlite3.connect(self._intent_dir / f"{self.scenario}.sqlite3")
        con.row_factory = sqlite3.Row
        self._con = con
        self._invalidate_items_cache()
        self._dirty = False

    def _ensure_fresh(self) -> None:
        if self._dirty:
            self._reload()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None  # type: ignore[assignment]
        self._tmp.cleanup()

    def __enter__(self) -> ScenarioConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- tag writes --

    def _tag_csv(self, item_type: ItemType) -> tuple[Path, str]:
        if item_type == "calendar_event":
            raise ValueError(
                "calendar_event tag writes are unsupported via ScenarioConnection "
                "(no natural unique key in calendar.csv)"
            )
        rel, key = _TAG_CSV_KEY[item_type]
        return self.data_dir / rel.format(scenario=self.scenario), key

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
) -> Generator[ScenarioConnection]:
    sc = ScenarioConnection(data_dir, scenario)
    try:
        yield sc
    finally:
        sc.close()


__all__ = [
    "ItemType",
    "RunConnection",
    "ScenarioConnection",
    "open_run",
    "open_scenario",
]
