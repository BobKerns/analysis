"""#298: one file of an ensemble batch's member series, read in one go.

Reading an ensemble from its member run files rebuilds every member's net
worth (after tax, its drawdown and unpaid tax) or account balance from the
run's raw tables in pandas, tens of milliseconds a member, again for every
widget and every new choice of account or dollars. So ``sim run`` stores the
results beside the members, in ``<batch>/series.sqlite3``:

- ``batch``: the format and the code that wrote it, and the recipe's
  ``scenario.meta_json``;
- ``member_file``: every ``member_*.sqlite3`` the store was built from, by
  size and modification time, failed members included;
- ``member``: each succeeded member's index, seed, months, CPI index,
  CPI-constant net worth (the engine's own), deaths, and what guard rails
  (#305) read besides the series: the household's last death, the
  date_markers, and the accounts' kinds, subkinds and tags;
- ``subject``: net worth, after-tax net worth, each account, each asset or
  liability lot, and the dollars in each asset class (#359), for every
  asset lot and for each account tag's (a strategy's portfolio);
- ``series``: per subject and member, the nominal dollars by month;
- ``flow_key`` and ``flow``: per member, each month's transfers summed by
  from account, to account and rule tags (``RunConnection.monthly_flows``),
  from which any rail's spending or withdrawals is rebuilt at read time, so
  rails stay a lens on ensembles too;
- ``allocation_target`` and ``watch``: the batch's target sets, and the
  strategies steering to them with their bands, from which allocation drift
  is worked out at read time, as guard rails are.

CPI-constant figures other than pre-tax net worth are converted at read time,
by each member's own index and with the functions ``RunConnection`` uses, so
a store and the member files agree to the last bit. Each member's series is
computed by the process that ran it, while ``sim run``'s workers are still
running in parallel; a batch without a store (or with one written by other
code, or from other member files) gets one built the first time it's opened.

Values are zlib-compressed little-endian float64 arrays (months: ISO
dates), one per row, so one subject for every member is one indexed
query. The file is rebuilt whole, never updated.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from analysis.frames import index_years
from analysis.sim import drift as allocation_drift
from analysis.sim import units
from analysis.sim.asset_classes import weight_sets
from analysis.sim.connection import EXPENSE_CATEGORIES, RunConnection, account_balance_cpi, to_cpi_constant
from analysis.sim.schema import connect_sim_file
from analysis.sim.drift import Watch
from analysis.sim.types import Death, DollarMode

if TYPE_CHECKING:
    from analysis.sim.guard_rails import RunPieces

SERIES_DB_FILENAME = "series.sqlite3"
# Bump when the stored layout changes. A change to how a series is derived
# needs no bump: the store records a hash of the modules that derive them.
FORMAT_VERSION = 4

NET_WORTH = "net_worth"
AFTER_TAX = "after_tax"
ACCOUNT = "account"
LOT = "lot"
# An asset class's dollars: account is the portfolio tag ('' for every asset
# lot), lot the asset class.
CLASS = "class"
# Spending against the plan (#408): account is the expense category, lot
# DELIVERED or BUDGETED; each month's flow, not a balance.
SPENDING = "spending"
DELIVERED = "delivered"
BUDGETED = "budgeted"

_DDL = """
CREATE TABLE batch (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    format_version  INTEGER NOT NULL,
    code_hash       TEXT NOT NULL,
    built_at        TEXT NOT NULL,
    meta_json       TEXT NOT NULL
);
CREATE TABLE member_file (
    stem      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL
);
CREATE TABLE member (
    id             INTEGER PRIMARY KEY,
    stem           TEXT NOT NULL UNIQUE,
    member_index   INTEGER,
    seed           INTEGER,
    months         BLOB NOT NULL,
    cpi_index      BLOB NOT NULL,
    net_worth_cpi  BLOB NOT NULL,
    deaths_json    TEXT NOT NULL,
    household_end  TEXT,
    markers_json   TEXT NOT NULL,
    accounts_json  TEXT NOT NULL
);
-- account and lot are '' where they don't apply, so UNIQUE holds.
CREATE TABLE subject (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    account       TEXT NOT NULL,
    lot           TEXT NOT NULL,
    account_kind  TEXT,
    UNIQUE (kind, account, lot)
);
CREATE TABLE series (
    subject_id  INTEGER NOT NULL REFERENCES subject(id),
    member_id   INTEGER NOT NULL REFERENCES member(id),
    nominal     BLOB NOT NULL,
    PRIMARY KEY (subject_id, member_id)
) WITHOUT ROWID;
CREATE TABLE flow_key (
    id         INTEGER PRIMARY KEY,
    from_name  TEXT NOT NULL,
    to_name    TEXT NOT NULL,
    rule_tags  TEXT NOT NULL,
    UNIQUE (from_name, to_name, rule_tags)
);
CREATE TABLE flow (
    flow_id    INTEGER NOT NULL REFERENCES flow_key(id),
    member_id  INTEGER NOT NULL REFERENCES member(id),
    cents      BLOB NOT NULL,
    PRIMARY KEY (flow_id, member_id)
) WITHOUT ROWID;
CREATE TABLE allocation_target (
    target_set     TEXT NOT NULL,
    as_of          TEXT NOT NULL,
    asset_class    TEXT NOT NULL,
    weight_micros  INTEGER NOT NULL,
    PRIMARY KEY (target_set, as_of, asset_class)
);
CREATE TABLE watch (
    strategy    TEXT PRIMARY KEY,
    target_set  TEXT NOT NULL,
    portfolio   TEXT,
    band        REAL
);
"""


def series_db_path(batch_dir: Path) -> Path:
    return Path(batch_dir) / SERIES_DB_FILENAME


def _code_hash() -> str:
    """The format and the source of the modules that derive the series: a
    store written by different code is rebuilt, not trusted."""
    import analysis.sim.asset_classes as asset_classes
    import analysis.sim.connection as connection
    import analysis.sim.reports as reports

    digest = hashlib.sha256(str(FORMAT_VERSION).encode())
    for module_file in (connection.__file__, __file__, allocation_drift.__file__, asset_classes.__file__,
                        reports.__file__):
        digest.update(Path(module_file).read_bytes())
    return digest.hexdigest()


# A month a subject has no row for, in a cents array.
_MISSING = np.iinfo(np.int64).min


def _pack(values: np.ndarray) -> bytes:
    """``values``, compressed: integer cents (``_MISSING`` for no row) as
    month-to-month differences, which compress to a fraction of their
    size, or else float64 dollars as they are."""
    if values.dtype.kind == "i":
        cents = np.ascontiguousarray(values, dtype="<i8")
        # Wrapping arithmetic: cumsum undoes it exactly, sentinel included.
        deltas = np.diff(cents, prepend=np.int64(0))
        return b"c" + zlib.compress(deltas.tobytes(), 6)
    return b"f" + zlib.compress(np.ascontiguousarray(values, dtype="<f8").tobytes(), 1)


def _unpack(blob: bytes) -> np.ndarray:
    """``_pack``'s values as float64 dollars, NaN for no row."""
    raw = zlib.decompress(blob[1:])
    if blob[:1] == b"f":
        return np.frombuffer(raw, dtype="<f8")
    cents = np.cumsum(np.frombuffer(raw, dtype="<i8"))
    # Divided as the connection divides a run's cents, to the same bits.
    return np.where(cents == _MISSING, np.nan, cents / 100)


