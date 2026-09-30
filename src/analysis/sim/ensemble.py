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

import pandas as pd

from analysis.sim.connection import RunConnection
from analysis.sim.distributions import TruncatedNormal, Uniform, member_rng
from analysis.sim.types import DollarMode

__all__ = [
    "EnsembleError", "resolve_member", "load_ensemble",
    "members_net_worth_by_month", "summary",
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
        return TruncatedNormal(
            mean=_bound(row["vary_mean"], target), sd=_bound(row["vary_sd"], target),
            clip_low=_bound(row["vary_clip_low"], target),
            clip_high=_bound(row["vary_clip_high"], target),
        )
    raise EnsembleError(f"row {row['name']!r}: unknown vary_distribution {kind!r}")


def _row_aux_kind(row: sqlite3.Row) -> str:
    params = json.loads(row["payload_json"] or "{}")
    return params.get("aux_kind", "rate")


def _current_rate_value(con: sqlite3.Connection, row: sqlite3.Row) -> int:
    """The series' latest rate as of ``row``'s own date, read from the
    (not-yet-run) intent's own `aux_table`.

    This is the recipe's baseline only -- it does not account for any other
    calendar_event that would change the same series earlier in a real run;
    ADR-006 doesn't pre-simulate the calendar to resolve a ramp's start
    value, only reads the snapshot as loaded.
    """
    aux_kind = _row_aux_kind(row)
    found = con.execute(
        "SELECT payload_json FROM aux_table "
        "WHERE kind = ? AND series = ? AND effective_date <= ? "
        "ORDER BY effective_date DESC LIMIT 1",
        (aux_kind, row["series"], row["date"]),
    ).fetchone()
    if found is None:
        raise EnsembleError(
            f"ramp on row {row['name']!r}: no existing {aux_kind!r} row for "
            f"series {row['series']!r} on or before {row['date']} to ramp from"
        )
    return json.loads(found[0])["annual_rate_micros"]


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
    row first, then every `vary_target='amount'` row (ADR-006 SS3, SS5)."""
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
            drawn = date.fromordinal(round(dist.draw(member_rng(seed, member_index, row["name"]))))
            con.execute(
                "UPDATE calendar_event SET date = ?, vary_distribution = NULL WHERE id = ?",
                (drawn.isoformat(), row["id"]),
            )
            draws.append((row["name"], "date", drawn.isoformat()))

        for row in [r for r in rows if r["vary_target"] == "amount"]:
            dist = _build_distribution(row, "amount")
            target = round(dist.draw(member_rng(seed, member_index, row["name"])))
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


def load_ensemble(runs_dir: Path) -> dict[str, RunConnection]:
    """Every ``member_*.sqlite3`` run file directly under ``runs_dir``, keyed
    by filename stem. Caller owns closing each connection."""
    paths = sorted(Path(runs_dir).glob("member_*.sqlite3"))
    if not paths:
        raise EnsembleError(f"no member run files found under {runs_dir}")
    return {p.stem: RunConnection(p) for p in paths}


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
