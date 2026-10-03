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
from collections.abc import Mapping
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pandas as pd

from analysis.sim.connection import RunConnection
from analysis.sim.distributions import (
    Distribution,
    DistributionError,
    Triangular,
    TruncatedNormal,
    Uniform,
    member_rng,
    resolve_truncated_normal_bounds,
)
from analysis.sim.correlation import (
    CorrelationError,
    Link,
    probability,
    resolve_scores,
    standard_normal,
    tree_members,
)
from analysis.sim.loader import BATCH_MANIFEST_FILENAME, latest_ensemble_batch
from analysis.sim.registries import ENSEMBLE_MODES, LATENT_FACTOR_KIND
from analysis.sim.types import DollarMode

__all__ = [
    "EnsembleError", "resolve_member", "load_ensemble", "member_run_statuses",
    "latest_ensemble_batch", "members_net_worth_by_month", "summary",
    "write_batch_manifest", "read_batch_manifest", "oat_plan", "build_distribution",
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


def build_distribution(row: Mapping[str, Any], target: str) -> Distribution:
    """The distribution a varying row's ``vary_*`` columns describe, in the
    target column's own unit (a day ordinal for a date target). The loader
    already validated every row, so a failure here would mean an intent
    that skipped validation, not a normal case."""
    kind = row["vary_distribution"]
    try:
        if kind == "uniform":
            return Uniform(low=_bound(row["vary_low"], target), high=_bound(row["vary_high"], target))
        if kind == "triangular":
            low, high = _bound(row["vary_low"], target), _bound(row["vary_high"], target)
            mode = _bound(row["vary_mode"], target)
            return Triangular(low, (low + high) / 2 if mode is None else mode, high)
        if kind == "truncated_normal":
            # vary_low/vary_high double as truncated_normal's own (optional)
            # clip bounds; any of median/sd/low/high left blank is inferred
            # from the others (distributions.resolve_truncated_normal_bounds).
            skew = _shape(row, "vary_skew", 0.0)
            tailweight = _shape(row, "vary_tailweight", 1.0)
            median, sd, low, high = resolve_truncated_normal_bounds(
                _bound(row["vary_median"], target), _bound(row["vary_sd"], target),
                _bound(row["vary_low"], target), _bound(row["vary_high"], target),
                skew=skew, tailweight=tailweight,
            )
            return TruncatedNormal(median=median, sd=sd, clip_low=low, clip_high=high,
                                   skew=skew, tailweight=tailweight)
    except DistributionError as e:
        raise EnsembleError(f"row {row['name']!r}: {e}") from e
    raise EnsembleError(f"row {row['name']!r}: unknown vary_distribution {kind!r}")


def _shape(row: Mapping[str, Any], column: str, default: float) -> float:
    """A unitless shape cell (#31), or its default when blank or absent."""
    value = row.get(column) if hasattr(row, "get") else row[column]
    return default if value in (None, "") else float(value)


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


def _resolve_value(
    row: sqlite3.Row, dist: Distribution, *, as_int: bool,
    overrides: dict[str, float] | None, seed: int | None, member_index: int,
    score: float | None = None,
) -> tuple[float, float | None]:
    """One varying row's value on one member, and the OAT quantile it was
    resolved at (``None`` unless ``overrides`` names this row):

    - named in ``overrides``: that quantile of ``dist`` (ADR-007 §2);
    - otherwise, on member 0 or any OAT member: ``dist``'s central value;
    - otherwise, in a correlation tree (ADR-008): ``dist.value_at(Phi(score))``,
      ``score`` being this row's correlated standard normal score;
    - otherwise: a draw from this (member, row)'s own RNG stream (ADR-006 §3),
      exactly as before correlation existed.

    ``as_int`` selects the discrete (day-ordinal) form, for a date target.
    """
    if overrides is not None and row["name"] in overrides:
        q = overrides[row["name"]]
        try:
            return (dist.quantile_int(q) if as_int else dist.quantile(q)), q
        except DistributionError as e:
            raise EnsembleError(f"row {row['name']!r}: {e}") from e
    if overrides is not None or member_index == 0:
        return (dist.central_int() if as_int else dist.central()), None
    if score is not None:
        u = probability(score)
        return (dist.value_at_int(u) if as_int else dist.value_at(u)), None
    if seed is None:
        raise EnsembleError(f"row {row['name']!r}: a random draw needs the ensemble's seed")
    rng = member_rng(seed, member_index, row["name"])
    return (dist.draw_int(rng) if as_int else dist.draw(rng)), None


def _correlation_links(con: sqlite3.Connection) -> tuple[dict[str, Link], list[str]]:
    """The intent's correlation forest (ADR-008): each varying row's or
    factor's link to its parent, and the latent factors' names."""
    links = {
        name: Link(parent, rho) for name, parent, rho in con.execute(
            "SELECT name, vary_correlate_with, vary_correlation FROM calendar_event "
            "WHERE vary_correlate_with IS NOT NULL AND name IS NOT NULL"
        )
    }
    factors = [name for (name,) in con.execute(
        "SELECT name FROM calendar_event WHERE kind = ? AND name IS NOT NULL ORDER BY name",
        (LATENT_FACTOR_KIND,),
    )]
    return links, factors


def _member_scores(
    links: dict[str, Link], *, overrides: dict[str, float] | None, seed: int | None,
    member_index: int,
) -> dict[str, float]:
    """Every tree member's standard normal score on this member (ADR-008 §5):

    - a random member: each node's noise from its own keyed stream
      (``member_rng(seed, member, name)``), combined down the tree;
    - member 0: all 0 -- every row at its median, its ``central()``;
    - an OAT member: 0, except an overridden row at ``Phi^-1(q)``. Its
      children aren't moved: OAT stays "this row alone" (ADR-008 §5).

    Rows outside every tree get no score and resolve exactly as before."""
    members = tree_members(links)
    if not members:
        return {}
    if overrides is not None or member_index == 0:
        scores = dict.fromkeys(members, 0.0)
        for name, q in (overrides or {}).items():
            if name in scores:
                # Keep q = 0 or 1 finite: the score is provenance, and the
                # value itself comes from dist.quantile(q).
                scores[name] = NormalDist().inv_cdf(min(max(q, 1e-12), 1 - 1e-12))
        return scores
    if seed is None:
        raise EnsembleError("a correlated draw needs the ensemble's seed")
    try:
        return resolve_scores(links, {
            name: standard_normal(member_rng(seed, member_index, name)) for name in members
        })
    except CorrelationError as e:  # the loader rejects these first
        raise EnsembleError(str(e)) from e


def resolve_member(
    recipe_intent_path: Path,
    member_path: Path,
    *,
    seed: int | None,
    member_index: int,
    member_count: int,
    recipe_tree_id: str,
    overrides: dict[str, float] | None = None,
    mode: str = "monte_carlo",
) -> Path:
    """Copy ``recipe_intent_path`` to ``member_path`` and resolve every
    `vary_distribution` row into a literal value: every `vary_target='date'`
    row first, then every `vary_target='amount'` row (ADR-006 SS3, SS5).

    Member 0 is always deterministic -- each row's distribution central
    value rather than a draw -- so every ensemble has one member that's a
    plausible, in-bounds "base case" instead of just a bag of random draws,
    and anything that only knows how to open a single run file can treat an
    ensemble recipe's member 0 as that scenario's one run (ADR-006 §8).

    ``overrides`` (ADR-007 §2) makes this an OAT sensitivity member: a row
    named there resolves to that quantile of its distribution, and every
    other row to its central value, exactly as member 0's does -- no RNG is
    consulted at all. ``mode`` is recorded in ``ensemble_manifest``; a
    ``sensitivity`` member records a NULL seed, since it draws nothing, and
    ``seed`` may be ``None`` for one.
    """
    if mode == "monte_carlo" and seed is None:
        raise EnsembleError("a Monte Carlo member needs the ensemble's seed")
    if mode not in ENSEMBLE_MODES:
        raise EnsembleError(f"unknown ensemble mode {mode!r}")
    member_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(recipe_intent_path, member_path)

    con = sqlite3.connect(member_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM calendar_event WHERE vary_distribution IS NOT NULL"
        ).fetchall()
        if overrides is not None:
            unknown = set(overrides) - {r["name"] for r in rows}
            if unknown:
                raise EnsembleError(
                    f"overrides name no unresolved vary_* row: {sorted(unknown)}"
                )
        links, factors = _correlation_links(con)
        scores = _member_scores(
            links, overrides=overrides, seed=seed, member_index=member_index,
        )

        draws: list[tuple[str, str, str, float | None, str | None, float | None]] = []

        for row in [r for r in rows if r["vary_target"] == "date"]:
            dist = build_distribution(row, "date")
            if not isinstance(dist, Uniform | Triangular):
                # The loader rejects truncated_normal with a date target (its
                # sd is a numeric spread, not a date span); this is
                # unreachable for an intent that passed validation.
                raise EnsembleError(
                    f"row {row['name']!r}: {row['vary_distribution']!r} has no "
                    "discrete date draw"
                )
            drawn_ordinal, quantile = _resolve_value(
                row, dist, as_int=True, overrides=overrides, seed=seed, member_index=member_index,
                score=scores.get(row["name"]),
            )
            drawn = date.fromordinal(int(drawn_ordinal))
            con.execute(
                "UPDATE calendar_event SET date = ?, vary_distribution = NULL WHERE id = ?",
                (drawn.isoformat(), row["id"]),
            )
            draws.append((row["name"], "date", drawn.isoformat(), quantile,
                          row["vary_distribution"], scores.get(row["name"])))

        for row in [r for r in rows if r["vary_target"] == "amount"]:
            dist = build_distribution(row, "amount")
            value, quantile = _resolve_value(
                row, dist, as_int=False, overrides=overrides, seed=seed, member_index=member_index,
                score=scores.get(row["name"]),
            )
            target = round(value)
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
            draws.append((row["name"], "amount", str(target), quantile,
                          row["vary_distribution"], scores.get(row["name"])))
        # Factors are drawn too (ADR-008 §6), so achieved correlations can be
        # checked after the fact; their value is just their score.
        for factor in factors:
            z = scores.get(factor)
            if z is not None:
                draws.append((factor, "factor", repr(z), None, None, z))

        ensemble_name = con.execute("SELECT name FROM scenario").fetchone()[0]
        con.execute(
            "INSERT INTO ensemble_manifest("
            "id, ensemble_name, member_index, member_count, seed, "
            "recipe_tree_id, generated_at, mode) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                ensemble_name, member_index, member_count,
                None if mode == "sensitivity" else _member_seed(seed, member_index),
                recipe_tree_id, _now_iso(), mode,
            ),
        )
        con.executemany(
            "INSERT INTO ensemble_draw(calendar_event_name, vary_target, drawn_value, "
            "quantile, distribution, z) VALUES (?, ?, ?, ?, ?, ?)",
            draws,
        )
        con.commit()
    finally:
        con.close()
    return member_path