def _pack_months(index: pd.DatetimeIndex) -> bytes:
    return zlib.compress(",".join(index.strftime("%Y-%m-%d")).encode(), 1)


def _unpack_months(blob: bytes) -> pd.DatetimeIndex:
    # Parsed as the connection parses a run's months, so the index matches
    # it exactly, resolution included.
    return pd.DatetimeIndex(
        pd.to_datetime(pd.Series(zlib.decompress(blob).decode().split(","))), name="month")


def _floats(series: pd.Series) -> np.ndarray:
    """``series`` as float64, a missing value (``pd.NA`` too) as NaN."""
    return pd.to_numeric(series, errors="coerce").astype("float64").to_numpy(na_value=np.nan)


@dataclass
class MemberSeries:
    """One member's series, as ``member_series`` reads them from its run file."""

    stem: str
    member_index: int | None
    seed: int | None
    months: pd.DatetimeIndex
    cpi_index: np.ndarray
    net_worth_cpi: np.ndarray
    deaths: list[Death]
    meta_json: str
    # (kind, account, lot) -> (account kind, nominal dollars on ``months``,
    # NaN where the subject has no row that month)
    subjects: dict[tuple[str, str, str], tuple[str | None, np.ndarray]] = field(default_factory=dict)
    household_end: date | None = None
    markers: dict[str, date] = field(default_factory=dict)
    # name -> (kind, subkind, tags)
    accounts: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    # (from, to, rule tags) -> cents on ``months``
    flows: dict[tuple[str, str, str], np.ndarray] = field(default_factory=dict)
    # allocation_target rows, and the strategies steering to them (#359)
    targets: list[tuple[str, str, str, int]] = field(default_factory=list)
    watches: list[Watch] = field(default_factory=list)


