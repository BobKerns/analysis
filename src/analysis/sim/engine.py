"""Engine loop. Mirrors ADR-001/002.

v1 fires ``amount_rule_kind='fixed_amount'`` scheduled transactions with
``monthly``/``quarterly``/``annual``/``one_off`` cadence. Other rule kinds
(percent_of_balance, mortgage_payment, ss_benefit, rmd, waterfall_fill)
are tallied as ``skipped`` and recorded in ``simulation_run.run_args_json``
under the ``skipped_rule_kinds`` key; they land in later slices.
"""

from __future__ import annotations

import calendar
import json
from datetime import date
from pathlib import Path
from typing import Any

from analysis.sim.runs import finalize_run, open_run_file

__all__ = ["run_scenario", "EngineError"]


class EngineError(RuntimeError):
    pass


SUPPORTED_RULE_KINDS = frozenset({"fixed_amount"})


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
        balances: dict[int, int] = {a_id: bal for (a_id, bal, _basis) in accounts}
        cost_bases: dict[int, int | None] = {a_id: cb for (a_id, _b, cb) in accounts}

        txns = con.execute(
            'SELECT id, from_account, to_account, cadence, month_of_year, '
            '       amount_rule_kind, base_amount, tax_character, '
            '       start, "end", priority '
            'FROM scheduled_transaction WHERE model_id = ? '
            'ORDER BY priority, id',
            (model_id,),
        ).fetchall()

        skipped_counts: dict[str, int] = {}

        previous_step_id: int | None = None
        for step_index, month in enumerate(months):
            month_s = month.isoformat()
            cur = con.execute(
                "INSERT INTO simulation_step(step_index, month, previous_step_id) "
                "VALUES (?, ?, ?)",
                (step_index, month_s, previous_step_id),
            )
            step_id = cur.lastrowid

            for txn in txns:
                (tid, frm, to, cadence, moy, rule_kind, base_amount, tax_char,
                 t_start, t_end, _prio) = txn
                if month_s < t_start:
                    continue
                if t_end is not None and month_s > t_end:
                    continue
                if not _cadence_fires(cadence, moy, month, _parse_date(t_start)):
                    continue
                if rule_kind not in SUPPORTED_RULE_KINDS:
                    skipped_counts[rule_kind] = skipped_counts.get(rule_kind, 0) + 1
                    continue
                amount = base_amount
                if amount is None:
                    raise EngineError(
                        f"scheduled_transaction id={tid} fixed_amount missing base_amount"
                    )
                con.execute(
                    "INSERT INTO transfer_event("
                    "  step_id, date, from_account, to_account, amount, "
                    "  tax_character, reason_kind, reason_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, 'rule', ?)",
                    (step_id, month_s, frm, to, amount, tax_char, tid),
                )
                balances[frm] = balances.get(frm, 0) - amount
                balances[to] = balances.get(to, 0) + amount

            con.executemany(
                "INSERT INTO account_state(step_id, account_id, balance, cost_basis) "
                "VALUES (?, ?, ?, ?)",
                [(step_id, a_id, balances[a_id], cost_bases[a_id])
                 for a_id in balances],
            )
            previous_step_id = step_id

        if skipped_counts:
            con.execute(
                "UPDATE simulation_run SET run_args_json = json_set("
                "  run_args_json, '$.skipped_rule_kinds', json(?)) WHERE id = 1",
                (json.dumps(skipped_counts, sort_keys=True),),
            )

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


def _cadence_fires(
    cadence: str, month_of_year: int | None, month: date, start: date,
) -> bool:
    if cadence == "monthly":
        return True
    if cadence == "quarterly":
        return ((month.year - start.year) * 12 + (month.month - start.month)) % 3 == 0
    if cadence == "annual":
        target = month_of_year if month_of_year is not None else start.month
        return month.month == target
    if cadence == "one_off":
        return month.year == start.year and month.month == start.month
    raise EngineError(f"unknown cadence: {cadence}")