def oat_plan(
    row_names: list[str], quantiles: tuple[float, ...], only: list[str] | None = None,
) -> list[dict[str, float]]:
    """The ``overrides`` for every member of an OAT sensitivity batch
    (ADR-007 §2): index 0 is ``{}`` (the base -- every row central), then one
    ``{name: q}`` per (row, quantile), rows by name, quantiles ascending.

    ``only`` restricts which rows are perturbed; naming a row that isn't
    varying raises, so a typo can't silently perturb nothing.
    """
    if only is not None:
        unknown = set(only) - set(row_names)
        if unknown:
            raise EnsembleError(f"--only names no varying row: {sorted(unknown)}")
        row_names = [n for n in row_names if n in set(only)]
    return [{}] + [{name: q} for name in sorted(row_names) for q in quantiles]


def write_batch_manifest(
    batch_dir: Path, *, member_count: int,
    failures: list[tuple[int, Path, str]],
    mode: str = "monte_carlo",
    quantiles: tuple[float, ...] | None = None,
    rows: list[str] | None = None,
) -> None:
    """Persist what ``_run_ensemble`` knows that a run file alone can't show:
    how many members were expected, and which ones failed before
    ``resolve_member``/``run_scenario`` ever produced a run file for them to
    record their own ``status='failed'`` in. Without this, a batch where
    every member's resolution fails has no ``member_*.sqlite3`` files at
    all and is invisible to every other function in this module.
    """
    path = Path(batch_dir) / BATCH_MANIFEST_FILENAME
    manifest: dict[str, Any] = {
        "mode": mode,
        "member_count": member_count,
        "failures": [
            {"index": i, "intent_path": str(intent_path), "error": error}
            for i, intent_path, error in failures
        ],
    }
    if mode == "sensitivity":
        # ADR-007 §5: what an OAT batch perturbed, in oat_plan's member order.
        manifest["quantiles"] = list(quantiles or ())
        manifest["rows"] = list(rows or ())
    path.write_text(json.dumps(manifest, indent=2))


def read_batch_manifest(batch_dir: Path) -> dict[str, Any] | None:
    """``write_batch_manifest``'s record for this batch, or ``None`` for a
    batch generated before it existed (or synthesized directly by a test,
    bypassing ``_run_ensemble``)."""
    path = Path(batch_dir) / BATCH_MANIFEST_FILENAME
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text())
    # Batches written before ADR-007 carry no mode; they're all Monte Carlo.
    manifest.setdefault("mode", "monte_carlo")
    return manifest


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