def member_series(run_path: Path) -> MemberSeries:
    """Every series a store keeps for the member run file ``run_path``."""
    run_path = Path(run_path)
    con = RunConnection(run_path)
    try:
        nw = con.net_worth_frame()
        months = pd.DatetimeIndex(nw.index)
        member = con.ensemble_member()
        meta = con._read_sql("SELECT meta_json FROM scenario")
        out = MemberSeries(
            stem=run_path.stem,
            member_index=None if member is None else member[0],
            seed=None if member is None else member[1],
            months=months,
            cpi_index=_floats(nw["cumulative_index_micros"]),
            net_worth_cpi=_floats(nw["cpi_constant"]),
            deaths=con.deaths(),
            meta_json=str(meta["meta_json"].iloc[0] or "{}") if len(meta) else "{}",
            household_end=con.household_end(),
            markers=con.date_markers(),
        )
        accounts = con.items("account")
        out.accounts = {
            str(name): (str(kind), str(subkind or ""), str(tags or ""))
            for name, kind, subkind, tags in zip(
                accounts["name"], accounts["kind"], accounts["subkind"], accounts["tags"],
                strict=True)
        }
        _add_flows(out, con.monthly_flows(), months)
        out.subjects[(NET_WORTH, "", "")] = (None, nw["nominal_cents"].to_numpy(dtype=np.int64))
        out.subjects[(AFTER_TAX, "", "")] = (
            None, _floats(con.after_tax_net_worth_frame()["nominal"].reindex(months)))
        _add_balances(out, con._con, months)
        _add_classes(out, con._con, months)
        _add_spending(out, con, months)
        return out
    finally:
        con.close()


def _add_balances(out: MemberSeries, con: sqlite3.Connection, months: pd.DatetimeIndex) -> None:
    """Every account's and asset or liability lot's cents by month into
    ``out``, ``_MISSING`` where it has no row. Read from ``lot_state`` once
    and summed per account in numpy, as the ``account_state`` view sums
    it: a query and a Series per account cost more than the run's whole
    read. A month outside ``months`` is dropped, as a reindex would."""
    position = {month: i for i, month in enumerate(months.strftime("%Y-%m-%d"))}
    step_at = {step: position.get(month, -1)
               for step, month in con.execute("SELECT id, month FROM simulation_step")}
    lots = con.execute(
        "SELECT m.id, a.id, a.name, a.kind, m.lot FROM account_lot m "
        "JOIN account a ON a.id = m.account_id ORDER BY a.name, m.id").fetchall()
    rows = np.array(con.execute("SELECT step_id, lot_id, balance FROM lot_state").fetchall(),
                    dtype=np.int64).reshape(-1, 3)
    at = np.array([step_at.get(int(step), -1) for step in rows[:, 0]], dtype=np.int64)
    rows, at = rows[at >= 0], at[at >= 0]
    lot_row = {lot_id: i for i, (lot_id, *_rest) in enumerate(lots)}
    which = np.array([lot_row[int(lot_id)] for lot_id in rows[:, 1]], dtype=np.int64)
    cents = np.zeros((len(lots), len(months)), dtype=np.int64)
    seen = np.zeros((len(lots), len(months)), dtype=bool)
    cents[which, at] = rows[:, 2]
    seen[which, at] = True
    accounts: dict[int, list[int]] = {}
    for i, (_lot_id, account_id, name, kind, lot) in enumerate(lots):
        accounts.setdefault(account_id, []).append(i)
        if kind in ("asset", "liability") and seen[i].any():
            out.subjects[(LOT, name, lot)] = (kind, np.where(seen[i], cents[i], _MISSING))
    for members in accounts.values():
        _lot_id, _account_id, name, kind, _lot = lots[members[0]]
        present = seen[members].any(axis=0)
        if not present.any():
            continue
        total = cents[members].sum(axis=0)
        # An income account's balance is a credit, shown as inflow.
        shown = -total if kind == "income" else total
        out.subjects[(ACCOUNT, name, "")] = (kind, np.where(present, shown, _MISSING))


def _add_classes(out: MemberSeries, con: sqlite3.Connection, months: pd.DatetimeIndex) -> None:
    """Each portfolio's cents per asset class by month (``drift.class_dollars``),
    and the run's target sets and the strategies steering to them."""
    if allocation_drift.has_table(con, "allocation_target"):
        out.targets = [tuple(row) for row in con.execute(
            "SELECT target_set, as_of, asset_class, weight_micros FROM allocation_target")]
        out.watches = allocation_drift.watches(con)
    iso = months.strftime("%Y-%m-%d")
    for portfolio, frame in allocation_drift.class_dollars(con).items():
        cents = np.rint(frame.reindex(iso).fillna(0.0).to_numpy(dtype=float) * 100).astype(np.int64)
        for j, asset_class in enumerate(frame.columns):
            out.subjects[(CLASS, portfolio, asset_class)] = ("asset", cents[:, j])


