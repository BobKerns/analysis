"""Engine loop. Mirrors ADR-001/002.

Liability sign convention: account balances for ``kind='liability'`` store
outstanding-debt magnitude as a non-negative integer; the engine inverts the
delta sign when posting a ``transfer_event`` against a liability account in
either direction so that a payment reduces debt and a draw increases it.

v2 rule kinds: ``fixed_amount``, ``percent_of_balance`` (basis='opening',
rate from aux_table), ``waterfall_fill`` (drain ordered sources to top up a
target account), ``refill_transfer`` (conditional fixed top-up when a
target account falls below a threshold; sources optional),
``mortgage_payment`` (multi-leg P&I), ``ss_benefit``
(COLA-adjusted, claim-age gated), ``rmd`` (annual December, age-gated).
All v0/v1 rule kinds are now supported.
"""

from __future__ import annotations

import calendar
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from analysis.sim.runs import finalize_run, open_run_file

__all__ = ["run_scenario", "EngineError"]


class EngineError(RuntimeError):
    pass


SUPPORTED_RULE_KINDS = frozenset(
    {"fixed_amount", "percent_of_balance", "waterfall_fill",
     "refill_transfer",
    "mortgage_payment", "ss_benefit", "rmd", "tax_accumulator"}
)

SUPPORTED_CALENDAR_KINDS = frozenset(
    {"set_rate", "set_amount", "retire_scheduled", "one_off_transfer"}
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
        account_owner: dict[int, str | None] = {
            r[0]: r[1] for r in con.execute("SELECT id, owner FROM account")
        }
        account_id_by_name: dict[str, int] = {
            r[0]: r[1] for r in con.execute("SELECT name, id FROM account")
        }
        person_lifespan: dict[str, str] = {
            r[0]: r[1] for r in con.execute(
                "SELECT person, final_month FROM person_lifespan"
            )
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

        txns: list[list[Any]] = [
            list(r) for r in con.execute(
                'SELECT id, name, from_account, to_account, cadence, month_of_year, '
                '       amount_rule_kind, interest_account, person, series, '
                '       rate_micros, claim_age, amount_cents, params_json, base_amount, '
                '       tax_character, start, "end", priority, base_date, inflation_series, '
                '       ytd_amount, ytd_preroll '
                'FROM scheduled_transaction WHERE model_id = ? '
                'ORDER BY priority, id',
                (model_id,),
            ).fetchall()
        ]
        txn_by_name: dict[str, list[Any]] = {
            r[1]: r for r in txns
        }

        year_to_date_rows = con.execute(
            "SELECT id, from_account, to_account, amount, tax_character, year "
            "FROM year_to_date"
        ).fetchall()

        calendar_events = con.execute(
            'SELECT id, calendar_id, date, kind, '
            '       scheduled_name, account_1, account_2, series, amount, '
            '       param_date, tax_character, payload_json '
            'FROM calendar_event ORDER BY date, id'
        ).fetchall()

        sources = _load_sources(con)
        aux = _load_aux(con)
        inflation_indexes = _initialize_inflation_indexes(aux)
        inflation_factor_cache: dict[tuple[str, date, date], int] = {}

        skipped_counts: dict[str, int] = {}
        skipped_calendar: dict[str, int] = {}
        applied_event_ids: set[int] = set()

        previous_step_id: int | None = None
        for step_index, month in enumerate(months):
            month_s = month.isoformat()
            month_start_s = month.replace(day=1).isoformat()
            cur = con.execute(
                "INSERT INTO simulation_step(step_index, month, previous_step_id) "
                "VALUES (?, ?, ?)",
                (step_index, month_s, previous_step_id),
            )
            step_id = cur.lastrowid
            _advance_inflation_indexes(
                con, step_id, month_s, aux, inflation_indexes,
            )

            if step_index == 0:
                _apply_ytd_catchups(con, step_id, month, txns, year_to_date_rows)

            for ev in calendar_events:
                (ev_id, _cal_id, ev_date, ev_kind,
                 _p_sched_name, _p_acct1, _p_acct2, _p_series, _p_amount,
                 _p_param_date, _p_tax_char, ev_payload_json) = ev
                if ev_id in applied_event_ids:
                    continue
                if not (month_start_s <= ev_date <= month_s):
                    continue
                if ev_kind not in SUPPORTED_CALENDAR_KINDS:
                    skipped_calendar[ev_kind] = skipped_calendar.get(ev_kind, 0) + 1
                    applied_event_ids.add(ev_id)
                    continue
                _apply_calendar_event(
                    con, ev_id, ev_date, ev_kind,
                    _build_cal_payload(
                        _p_sched_name, _p_acct1, _p_acct2, _p_series, _p_amount,
                        _p_param_date, _p_tax_char, ev_payload_json,
                    ),
                    txns, txn_by_name, aux, account_id_by_name, account_kinds,
                    balances, step_id, month_s, model_id,
                )
                con.execute(
                    "INSERT INTO applied_calendar_event(step_id, calendar_event_id) "
                    "VALUES (?, ?)",
                    (step_id, ev_id),
                )
                applied_event_ids.add(ev_id)

            opening_balances = dict(balances)

            for txn in txns:
                (tid, _name, frm, to, cadence, moy, rule_kind,
                 p_interest_acct, p_person, p_series, p_rate_micros,
                 p_claim_age, p_amount_cents, ext_json, base_amount,
                 tax_char, t_start, t_end, _prio, t_base_date, t_inflation_series,
                 _t_ytd_amount, _t_ytd_preroll) = txn
                if month_s < t_start:
                    continue
                if t_end is not None and month_s > t_end:
                    continue
                if person_lifespan and _past_person_lifespan(
                    month_s, p_person, frm, to, account_owner, person_lifespan,
                ):
                    continue
                fire_count = _cadence_count(
                    cadence, moy, month,
                    _parse_date(t_start),
                    _parse_date(t_end) if t_end is not None else None,
                )
                if fire_count == 0:
                    continue
                if cadence == "biweekly" and rule_kind != "fixed_amount":
                    raise EngineError(
                        f"scheduled_transaction id={tid}: biweekly cadence is only "
                        f"supported with fixed_amount (got {rule_kind!r})"
                    )
                if rule_kind not in SUPPORTED_RULE_KINDS:
                    skipped_counts[rule_kind] = skipped_counts.get(rule_kind, 0) + 1
                    continue
                params: dict[str, Any] = {}
                if p_interest_acct is not None:
                    params["interest_account"] = p_interest_acct
                if p_person is not None:
                    params["person"] = p_person
                if p_series is not None:
                    params["series"] = p_series
                if p_rate_micros is not None:
                    params["rate_micros"] = p_rate_micros
                if p_claim_age is not None:
                    params["claim_age"] = p_claim_age
                if p_amount_cents is not None:
                    params["amount_cents"] = p_amount_cents
                if ext_json:
                    params.update(json.loads(ext_json))

                match rule_kind:
                    case "fixed_amount":
                        if base_amount is None:
                            raise EngineError(
                                f"scheduled_transaction id={tid} fixed_amount missing base_amount"
                            )
                        amount = base_amount
                        if t_inflation_series and t_inflation_series != "fixed":
                            if not t_base_date:
                                raise EngineError(
                                    f"scheduled_transaction id={tid}: inflation_series set "
                                    "without base_date"
                                )
                            factor = _cumulative_inflation_factor(
                                aux, t_inflation_series, _parse_date(t_base_date), month,
                                inflation_factor_cache,
                            )
                            amount = amount * factor // 1_000_000
                        _post_transfer(
                            con, balances, account_kinds, step_id, month_s,
                            frm, to, amount * fire_count, tax_char, tid,
                        )
                    case "percent_of_balance":
                        amount = _amount_percent_of_balance(
                            params, opening_balances, balances, to, aux,
                            month_s, tid,
                        )
                        if amount > 0:
                            _post_transfer(
                                con, balances, account_kinds, step_id, month_s,
                                frm, to, amount, tax_char, tid,
                            )
                    case "waterfall_fill":
                        _apply_waterfall_fill(
                            con, balances, account_kinds, step_id, month_s,
                            params, sources.get(tid, []), to,
                            tax_char, tid,
                        )
                    case "refill_transfer":
                        _apply_refill_transfer(
                            con, balances, account_kinds, step_id, month_s,
                            params, sources.get(tid, []), frm, to,
                            fire_count, tax_char, tid,
                        )
                    case "mortgage_payment":
                        _apply_mortgage_payment(
                            con, balances, account_kinds, step_id, month_s,
                            params, base_amount, frm, to, account_id_by_name,
                            tax_char, tid,
                        )
                    case "ss_benefit":
                        _apply_ss_benefit(
                            con, balances, account_kinds, step_id, month_s, month,
                            params, frm, to, person_birthdates, aux, tax_char, tid,
                        )
                    case "rmd":
                        _apply_rmd(
                            con, balances, account_kinds, step_id, month_s, month,
                            params, frm, to,
                            person_birthdates, prior_year_end_balances, aux,
                            tax_char, tid,
                        )
                    case "tax_accumulator":
                        if cadence != "annual":
                            raise EngineError(
                                f"tax_accumulator id={tid}: cadence must be 'annual'"
                            )
                        _apply_tax_accumulator(
                            con, balances, account_kinds, step_id, month_s, month,
                            params, from_id=frm, to_id=to, aux=aux,
                            tax_char=tax_char, tid=tid,
                        )
                    case _:  # pragma: no cover
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
        if skipped_calendar:
            con.execute(
                "UPDATE simulation_run SET run_args_json = json_set("
                "  run_args_json, '$.skipped_calendar_kinds', json(?)) WHERE id = 1",
                (json.dumps(skipped_calendar, sort_keys=True),),
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


def _ytd_catchup_date(start: date) -> date:
    """ADR-003 §4.10: date stamped on a partial-first-year catch-up transfer --
    the last day of the month before ``start``, so it lands in the same
    calendar year without colliding with ``start``'s own first-month flows.
    Degenerates to ``start`` itself when the scenario starts in January.
    """
    if start.month == 1:
        return start
    prev_month = start.month - 1
    last_day = calendar.monthrange(start.year, prev_month)[1]
    return date(start.year, prev_month, last_day)


def _year_to_date_row_date(start: date, year: int | None) -> date:
    """ADR-003 §4.10: a year_to_date.csv row's date. Defaults (``year`` unset
    or equal to ``start.year``) to ``_ytd_catchup_date``; an explicit
    different (fully prior) year is dated that year's Dec 31.
    """
    if year is None or year == start.year:
        return _ytd_catchup_date(start)
    return date(year, 12, 31)


def _post_ytd_transfer(
    con, step_id: int, date_s: str,
    from_id: int, to_id: int, amount: int, tax_char: str,
    reason_kind: str, reason_id: int,
) -> None:
    """ADR-003 §4.10: balance-neutral catch-up entry. Visible to income/expense/
    tax reporting like any other transfer_event, but does not mutate account
    balances -- those already reflect the post-receipt state via opening_balance.
    ``reason_kind`` distinguishes the two YTD sources ('ytd_rule' vs 'ytd')
    since their ``reason_id`` values come from separate id spaces
    (scheduled_transaction.id vs year_to_date.id) that can otherwise collide.
    """
    if amount == 0:
        return
    con.execute(
        "INSERT INTO transfer_event("
        "  step_id, date, from_account, to_account, amount, "
        "  tax_character, reason_kind, reason_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (step_id, date_s, from_id, to_id, amount, tax_char, reason_kind, reason_id),
    )


def _apply_ytd_catchups(
    con, step_id: int, start: date,
    txns: list[list[Any]],
    year_to_date_rows: list[tuple[int, int, int, int, str, int | None]],
) -> None:
    """Fire partial-first-year catch-up transfers once, at step 0."""
    date_s = _ytd_catchup_date(start).isoformat()
    for txn in txns:
        (tid, _name, frm, to, _cadence, _moy, _rule_kind,
         _p_interest_acct, _p_person, _p_series, _p_rate_micros,
         _p_claim_age, _p_amount_cents, _ext_json, base_amount,
         tax_char, _t_start, _t_end, _prio, _t_base_date, _t_inflation_series,
         ytd_amount, ytd_preroll) = txn
        if ytd_amount is not None:
            amount = ytd_amount
        elif ytd_preroll:
            amount = (base_amount or 0) * (start.month - 1)
        else:
            continue
        _post_ytd_transfer(con, step_id, date_s, frm, to, amount, tax_char, "ytd_rule", tid)
    for yid, y_from, y_to, y_amount, y_tax_char, y_year in year_to_date_rows:
        row_date_s = _year_to_date_row_date(start, y_year).isoformat()
        _post_ytd_transfer(
            con, step_id, row_date_s, y_from, y_to, y_amount, y_tax_char, "ytd", yid,
        )


def _past_person_lifespan(
    month_s: str,
    p_person: str | None,
    from_id: int,
    to_id: int,
    account_owner: dict[int, str | None],
    person_lifespan: dict[str, str],
) -> bool:
    """True if this rule is associated with a person whose final_month has
    passed (ADR-003 §4.9): the rule's explicit ``person`` param, or the
    owner of either leg's account.
    """
    associated = {p_person, account_owner.get(from_id), account_owner.get(to_id)}
    associated.discard(None)
    return any(
        month_s > person_lifespan[person]
        for person in associated
        if person in person_lifespan
    )


def _build_cal_payload(
    scheduled_name: str | None, account_1: str | None, account_2: str | None,
    series: str | None, amount: int | None, param_date: str | None,
    tax_character: str | None, ext_json: str | None,
) -> dict[str, Any]:
    """Build the payload dict for a calendar event from promoted columns + JSON ext."""
    payload: dict[str, Any] = {}
    if scheduled_name is not None:
        payload["scheduled_name"] = scheduled_name
    if account_1 is not None:
        payload["account_1"] = account_1
    if account_2 is not None:
        payload["account_2"] = account_2
    if series is not None:
        payload["series"] = series
    if amount is not None:
        payload["amount"] = amount
    if param_date is not None:
        payload["param_date"] = param_date
    if tax_character is not None:
        payload["tax_character"] = tax_character
    if ext_json:
        payload.update(json.loads(ext_json))
    return payload


def _amount_percent_of_balance(
    params: dict[str, Any],
    opening_balances: dict[int, int],
    current_balances: dict[int, int],
    to_id: int,
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    month_s: str, tid: int,
) -> int:
    rate_kind = params.get("rate_kind", "rate")
    rate_series = params.get("series")
    basis = params.get("rate_basis", "opening")
    base = (opening_balances if basis == "opening" else current_balances).get(to_id, 0)
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
    params: dict[str, Any], base_amount: int | None,
    from_id: int, liab_id: int,
    account_id_by_name: dict[str, int],
    tax_char: str, tid: int,
) -> None:
    """Multi-leg P&I per ADR-003 §2.7. Interest leg writes first; principal
    leg's ``parent_event_id`` points at the interest leg. The liability
    account is the row's ``to_account``; the interest-expense account is
    named by ``interest_account``.
    """
    if base_amount is None:
        raise EngineError(
            f"mortgage_payment id={tid}: missing base_amount (payment_amount)"
        )
    interest_name = params.get("interest_account")
    annual_micros = params.get("rate_micros")
    if not (interest_name and annual_micros is not None):
        raise EngineError(
            f"mortgage_payment id={tid}: requires interest_account and rate_micros"
        )
    interest_id = account_id_by_name.get(interest_name)
    if interest_id is None:
        raise EngineError(
            f"mortgage_payment id={tid}: unknown interest_account {interest_name!r}"
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
    pia = params.get("amount_cents")
    cola_series = params.get("series")
    if not person or claim_age is None or pia is None or not cola_series:
        raise EngineError(
            f"ss_benefit id={tid}: requires person, claim_age, "
            "base_pia_at_fra_cents, cola_series (person, claim_age, amount_cents, series)"
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
    person_birthdates: dict[str, date],
    prior_year_end_balances: dict[int, int],
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    tax_char: str, tid: int,
) -> None:
    """ADR-003 §2.3. Annual December RMD; age-gated by ``start_age_series``.
    Subject account is the row's ``from_account`` (the IRA being drawn down).
    ``dec31_prev_year_balance`` from prior-year-end ``account_state``, or
    ``account_model_member.prior_year_end_balance`` when no such step exists.
    """
    person = params.get("person")
    factor_table = params.get("factor_table", "rmd_factors")
    start_age_series = params.get("start_age_series", "rmd_start_age")
    if not person:
        raise EngineError(
            f"rmd id={tid}: requires person"
        )
    bd = person_birthdates.get(person)
    if bd is None:
        raise EngineError(f"rmd id={tid}: unknown person {person!r}")
    subject_id = from_id
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
            f"prior_year_end_balance for from_account id={subject_id}"
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


def _apply_refill_transfer(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str,
    params: dict[str, Any], source_ids: list[int],
    from_id: int, to_id: int, fire_count: int,
    tax_char: str, tid: int,
) -> None:
    refill_cents = params.get("amount_cents")
    if refill_cents is None:
        raise EngineError(
            f"refill_transfer id={tid}: missing amount (refill size)"
        )
    threshold = int(params.get("threshold_cents", 0))
    if balances.get(to_id, 0) >= threshold:
        return
    needed = int(refill_cents) * fire_count
    if source_ids:
        remaining = needed
        for src_id in source_ids:
            if remaining <= 0:
                break
            available = max(0, balances.get(src_id, 0))
            take = min(remaining, available)
            if take <= 0:
                continue
            _post_transfer(
                con, balances, account_kinds, step_id, month_s,
                src_id, to_id, take, tax_char, tid,
            )
            remaining -= take
    else:
        _post_transfer(
            con, balances, account_kinds, step_id, month_s,
            from_id, to_id, needed, tax_char, tid,
        )


def _apply_waterfall_fill(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str,
    params: dict[str, Any], source_ids: list[int],
    target_id: int,
    tax_char: str, tid: int,
) -> None:
    target_balance = params.get("amount_cents")
    if target_balance is None:
        raise EngineError(
            f"waterfall_fill id={tid}: missing target balance (amount_cents)"
        )
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


def _apply_tax_accumulator(
    con,
    balances: dict[int, int],
    account_kinds: dict[int, str],
    step_id: int,
    month_s: str,
    month: date,
    params: dict[str, Any],
    *,
    from_id: int,
    to_id: int,
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    tax_char: str,
    tid: int,
) -> None:
    """Compute and optionally pay forecasted income tax for a target year.

    Supported params:
    - year: "previous" (default), "current", or explicit year integer
    - scope: "federal" (default) or "state"
    - filing_status_aux: defaults to "filing_status"
    - bracket_aux: defaults to "federal_brackets" for federal, "state_brackets" for state
    - bracket_series: optional, required when state_brackets has multiple series
    - standard_deduction_aux: defaults to "standard_deduction"
    - ss_threshold_aux: defaults to "ss_taxation_thresholds" (federal only)
    """
    year_mode = str(params.get("year", "previous"))
    scope = str(params.get("scope", "federal"))
    if scope not in {"federal", "state"}:
        raise EngineError(
            f"tax_accumulator id={tid}: scope must be 'federal' or 'state' (got {scope!r})"
        )

    if year_mode == "previous":
        tax_year = month.year - 1
    elif year_mode == "current":
        tax_year = month.year
    else:
        try:
            tax_year = int(year_mode)
        except ValueError as e:
            raise EngineError(
                f"tax_accumulator id={tid}: invalid year={year_mode!r}"
            ) from e

    filing_status_aux = str(params.get("filing_status_aux", "filing_status"))
    standard_deduction_aux = str(params.get("standard_deduction_aux", "standard_deduction"))
    ss_threshold_aux = str(params.get("ss_threshold_aux", "ss_taxation_thresholds"))
    bracket_aux = str(
        params.get(
            "bracket_aux",
            "federal_brackets" if scope == "federal" else "state_brackets",
        )
    )
    bracket_series = params.get("bracket_series")

    as_of = f"{tax_year}-12-31"
    filing_status = _active_filing_status(aux, filing_status_aux, as_of, tid)

    if bracket_aux == "state_brackets" and bracket_series is None:
        # If exactly one state bracket series exists, default to it.
        state_series = sorted(
            series for (kind, series) in aux if kind == "state_brackets" and series is not None
        )
        if len(state_series) == 1:
            bracket_series = state_series[0]
        elif len(state_series) > 1:
            raise EngineError(
                f"tax_accumulator id={tid}: bracket_series required for state_brackets"
            )

    ordinary_income, ltcg_income, ss_income = _tax_year_income_buckets(
        con, tax_year=tax_year,
    )

    taxable_ss = 0
    if scope == "federal" and ss_income > 0:
        thresholds = _active_aux_rows(aux, ss_threshold_aux, None, as_of)
        thresholds = [r for r in thresholds if r.get("filing_status") == filing_status]
        if not thresholds:
            raise EngineError(
                f"tax_accumulator id={tid}: missing ss thresholds for filing_status={filing_status!r}"
            )
        tier1 = int(thresholds[0]["tier1_floor"])
        tier2 = int(thresholds[0]["tier2_floor"])
        taxable_ss = _taxable_social_security_cents(
            ordinary_income=ordinary_income,
            ltcg_income=ltcg_income,
            ss_income=ss_income,
            tier1=tier1,
            tier2=tier2,
        )

    deduction_rows = _active_aux_rows(aux, standard_deduction_aux, None, as_of)
    deduction_rows = [r for r in deduction_rows if r.get("filing_status") == filing_status]
    if not deduction_rows:
        raise EngineError(
            f"tax_accumulator id={tid}: missing standard deduction for filing_status={filing_status!r}"
        )
    standard_deduction = int(deduction_rows[0]["amount"])

    taxable_income = max(0, ordinary_income + ltcg_income + taxable_ss - standard_deduction)

    bracket_rows = _active_aux_rows(aux, bracket_aux, bracket_series, as_of)
    bracket_rows = [r for r in bracket_rows if r.get("filing_status") == filing_status]
    if not bracket_rows:
        raise EngineError(
            f"tax_accumulator id={tid}: missing brackets kind={bracket_aux!r} "
            f"series={bracket_series!r} filing_status={filing_status!r}"
        )
    brackets: list[tuple[int, int]] = sorted(
        (int(r["floor"]), int(r["marginal_rate_micros"]))
        for r in bracket_rows
    )
    tax_liability = _progressive_tax_cents(taxable_income, brackets)

    paid_transfers = con.execute(
        "SELECT COALESCE(SUM(amount), 0) "
        "FROM transfer_event "
        "WHERE substr(date, 1, 4) = ? "
        "  AND to_account = ?",
        (str(tax_year), to_id),
    ).fetchone()[0]
    already_paid = int(paid_transfers or 0)
    tax_due = max(0, tax_liability - already_paid)
    tax_overpaid = max(0, already_paid - tax_liability)

    if tax_due > 0:
        _post_transfer(
            con, balances, account_kinds, step_id, month_s,
            from_id, to_id, tax_due, tax_char, tid,
        )

    computed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary_rows = {
        f"{scope}_ordinary_income": ordinary_income,
        f"{scope}_ltcg_income": ltcg_income,
        f"{scope}_ss_income": ss_income,
        f"{scope}_taxable_ss": taxable_ss,
        f"{scope}_adjusted_gross_income": ordinary_income + ltcg_income + taxable_ss,
        f"{scope}_standard_deduction": standard_deduction,
        f"{scope}_taxable_income": taxable_income,
        f"{scope}_tax_liability": tax_liability,
        f"{scope}_tax_paid": already_paid,
        f"{scope}_tax_due": tax_due,
        f"{scope}_tax_overpaid": tax_overpaid,
    }

    for bucket, amount in summary_rows.items():
        con.execute(
            "INSERT OR REPLACE INTO tax_year_summary(year, bucket, amount, computed_at) "
            "VALUES (?, ?, ?, ?)",
            (tax_year, bucket, int(amount), computed_at),
        )


def _tax_year_income_buckets(con, *, tax_year: int) -> tuple[int, int, int]:
    rows = con.execute(
        "SELECT tax_character, COALESCE(SUM(amount), 0) "
        "FROM transfer_event "
        "WHERE substr(date, 1, 4) = ? "
        "GROUP BY tax_character",
        (str(tax_year),),
    ).fetchall()
    by_char = {k: int(v or 0) for k, v in rows}
    ordinary = by_char.get("ordinary", 0) + by_char.get("tax_deferred_out", 0)
    ltcg = by_char.get("ltcg", 0)
    ss = by_char.get("ss_benefit", 0)
    return ordinary, ltcg, ss


def _active_filing_status(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    filing_status_aux: str,
    as_of: str,
    tid: int,
) -> str:
    rows = _active_aux_rows(aux, filing_status_aux, None, as_of)
    if not rows:
        raise EngineError(
            f"tax_accumulator id={tid}: no filing_status row effective <= {as_of}"
        )
    status = rows[0].get("filing_status")
    if not status:
        raise EngineError(
            f"tax_accumulator id={tid}: filing_status payload missing filing_status"
        )
    return str(status)


def _taxable_social_security_cents(
    *,
    ordinary_income: int,
    ltcg_income: int,
    ss_income: int,
    tier1: int,
    tier2: int,
) -> int:
    """Approximate taxable Social Security amount under provisional-income rules."""
    provisional = ordinary_income + ltcg_income + (ss_income // 2)
    if provisional <= tier1:
        return 0
    if provisional <= tier2:
        return min(ss_income // 2, (provisional - tier1) // 2)
    base = min(ss_income // 2, (tier2 - tier1) // 2)
    addl = ((provisional - tier2) * 85) // 100
    return min((ss_income * 85) // 100, base + addl)


def _progressive_tax_cents(taxable_income: int, brackets: list[tuple[int, int]]) -> int:
    if taxable_income <= 0 or not brackets:
        return 0
    tax = 0
    for i, (floor, rate_micros) in enumerate(brackets):
        if taxable_income <= floor:
            continue
        next_floor = brackets[i + 1][0] if i + 1 < len(brackets) else None
        upper = taxable_income if next_floor is None else min(taxable_income, next_floor)
        taxable_slice = max(0, upper - floor)
        if taxable_slice > 0:
            tax += (taxable_slice * rate_micros) // 1_000_000
    return tax


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


def _initialize_inflation_indexes(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
) -> dict[str, int]:
    indexes: dict[str, int] = {}
    for (kind, series), _rows in aux.items():
        if kind != "inflation" or series is None:
            continue
        indexes[series] = 1_000_000
    return indexes


def _advance_inflation_indexes(
    con,
    step_id: int,
    month_s: str,
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    indexes: dict[str, int],
) -> None:
    for series in sorted(indexes):
        payload = _lookup_aux(aux, "inflation", series, month_s)
        if payload is None:
            raise EngineError(
                f"missing inflation aux row for series={series!r} effective <= {month_s}"
            )
        annual_micros = int(payload.get("annual_rate_micros", 0))
        indexes[series] = (
            indexes[series] * (12_000_000 + annual_micros)
        ) // 12_000_000
        con.execute(
            "INSERT INTO inflation_index_state("
            "step_id, series, cumulative_index_micros"
            ") VALUES (?, ?, ?)",
            (step_id, series, indexes[series]),
        )


def _cumulative_inflation_factor(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    series: str, from_date: date, to_date: date,
    cache: dict[tuple[str, date, date], int],
) -> int:
    """ADR-003 §2.1: cumulative monthly-compounded inflation factor (micros;
    1_000_000 = 1.0x) from ``from_date`` (exclusive) through ``to_date``
    (inclusive). Returns the inverse factor if ``to_date`` precedes
    ``from_date``.
    """
    if to_date == from_date:
        return 1_000_000
    key = (series, from_date, to_date)
    cached = cache.get(key)
    if cached is not None:
        return cached
    lo, hi = (from_date, to_date) if from_date < to_date else (to_date, from_date)
    factor = 1_000_000
    for month in _month_ends(lo, hi):
        if month <= lo:
            continue
        month_s = month.isoformat()
        payload = _lookup_aux(aux, "inflation", series, month_s)
        if payload is None:
            raise EngineError(
                f"fixed_amount inflation: no aux row for series={series!r} "
                f"effective <= {month_s}"
            )
        annual_micros = int(payload.get("annual_rate_micros", 0))
        factor = factor * (12_000_000 + annual_micros) // 12_000_000
    if from_date > to_date:
        factor = (1_000_000 * 1_000_000) // factor
    cache[key] = factor
    return factor


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


def _active_aux_rows(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    kind: str,
    series: str | None,
    month_s: str,
) -> list[dict[str, Any]]:
    rows = aux.get((kind, series))
    if not rows:
        return []
    effective_dates = [effective for effective, _ in rows if effective <= month_s]
    if not effective_dates:
        return []
    active_effective = max(effective_dates)
    return [payload for effective, payload in rows if effective == active_effective]


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
    match cadence:
        case "monthly":
            return True
        case "quarterly":
            return ((month.year - start.year) * 12 + (month.month - start.month)) % 3 == 0
        case "annual":
            target = month_of_year if month_of_year is not None else start.month
            return month.month == target
        case "one_off":
            return month.year == start.year and month.month == start.month
        case _:
            raise EngineError(f"unknown cadence: {cadence}")


def _biweekly_count_in_month(start: date, month: date, end: date | None) -> int:
    """Number of biweekly firings (anchored at ``start``, 14-day stride) in ``month``.

    ``month`` is any date in the target calendar month; ``end`` (inclusive)
    clips later firings (pass None for unbounded).
    """
    first = date(month.year, month.month, 1)
    next_year = month.year + (1 if month.month == 12 else 0)
    next_month = 1 if month.month == 12 else month.month + 1
    last = date(next_year, next_month, 1) - timedelta(days=1)
    if start > last:
        return 0
    if start >= first:
        d = start
    else:
        gap = (first - start).days
        d = start + timedelta(days=((gap + 13) // 14) * 14)
    count = 0
    while d <= last:
        if end is not None and d > end:
            break
        count += 1
        d += timedelta(days=14)
    return count


def _cadence_count(
    cadence: str, month_of_year: int | None, month: date,
    start: date, end: date | None,
) -> int:
    """Number of times a scheduled txn fires inside ``month`` (0 = skip)."""
    if cadence == "biweekly":
        return _biweekly_count_in_month(start, month, end)
    return 1 if _cadence_fires(cadence, month_of_year, month, start) else 0


def _apply_calendar_event(
    con,
    ev_id: int, ev_date: str, ev_kind: str, payload: dict[str, Any],
    txns: list[list[Any]],
    txn_by_name: dict[str, list[Any]],
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    account_id_by_name: dict[str, int],
    account_kinds: dict[int, str],
    balances: dict[int, int],
    step_id: int, month_s: str, model_id: int,
) -> None:
    """Expand one calendar_event per ADR-003 \u00a73."""
    match ev_kind:
        case "set_rate":
            aux_kind = payload.get("aux_kind", "rate")
            series = payload.get("series")
            new_value = payload.get("amount")
            if series is None or new_value is None:
                raise EngineError(
                    f"set_rate event id={ev_id}: requires series and amount (new_value_micros)"
                )
            row_payload = {"annual_rate_micros": int(new_value)}
            con.execute(
                "INSERT INTO aux_table(kind, series, effective_date, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (aux_kind, series, ev_date, json.dumps(row_payload)),
            )
            bucket = aux.setdefault((aux_kind, series), [])
            bucket.append((ev_date, row_payload))
            bucket.sort(key=lambda r: r[0])
        case "set_amount":
            name = payload.get("scheduled_name")
            new_amount = payload.get("amount")
            if name is None or new_amount is None:
                raise EngineError(
                    f"set_amount event id={ev_id}: requires scheduled_name and "
                    "new_base_amount (scheduled_name, amount)"
                )
            rec = txn_by_name.get(name)
            if rec is None:
                raise EngineError(
                    f"set_amount event id={ev_id}: unknown scheduled_name {name!r}"
                )
            rec[14] = int(new_amount)
            new_base_date = payload.get("param_date")
            if new_base_date:
                rec[19] = new_base_date
            con.execute(
                "UPDATE scheduled_transaction SET base_amount = ?, "
                "base_date = COALESCE(?, base_date) WHERE id = ?",
                (int(new_amount), new_base_date, rec[0]),
            )
        case "retire_scheduled":
            name = payload.get("scheduled_name")
            if name is None:
                raise EngineError(
                    f"retire_scheduled event id={ev_id}: requires scheduled_name"
                )
            rec = txn_by_name.get(name)
            if rec is None:
                raise EngineError(
                    f"retire_scheduled event id={ev_id}: unknown scheduled_name "
                    f"{name!r}"
                )
            rec[17] = ev_date
            con.execute(
                'UPDATE scheduled_transaction SET "end" = ? WHERE id = ?',
                (ev_date, rec[0]),
            )
        case "one_off_transfer":
            frm_name = payload.get("account_1")
            to_name = payload.get("account_2")
            amount = payload.get("amount")
            tax_char = payload.get("tax_character") or "none"
            if frm_name is None or to_name is None or amount is None:
                raise EngineError(
                    f"one_off_transfer event id={ev_id}: requires from_account, "
                    "to_account, amount (account_1, account_2, amount)"
                )
            from_id = account_id_by_name.get(frm_name)
            to_id = account_id_by_name.get(to_name)
            if from_id is None or to_id is None:
                raise EngineError(
                    f"one_off_transfer event id={ev_id}: unknown account "
                    f"(account_1={frm_name!r} or account_2={to_name!r})"
                )
            cur = con.execute(
                "INSERT INTO scheduled_transaction("
                "  model_id, name, from_account, to_account, cadence, "
                "  amount_rule_kind, base_amount, tax_character, start, \"end\", "
                "  priority, created_by_event_id, description"
                ") VALUES (?, ?, ?, ?, 'one_off', 'fixed_amount', ?, ?, ?, ?, "
                "  200, ?, ?)",
                (model_id, f"oneoff_evt_{ev_id}", from_id, to_id,
                 int(amount), tax_char, ev_date, ev_date, ev_id,
                 payload.get("description")),
            )
            new_tid = cur.lastrowid
            _post_transfer(
                con, balances, account_kinds, step_id, month_s,
                from_id, to_id, int(amount), tax_char, new_tid,
            )
        case _:  # pragma: no cover
            raise EngineError(f"supported but unhandled calendar kind: {ev_kind}")
