"""Engine loop v0. Mirrors ADR-001/002.

This skeleton emits the month-end step chain and carries opening balances
forward unchanged. No scheduled-transaction firing, no tax scan; those land
in later slices.
"""

from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path
from typing import Any

from analysis.sim.runs import finalize_run, open_run_file

__all__ = ["run_scenario", "EngineError"]


class EngineError(RuntimeError):
    pass


def run_scenario(
    intent_path: Path,
    run_path: Path,
    *,
    run_by: str,
    data_mode: str,
    allow_dirty_data: bool = False,
    allow_dirty_code: bool = False,
    run_args: dict[str, Any] | None = None,
    engine_options: dict[str, Any] | None = None,
) -> Path:
    con = open_run_file(
        run_path, intent_path,
        run_by=run_by, data_mode=data_mode,
        allow_dirty_data=allow_dirty_data, allow_dirty_code=allow_dirty_code,
        run_args=run_args, engine_options=engine_options,
    )
    try:
        scenario = con.execute(
            'SELECT start, "end", account_model_id FROM scenario'
        ).fetchone()
        if scenario is None:
            raise EngineError("scenario table is empty in intent snapshot")
        start_s, end_s, model_id = scenario
        months = list(_month_ends(_parse_date(start_s), _parse_date(end_s)))
        if not months:
            raise EngineError(f"scenario range is empty: start={start_s} end={end_s}")

        accounts = con.execute(
            "SELECT a.id, COALESCE(m.opening_balance, 0), m.opening_cost_basis "
            "FROM account a "
            "LEFT JOIN account_model_member m "
            "  ON m.account_id = a.id AND m.model_id = ? "
            "ORDER BY a.id",
            (model_id,),
        ).fetchall()

        previous_step_id: int | None = None
        for step_index, month in enumerate(months):
            cur = con.execute(
                "INSERT INTO simulation_step(step_index, month, previous_step_id) "
                "VALUES (?, ?, ?)",
                (step_index, month.isoformat(), previous_step_id),
            )
            step_id = cur.lastrowid
            con.executemany(
                "INSERT INTO account_state(step_id, account_id, balance, cost_basis) "
                "VALUES (?, ?, ?, ?)",
                [(step_id, a_id, bal, basis) for (a_id, bal, basis) in accounts],
            )
            previous_step_id = step_id

        finalize_run(con, status="succeeded")
    except Exception as e:
        finalize_run(con, status="failed", failure_reason=f"{type(e).__name__}: {e}")
        raise
    finally:
        con.close()
    return Path(run_path)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _month_ends(start: date, end: date):
    """Yield month-end dates from the month containing ``start`` through ``end``."""
    y, m = start.year, start.month
    while True:
        last = calendar.monthrange(y, m)[1]
        d = date(y, m, last)
        if d > end:
            return
        yield d
        m += 1
        if m == 13:
            m = 1
            y += 1