def _add_spending(out: MemberSeries, con: RunConnection, months: pd.DatetimeIndex) -> None:
    """Each expense category's delivered and budgeted cents by month
    (``reports.spending_by_month``). A month outside ``months`` (a
    year-to-date catch-up dated before the run) is added to the nearest end:
    it is in the same year, and converts at that end's CPI, as the report
    converts it."""
    from analysis.sim.reports import spending_by_month

    delivered, budgeted = spending_by_month(con)
    if delivered.empty:
        return
    position = np.clip(months.searchsorted(pd.DatetimeIndex(delivered.index)), 0, len(months) - 1)
    for lot, frame in ((DELIVERED, delivered), (BUDGETED, budgeted)):
        for category in frame.columns:
            cents = np.zeros(len(months), dtype=np.int64)
            np.add.at(cents, position, np.rint(frame[category].to_numpy(dtype=float) * 100).astype(np.int64))
            out.subjects[(SPENDING, str(category), lot)] = ("expense", cents)


def _add_flows(out: MemberSeries, flows: pd.DataFrame, months: pd.DatetimeIndex) -> None:
    """``flows`` as one cents array on ``months`` per (from, to, rule tags).
    A month outside ``months`` (a year-to-date catch-up dated before the
    run) is dropped, as a reindex to the run's months would."""
    at = pd.Series(np.arange(len(months)), index=months)
    position = at.reindex(pd.DatetimeIndex(flows["month"])).to_numpy()
    inside = ~np.isnan(position)
    flows = flows[inside]
    position = position[inside].astype(np.int64)
    for key, rows in flows.groupby(["from_name", "to_name", "rule_tags"]).indices.items():
        cents = np.zeros(len(months), dtype=np.int64)
        np.add.at(cents, position[rows], flows["cents"].to_numpy(dtype=np.int64)[rows])
        out.flows[(str(key[0]), str(key[1]), str(key[2]))] = cents


def _member_files(batch_dir: Path) -> dict[str, tuple[int, int]]:
    """Every member run file in ``batch_dir``: stem -> (size, mtime_ns)."""
    files = {}
    for path in sorted(Path(batch_dir).glob("member_*.sqlite3")):
        stat = path.stat()
        files[path.stem] = (stat.st_size, stat.st_mtime_ns)
    return files


def write_series_db(batch_dir: Path, members: Iterable[MemberSeries]) -> Path:
    """Write ``batch_dir``'s store from ``members`` (its succeeded ones),
    replacing any there. Returns its path."""
    from analysis.sim.pool import member_components, pool_record

    path = series_db_path(batch_dir)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    con = connect_sim_file(tmp)
    try:
        con.executescript(_DDL)
        con.executemany("INSERT INTO member_file VALUES (?, ?, ?)",
                        [(stem, *stat) for stem, stat in _member_files(batch_dir).items()])
        subject_ids: dict[tuple[str, str, str], int] = {}
        # The batch's meta is its first member's, but a pool's is its base
        # component's (ADR-009 §6), from that component's first member.
        meta_json: str | None = None
        base_meta: str | None = None
        record = pool_record(batch_dir)
        origin = member_components(batch_dir) or {}
        base = record.get("base") if record is not None else None
        flow_ids: dict[tuple[str, str, str], int] = {}
        targets_written = False
        for member_id, member in enumerate(sorted(members, key=lambda m: m.stem), start=1):
            meta_json = member.meta_json if meta_json is None else meta_json
            if not targets_written:
                # Targets and strategies aren't varied, so every member has the
                # first one's.
                con.executemany("INSERT INTO allocation_target VALUES (?, ?, ?, ?)", member.targets)
                con.executemany("INSERT INTO watch VALUES (?, ?, ?, ?)",
                                [(w.strategy, w.target_set, w.portfolio, w.band) for w in member.watches])
                targets_written = True
            if base_meta is None and base is not None and origin.get(member.member_index) == base:
                base_meta = member.meta_json
            con.execute(
                "INSERT INTO member VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (member_id, member.stem, member.member_index, member.seed,
                 _pack_months(member.months), _pack(member.cpi_index),
                 _pack(member.net_worth_cpi),
                 json.dumps([[d.person, d.month.isoformat()] for d in member.deaths]),
                 None if member.household_end is None else member.household_end.isoformat(),
                 json.dumps({name: when.isoformat() for name, when in member.markers.items()}),
                 json.dumps(member.accounts)),
            )
            for key, cents in member.flows.items():
                if key not in flow_ids:
                    flow_ids[key] = len(flow_ids) + 1
                    con.execute("INSERT INTO flow_key VALUES (?, ?, ?, ?)", (flow_ids[key], *key))
                con.execute("INSERT INTO flow VALUES (?, ?, ?)",
                            (flow_ids[key], member_id, _pack(cents)))
            for key, (account_kind, values) in member.subjects.items():
                if key not in subject_ids:
                    subject_ids[key] = len(subject_ids) + 1
                    con.execute("INSERT INTO subject VALUES (?, ?, ?, ?, ?)",
                                (subject_ids[key], *key, account_kind))
                con.execute("INSERT INTO series VALUES (?, ?, ?)",
                            (subject_ids[key], member_id, _pack(values)))
        con.execute("INSERT INTO batch VALUES (1, ?, ?, ?, ?)",
                    (FORMAT_VERSION, _code_hash(), datetime.now(UTC).isoformat(),
                     _pool_meta(batch_dir, base_meta or meta_json or "{}")))
        con.commit()
    finally:
        con.close()
    os.replace(tmp, path)
    return path


