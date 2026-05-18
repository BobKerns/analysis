"""Engine loop. Mirrors ADR-001/002.

Liability sign convention: account balances for ``kind='liability'`` store
outstanding-debt magnitude as a non-negative integer; the engine inverts the
delta sign when posting a ``transfer_event`` against a liability account in
either direction so that a payment reduces debt and a draw increases it.

v2 rule kinds: ``fixed_amount``, ``percent_of_balance`` (basis='opening',
rate from aux_table), ``waterfall_fill`` (drain ordered sources to top up a
target account), ``mortgage_payment`` (multi-leg P&I), ``ss_benefit``
(COLA-adjusted, claim-age gated), ``rmd`` (annual December, age-gated).
All v0/v1 rule kinds are now supported.
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


SUPPORTED_RULE_KINDS = frozenset(
    {"fixed_amount", "percent_of_balance", "waterfall_fill",
     "mortgage_payment", "ss_benefit", "rmd"}
)

SS_FRA = 67


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

        account_kinds: dict[int, str] = {
            r[0]: r[1] for r in con.execute("SELECT id, kind FROM account")
        }
        account_id_by_name: dict[str, int] = {
            r[0]: r[1] for r in con.execute("SELECT name, id FROM account")
        }
        person_birthdates: dict[str, date] = {
            r[0]: _parse_date(r[1])
            for r in con.execute("SELECT id, birthdate FROM person")
        }
        prior_year_end_balances: dict[int, int] = {
            r[0]: r[1] for r in con.execute(
                "SELECT account_id, prior_year_end_balance "
                "FROM account_model_member WHERE model_id = ? "
                "AND prior_year_end_balance IS NOT NULL",
                (model_id,),
            )
        }

        txns = con.execute(
            'SELECT id, name, from_account, to_account, cadence, month_of_year, '
            '       amount_rule_kind, amount_rule_params_json, base_amount, '
            '       tax_character, start, "end", priority '
            'FROM scheduled_transaction WHERE model_id = ? '
            'ORDER BY priority, id',
            (model_id,),
        ).fetchall()

        sources = _load_sources(con)
        aux = _load_aux(con)

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

            opening_balances = dict(balances)

            for txn in txns:
                (tid, _name, frm, to, cadence, moy, rule_kind, params_json,
                 base_amount, tax_char, t_start, t_end, _prio) = txn
                if month_s < t_start:
                    continue
                if t_end is not None and month_s > t_end:
                    continue
                if not _cadence_fires(cadence, moy, month, _parse_date(t_start)):
                    continue
                if rule_kind not in SUPPORTED_RULE_KINDS:
                    skipped_counts[rule_kind] = skipped_counts.get(rule_kind, 0) + 1
                    continue
                params = json.loads(params_json or "{}")

                if rule_kind == "fixed_amount":
                    if base_amount is None:
                        raise EngineError(
                            f"scheduled_transaction id={tid} fixed_amount missing base_amount"
                        )
                    _post_transfer(
                        con, balances, account_kinds, step_id, month_s,
                        frm, to, base_amount, tax_char, tid,
                    )
                elif rule_kind == "percent_of_balance":
                    amount = _amount_percent_of_balance(
                        params, opening_balances, balances, account_id_by_name, aux,
                        month_s, tid,
                    )
                    if amount > 0:
                        _post_transfer(
                            con, balances, account_kinds, step_id, month_s,
                            frm, to, amount, tax_char, tid,
                        )
                elif rule_kind == "waterfall_fill":
                    _apply_waterfall_fill(
                        con, balances, account_kinds, step_id, month_s,
                        params, sources.get(tid, []), account_id_by_name,
                        tax_char, tid,
                    )
                elif rule_kind == "mortgage_payment":
                    _apply_mortgage_payment(
                        con, balances, account_kinds, step_id, month_s,
                        params, base_amount, frm, account_id_by_name,
                        tax_char, tid,
                    )
                elif rule_kind == "ss_benefit":
                    _apply_ss_benefit(
                        con, balances, account_kinds, step_id, month_s, month,
                        params, frm, to, person_birthdates, aux, tax_char, tid,
                    )
                elif rule_kind == "rmd":
                    _apply_rmd(
                        con, balances, account_kinds, step_id, month_s, month,
                        params, frm, to, account_id_by_name,
                        person_birthdates, prior_year_end_balances, aux,
                        tax_char, tid,
                    )
                else:  # pragma: no cover
                    raise EngineError(f"supported but unhandled rule_kind: {rule_kind}")

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


def _post_transfer(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str,
    from_id: int, to_id: int, amount: int, tax_char: str, reason_id: int,
    parent_event_id: int | None = None,
) -> int | None:
    """Insert one transfer_event and adjust balances. Liability balances
    store debt magnitude; the sign is inverted when posting against them.
    Returns the new event's rowid, or None when amount==0 (no insert).
    """
    if amount == 0:
        return None
    cur = con.execute(
        "INSERT INTO transfer_event("
        "  step_id, date, from_account, to_account, amount, "
        "  tax_character, reason_kind, reason_id, parent_event_id"
        ") VALUES (?, ?, ?, ?, ?, ?, 'rule', ?, ?)",
        (step_id, month_s, from_id, to_id, amount, tax_char, reason_id,
         parent_event_id),
    )
    balances[from_id] = balances.get(from_id, 0) + _delta(account_kinds[from_id], -amount)
    balances[to_id] = balances.get(to_id, 0) + _delta(account_kinds[to_id], +amount)
    return cur.lastrowid


def _delta(kind: str, raw: int) -> int:
    return -raw if kind == "liability" else raw


def _amount_percent_of_balance(
    params: dict[str, Any],
    opening_balances: dict[int, int],
    current_balances: dict[int, int],
    account_id_by_name: dict[str, int],
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    month_s: str, tid: int,
) -> int:
    source_name = params.get("source_account")
    rate_kind = params.get("rate_kind")
    rate_series = params.get("rate_series")
    basis = params.get("rate_basis", "opening")
    if not source_name or not rate_kind:
        raise EngineError(f"percent_of_balance id={tid}: missing source_account/rate_kind")
    src_id = account_id_by_name.get(source_name)
    if src_id is None:
        raise EngineError(f"percent_of_balance id={tid}: unknown source_account {source_name!r}")
    base = (opening_balances if basis == "opening" else current_balances).get(src_id, 0)
    if base <= 0:
        return 0
    payload = _lookup_aux(aux, rate_kind, rate_series, month_s)
    if payload is None:
        raise EngineError(
            f"percent_of_balance id={tid}: no aux row for kind={rate_kind!r} "
            f"series={rate_series!r} effective <= {month_s}"
        )
    annual_micros = payload.get("annual_rate_micros")
    if annual_micros is None:
        raise EngineError(
            f"percent_of_balance id={tid}: aux payload missing annual_rate_micros"
        )
    # monthly_rate = annual_micros / 12 / 1_000_000; amount = base * monthly_rate
    return (base * int(annual_micros)) // (12 * 1_000_000)


def _apply_mortgage_payment(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str,
    params: dict[str, Any], base_amount: int | None, from_id: int,
    account_id_by_name: dict[str, int],
    tax_char: str, tid: int,
) -> None:
    """Multi-leg P&I per ADR-003 §2.7. Interest leg writes first; principal
    leg's ``parent_event_id`` points at the interest leg.
    """
    if base_amount is None:
        raise EngineError(
            f"mortgage_payment id={tid}: missing base_amount (payment_amount)"
        )
    liab_name = params.get("liability_account")
    interest_name = params.get("interest_expense_account")
    annual_micros = params.get("annual_rate_micros")
    if not (liab_name and interest_name and annual_micros is not None):
        raise EngineError(
            f"mortgage_payment id={tid}: requires liability_account, "
            "interest_expense_account, annual_rate_micros in params"
        )
    liab_id = account_id_by_name.get(liab_name)
    interest_id = account_id_by_name.get(interest_name)
    if liab_id is None or interest_id is None:
        raise EngineError(
            f"mortgage_payment id={tid}: unknown account in params"
        )
    outstanding = max(0, balances.get(liab_id, 0))
    if outstanding == 0:
        return
    interest = (outstanding * int(annual_micros)) // (12 * 1_000_000)
    payment = int(base_amount)
    principal = payment - interest
    if principal < 0:
        raise EngineError(
            f"mortgage_payment id={tid}: interest {interest} exceeds payment {payment}"
        )
    if principal > outstanding:
        principal = outstanding
    interest_event_id = _post_transfer(
        con, balances, account_kinds, step_id, month_s,
        from_id, interest_id, interest, tax_char, tid,
    )
    _post_transfer(
        con, balances, account_kinds, step_id, month_s,
        from_id, liab_id, principal, tax_char, tid,
        parent_event_id=interest_event_id,
    )


def _claim_multiplier_micros(claim_age: int, fra: int = SS_FRA) -> int:
    months_diff = (claim_age - fra) * 12
    if months_diff == 0:
        return 1_000_000
    if months_diff > 0:
        months_late = min(months_diff, 36)
        years_late, rem_months = divmod(months_late, 12)
        return 1_000_000 + years_late * 80_000 + rem_months * 20_000 // 3
    months_early = -months_diff
    first36 = min(36, months_early)
    over = months_early - first36
    reduction = first36 * 50_000 // 9 + over * 50_000 // 12
    return 1_000_000 - reduction


def _apply_ss_benefit(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str, month: date,
    params: dict[str, Any], from_id: int, to_id: int,
    person_birthdates: dict[str, date],
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    tax_char: str, tid: int,
) -> None:
    """ADR-003 §2.5. Claim-age multiplier + annual-January COLA compounding."""
    person = params.get("person")
    claim_age = params.get("claim_age")
    pia = params.get("base_pia_at_fra_cents")
    cola_series = params.get("cola_series")
    if not person or claim_age is None or pia is None or not cola_series:
        raise EngineError(
            f"ss_benefit id={tid}: requires person, claim_age, "
            "base_pia_at_fra_cents, cola_series in params"
        )
    bd = person_birthdates.get(person)
    if bd is None:
        raise EngineError(f"ss_benefit id={tid}: unknown person {person!r}")
    claim_date = date(bd.year + int(claim_age), bd.month, bd.day)
    if month < claim_date:
        return
    payload = _lookup_aux(aux, "inflation", cola_series, month_s)
    if payload is None:
        raise EngineError(
            f"ss_benefit id={tid}: no inflation aux for series {cola_series!r}"
        )
    rate = int(payload.get("annual_rate_micros", 0))
    mult_micros = _claim_multiplier_micros(int(claim_age))
    factor_micros = 1_000_000
    for _ in range(month.year - claim_date.year):
        factor_micros = factor_micros * (1_000_000 + rate) // 1_000_000
    amount = int(pia) * mult_micros * factor_micros // (10 ** 12)
    if amount > 0:
        _post_transfer(
            con, balances, account_kinds, step_id, month_s,
            from_id, to_id, amount, tax_char, tid,
        )


def _apply_rmd(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str, month: date,
    params: dict[str, Any], from_id: int, to_id: int,
    account_id_by_name: dict[str, int],
    person_birthdates: dict[str, date],
    prior_year_end_balances: dict[int, int],
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    tax_char: str, tid: int,
) -> None:
    """ADR-003 §2.3. Annual December RMD; age-gated by ``start_age_series``.
    ``dec31_prev_year_balance`` from prior-year-end ``account_state``, or
    ``account_model_member.prior_year_end_balance`` when no such step exists.
    """
    subject_name = params.get("subject_account")
    person = params.get("person")
    factor_table = params.get("factor_table", "rmd_factors")
    start_age_series = params.get("start_age_series", "rmd_start_age")
    if not subject_name or not person:
        raise EngineError(
            f"rmd id={tid}: requires subject_account and person in params"
        )
    bd = person_birthdates.get(person)
    if bd is None:
        raise EngineError(f"rmd id={tid}: unknown person {person!r}")
    subject_id = account_id_by_name.get(subject_name)
    if subject_id is None:
        raise EngineError(f"rmd id={tid}: unknown subject_account {subject_name!r}")
    age_year_end = month.year - bd.year
    start_payload = _lookup_aux(aux, start_age_series, None, month_s)
    if start_payload is None or "start_age" not in start_payload:
        raise EngineError(
            f"rmd id={tid}: missing {start_age_series!r} aux with start_age"
        )
    if age_year_end < int(start_payload["start_age"]):
        return
    prev_year = month.year - 1
    row = con.execute(
        "SELECT ast.balance FROM account_state ast "
        "JOIN simulation_step s ON s.id = ast.step_id "
        "WHERE ast.account_id = ? AND s.month = ?",
        (subject_id, f"{prev_year}-12-31"),
    ).fetchone()
    if row is not None:
        prev_bal = int(row[0])
    elif subject_id in prior_year_end_balances:
        prev_bal = int(prior_year_end_balances[subject_id])
    else:
        raise EngineError(
            f"rmd id={tid}: no Dec-{prev_year} account_state and no "
            f"prior_year_end_balance for {subject_name!r}"
        )
    factor_payload = _lookup_rmd_factor(aux, factor_table, age_year_end, month_s)
    if factor_payload is None:
        raise EngineError(
            f"rmd id={tid}: no {factor_table!r} factor for age {age_year_end} "
            f"effective <= {month_s}"
        )
    factor_micros = int(factor_payload["factor_micros"])
    amount = prev_bal * 1_000_000 // factor_micros
    if amount > 0:
        _post_transfer(
            con, balances, account_kinds, step_id, month_s,
            from_id, to_id, amount, tax_char, tid,
        )


def _lookup_rmd_factor(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    kind: str, age: int, month_s: str,
) -> dict[str, Any] | None:
    rows = aux.get((kind, None))
    if not rows:
        return None
    chosen: dict[str, Any] | None = None
    for effective, payload in rows:
        if effective <= month_s and int(payload.get("age", -1)) == age:
            chosen = payload
    return chosen


def _apply_waterfall_fill(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str,
    params: dict[str, Any], source_ids: list[int],
    account_id_by_name: dict[str, int],
    tax_char: str, tid: int,
) -> None:
    target_name = params.get("target_account")
    target_balance = params.get("target_balance_cents")
    if target_name is None or target_balance is None:
        raise EngineError(
            f"waterfall_fill id={tid}: missing target_account/target_balance_cents"
        )
    target_id = account_id_by_name.get(target_name)
    if target_id is None:
        raise EngineError(f"waterfall_fill id={tid}: unknown target {target_name!r}")
    if not source_ids:
        raise EngineError(f"waterfall_fill id={tid}: no scheduled_transaction_source rows")
    deficit = int(target_balance) - balances.get(target_id, 0)
    if deficit <= 0:
        return
    for src_id in source_ids:
        if deficit <= 0:
            break
        available = max(0, balances.get(src_id, 0))
        take = min(deficit, available)
        if take <= 0:
            continue
        _post_transfer(
            con, balances, account_kinds, step_id, month_s,
            src_id, target_id, take, tax_char, tid,
        )
        deficit -= take


def _load_sources(con) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for tid, _pos, aid in con.execute(
        "SELECT scheduled_transaction_id, position, account_id "
        "FROM scheduled_transaction_source ORDER BY scheduled_transaction_id, position"
    ):
        out.setdefault(tid, []).append(aid)
    return out


def _load_aux(con) -> dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]]:
    out: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]] = {}
    for kind, series, effective, payload_json in con.execute(
        "SELECT kind, series, effective_date, payload_json FROM aux_table"
    ):
        out.setdefault((kind, series), []).append(
            (effective, json.loads(payload_json or "{}"))
        )
    for v in out.values():
        v.sort(key=lambda row: row[0])
    return out


def _lookup_aux(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    kind: str, series: str | None, month_s: str,
) -> dict[str, Any] | None:
    rows = aux.get((kind, series))
    if not rows:
        return None
    chosen: dict[str, Any] | None = None
    for effective, payload in rows:
        if effective <= month_s:
            chosen = payload
        else:
            break
    return chosen


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
