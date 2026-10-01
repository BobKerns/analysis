"""ADR-006: generating and summarizing ensemble scenarios.

``resolve_member`` (Phase 2) copies a recipe's intent file and resolves every
`calendar_event` row with `vary_distribution` set into a concrete, literal
value -- dates first, then amounts/rates (ADR-006 SS5). The result is an
ordinary intent file: `run_scenario`/`open_run_file` are never imported here
and never modified by this module.

``load_ensemble``/``members_net_worth_by_month``/``summary`` (Phase 4) read
back an already-run ensemble's member run files and report mean/median/SD/
range across members, the same wide-frame-then-row-stats shape
``notebook_ui.compare_plots.net_worth_by_month`` uses across scenarios.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from calendar import monthrange
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from analysis.sim.connection import RunConnection
from analysis.sim.distributions import (
    DistributionError,
    TruncatedNormal,
    Uniform,
    member_rng,
    resolve_truncated_normal_bounds,
)
from analysis.sim.loader import BATCH_MANIFEST_FILENAME, latest_ensemble_batch
from analysis.sim.types import DollarMode

__all__ = [
    "EnsembleError", "resolve_member", "load_ensemble", "member_run_statuses",
    "latest_ensemble_batch", "members_net_worth_by_month", "summary",
    "write_batch_manifest", "read_batch_manifest",
]


class EnsembleError(RuntimeError):
    """A recipe's `vary_*` row can't be resolved as specified."""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _member_seed(seed: int, member_index: int) -> int:
    """A per-member seed recorded in `ensemble_manifest` for humans/diagnostics.

    Not fed back into `member_rng` -- every row draw is always derived from
    the ensemble's own master ``seed`` plus the member index and row name
    (ADR-006 SS3), never from this value.
    """
    digest = hashlib.sha256(f"{seed}:{member_index}:member".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2**63)  # fits SQLite's signed 64-bit INTEGER


def _add_months_end(d: date, months: int) -> date:
    """``d`` advanced by ``months`` whole months, landing on that month's own
    last day (matches calendar.csv's month-end convention)."""
    total = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    return date(year, month, monthrange(year, month)[1])


def _bound(raw: str | None, target: str) -> float | None:
    """A `vary_low`/`vary_high`/etc. cell as a float draw bound: the row's own
    unit for `amount` (already a plain int, stored as text); a date's
    ordinal for `date` (ADR-006 SS2's "target column's own unit")."""
    if raw in (None, ""):
        return None
    if target == "date":
        return float(date.fromisoformat(raw).toordinal())
    return float(raw)


def _build_distribution(row: sqlite3.Row, target: str) -> Uniform | TruncatedNormal:
    kind = row["vary_distribution"]
    if kind == "uniform":
        return Uniform(low=_bound(row["vary_low"], target), high=_bound(row["vary_high"], target))
    if kind == "truncated_normal":
        # vary_low/vary_high double as truncated_normal's own (optional)
        # clip bounds; any of mean/sd/low/high left blank is inferred from
        # the other three (ADR-006, distributions.resolve_truncated_normal_bounds)
        # -- the loader already validated this resolves, so a failure here
        # would mean an intent that skipped validation, not a normal case.
        try:
            mean, sd, low, high = resolve_truncated_normal_bounds(
                _bound(row["vary_mean"], target), _bound(row["vary_sd"], target),
                _bound(row["vary_low"], target), _bound(row["vary_high"], target),
            )
        except DistributionError as e:
            raise EnsembleError(f"row {row['name']!r}: {e}") from e
        return TruncatedNormal(mean=mean, sd=sd, clip_low=low, clip_high=high)
    raise EnsembleError(f"row {row['name']!r}: unknown vary_distribution {kind!r}")


def _row_aux_kind(row: sqlite3.Row) -> str:
    params = json.loads(row["payload_json"] or "{}")
    return params.get("aux_kind", "rate")


def _current_rate_value(con: sqlite3.Connection, row: sqlite3.Row) -> int:
    """The series' latest *known* rate as of ``row``'s own date: the later of
    the (not-yet-run) intent's own `aux_table` baseline and any literal
    (already-resolved, non-varying) `set_rate` calendar_event for the same
    series and aux_kind dated on or before this row's date.

    A rate change the recipe itself already authors earlier in the calendar
    is part of "the current rate", not just the load-time snapshot
    underneath it -- otherwise a ramp starting after such a change would
    reset to the pre-change value before ascending. This still doesn't
    pre-simulate the calendar in general: an *unresolved* (still-varying)
    earlier `set_rate` row is excluded, since its value isn't known yet
    either (ADR-006's open question on cross-variation ordering beyond the
    two-pass rule).
    """
    aux_kind = _row_aux_kind(row)
    candidates: list[tuple[str, int]] = []

    aux_found = con.execute(
        "SELECT effective_date, payload_json FROM aux_table "
        "WHERE kind = ? AND series = ? AND effective_date <= ? "
        "ORDER BY effective_date DESC LIMIT 1",
        (aux_kind, row["series"], row["date"]),
    ).fetchone()
    if aux_found is not None:
        candidates.append((aux_found[0], json.loads(aux_found[1])["annual_rate_micros"]))

    for ev_date, ev_amount, ev_payload in con.execute(
        "SELECT date, amount, payload_json FROM calendar_event "
        "WHERE kind = 'set_rate' AND series = ? AND date <= ? AND id != ? "
        "AND vary_distribution IS NULL AND amount IS NOT NULL "
        "ORDER BY date DESC",
        (row["series"], row["date"], row["id"]),
    ).fetchall():
        if (json.loads(ev_payload or "{}").get("aux_kind", "rate")) == aux_kind:
            candidates.append((ev_date, ev_amount))
            break

    if not candidates:
        raise EnsembleError(
            f"ramp on row {row['name']!r}: no existing {aux_kind!r} row for "
            f"series {row['series']!r} on or before {row['date']} to ramp from"
        )
    candidates.sort(key=lambda c: c[0])
    return candidates[-1][1]


def _apply_ramp(con: sqlite3.Connection, row: sqlite3.Row, target: int, months: int) -> None:
    """Expand one `vary_ramp_months` row into ``months`` additional ordinary
    `set_rate` rows, linearly interpolated from the series' current value up
    to ``target``, landing on it at ``row['date'] + months`` (ADR-006 SS4)."""
    current = _current_rate_value(con, row)
    con.execute(
        "UPDATE calendar_event SET amount = ?, vary_distribution = NULL WHERE id = ?",
        (current, row["id"]),
    )
    base_date = date.fromisoformat(row["date"])
    payload = json.loads(row["payload_json"] or "{}")
    payload.setdefault("aux_kind", _row_aux_kind(row))
    for k in range(1, months + 1):
        value = round(current + (target - current) * (k / months))
        step_date = _add_months_end(base_date, k)
        con.execute(
            "INSERT INTO calendar_event("
            "calendar_id, date, kind, series, amount, payload_json, description) "
            "VALUES (?, ?, 'set_rate', ?, ?, ?, ?)",
            (
                row["calendar_id"], step_date.isoformat(), row["series"], value,
                json.dumps(payload),
                f"ADR-006 ramp step {k}/{months} for {row['name']}",
            ),
        )


def resolve_member(
    recipe_intent_path: Path,
    member_path: Path,
    *,
    seed: int,
    member_index: int,
    member_count: int,
    recipe_tree_id: str,
) -> Path:
    """Copy ``recipe_intent_path`` to ``member_path`` and resolve every
    `vary_distribution` row into a literal value: every `vary_target='date'`
    row first, then every `vary_target='amount'` row (ADR-006 SS3, SS5).

    Member 0 is always deterministic -- each row's distribution central
    value rather than a draw -- so every ensemble has one member that's a
    plausible, in-bounds "base case" instead of just a bag of random draws,
    and anything that only knows how to open a single run file can treat an
    ensemble recipe's member 0 as that scenario's one run (ADR-006 §8).
    """
    member_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(recipe_intent_path, member_path)

    con = sqlite3.connect(member_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM calendar_event WHERE vary_distribution IS NOT NULL"
        ).fetchall()

        draws: list[tuple[str, str, str]] = []

        for row in [r for r in rows if r["vary_target"] == "date"]:
            dist = _build_distribution(row, "date")
            if not isinstance(dist, Uniform):
                # The loader already rejects any other distribution with a
                # date target (only a numeric spread ever needs one); this
                # is unreachable for an intent that passed validation.
                raise EnsembleError(
                    f"row {row['name']!r}: {row['vary_distribution']!r} has no "
                    "discrete date draw"
                )
            drawn_ordinal = (
                dist.central_int() if member_index == 0
                else dist.draw_int(member_rng(seed, member_index, row["name"]))
            )
            drawn = date.fromordinal(drawn_ordinal)
            con.execute(
                "UPDATE calendar_event SET date = ?, vary_distribution = NULL WHERE id = ?",
                (drawn.isoformat(), row["id"]),
            )
            draws.append((row["name"], "date", drawn.isoformat()))

        for row in [r for r in rows if r["vary_target"] == "amount"]:
            dist = _build_distribution(row, "amount")
            target = round(
                dist.central() if member_index == 0
                else dist.draw(member_rng(seed, member_index, row["name"]))
            )
            if row["kind"] == "one_off_transfer" and target <= 0:
                # Belt-and-suspenders: the loader already requires a strictly
                # positive uniform range / truncated_normal clip floor for a
                # transfer amount, so this should be unreachable -- but a
                # nonpositive draw reaching _post_transfer would reverse or
                # silently omit the transfer, so it's worth failing loudly
                # rather than trusting that invariant silently.
                raise EnsembleError(
                    f"row {row['name']!r}: drew a nonpositive one_off_transfer "
                    f"amount ({target}); its distribution's bounds should have "
                    "prevented this"
                )
            if row["vary_ramp_months"]:
                _apply_ramp(con, row, target, int(row["vary_ramp_months"]))
            else:
                con.execute(
                    "UPDATE calendar_event SET amount = ?, vary_distribution = NULL "
                    "WHERE id = ?",
                    (target, row["id"]),
                )
            draws.append((row["name"], "amount", str(target)))

        ensemble_name = con.execute("SELECT name FROM scenario").fetchone()[0]
        con.execute(
            "INSERT INTO ensemble_manifest("
            "id, ensemble_name, member_index, member_count, seed, "
            "recipe_tree_id, generated_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
            (
                ensemble_name, member_index, member_count,
                _member_seed(seed, member_index), recipe_tree_id, _now_iso(),
            ),
        )
        con.executemany(
            "INSERT INTO ensemble_draw(calendar_event_name, vary_target, drawn_value) "
            "VALUES (?, ?, ?)",
            draws,
        )
        con.commit()
    finally:
        con.close()
    return member_path


def write_batch_manifest(
    batch_dir: Path, *, member_count: int,
    failures: list[tuple[int, Path, str]],
) -> None:
    """Persist what ``_run_ensemble`` knows that a run file alone can't show:
    how many members were expected, and which ones failed before
    ``resolve_member``/``run_scenario`` ever produced a run file for them to
    record their own ``status='failed'`` in. Without this, a batch where
    every member's resolution fails has no ``member_*.sqlite3`` files at
    all and is invisible to every other function in this module.
    """
    path = Path(batch_dir) / BATCH_MANIFEST_FILENAME
    path.write_text(json.dumps({
        "member_count": member_count,
        "failures": [
            {"index": i, "intent_path": str(intent_path), "error": error}
            for i, intent_path, error in failures
        ],
    }, indent=2))


def read_batch_manifest(batch_dir: Path) -> dict[str, Any] | None:
    """``write_batch_manifest``'s record for this batch, or ``None`` for a
    batch generated before it existed (or synthesized directly by a test,
    bypassing ``_run_ensemble``)."""
    path = Path(batch_dir) / BATCH_MANIFEST_FILENAME
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def member_run_statuses(batch_dir: Path) -> dict[str, str]:
    """``simulation_run.status`` of every ``member_*.sqlite3`` run file
    directly under ``batch_dir``, keyed by filename stem, merged with
    ``batch_manifest.json``'s record of any member that failed before a run
    file could even be created (``resolve_member`` itself raising) --
    reported as ``"failed_to_resolve"``, so it isn't silently absent from a
    count that's supposed to total ``member_count``.

    A member whose run raised mid-simulation still has a run file --
    ``run_scenario`` writes ``status='failed'`` before re-raising, rather
    than deleting it, so its (partial, not reliably comparable) history
    stays inspectable -- so status has to be checked explicitly rather than
    assuming every file found is a succeeded member.
    """
    statuses: dict[str, str] = {}
    for p in sorted(Path(batch_dir).glob("member_*.sqlite3")):
        con = sqlite3.connect(p)
        try:
            row = con.execute("SELECT status FROM simulation_run").fetchone()
        finally:
            con.close()
        statuses[p.stem] = row[0] if row else "unknown"
    manifest = read_batch_manifest(batch_dir)
    if manifest:
        for failure in manifest.get("failures", []):
            stem = f"member_{failure['index']:04d}"
            statuses.setdefault(stem, "failed_to_resolve")
    return statuses


def load_ensemble(batch_dir: Path) -> dict[str, RunConnection]:
    """Every succeeded ``member_*.sqlite3`` run file directly under
    ``batch_dir``, keyed by filename stem. Caller owns closing each
    connection. A failed member (see ``member_run_statuses``) is excluded --
    it would otherwise distort the mean/range with a partial run -- so
    reporting the failure count/detail alongside a summary means calling
    ``member_run_statuses`` too.
    """
    statuses = member_run_statuses(batch_dir)
    if not statuses:
        raise EnsembleError(f"no member run files found under {batch_dir}")
    succeeded = [stem for stem, status in statuses.items() if status == "succeeded"]
    if not succeeded:
        raise EnsembleError(
            f"no succeeded member run files found under {batch_dir} "
            f"({len(statuses)} present, none succeeded: {statuses})"
        )
    return {stem: RunConnection(Path(batch_dir) / f"{stem}.sqlite3") for stem in succeeded}


def members_net_worth_by_month(
    connections: dict[str, RunConnection], mode: DollarMode = DollarMode.NOMINAL,
) -> pd.DataFrame:
    """Net worth (USD) by month, one column per member."""
    value_column = "cpi_constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
    return pd.DataFrame({
        name: con.net_worth_frame()[value_column] for name, con in connections.items()
    })


def summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Mean/median/SD/range across ``frame``'s columns (one column per
    member), row by row (ADR-006 SS8)."""
    return pd.DataFrame({
        "mean": frame.mean(axis=1),
        "median": frame.median(axis=1),
        "std": frame.std(axis=1),
        "min": frame.min(axis=1),
        "max": frame.max(axis=1),
    })