def _pool_meta(batch_dir: Path, meta_json: str) -> str:
    """The meta a batch's reports read: its first member's, but for a pool
    (ADR-009) with the pool's own ``reserve`` in place of its base
    component's, when the pool sets one."""
    from analysis.sim.pool import pool_record

    record = pool_record(batch_dir)
    if record is None or "reserve" not in record:
        return meta_json
    return json.dumps({**json.loads(meta_json or "{}"), "reserve": record["reserve"]})


def build_series_db(batch_dir: Path, on_member: Callable[[], None] | None = None) -> Path:
    """Build ``batch_dir``'s store from its succeeded member files, calling
    ``on_member`` after each one is read."""
    from analysis.sim.ensemble import EnsembleError, member_run_statuses

    succeeded = [stem for stem, status in member_run_statuses(batch_dir).items()
                 if status == "succeeded"]
    if not succeeded:
        # As `load_ensemble` says it: a batch with nothing to show has no store.
        raise EnsembleError(f"no succeeded member run files found under {batch_dir}")

    def each() -> Iterable[MemberSeries]:
        for stem in succeeded:
            yield member_series(Path(batch_dir) / f"{stem}.sqlite3")
            if on_member is not None:
                on_member()

    return write_series_db(batch_dir, each())


def series_db_current(batch_dir: Path) -> bool:
    """Whether ``batch_dir`` has a store this code wrote from the member
    files there now."""
    path = series_db_path(batch_dir)
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT format_version, code_hash FROM batch").fetchone()
            files = {stem: (size, mtime) for stem, size, mtime
                     in con.execute("SELECT stem, size, mtime_ns FROM member_file")}
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return (row is not None and row[0] == FORMAT_VERSION and row[1] == _code_hash()
            and files == _member_files(batch_dir))


def open_series(batch_dir: Path, on_member: Callable[[], None] | None = None) -> EnsembleSeries:
    """``batch_dir``'s store, built first when it's missing or out of date
    (``on_member`` is called as each member is read for it)."""
    if not series_db_current(batch_dir):
        build_series_db(batch_dir, on_member)
    return EnsembleSeries(series_db_path(batch_dir))


@dataclass(frozen=True)
class _Member:
    stem: str
    member_index: int | None
    seed: int | None
    months: pd.DatetimeIndex
    cpi_index: pd.Series
    net_worth_cpi: pd.Series
    deaths: list[Death]
    household_end: date | None
    markers: dict[str, date]
    accounts: dict[str, tuple[str, str, str]]


