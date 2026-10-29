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
  CPI-constant net worth (the engine's own), and deaths;
- ``subject``: net worth, after-tax net worth, each account, each asset or
  liability lot;
- ``series``: per subject and member, the nominal dollars by month.

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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analysis.frames import index_years
from analysis.sim import units
from analysis.sim.connection import RunConnection, account_balance_cpi, to_cpi_constant
from analysis.sim.schema import connect_sim_file
from analysis.sim.types import Death, DollarMode

SERIES_DB_FILENAME = "series.sqlite3"
# Bump when the stored layout changes. A change to how a series is derived
# needs no bump: the store records a hash of the modules that derive them.
FORMAT_VERSION = 1

NET_WORTH = "net_worth"
AFTER_TAX = "after_tax"
ACCOUNT = "account"
LOT = "lot"

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
    deaths_json    TEXT NOT NULL
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
"""


def series_db_path(batch_dir: Path) -> Path:
    return Path(batch_dir) / SERIES_DB_FILENAME


def _code_hash() -> str:
    """The format and the source of the modules that derive the series: a
    store written by different code is rebuilt, not trusted."""
    import analysis.sim.connection as connection

    digest = hashlib.sha256(str(FORMAT_VERSION).encode())
    for module_file in (connection.__file__, __file__):
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
        )
        out.subjects[(NET_WORTH, "", "")] = (None, nw["nominal_cents"].to_numpy(dtype=np.int64))
        out.subjects[(AFTER_TAX, "", "")] = (
            None, _floats(con.after_tax_net_worth_frame()["nominal"].reindex(months)))
        _add_balances(out, con._con, months)
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
        for member_id, member in enumerate(sorted(members, key=lambda m: m.stem), start=1):
            meta_json = member.meta_json if meta_json is None else meta_json
            if base_meta is None and base is not None and origin.get(member.member_index) == base:
                base_meta = member.meta_json
            con.execute(
                "INSERT INTO member VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (member_id, member.stem, member.member_index, member.seed,
                 _pack_months(member.months), _pack(member.cpi_index),
                 _pack(member.net_worth_cpi),
                 json.dumps([[d.person, d.month.isoformat()] for d in member.deaths])),
            )
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


class EnsembleSeries:
    """An ensemble batch's series, read from its store. Holds what it has
    read, so asking again costs nothing."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.batch_dir = self.path.parent
        with self._connect() as con:
            self._meta = json.loads(con.execute("SELECT meta_json FROM batch").fetchone()[0])
            self._members: dict[int, _Member] = {}
            for (member_id, stem, index, seed, months, cpi, nw_cpi, deaths
                 ) in con.execute("SELECT * FROM member ORDER BY stem"):
                at = _unpack_months(months)
                self._members[member_id] = _Member(
                    stem, index, seed, at,
                    pd.Series(_unpack(cpi), index=at),
                    pd.Series(_unpack(nw_cpi), index=at),
                    [Death(name, date.fromisoformat(month)) for name, month in json.loads(deaths)],
                )
            self._subjects = {
                (kind, account, lot): (subject_id, account_kind)
                for subject_id, kind, account, lot, account_kind
                in con.execute("SELECT id, kind, account, lot, account_kind FROM subject ORDER BY id")
            }
        self._frames: dict[tuple[Any, ...], pd.DataFrame] = {}
        from analysis.sim.pool import pool_record

        # ADR-009: a pool's components may hold different accounts.
        self.is_pool = pool_record(self.batch_dir) is not None

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