class EnsembleSeries:
    """An ensemble batch's series, read from its store. Holds what it has
    read, so asking again costs nothing."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.batch_dir = self.path.parent
        with self._connect() as con:
            self._meta = json.loads(con.execute("SELECT meta_json FROM batch").fetchone()[0])
            self._members: dict[int, _Member] = {}
            for (member_id, stem, index, seed, months, cpi, nw_cpi, deaths, household_end,
                 markers, accounts) in con.execute("SELECT * FROM member ORDER BY stem"):
                at = _unpack_months(months)
                self._members[member_id] = _Member(
                    stem, index, seed, at,
                    pd.Series(_unpack(cpi), index=at),
                    pd.Series(_unpack(nw_cpi), index=at),
                    [Death(name, date.fromisoformat(month)) for name, month in json.loads(deaths)],
                    None if household_end is None else date.fromisoformat(household_end),
                    {name: date.fromisoformat(when) for name, when in json.loads(markers).items()},
                    {name: (kind, subkind, tags)
                     for name, (kind, subkind, tags) in json.loads(accounts).items()},
                )
            self._subjects = {
                (kind, account, lot): (subject_id, account_kind)
                for subject_id, kind, account, lot, account_kind
                in con.execute("SELECT id, kind, account, lot, account_kind FROM subject ORDER BY id")
            }
        self._frames: dict[tuple[Any, ...], pd.DataFrame] = {}
        self._memo: dict[tuple[Any, ...], Any] = {}
        from analysis.sim.pool import pool_record

        # ADR-009: a pool's components may hold different accounts.
        self.is_pool = pool_record(self.batch_dir) is not None

    def memo[T](self, key: tuple[Any, ...], build: Callable[[], T]) -> T:
        """``build()``, held under ``key`` like the frames, so asking again
        costs nothing (what guard rails make of the members, say)."""
        if key not in self._memo:
            self._memo[key] = build()
        return self._memo[key]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)

    def __len__(self) -> int:
        return len(self._members)

    @property
    def stems(self) -> list[str]:
        return [m.stem for m in self._members.values()]

    def member_ids(self) -> dict[str, tuple[int, int | None]]:
        """Stem -> (member index, seed), for pairing two ensembles' members."""
        return {m.stem: (m.member_index, m.seed) for m in self._members.values()
                if m.member_index is not None}

    def scenario_meta(self) -> dict[str, Any]:
        return self._meta

    def reserve(self) -> float | None:
        """The recipe's ``meta_json`` ``reserve`` in CPI-constant dollars."""
        raw = self._meta.get("reserve")
        return None if raw is None else units.json_dollars_to_cents(raw) / 100

    def base_case_deaths(self) -> list[Death]:
        """Member 0's deaths, or none when member 0 didn't succeed."""
        for m in self._members.values():
            if m.member_index == 0:
                return m.deaths
        return []

    def accounts(self) -> list[str]:
        return sorted(account for kind, account, _lot in self._subjects if kind == ACCOUNT)

    def lots_by_account(self) -> dict[str, list[str]]:
        """Asset and liability accounts' lots, single lots included, in
        definition order (``RunConnection.lots_by_account(single=True)``)."""
        lots: dict[str, list[str]] = {}
        for kind, account, lot in self._subjects:
            if kind == LOT:
                lots.setdefault(account, []).append(lot)
        return dict(sorted(lots.items()))

    def years(self) -> list[int]:
        return sorted({int(year) for m in self._members.values() for year in index_years(m.cpi_index)})

    # ---------- allocation drift (#359) ----------

    def target_sets(self) -> dict[str, list[tuple[str, dict[str, float]]]]:
        """The batch's target sets, as ``drift.target_sets`` reads a run's."""
        def build() -> dict[str, list[tuple[str, dict[str, float]]]]:
            with self._connect() as con:
                return weight_sets(
                    (name, as_of, asset_class, weight / 1_000_000) for name, as_of, asset_class, weight
                    in con.execute("SELECT target_set, as_of, asset_class, weight_micros FROM allocation_target"))
        return self.memo(("target_sets",), build)

    def watches(self) -> list[Watch]:
        """The batch's strategies that steer to a target set."""
        def build() -> list[Watch]:
            with self._connect() as con:
                return [Watch(*row) for row in con.execute(
                    "SELECT strategy, target_set, portfolio, band FROM watch ORDER BY strategy")]
        return self.memo(("watches",), build)

    def portfolios(self) -> list[str]:
        """The account tags whose asset classes the store holds ('' first: every asset lot)."""
        return sorted({account for kind, account, _lot in self._subjects if kind == CLASS})

    def class_dollars(self, portfolio: str = "") -> dict[str, pd.DataFrame]:
        """Each member's nominal dollars per asset class, by ISO month, for
        ``portfolio`` (an account tag; '' every asset lot)."""
        def build() -> dict[str, pd.DataFrame]:
            classes = [lot for kind, account, lot in self._subjects if kind == CLASS and account == portfolio]
            values = {c: self._nominal((CLASS, portfolio, c)) for c in classes}
            out = {}
            for member_id, member in self._members.items():
                index = pd.Index(member.months.strftime("%Y-%m-%d"), name="month")
                out[member.stem] = pd.DataFrame(
                    {c: values[c][member_id] for c in classes if member_id in values[c]},
                    index=index).fillna(0.0)
            return out
        return self.memo(("class_dollars", portfolio), build)

    def drift(self, target_set: str, portfolio: str | None = None) -> dict[str, pd.DataFrame]:
        """Each member's ``drift.drift`` against ``target_set``."""
        from analysis.sim.ensemble import EnsembleError

        sets = self.target_sets()
        if target_set not in sets:
            raise EnsembleError(
                f"no target set {target_set!r} in this batch (known: {', '.join(sorted(sets)) or 'none'})")
        key = portfolio or ""
        if key and key not in self.portfolios():
            raise EnsembleError(f"no asset account tagged {key!r} in this batch")
        return self.memo(("drift", target_set, key), lambda: {
            stem: allocation_drift.drift(frame, sets[target_set]) for stem, frame in self.class_dollars(key).items()})

    def drift_frame(self, target_set: str, portfolio: str | None = None,
                    column: str = allocation_drift.MAX_ABS) -> pd.DataFrame:
        """One column of every member's drift (``max_abs`` by default): a
        column per member, by month (a ``DatetimeIndex``, as ``frame`` has)."""
        frames = self.drift(target_set, portfolio)
        columns = {stem: (frame[column] if column in frame.columns else pd.Series(0.0, index=frame.index))
                   for stem, frame in frames.items()}
        out = pd.DataFrame(columns)
        out.index = pd.DatetimeIndex(pd.to_datetime(out.index))
        return out

    # ---------- spending against the plan (#408) ----------

    def spending_categories(self) -> list[str]:
        """The expense categories any member spent on, in report order."""
        found = {account for kind, account, lot in self._subjects if kind == SPENDING}
        return [c for c in EXPENSE_CATEGORIES if c in found] + sorted(found - set(EXPENSE_CATEGORIES))

    def whole_years(self) -> list[int]:
        """The years every member covers all twelve months of."""
        from analysis.sim.reports import whole_years

        sets = [set(whole_years(m.months)) for m in self._members.values()]
        return sorted(set.intersection(*sets)) if sets else []

    def spending(
        self, mode: DollarMode = DollarMode.CPI_CONSTANT, category: str | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Each member's yearly spending, delivered and budgeted: a column
        per member, by year, in ``mode`` dollars, for one expense category
        or (None) all of them. A member that spent nothing has zeros, and a
        batch without a strategy has budgeted equal to delivered."""
        def build() -> tuple[pd.DataFrame, pd.DataFrame]:
            categories = [category] if category is not None else self.spending_categories()
            out = []
            for lot in (DELIVERED, BUDGETED):
                values = [self._nominal((SPENDING, c, lot)) for c in categories]
                columns: dict[str, pd.Series] = {}
                for member_id, member in self._members.items():
                    monthly = pd.Series(0.0, index=member.months)
                    for by_member in values:
                        if member_id in by_member:
                            monthly = monthly + by_member[member_id]
                    if mode == DollarMode.CPI_CONSTANT:
                        monthly = to_cpi_constant(monthly, member.cpi_index)
                    columns[member.stem] = monthly.groupby(index_years(monthly)).sum()
                frame = pd.DataFrame(columns).fillna(0.0)
                frame.index.name = "year"
                out.append(frame)
            return out[0], out[1]
        return self.memo(("spending", mode, category), build)

    def _nominal(self, key: tuple[str, str, str]) -> dict[int, np.ndarray]:
        subject = self._subjects.get(key)
        if subject is None:
            return {}
        with self._connect() as con:
            return {member_id: _unpack(blob) for member_id, blob in con.execute(
                "SELECT member_id, nominal FROM series WHERE subject_id = ?", (subject[0],))}

    def frame(
        self, account: str | None = None, mode: DollarMode = DollarMode.NOMINAL,
        lot: str | None = None, *, after_tax: bool = False,
    ) -> pd.DataFrame:
        """``ensemble.members_balance_by_month`` from the store: one column
        per member, by month, in ``mode`` dollars."""
        from analysis.sim.ensemble import EnsembleError

        key = (account, mode, lot, after_tax)
        if key in self._frames:
            return self._frames[key]
        if account is None:
            if lot is not None:
                raise EnsembleError("a lot needs its account")
            subject = (AFTER_TAX if after_tax else NET_WORTH, "", "")
        elif after_tax:
            raise EnsembleError("after-tax figures are for net worth, not one account")
        else:
            subject = (ACCOUNT, account, "") if lot is None else (LOT, account, lot)
        nominal = self._nominal(subject)
        account_kind = self._subjects[subject][1] if subject in self._subjects else None
        columns: dict[str, pd.Series] = {}
        for member_id, member in self._members.items():
            if member_id not in nominal:
                if self.is_pool and nominal:
                    # A pool member whose component lacks the account holds
                    # none of it, rather than dropping out of the pooled
                    # statistics (ADR-009 §7), as a scenario compare does (#174).
                    columns[member.stem] = pd.Series(0.0, index=member.months)
                continue
            values = pd.Series(nominal[member_id], index=member.months)
            columns[member.stem] = self._convert(subject, account_kind, member, values, mode)
        frame = pd.DataFrame(columns)
        if frame.empty and account is not None:
            raise EnsembleError(
                f"no account {account!r} in these members" if lot is None
                else f"no lot {lot!r} of account {account!r} in these members")
        self._frames[key] = frame
        return frame

    def rail_pieces(self) -> Iterator[tuple[str, int | None, RunPieces]]:
        """Each member's stem, index and what its guard rails read (#305),
        as ``guard_rails.run_pieces`` gives them for a run file."""
        from analysis.sim.guard_rails import RunPieces

        with self._connect() as con:
            keys = {flow_id: (from_name, to_name, rule_tags) for flow_id, from_name, to_name, rule_tags
                    in con.execute("SELECT id, from_name, to_name, rule_tags FROM flow_key")}
            blobs: dict[int, list[tuple[int, bytes]]] = {}
            for flow_id, member_id, blob in con.execute("SELECT flow_id, member_id, cents FROM flow"):
                blobs.setdefault(member_id, []).append((flow_id, blob))
        assets = sorted({name for m in self._members.values()
                         for name, (kind, _subkind, _tags) in m.accounts.items() if kind == "asset"})
        balances = {name: self._nominal((ACCOUNT, name, "")) for name in assets}
        for member_id, member in self._members.items():
            months = member.months
            parts = []
            for flow_id, blob in blobs.get(member_id, []):
                cents = np.rint(_unpack(blob) * 100)
                at = np.flatnonzero(cents)
                from_name, to_name, rule_tags = keys[flow_id]
                parts.append(pd.DataFrame({
                    "month": months[at], "from_name": from_name, "to_name": to_name,
                    "rule_tags": rule_tags, "cents": cents[at]}))
            flows = (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
                columns=["month", "from_name", "to_name", "rule_tags", "cents"]))
            mine = [name for name, (kind, _s, _t) in member.accounts.items() if kind == "asset"]
            frame = pd.DataFrame(
                {name: balances[name].get(member_id, np.full(len(months), np.nan)) for name in mine},
                index=months, columns=mine).fillna(0.0)
            accounts = pd.DataFrame.from_dict(
                member.accounts, orient="index", columns=["kind", "subkind", "tags"])
            yield member.stem, member.member_index, RunPieces(
                months=months, index=member.cpi_index.to_numpy(dtype=float), flows=flows,
                balances=frame, accounts=accounts, markers=member.markers,
                household_end=member.household_end,
            )

    @staticmethod
    def _convert(subject: tuple[str, str, str], account_kind: str | None, member: _Member,
                 values: pd.Series, mode: DollarMode) -> pd.Series:
        """``values`` as the connection would give them in ``mode``."""
        kind = subject[0]
        if kind == NET_WORTH:
            return values if mode != DollarMode.CPI_CONSTANT else member.net_worth_cpi
        if kind == AFTER_TAX:
            if mode != DollarMode.CPI_CONSTANT:
                return values
            return to_cpi_constant(values, member.cpi_index).where(member.net_worth_cpi.notna())
        # An account or lot has no row in a month before it exists.
        values = values.dropna()
        if mode != DollarMode.CPI_CONSTANT or values.empty:
            return values
        if kind == LOT:
            return to_cpi_constant(values, member.cpi_index)
        return account_balance_cpi(values, account_kind, member.cpi_index)


def pair_members(
    baseline: Mapping[str, tuple[int, int | None]], variant: Mapping[str, tuple[int, int | None]],
) -> tuple[list[tuple[str, str]], str | None]:
    """Members of two ensembles that drew the same future (see
    ``ensemble.member_pairs``), from each one's stem -> (member index, seed)."""
    left = {index: (stem, seed) for stem, (index, seed) in baseline.items()}
    right = {index: (stem, seed) for stem, (index, seed) in variant.items()}
    common = sorted(left.keys() & right.keys())
    if not common:
        return [], "no member index succeeded in both ensembles"
    if any(left[i][1] is None or left[i][1] != right[i][1] for i in common):
        return [], "the recipes have different seeds, so their members drew different futures"
    return [(left[i][0], right[i][0]) for i in common], None
