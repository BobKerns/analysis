"""Engine loop. Mirrors ADR-001/002.

Liability sign convention: account balances for ``kind='liability'`` store
outstanding-debt magnitude as a non-negative integer; the engine inverts the
delta sign when posting a ``transfer_event`` against a liability account in
either direction so that a payment reduces debt and a draw increases it.

v2 rule kinds: ``fixed_amount``, ``percent_of_balance`` (basis='opening',
rate from aux_table), ``mortgage_payment`` (multi-leg P&I), ``ss_benefit``
(COLA-adjusted, claim-age gated), ``rmd`` (annual December, age-gated).

Liquidity chains (ADR-003 §4.7) replace the old ``waterfall_fill``/
``refill_transfer`` rule kinds. An account with ``minimum_balance_cents``/
``refill_chain_id`` set is refilled proactively: every debit against it is
checked look-ahead by ``_ensure_min_balance`` (called from ``_post_transfer``)
*before* posting, and draws its refill chain first if the debit would drop it
below the minimum. An account with ``maximum_balance_cents``/
``overflow_chain_id`` set has no such per-transaction analogue (only
accumulated credits can push it over), so its excess is swept once at month
end -- see ``_settle_accounts``.
"""

from __future__ import annotations

import calendar
import functools
import json
import warnings
from collections import Counter
from dataclasses import dataclass
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from analysis.sim import registries, units
from analysis.sim.runs import finalize_run, open_run_file
from analysis.sim.schema import inserted_id

__all__ = ["run_scenario", "EngineError"]


class EngineError(RuntimeError):
    pass


SUPPORTED_RULE_KINDS = frozenset(
    {"fixed_amount", "percent_of_balance",
    "mortgage_payment", "ss_benefit", "rmd", "tax_accumulator", "on_death", "fill_bracket", "irmaa"}
)

SUPPORTED_CALENDAR_KINDS = frozenset(
    {"set_rate", "set_amount", "retire_scheduled", "swap_aux_table",
     "one_off_transfer", "transfer_balance"}
)

# Positions in the `txns` rows loaded by run_scenario, for the fields that
# calendar events mutate in place.
_SCHED_AMOUNT_CENTS = 12
_SCHED_END = 17
_SCHED_BASE_DATE = 19

SS_FRA = 67


def run_scenario(
    intent_path: Path,
    run_path: Path,
    *,
    run_by: str,
    allow_dirty_data: bool = False,
    allow_dirty_code: bool = False,
    run_args: dict[str, Any] | None = None,
    engine_options: dict[str, Any] | None = None,
) -> Path:
    con = open_run_file(
        run_path, intent_path,
        run_by=run_by,
        allow_dirty_data=allow_dirty_data, allow_dirty_code=allow_dirty_code,
        run_args=run_args, engine_options=engine_options,
    )
    try:
        scenario = con.execute(
            'SELECT start, "end" FROM scenario'
        ).fetchone()
        if scenario is None:
            raise EngineError("scenario table is empty in intent snapshot")
        start_s, end_s = scenario
        months = list(_month_ends(_parse_date(start_s), _parse_date(end_s)))
        if not months:
            raise EngineError(f"scenario range is empty: start={start_s} end={end_s}")

        # The engine's nodes are lots (ADR-005): every dict below is keyed by
        # account_lot.id, with each lot inheriting its account's kind,
        # subkind, tax treatment, owner and name.
        lots = con.execute(
            "SELECT m.id, m.opening_balance, m.opening_cost_basis, "
            "       m.prior_year_end_balance, a.kind, a.subkind, m.tax_treatment, "
            "       a.owner, m.quantity_micros, a.name, m.acquired "
            "FROM account_lot m "
            "JOIN account a ON a.id = m.account_id "
            "ORDER BY m.id"
        ).fetchall()
        balances: dict[int, int] = {r[0]: r[1] for r in lots}
        cost_bases: dict[int, int | None] = {r[0]: r[2] for r in lots}
        prior_year_end_balances: dict[int, int] = {
            r[0]: r[3] for r in lots if r[3] is not None
        }
        account_kinds: dict[int, str] = {r[0]: r[4] for r in lots}
        account_subkind: dict[int, str] = {r[0]: r[5] for r in lots}
        account_tax_treatment: dict[int, str | None] = {r[0]: r[6] for r in lots}
        quantities: dict[int, int | None] = {r[0]: r[8] for r in lots}
        # A lot declared up front for a future purchase starts with no basis.
        for r in lots:
            if (
                cost_bases[r[0]] is None and r[1] == 0 and r[4] == "asset"
                and registries.withdrawal_tax_character(r[6], r[5]) is None
            ):
                cost_bases[r[0]] = 0
        acquired: dict[int, date | None] = {
            r[0]: _parse_date(r[10]) if r[10] else None for r in lots
        }
        lot_account_name: dict[int, str] = {r[0]: r[9] for r in lots}
        lots_by_account: dict[str, list[int]] = {}
        for lot_id, owner in lot_account_name.items():
            lots_by_account.setdefault(owner, []).append(lot_id)
        growth_names = {c.name for c in registries.GROWTH_COUNTERPARTY_ACCOUNTS.values()}
        growth_sources = frozenset(r[0] for r in lots if r[9] in growth_names)
        account_id_by_name: dict[str, int] = {
            r[0]: r[1] for r in con.execute(
                "SELECT name, lot_id FROM chain WHERE lot_id IS NOT NULL"
            )
        }
        account_name: dict[int, str] = {
            lot_id: name for name, lot_id in account_id_by_name.items()
        }
        chain_id_by_name: dict[str, int] = {
            r[0]: r[1] for r in con.execute("SELECT name, id FROM chain")
        }
        person_lifespan: dict[str, str] = {
            r[0]: r[1] for r in con.execute(
                "SELECT person, final_month FROM person_lifespan"
            )
        }
        rule_lifespan = _rule_lifespans(con, person_lifespan)
        # A pseudo-person such as `joint` may have no birthdate (#217).
        person_birthdates: dict[str, date] = {
            r[0]: _parse_date(r[1])
            for r in con.execute("SELECT id, birthdate FROM person WHERE birthdate != ''")
        }
        # The people who file taxes: role 'primary', or everyone if none is marked.
        taxpayers = {
            r[0] for r in con.execute("SELECT id FROM person WHERE role = 'primary'")
        } or set(person_birthdates)
        txns: list[list[Any]] = [
            list(r) for r in con.execute(
                'SELECT id, name, from_account, to_account, cadence, month_of_year, '
                '       amount_rule_kind, interest_account, person, series, '
                '       rate_micros, claim_age, amount_cents, params_json, '
                '       tax_character, tax_year, start, "end", priority, base_date, '
                '       ytd_amount, ytd_preroll, inflation_cadence '
                'FROM scheduled_transaction '
                'ORDER BY priority, id'
            ).fetchall()
        ]
        # A fill_bracket conversion runs after everything else in its month
        # (#252): the year's RMD has to come out first (an RMD can't be
        # converted), and its target needs the year's whole income.
        txns.sort(key=lambda r: r[6] == "fill_bracket")
        txn_by_name: dict[str, list[Any]] = {
            r[1]: r for r in txns
        }
        tax_rules: list[tuple[str, dict[str, Any]]] = [
            (r[1], json.loads(r[13]) if r[13] else {}) for r in txns
            if r[6] == "tax_accumulator"
        ]

        year_to_date_rows = con.execute(
            "SELECT id, from_lot_id, to_lot_id, amount, tax_character, year "
            "FROM year_to_date"
        ).fetchall()

        # ADR-006: an unresolved row is always a mistake (a recipe run
        # directly, or a resolution step skipped) -- reject it up front,
        # rather than only when its own month is simulated, so a row dated
        # outside the scenario's range or of an unsupported kind can't slip
        # through unnoticed.
        unresolved = con.execute(
            "SELECT id, name FROM calendar_event WHERE vary_distribution IS NOT NULL"
        ).fetchall()
        if unresolved:
            names = ", ".join(repr(name or f"id={ev_id}") for ev_id, name in unresolved)
            raise EngineError(
                f"calendar_event(s) still have vary_distribution set: {names} -- "
                "an ensemble recipe must be resolved (the 'vary' step) before it can be run"
            )

        calendar_events = con.execute(
            'SELECT id, calendar_id, date, kind, '
            '       scheduled_name, account_1, account_2, series, amount, '
            '       param_date, tax_character, payload_json, vary_distribution '
            'FROM calendar_event ORDER BY date, id'
        ).fetchall()

        chain_graph = _load_chain_graph(con)
        settle_order = _settle_order(chain_graph)
        aux = _load_aux(con)
        inflation_indexes = _initialize_inflation_indexes(aux)
        inflation_factor_cache: dict[tuple[str, date, date, str], int] = {}
        refill_ctx = RefillContext(
            chain_graph, aux, _parse_date(start_s), inflation_factor_cache,
            account_tax_treatment, account_subkind, account_name, cost_bases,
            quantities, growth_sources, acquired,
            chain_name=dict(con.execute("SELECT id, name FROM chain").fetchall()),
            roth=_roth_lots(con, person_birthdates),
        )
        # A Roth lot tracks its basis (contributions and conversions), which
        # comes out tax-free whatever the clock says. A blank opening basis
        # takes the whole opening balance as basis.
        for lot_id in refill_ctx.roth:
            if cost_bases.get(lot_id) is None:
                cost_bases[lot_id] = balances.get(lot_id, 0)

        irmaa = IrmaaContext(
            aux, inflation_factor_cache, start_year=_parse_date(start_s).year,
            prior_magi=_prior_magi(con), person_lifespan=person_lifespan,
            taxpayers=taxpayers, rules=[r for r in txns if r[6] == "irmaa"],
        )

        step_ups = _basis_step_ups(con)

        skipped_counts: dict[str, int] = {}
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
            step_id = inserted_id(cur)
            if step_index == 0:
                _apply_ytd_catchups(
                    con, step_id, month, txns, year_to_date_rows, chain_graph,
                    account_id_by_name=account_id_by_name,
                    balances=balances,
                    prior_year_end_balances=prior_year_end_balances,
                )

            for ev in calendar_events:
                (ev_id, _cal_id, ev_date, ev_kind,
                 _p_sched_name, _p_acct1, _p_acct2, _p_series, _p_amount,
                 _p_param_date, _p_tax_char, ev_payload_json, ev_vary_distribution) = ev
                if ev_id in applied_event_ids:
                    continue
                if not (month_start_s <= ev_date <= month_s):
                    continue
                if ev_kind in registries.INERT_CALENDAR_KINDS:
                    # A correlation anchor (ADR-008) or a date marker (#281):
                    # read by the ensemble step and the loader, with no effect
                    # of its own -- not an unsupported kind.
                    applied_event_ids.add(ev_id)
                    continue
                if ev_kind not in SUPPORTED_CALENDAR_KINDS:
                    # The loader rejects unknown kinds, so this is an intent
                    # built some other way (#197: no kind is skipped any more).
                    raise EngineError(
                        f"calendar_event id={ev_id}: unsupported kind {ev_kind!r}"
                    )
                _apply_calendar_event(
                    con, ev_id, ev_date, ev_kind,
                    _build_cal_payload(
                        _p_sched_name, _p_acct1, _p_acct2, _p_series, _p_amount,
                        _p_param_date, _p_tax_char, ev_payload_json,
                    ),
                    txns, txn_by_name, aux, chain_id_by_name, account_kinds,
                    balances, step_id, month_s, refill_ctx=refill_ctx,
                    vary_distribution=ev_vary_distribution,
                )
                con.execute(
                    "INSERT INTO applied_calendar_event(step_id, calendar_event_id) "
                    "VALUES (?, ?)",
                    (step_id, ev_id),
                )
                applied_event_ids.add(ev_id)

            # After the month's calendar events, so a set_rate or swap of an
            # inflation series dated this month moves this month's index, as
            # it moves the rules' inflation lookups.
            _advance_inflation_indexes(
                con, step_id, month_s, aux, inflation_indexes,
            )

            opening_balances = dict(balances)

            for txn in txns:
                (tid, _name, frm, to_chain, cadence, moy, rule_kind,
                 p_interest_acct, p_person, p_series, p_rate_micros,
                 p_claim_age, p_amount_cents, ext_json,
                 tax_char, tax_year, t_start, t_end, _prio, t_base_date,
                 _t_ytd_amount, _t_ytd_preroll, t_inflation_cadence) = txn
                rule_start = _parse_scheduled_date(t_start, tid, _name, "start")
                rule_end = (
                    _parse_scheduled_date(t_end, tid, _name, "end")
                    if t_end is not None else None
                )
                from_account = chain_graph.leaf_lot.get(frm)
                to = chain_graph.leaf_lot.get(to_chain)
                if to is None and rule_kind != "fixed_amount":
                    raise EngineError(
                        f"scheduled_transaction id={tid}: interior chain destination "
                        "is only supported for fixed_amount rules"
                    )
                if month_s < t_start:
                    continue
                if t_end is not None and month_s > t_end:
                    continue
                # Only a rule's own `person` stops it at a death (#256).
                # percent_of_balance is asset growth (ADR-003 §2.2: "an asset
                # account's growth is a property of the account"), never a
                # personal service tied to the living owner, so it's exempt
                # even with a person. on_death is the
                # rule kind that FIRES on the death transition itself, so it
                # must not be suppressed by the very gating it triggers.
                # A `joint` rule stops at the household's last death.
                if (
                    rule_lifespan and rule_kind not in ("percent_of_balance", "on_death")
                    and _past_person_lifespan(month_s, p_person, rule_lifespan)
                ):
                    continue
                fire_count = _cadence_count(
                    cadence, moy, month,
                    rule_start, rule_end,
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
                        if p_amount_cents is None:
                            raise EngineError(
                                f"scheduled_transaction id={tid} fixed_amount missing amount"
                            )
                        amount = p_amount_cents
                        if p_series and p_series != "fixed":
                            if not t_base_date:
                                raise EngineError(
                                    f"scheduled_transaction id={tid}: series set "
                                    "without base_date"
                                )
                            factor = _cumulative_inflation_factor(
                                aux, p_series, _parse_date(t_base_date), month,
                                inflation_factor_cache,
                                cadence=t_inflation_cadence or "monthly",
                            )
                            amount = amount * factor // 1_000_000
                        _post_fixed_amount(
                            con, balances, account_kinds, step_id, month_s, refill_ctx,
                            frm, to_chain, amount * fire_count, tax_char, tid,
                            tax_year=tax_year, what=f"scheduled_transaction id={tid} ({_name!r})",
                        )
                    case "percent_of_balance":
                        assert to is not None  # only fixed_amount may target an interior chain
                        if from_account is None:
                            raise EngineError(
                                f"scheduled_transaction id={tid} ({_name!r}): percent_of_balance "
                                "needs an account or lot source, not an interior chain"
                            )
                        amount = _amount_percent_of_balance(
                            params, opening_balances, balances, to, aux,
                            month_s, tid,
                        )
                        if amount > 0:
                            _post_transfer(
                                con, balances, account_kinds, step_id, month_s,
                                from_account, to, amount, tax_char, tid,
                                refill_ctx=refill_ctx,
                            )
                        elif amount < 0:
                            # Negative rate = depreciation: the legs swap, so
                            # value leaves the asset for the counterparty.
                            _post_transfer(
                                con, balances, account_kinds, step_id, month_s,
                                to, from_account, -amount, tax_char, tid,
                                refill_ctx=refill_ctx,
                            )
                    case "mortgage_payment":
                        assert to is not None  # only fixed_amount may target an interior chain
                        _apply_mortgage_payment(
                            con, balances, account_kinds, step_id, month_s,
                            params, p_amount_cents, from_account, to, account_id_by_name,
                            tax_char, tid, graph=chain_graph,
                            source_chain_id=frm,
                            account_tax_treatment=account_tax_treatment,
                            account_subkind=account_subkind, account_name=account_name,
                            refill_ctx=refill_ctx,
                        )
                    case "ss_benefit":
                        _apply_ss_benefit(
                            con, balances, account_kinds, step_id, month_s, month,
                            params, from_account, to, person_birthdates, aux, tax_char, tid,
                            chain_graph=chain_graph, to_chain_id=to_chain,
                            account_tax_treatment=account_tax_treatment,
                            account_subkind=account_subkind, account_name=account_name,
                            inflation_factor_cache=inflation_factor_cache,
                            chain_base_date=_parse_date(start_s),
                            source_tax_year=tax_year, refill_ctx=refill_ctx,
                        )
                    case "rmd":
                        subject = params.get("subject_account") or lot_account_name.get(from_account)
                        if subject not in lots_by_account:
                            raise EngineError(
                                f"rmd id={tid}: subject_account {subject!r} is not an account "
                                "(a chain source needs it in params_json)"
                            )
                        assert to is not None  # only fixed_amount may target an interior chain
                        _apply_rmd(
                            con, balances, account_kinds, step_id, month_s, month,
                            params, from_account, to,
                            person_birthdates, prior_year_end_balances, aux,
                            tax_char, tid,
                            # A Roth lot of a retirement account owes no RMD
                            # and counts toward none (#217).
                            subject_lots=[
                                lot for lot in lots_by_account[subject]
                                if account_tax_treatment.get(lot) != "tax_free"
                            ],
                            graph=chain_graph, source_chain_id=frm,
                            account_tax_treatment=account_tax_treatment,
                            account_subkind=account_subkind, account_name=account_name,
                            chain_context={
                                "aux": aux, "chain_base_date": _parse_date(start_s),
                                "inflation_factor_cache": inflation_factor_cache,
                            },
                            refill_ctx=refill_ctx,
                        )
                    case "irmaa":
                        charge = irmaa.charge(con, month.year, params, tid)
                        if charge > 0:
                            _post_fixed_amount(
                                con, balances, account_kinds, step_id, month_s, refill_ctx,
                                frm, to_chain, charge * fire_count, tax_char, tid,
                                tax_year=tax_year,
                                what=f"scheduled_transaction id={tid} ({_name!r})",
                            )
                    case "on_death":
                        _apply_on_death(
                            con, balances, account_kinds, step_id, month_s,
                            params, from_account, to, rule_lifespan,
                            tax_char, tid, refill_ctx=refill_ctx,
                        )
                    case "tax_accumulator":
                        if cadence != "annual":
                            raise EngineError(
                                f"tax_accumulator id={tid}: cadence must be 'annual'"
                            )
                        assert to is not None  # only fixed_amount may target an interior chain
                        _apply_tax_accumulator(
                            con, balances, account_kinds, step_id, month_s, month,
                            params, from_id=from_account, to_id=to, aux=aux,
                            tax_char=tax_char, tid=tid, graph=chain_graph,
                            source_chain_id=frm,
                            account_tax_treatment=account_tax_treatment,
                            account_subkind=account_subkind, account_name=account_name,
                            tax_year=tax_year,
                            person_lifespan=person_lifespan, taxpayers=taxpayers,
                            refill_ctx=refill_ctx,
                        )
                    case "fill_bracket":
                        if from_account is None or to is None:
                            raise EngineError(
                                f"fill_bracket id={tid} ({_name!r}): converts from one lot "
                                "to another, not through a chain"
                            )
                        _apply_fill_bracket(
                            con, balances, account_kinds, step_id, month_s, month,
                            params, from_account, to, tax_char, tid, _name,
                            aux=aux, tax_rules=tax_rules,
                            inflation=(
                                (p_series, _parse_date(t_base_date), t_inflation_cadence or "monthly")
                                if p_series and p_series != "fixed" and t_base_date else None
                            ),
                            inflation_factor_cache=inflation_factor_cache,
                            person_lifespan=person_lifespan, taxpayers=taxpayers,
                            refill_ctx=refill_ctx, irmaa=irmaa,
                        )
                    case _:  # pragma: no cover
                        raise EngineError(f"supported but unhandled rule_kind: {rule_kind}")

            _settle_accounts(
                con, balances, account_kinds, account_subkind, account_tax_treatment,
                account_name, step_id, month_s, chain_graph, settle_order,
                aux=aux, chain_base_date=_parse_date(start_s),
                inflation_factor_cache=inflation_factor_cache, refill_ctx=refill_ctx,
            )

            if month_s in step_ups:
                _step_up_basis(
                    con, step_id, month_s, step_ups[month_s], balances, cost_bases, acquired,
                )

            con.executemany(
                "INSERT INTO lot_state(step_id, lot_id, balance, cost_basis, "
                "                      quantity_micros) VALUES (?, ?, ?, ?, ?)",
                [(step_id, lot_id, balances[lot_id], cost_bases[lot_id],
                  quantities[lot_id])
                 for lot_id in balances],
            )
            previous_step_id = step_id

        # Tax tail: the scenario's declared end year deserves a correct
        # reconciliation too, but that only fires the following April. Run
        # tax_accumulator rules one extra year past scenario.end purely to
        # compute tax_year_summary for the final year -- no simulation_step,
        # account_state, or transfer_event rows are written, so "simulation
        # data" queries stay bounded to <= scenario.end as usual.
        end_date = _parse_date(end_s)
        tail_last_step_id = previous_step_id
        for tail_month in _month_ends(
            date(end_date.year + 1, 1, 1), date(end_date.year + 1, 12, 31),
        ):
            tail_month_s = tail_month.isoformat()
            for txn in txns:
                (tid, _name, frm, to_chain, cadence, moy, rule_kind,
                 _p_interest_acct, _p_person, _p_series, _p_rate_micros,
                 _p_claim_age, _p_amount_cents, ext_json,
                 tax_char, _tax_year, t_start, t_end, _prio, _t_base_date,
                 _t_ytd_amount, _t_ytd_preroll, _t_inflation_cadence) = txn
                rule_start = _parse_scheduled_date(t_start, tid, _name, "start")
                rule_end = (
                    _parse_scheduled_date(t_end, tid, _name, "end")
                    if t_end is not None else None
                )
                if rule_kind != "tax_accumulator":
                    continue
                tail_to = chain_graph.leaf_lot.get(to_chain)
                if tail_to is None:
                    raise EngineError(
                        f"tax_accumulator id={tid}: tax settlement destination must "
                        "resolve to a concrete account"
                    )
                if tail_month_s < t_start:
                    continue
                if t_end is not None and tail_month_s > t_end:
                    continue
                fire_count = _cadence_count(
                    cadence, moy, tail_month,
                    rule_start, rule_end,
                )
                if fire_count == 0:
                    continue
                params = json.loads(ext_json) if ext_json else {}
                _apply_tax_accumulator(
                    con, balances, account_kinds, tail_last_step_id,
                    tail_month_s, tail_month,
                    params, from_id=None, to_id=tail_to, aux=aux,
                    tax_char=tax_char, tid=tid, post_transfer=False,
                    tax_year=_tax_year,
                    person_lifespan=person_lifespan, taxpayers=taxpayers,
                )

        if skipped_counts:
            con.execute(
                "UPDATE simulation_run SET run_args_json = json_set("
                "  run_args_json, '$.skipped_rule_kinds', json(?)) WHERE id = 1",
                (json.dumps(skipped_counts, sort_keys=True),),
            )
        if refill_ctx.negative_events:
            con.execute(
                "UPDATE simulation_run SET run_args_json = json_set("
                "  run_args_json, '$.negative_balance_warnings', json(?)) WHERE id = 1",
                (json.dumps(refill_ctx.negative_events),),
            )

        finalize_run(con, status="succeeded")
    except Exception as e:
        finalize_run(con, status="failed", failure_reason=f"{type(e).__name__}: {e}")
        raise
    finally:
        con.close()
    return Path(run_path)


class RothLot(NamedTuple):
    """A Roth lot's tax rules (#217). Its earnings come out tax-free only once
    the distribution is *qualified*: the owner is 59½ and five tax years have
    passed since ``clock_start``. Until then its basis still comes out
    tax-free and its earnings are ordinary income (the 10% additional tax
    under 59½ is not modeled)."""

    basis_first: bool
    """A Roth IRA pays out basis before earnings (IRC §408A(d)(4)); a Roth
    401k's nonqualified distribution is pro-rata."""
    clock_start: date | None
    """Jan 1 of the five-year clock's first year; None means it has run."""
    birthdate: date | None
    """Whose 59½ the distribution waits for; None means no age test, as
    after the owner's death (#256)."""
    then: tuple[date, RothLot] | None = None
    """From this date on, the lot follows these rules instead: a death
    changes the clock or the age test (#256)."""


def _earlier_clock(*clocks: date | None) -> date | None:
    """The earliest of several five-year clocks, where None (a clock that
    has run) is earliest of all."""
    return None if None in clocks else min(c for c in clocks if c is not None)


def _roth_lots(con, birthdates: Mapping[str, date]) -> dict[int, RothLot]:
    """Every Roth lot, by lot id, with its owner's five-year clock (#217).

    A Roth IRA's clock is the owner's, across all their Roth IRA lots; a Roth
    401k's is the plan's, across that account's Roth lots. It starts on Jan 1
    of the year of the earliest of those lots' ``acquired`` dates, or of the
    account's ``opened`` when a lot has none. With neither, it has run.

    A death changes the rules from the month after the final month (#256).
    The two ``primary`` persons are spouses. Under the default
    ``roth_survivor`` election, ``own``, a surviving spouse treats the
    decedent's Roth IRAs as their own (Treas. Reg. §1.408A-6 Q&A-7(b)): those
    lots and the survivor's own Roth IRA lots take the earlier of the two
    clocks and the survivor's 59½. Otherwise, as for a Roth 401k or a
    decedent with no surviving spouse, the decedent's lots keep the
    decedent's clock with no age test, since a distribution after death
    needs only the five years."""
    rows = con.execute(
        "SELECT m.id, m.subkind, m.acquired, a.opened, a.owner, a.id FROM account_lot m "
        "JOIN account a ON a.id = m.account_id WHERE m.subkind IN ('roth_ira', 'roth_401k')"
    ).fetchall()

    def clock_key(subkind: str, owner: str | None, account_id: int) -> tuple[str, object]:
        return ("ira", owner or account_id) if subkind == "roth_ira" else ("401k", account_id)

    years: dict[tuple[str, object], list[int]] = {}
    undated: set[tuple[str, object]] = set()  # a lot with no date: the clock has run
    for _lot, subkind, acquired, opened, owner, account_id in rows:
        key = clock_key(subkind, owner, account_id)
        dated = acquired or opened
        if dated:
            years.setdefault(key, []).append(int(dated[:4]))
        else:
            undated.add(key)
    clocks: dict[tuple[str, object], date | None] = {
        key: None if key in undated else date(min(years[key]), 1, 1)
        for key in years.keys() | undated
    }

    # The first day each person is dead: the month after their final month.
    deaths = {
        person: _parse_date(final_month) + timedelta(days=1)
        for person, final_month in con.execute("SELECT person, final_month FROM person_lifespan")
    }
    primaries = [r[0] for r in con.execute("SELECT id FROM person WHERE role = 'primary' ORDER BY id")]
    spouses = dict([primaries, primaries[::-1]]) if len(primaries) == 2 else {}
    meta = json.loads(con.execute("SELECT meta_json FROM scenario").fetchone()[0] or "{}")
    treat_as_own = meta.get("roth_survivor", "own") == "own"

    def survives(person: str | None, dies: date) -> bool:
        return person is not None and (person not in deaths or deaths[person] > dies)

    def phases(subkind: str, owner: str | None, clock: date | None) -> list[tuple[date, date | None, date | None]]:
        """The lot's rules after each death: (from, clock_start, birthdate)."""
        born = birthdates.get(owner) if owner else None
        if owner is None or owner not in deaths and spouses.get(owner) not in deaths:
            return []
        spouse = spouses.get(owner) if subkind == "roth_ira" and treat_as_own else None
        spouse_clock = clocks.get(("ira", spouse), clock) if spouse else clock
        steps = []
        if spouse in deaths and survives(owner, deaths[spouse]):
            # The owner survives and treats the spouse's Roth IRAs as their own.
            clock = _earlier_clock(clock, spouse_clock)
            steps.append((deaths[spouse], clock, born))
        if owner in deaths:
            dies = deaths[owner]
            if survives(spouse, dies):
                clock = _earlier_clock(clock, spouse_clock)
                steps.append((dies, clock, birthdates.get(spouse)))
                if spouse in deaths:
                    steps.append((deaths[spouse], clock, None))
            else:
                steps.append((dies, clock, None))
        return steps

    lots: dict[int, RothLot] = {}
    for lot, subkind, _acquired, _opened, owner, account_id in rows:
        clock = clocks[clock_key(subkind, owner, account_id)]
        then: tuple[date, RothLot] | None = None
        for start, step_clock, step_born in reversed(phases(subkind, owner, clock)):
            then = (start, RothLot(subkind == "roth_ira", step_clock, step_born, then))
        lots[lot] = RothLot(
            basis_first=subkind == "roth_ira",
            clock_start=clock,
            birthdate=birthdates.get(owner) if owner else None,
            then=then,
        )
    return lots


class StepUp(NamedTuple):
    """One lot's basis step-up at a death (#255): the lot, whose death it
    is, and the share of its gain or loss that resets: all of it (1) or half (2)."""
    lot_id: int
    person: str
    share: int


def _household(con) -> list[str]:
    """The household: the ``primary`` persons, or everyone with a role if
    none is marked."""
    primaries = [r[0] for r in con.execute("SELECT id FROM person WHERE role = 'primary' ORDER BY id")]
    return primaries or [
        r[0] for r in con.execute("SELECT id FROM person WHERE role IS NOT NULL ORDER BY id")]


def _rule_lifespans(con, person_lifespan: Mapping[str, str]) -> dict[str, str]:
    """The final month each rule ``person`` stops at: a person's own, and for
    ``joint`` the household's last death, so shared costs end with the second
    death. ``joint`` gets one only when every household member has a lifespan;
    while anyone may live on, a joint rule runs to the end."""
    lifespans = dict(person_lifespan)
    household = _household(con)
    if "joint" not in lifespans and household and all(p in lifespans for p in household):
        lifespans["joint"] = max(lifespans[p] for p in household)
    return lifespans


def _basis_step_ups(con) -> dict[str, list[StepUp]]:
    """The basis step-ups each final month brings (IRC §1014), by month.

    Only a non-cash asset lot that tracks a basis steps up (an investment, a
    home): retirement money and an HSA never do. A lot its account's ``owner`` held alone steps up in full at their
    death, and again at the death of the spouse who inherited it. A joint
    lot (owner blank or ``joint``) is the household's: the ``primary``
    persons, or everyone with a role if none is marked. At one of
    their deaths it steps up in full if no one else in the household is
    left alive, or under ``community_property`` (§1014(b)(6): both halves of
    community property step up). Otherwise only the decedent's half does
    (§2040(b))."""
    deaths = dict(con.execute("SELECT person, final_month FROM person_lifespan").fetchall())
    if not deaths:
        return {}
    primaries = [r[0] for r in con.execute("SELECT id FROM person WHERE role = 'primary' ORDER BY id")]
    household = _household(con)
    spouses = dict([primaries, primaries[::-1]]) if len(primaries) == 2 else {}
    meta = json.loads(con.execute("SELECT meta_json FROM scenario").fetchone()[0] or "{}")
    community = bool(meta.get("community_property", False))
    no_step_up = sorted(registries.CASH_SUBKINDS | {registries.RETIREMENT_SUBKIND, "hsa"})
    lots = con.execute(
        "SELECT m.id, a.owner FROM account_lot m JOIN account a ON a.id = m.account_id "
        "WHERE a.kind = 'asset' AND COALESCE(m.tax_treatment, 'none') IN ('taxable', 'none') "
        f"AND a.subkind NOT IN ({','.join('?' * len(no_step_up))}) ORDER BY m.id",
        no_step_up,
    ).fetchall()

    def alive_after(person: str, month: str) -> bool:
        return person not in deaths or deaths[person] > month

    plan: dict[str, list[StepUp]] = {}
    for person, month in sorted(deaths.items(), key=lambda d: (d[1], d[0])):
        stepped = {s.lot_id for s in plan.get(month, ())}
        for lot_id, owner in lots:
            if lot_id in stepped:
                continue  # two deaths in one month: the first stepped it up in full
            if owner in (None, "", "joint"):
                if person not in household:
                    continue
                survivor = any(p != person and alive_after(p, month) for p in household)
                # A joint tenancy is half each (§2040(b)); other shares are out of scope.
                share = 2 if survivor and not community else 1
            elif owner == person or (
                spouses.get(owner) == person and deaths.get(owner, month) < month
            ):
                share = 1
            else:
                continue
            plan.setdefault(month, []).append(StepUp(lot_id, person, share))
    return plan


def _step_up_basis(
    con, step_id: int, month_s: str, step_ups: Sequence[StepUp],
    balances: Mapping[int, int], cost_bases: dict[int, int | None],
    acquired: dict[int, date | None],
) -> None:
    """Reset each lot's basis at a death to its value at the end of the final
    month, the whole of it or ``1/share`` of the gain or loss, down as well as
    up (IRC §1014). Inherited property is long-term from then on (§1223(9)).
    Each step-up is recorded in ``basis_step_up``."""
    rows = []
    for lot_id, person, share in step_ups:
        before = cost_bases.get(lot_id)
        if before is None:
            continue  # not tracking a basis
        value = max(0, balances.get(lot_id, 0))
        after = before + (value - before) // share
        cost_bases[lot_id] = after
        acquired[lot_id] = None
        rows.append((step_id, month_s, lot_id, person, value, before, after))
    con.executemany(
        "INSERT INTO basis_step_up(step_id, date, lot_id, person, balance, "
        "                          basis_before, basis_after) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _roth_qualified(roth: RothLot, on: date) -> bool:
    """Whether a distribution on ``on`` is qualified: five tax years since the
    clock started, and the owner 59½. The loader requires a Roth owner's
    birthdate; with none (a hand-built ``RothLot``, or a lot whose owner has
    died) the age test passes."""
    while roth.then is not None and on >= roth.then[0]:
        roth = roth.then[1]
    if roth.clock_start is not None and on < roth.clock_start.replace(year=roth.clock_start.year + 5):
        return False
    if roth.birthdate is None:
        return True
    months = (on.year - roth.birthdate.year) * 12 + on.month - roth.birthdate.month
    # Born on the 31st, 59½ falls on the last day of a shorter month.
    day = min(roth.birthdate.day, calendar.monthrange(on.year, on.month)[1])
    return months > 59 * 12 + 6 or (months == 59 * 12 + 6 and on.day >= day)


def _roth_basis_out(ctx: RefillContext, balances: Mapping[int, int], lot: int, amount: int) -> int:
    """The basis in ``amount`` drawn from Roth ``lot``: basis first for a Roth
    IRA, pro-rata for a Roth 401k, qualified or not. Never more than the
    amount, so a lot worth less than its basis pays out basis only."""
    basis = ctx.cost_bases.get(lot) or 0
    if ctx.roth[lot].basis_first:
        return min(basis, amount)
    balance = balances.get(lot, 0)
    return min(amount, basis) if balance <= 0 else min(amount, basis, basis * amount // balance)


def _roth_taxed(ctx: RefillContext, balances: Mapping[int, int], lot: int, amount: int, on: date) -> int:
    """How much of ``amount`` drawn from Roth ``lot`` on ``on`` is taxed."""
    if _roth_qualified(ctx.roth[lot], on):
        return 0
    return amount - _roth_basis_out(ctx, balances, lot, amount)


def _leaves_roth(ctx: RefillContext | None, from_id: int, to_id: int) -> bool:
    """Money out of a Roth lot that leaves the retirement wrapper (a
    distribution, not a rollover into another retirement lot)."""
    return (
        ctx is not None and from_id in ctx.roth
        and ctx.account_subkind.get(to_id) != registries.RETIREMENT_SUBKIND
    )


class RefillAttempt(NamedTuple):
    """What the refill look-ahead did for an account's latest debit (#105),
    kept so a negative-balance warning can say which chain it tried and how
    much that chain supplied. ``status`` is one of ``REFILL_STATUSES``;
    ``needed``/``drawn`` are set only for ``"drawn"``."""

    status: str
    chain_id: int | None = None
    needed: int | None = None
    drawn: int | None = None


# "drawn": the chain was drawn (possibly for less than needed);
# "none_configured": the account has no refill_chain;
# "cycle": skipped because the account was already being refilled further up;
# "not_triggered": the debit left it at or above minimum_balance (only
# possible below zero when that minimum is itself negative).
REFILL_STATUSES = ("drawn", "none_configured", "cycle", "not_triggered")


class RefillContext:
    """Everything ``_post_transfer`` needs to look-ahead refill a debited
    account and to warn once when an asset actually goes negative (ADR-003
    §2.9). Built once per run in ``run_scenario`` and threaded through.
    """

    __slots__ = ("graph", "aux", "chain_base_date", "inflation_factor_cache",
                 "account_tax_treatment", "account_subkind", "account_name",
                 "cost_bases", "quantities", "growth_sources", "acquired",
                 "chain_name", "refilling", "last_refill", "negative_since",
                 "negative_events", "roth")

    def __init__(
        self, graph: ChainGraph, aux, chain_base_date: date,
        inflation_factor_cache: dict,
        account_tax_treatment: Mapping[int, str | None],
        account_subkind: dict[int, str], account_name: dict[int, str],
        cost_bases: dict[int, int | None],
        quantities: dict[int, int | None] | None = None,
        growth_sources: frozenset[int] = frozenset(),
        acquired: dict[int, date | None] | None = None,
        chain_name: dict[int, str] | None = None,
        roth: dict[int, RothLot] | None = None,
    ) -> None:
        self.graph = graph
        self.aux = aux
        self.chain_base_date = chain_base_date
        self.inflation_factor_cache = inflation_factor_cache
        self.account_tax_treatment = account_tax_treatment
        self.account_subkind = account_subkind
        self.account_name = account_name
        self.cost_bases = cost_bases
        self.quantities = quantities if quantities is not None else {}
        self.growth_sources = growth_sources
        self.acquired = acquired if acquired is not None else {}
        self.chain_name = chain_name if chain_name is not None else {}
        self.roth = roth if roth is not None else {}
        self.refilling: set[int] = set()  # reentrancy guard against refill cycles
        self.last_refill: dict[int, RefillAttempt] = {}
        self.negative_since: dict[int, bool] = {}
        self.negative_events: list[dict[str, Any]] = []


def _ensure_min_balance(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str, refill_ctx: RefillContext,
    account_id: int, debit_amount: int,
) -> int | None:
    """Look-ahead refill (ADR-003 §4.7): if debiting ``account_id`` by
    ``debit_amount`` would drop it below its minimum_balance, draw from its
    refill_chain BEFORE the debit posts, enough to reach refill_target (a
    fixed level), refill_amount (a fixed size, or more if needed to reach
    minimum_balance), or minimum_balance itself if neither is set.

    Not an error if the chain can't fully cover it -- the debit proceeds
    regardless and may leave the account negative (see ``_warn_if_negative``).

    Returns the highest transfer_event id from before the draw when a refill
    was attempted, else None. The refill legs post *before* the debit that
    triggered them, so the debit's id doesn't exist yet; the caller passes
    this mark to ``_link_refills`` once the debit is in.
    """
    graph = refill_ctx.graph
    refill_chain = graph.refill_chain_of.get(account_id)
    if refill_chain is None:
        refill_ctx.last_refill[account_id] = RefillAttempt("none_configured")
        return None
    if account_id in refill_ctx.refilling:
        refill_ctx.last_refill[account_id] = RefillAttempt("cycle", refill_chain)
        return None
    minimum = graph.minimum_of.get(account_id, 0)
    projected = balances.get(account_id, 0) - debit_amount
    if projected >= minimum:
        refill_ctx.last_refill[account_id] = RefillAttempt("not_triggered", refill_chain)
        return None
    mark = con.execute("SELECT COALESCE(MAX(id), 0) FROM transfer_event").fetchone()[0]
    deficit = minimum - projected
    target = graph.refill_target_of.get(account_id)
    amount_policy = graph.refill_amount_of.get(account_id)
    if target is not None:
        needed = max(deficit, target - projected)
    elif amount_policy is not None:
        needed = max(deficit, amount_policy)
    else:
        needed = deficit
    refill_ctx.refilling.add(account_id)
    try:
        drawn = _chain_draw(
            con, balances, account_kinds,
            refill_ctx.account_tax_treatment, refill_ctx.account_subkind,
            refill_ctx.account_name, step_id, month_s, graph,
            refill_chain, needed, account_id,
            refill_ctx=refill_ctx,
        )
    finally:
        refill_ctx.refilling.discard(account_id)
    refill_ctx.last_refill[account_id] = RefillAttempt("drawn", refill_chain, needed, drawn)
    return mark


def _link_refills(con, mark: int | None, debit_event_id: int | None) -> None:
    """Point the refill legs posted since ``mark`` at the debit that set them
    off (#104), via ``parent_event_id``. Only top-level legs are claimed: a
    nested refill (a refill source that needed its own refill) was already
    linked to the refill leg that triggered it, and a basis/gain split's
    gain leg already points at its basis leg."""
    if mark is None or debit_event_id is None:
        return
    con.execute(
        "UPDATE transfer_event SET parent_event_id = ? "
        "WHERE id > ? AND id < ? AND reason_kind = 'refill' AND parent_event_id IS NULL",
        (debit_event_id, mark, debit_event_id),
    )


def _warn_if_negative(
    refill_ctx: RefillContext, account_kinds: dict[int, str],
    account_id: int, balances: dict[int, int], month_s: str,
) -> None:
    """Warn once per negative transition (ADR-003 §4.7) -- an asset should
    only go negative once its refill chain (if any) is fully exhausted; not
    an error, but worth a one-time flag rather than silence or per-month spam.

    The warning and its ``negative_events`` record say what the refill
    look-ahead did for this debit (#105): which chain it tried and how much
    of what it needed that chain supplied. A misdirected refill_chain shows
    up as one that supplied $0.00, rather than as a bare shortfall.
    """
    if account_kinds.get(account_id) != "asset":
        return
    is_negative = balances.get(account_id, 0) < 0
    was_negative = refill_ctx.negative_since.get(account_id, False)
    if is_negative and not was_negative:
        name = refill_ctx.account_name.get(account_id, str(account_id))
        attempt = refill_ctx.last_refill.get(account_id)
        if attempt is None:
            # Every tracked debit runs the look-ahead first, so this is only
            # a caller that posted around it; describe the configuration.
            chain = refill_ctx.graph.refill_chain_of.get(account_id)
            attempt = RefillAttempt("none_configured" if chain is None else "not_triggered", chain)
        chain_name = (
            None if attempt.chain_id is None
            else refill_ctx.chain_name.get(attempt.chain_id, f"chain {attempt.chain_id}")
        )
        warnings.warn(
            f"{month_s}: account {name!r} went negative "
            f"(balance={balances[account_id] / 100:.2f}); "
            f"{_describe_refill(attempt, chain_name)}",
            RuntimeWarning,
            stacklevel=2,
        )
        refill_ctx.negative_events.append({
            "month": month_s, "account": name, "balance_cents": balances[account_id],
            "refill_status": attempt.status, "refill_chain": chain_name,
            "refill_needed_cents": attempt.needed, "refill_drawn_cents": attempt.drawn,
        })
    refill_ctx.negative_since[account_id] = is_negative


def describe_negative_balance_event(event: Mapping[str, Any]) -> str:
    """One recorded ``negative_balance_warnings`` entry's account of the
    refill look-ahead, worded as the run's ``RuntimeWarning`` was, so
    ``sim explain`` reads the same as the run did (#186)."""
    attempt = RefillAttempt(
        event.get("refill_status") or "none_configured", None,
        event.get("refill_needed_cents"), event.get("refill_drawn_cents"),
    )
    return _describe_refill(attempt, event.get("refill_chain"))


def _describe_refill(attempt: RefillAttempt, chain_name: str | None) -> str:
    """The negative-balance warning's account of the refill look-ahead (#105)."""
    match attempt.status:
        case "drawn":
            return (f"refill chain {chain_name!r} supplied "
                    f"${(attempt.drawn or 0) / 100:.2f} of ${(attempt.needed or 0) / 100:.2f}")
        case "cycle":
            return (f"refill chain {chain_name!r} was skipped: the account was "
                    "already being refilled (a refill cycle)")
        case "not_triggered":
            return (f"refill chain {chain_name!r} was not drawn: the debit left the "
                    "account at or above its minimum_balance")
        case _:
            return "no refill chain configured"


def _post_transfer(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str,
    from_id: int, to_id: int, amount: int, tax_char: str, reason_id: int,
    parent_event_id: int | None = None, *, reason_kind: str = "rule",
    tax_year: int | None = None, refill_ctx: RefillContext | None = None,
    requested: int | None = None, remainder: int | None = None,
    refill_mark: int | None = None, net_effects: bool = True,
) -> int | None:
    """Insert one transfer_event and adjust balances. Liability balances
    store debt magnitude; the sign is inverted when posting against them.
    Returns the new event's rowid, or None when amount==0 (no insert).

    ``requested`` is the ORIGINAL triggering rule's/event's total ask, the
    same value on every leg of one draw -- not that leg's own recursively-
    allocated slice -- so the legs can be scanned to see how one request
    broke down across accounts. ``remainder`` is how much of that request is
    still outstanding after this leg. Both default to a single-leg,
    fully-satisfied transfer (``requested = amount`` before the liability
    cap below, ``remainder = requested - amount`` after it); chain draws/
    sweeps pass both explicitly, tracked across their whole call tree.

    ``refill_mark`` is a caller's own ``_ensure_min_balance`` result, for a
    refill drawn on ``from_id`` before this call: those legs are linked to
    this event, and the look-ahead isn't run again. A second look-ahead
    could draw nothing (the caller sized the debit to what the refilled
    account can spare), but it would replace the caller's ``last_refill``
    record, so a warning would describe the empty re-check, not the draw.

    ``net_effects=False`` posts the leg alone, skipping the liability cap, the
    refill look-ahead, the lot-tracking updates and the negative-balance
    warning: a caller that splits one movement into several legs
    (``_post_withdrawal``) applies those once, to the net amount, since one
    leg on its own can overshoot it.
    """
    if amount == 0:
        return None
    if requested is None:
        requested = amount
    if net_effects and account_kinds[to_id] == "liability":
        amount = min(amount, max(0, balances.get(to_id, 0)))
        if amount == 0:
            return None
    if remainder is None:
        remainder = requested - amount
    track = refill_ctx if net_effects else None
    if track is not None and amount > 0 and refill_mark is None:
        refill_mark = _ensure_min_balance(
            con, balances, account_kinds, step_id, month_s, track,
            from_id, amount,
        )
    cur = con.execute(
        "INSERT INTO transfer_event("
        "  step_id, date, from_account, to_account, from_lot_id, to_lot_id, "
        "  amount, requested_cents, remainder_cents, tax_character, tax_year, "
        "  reason_kind, reason_id, parent_event_id"
        ") VALUES (?, ?, "
        "  (SELECT account_id FROM account_lot WHERE id = ?), "
        "  (SELECT account_id FROM account_lot WHERE id = ?), "
        "  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (step_id, month_s, from_id, to_id, from_id, to_id, amount, requested,
         remainder, tax_char, tax_year, reason_kind, reason_id, parent_event_id),
    )
    if track is not None:
        _track_lots(track, account_kinds, balances, from_id, to_id, amount, _parse_date(month_s))
    balances[from_id] = balances.get(from_id, 0) + _delta(account_kinds[from_id], -amount)
    balances[to_id] = balances.get(to_id, 0) + _delta(account_kinds[to_id], +amount)
    if track is not None:
        _warn_if_negative(track, account_kinds, from_id, balances, month_s)
    event_id = inserted_id(cur)
    _link_refills(con, refill_mark, event_id)
    return event_id


def _track_lots(
    ctx: RefillContext, account_kinds: dict[int, str], balances: dict[int, int],
    from_id: int, to_id: int, amount: int, on: date,
) -> None:
    """Shares leave ``from_id`` in proportion to the value withdrawn, and
    ``to_id`` records a purchase. Call before ``balances`` moves.

    Money out of a Roth lot takes its basis with it (``_roth_basis_out``).
    Rolled into another Roth lot, it carries that basis, or all of it as
    basis when the draw would have been qualified, as a qualified Roth 401k
    rollover into a Roth IRA does."""
    if amount <= 0:
        return
    carried = amount
    if from_id in ctx.roth:
        basis_out = _roth_basis_out(ctx, balances, from_id, amount)
        ctx.cost_bases[from_id] = (ctx.cost_bases.get(from_id) or 0) - basis_out
        if to_id in ctx.roth and not _roth_qualified(ctx.roth[from_id], on):
            carried = basis_out
    if account_kinds[from_id] == "asset":
        quantity = ctx.quantities.get(from_id)
        prior_balance = balances.get(from_id, 0)
        if quantity and prior_balance > 0:
            ctx.quantities[from_id] = quantity - min(
                quantity, quantity * amount // prior_balance
            )
    if account_kinds[to_id] == "asset":
        _record_purchase(ctx, balances, from_id, to_id, amount)
        if carried < amount and (to_basis := ctx.cost_bases.get(to_id)) is not None:
            ctx.cost_bases[to_id] = to_basis - (amount - carried)


def _post_fixed_amount(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str, ctx: RefillContext,
    from_chain: int, to_chain: int, amount: int, tax_char: str | None, tid: int,
    *, tax_year: int | None = None, what: str,
) -> None:
    """Move a fixed ``amount`` between two chains (ADR-003 §4.7): lot to lot
    directly, out of a lot through an interior chain's sweep, or into a lot
    through an interior chain's draw. Shared by fixed_amount rules and
    one_off_transfer events, so the two route a chain alike. A ``tax_char``
    of None lets the source lot decide, splitting a basis-tracked lot's
    withdrawal by its cost basis (``_post_withdrawal``)."""
    graph = ctx.graph
    from_lot, to_lot = graph.leaf_lot.get(from_chain), graph.leaf_lot.get(to_chain)
    if from_lot is not None and to_lot is not None:
        if tax_char is None:
            _post_withdrawal(
                con, balances, account_kinds, ctx.account_tax_treatment,
                ctx.account_subkind, ctx.account_name, ctx.cost_bases,
                step_id, month_s, from_lot, to_lot, amount, tid,
                tax_year=tax_year, refill_ctx=ctx,
            )
        else:
            _post_transfer(
                con, balances, account_kinds, step_id, month_s,
                from_lot, to_lot, amount, tax_char, tid,
                tax_year=tax_year, refill_ctx=ctx,
            )
    elif from_lot is not None:
        _chain_sweep(
            con, balances, account_kinds, ctx.account_tax_treatment,
            ctx.account_subkind, ctx.account_name, step_id, month_s,
            graph, to_chain, amount, from_lot,
            aux=ctx.aux, chain_base_date=ctx.chain_base_date,
            inflation_factor_cache=ctx.inflation_factor_cache,
            source_tax_character=tax_char, source_tax_year=tax_year,
            refill_ctx=ctx, reason_kind="rule", reason_id=tid,
        )
    elif to_lot is not None:
        _chain_draw(
            con, balances, account_kinds, ctx.account_tax_treatment,
            ctx.account_subkind, ctx.account_name, step_id, month_s,
            graph, from_chain, amount, to_lot,
            reason_kind="rule", reason_id=tid,
            tax_year=tax_year, refill_ctx=ctx,
        )
    else:
        raise EngineError(
            f"{what}: interior source and destination chains are not supported together"
        )


def _record_purchase(
    ctx: RefillContext, balances: dict[int, int], from_id: int, to_id: int, amount: int,
) -> None:
    """Money moved into a lot that tracks cost basis buys into it: a purchase, an
    inheritance, proceeds from another taxable lot. Its basis rises by the amount
    and, if shares are tracked, they rise at the lot's implied price. Growth
    from the built-in counterparties is appreciation and changes neither.
    """
    basis = ctx.cost_bases.get(to_id)
    if basis is None or from_id in ctx.growth_sources:
        return
    ctx.cost_bases[to_id] = basis + amount
    quantity, prior = ctx.quantities.get(to_id), balances.get(to_id, 0)
    if quantity and prior > 0:
        ctx.quantities[to_id] = quantity + quantity * amount // prior


def _is_long_term(acquired: date | None, sale: date) -> bool:
    """Held more than a year (IRC §1222): the sale falls after the first
    anniversary of the acquisition. A lot with no acquisition date is long-term."""
    if acquired is None:
        return True
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:  # Feb 29
        anniversary = date(acquired.year + 1, 2, 28)
    return sale > anniversary


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
    parent_event_id: int | None = None,
) -> int | None:
    """ADR-003 §4.10: balance-neutral catch-up entry. Visible to income/expense/
    tax reporting like any other transfer_event, but does not mutate account
    balances -- those already reflect the post-receipt state via opening_balance.
    ``reason_kind`` distinguishes the two YTD sources ('ytd_rule' vs 'ytd')
    since their ``reason_id`` values come from separate id spaces
    (scheduled_transaction.id vs year_to_date.id) that can otherwise collide.
    """
    if amount == 0:
        return None
    cur = con.execute(
        "INSERT INTO transfer_event("
        "  step_id, date, from_account, to_account, from_lot_id, to_lot_id, "
        "  amount, requested_cents, remainder_cents, tax_character, reason_kind, "
        "  reason_id, parent_event_id"
        ") VALUES (?, ?, "
        "  (SELECT account_id FROM account_lot WHERE id = ?), "
        "  (SELECT account_id FROM account_lot WHERE id = ?), "
        "  ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (step_id, date_s, from_id, to_id, from_id, to_id, amount, amount, 0,
         tax_char, reason_kind, reason_id, parent_event_id),
    )
    return inserted_id(cur)


def _first_chain_leaf_lot(graph: ChainGraph, chain_id: int) -> int | None:
    account_id = graph.leaf_lot.get(chain_id)
    if account_id is not None:
        return account_id
    for next_chain_id, *_allocation in graph.members.get(chain_id, []):
        account_id = _first_chain_leaf_lot(graph, next_chain_id)
        if account_id is not None:
            return account_id
    return None


def _mortgage_interest(outstanding: int, annual_micros: int) -> int:
    """One month's interest on ``outstanding``, as ``_apply_mortgage_payment``
    charges it."""
    return (outstanding * annual_micros) // (12 * 1_000_000)


def _mortgage_payments_split(
    payments: Sequence[int], annual_micros: int, starting_balance: int,
) -> list[tuple[int, int]]:
    """Each payment's (interest, principal), month by month from
    ``starting_balance``, as ``_apply_mortgage_payment`` would have split it."""
    outstanding = max(0, starting_balance)
    split: list[tuple[int, int]] = []
    for payment in payments:
        if outstanding <= 0:
            break
        interest = min(payment, _mortgage_interest(outstanding, annual_micros))
        principal = min(payment - interest, outstanding)
        split.append((interest, principal))
        outstanding -= principal
    return split


def _mortgage_balance_before(
    balance_after: int, payments: Sequence[int], annual_micros: int,
) -> int:
    """The loan balance before ``payments`` (oldest first) that leaves
    ``balance_after`` once they are paid: amortization run backward, so that
    ``_mortgage_payments_split`` from it lands on ``balance_after`` again.
    A partial-first-year scenario knows the balance at its start, not the
    one on Jan 1 that the year's interest accrued on (#267).

    Interest rounds down to the cent, so a few balances have no exact
    predecessor; each step then takes the smallest one that replays a cent
    high, and the split's principal is off by that cent per such month."""
    balance = max(0, balance_after)
    for payment in reversed(payments):
        # balance = before - (payment - interest(before)); interest rounds
        # down, so step the estimate to the smallest that doesn't replay low.
        before = round((balance + payment) * 12_000_000 / (12_000_000 + annual_micros))
        while before - payment + _mortgage_interest(before, annual_micros) > balance:
            before -= 1
        while before - payment + _mortgage_interest(before, annual_micros) < balance:
            before += 1
        balance = before
    return balance


def _ytd_preroll_posts(
    start: date,
    cadence: str,
    month_of_year: int | None,
    rule_start: date,
    amount_cents: int | None,
    tid: int,
) -> list[tuple[str, int]]:
    if amount_cents is None:
        raise EngineError(f"scheduled_transaction id={tid}: ytd_preroll requires amount")
    if start.month == 1:
        return []
    end = date(start.year, start.month, 1) - timedelta(days=1)
    posts: list[tuple[str, int]] = []
    for month in _month_ends(date(start.year, 1, 1), end):
        fire_count = _cadence_count(cadence, month_of_year, month, rule_start, None)
        if fire_count:
            posts.append((month.isoformat(), amount_cents * fire_count))
    return posts


def _apply_ytd_catchups(
    con, step_id: int, start: date,
    txns: list[list[Any]],
    year_to_date_rows: list[tuple[int, int, int, int, str, int | None]],
    graph: ChainGraph,
    *, account_id_by_name: dict[str, int] | None = None,
    balances: dict[int, int] | None = None,
    prior_year_end_balances: dict[int, int] | None = None,
) -> None:
    """Fire partial-first-year catch-up transfers once, at step 0."""
    date_s = _ytd_catchup_date(start).isoformat()
    for txn in txns:
        (tid, _name, frm, to_chain, cadence, moy, rule_kind,
         interest_acct, _p_person, _p_series, rate_micros,
         _p_claim_age, amount_cents, _ext_json,
         tax_char, _tax_year, _t_start, _t_end, _prio, _t_base_date,
         ytd_amount, ytd_preroll, _t_inflation_cadence) = txn
        if ytd_amount is not None:
            posts = [(date_s, ytd_amount)]
        elif ytd_preroll:
            rule_start = _parse_scheduled_date(_t_start, tid, _name, "start")
            posts = _ytd_preroll_posts(start, cadence, moy, rule_start, amount_cents, tid)
        else:
            continue
        if not posts:
            continue
        from_account = _first_chain_leaf_lot(graph, frm)
        if from_account is None:
            raise EngineError(f"scheduled_transaction id={tid}: YTD source chain has no leaf account")
        to_account = graph.leaf_lot.get(to_chain)
        if to_account is not None:
            if rule_kind == "mortgage_payment":
                _post_mortgage_ytd(
                    con, step_id, posts, from_account, to_account, txn,
                    account_id_by_name=account_id_by_name, balances=balances,
                    prior_year_end_balances=prior_year_end_balances,
                )
                continue
            for post_date_s, amount in posts:
                _post_ytd_transfer(
                    con, step_id, post_date_s, from_account, to_account, amount,
                    tax_char, "ytd_rule", tid,
                )
        else:
            for post_date_s, amount in posts:
                _post_ytd_chain(
                    con, step_id, post_date_s, graph, to_chain, from_account, amount,
                    tax_char, tid,
                )
    for yid, y_from, y_to, y_amount, y_tax_char, y_year in year_to_date_rows:
        row_date_s = _year_to_date_row_date(start, y_year).isoformat()
        _post_ytd_transfer(
            con, step_id, row_date_s, y_from, y_to, y_amount, y_tax_char, "ytd", yid,
        )


def _post_mortgage_ytd(
    con, step_id: int, posts: list[tuple[str, int]],
    from_account: int, to_account: int, txn: list[Any], *,
    account_id_by_name: dict[str, int] | None,
    balances: dict[int, int] | None,
    prior_year_end_balances: dict[int, int] | None,
) -> None:
    """A mortgage rule's catch-up, split into an interest leg and a principal
    leg as each payment would have been (ADR-003 §4.10). The split runs from
    the loan's balance on Jan 1: ``prior_year_end_balance`` when the lot
    gives one, else the opening balance with this year's payments backed out.
    ``ytd_amount`` posts the year's split once; ``ytd_preroll`` posts each
    month's at its month end."""
    (tid, _name, _frm, _to, _cadence, _moy, _kind,
     interest_acct, _p_person, _p_series, rate_micros,
     _p_claim_age, amount_cents, _ext_json,
     tax_char, *_rest, ytd_amount, _ytd_preroll, _inflation_cadence) = txn
    if account_id_by_name is None or balances is None:
        raise EngineError(
            f"scheduled_transaction id={tid}: mortgage YTD requires account context"
        )
    if amount_cents is None or rate_micros is None:
        raise EngineError(
            f"mortgage_payment id={tid}: YTD split requires amount and rate"
        )
    interest_id = account_id_by_name.get(interest_acct or "")
    if interest_id is None:
        raise EngineError(
            f"scheduled_transaction id={tid}: unknown interest_account {interest_acct!r}"
        )
    payment, rate = int(amount_cents), int(rate_micros)
    if payment <= 0:
        raise EngineError(f"mortgage_payment id={tid}: payment must be positive")
    if ytd_amount is not None:
        if ytd_amount <= 0:
            return  # nothing paid yet, as a zero catch-up posts nothing
        # Whole payments, then whatever is left over.
        whole, rest = divmod(int(ytd_amount), payment)
        payments = [payment] * whole + ([rest] if rest else [])
    else:
        payments = [amount for _date, amount in posts]
    jan_1 = (prior_year_end_balances or {}).get(to_account)
    if jan_1 is None:
        jan_1 = _mortgage_balance_before(balances.get(to_account, 0), payments, rate)
    split = _mortgage_payments_split(payments, rate, jan_1)
    if ytd_amount is not None:
        split = [(sum(i for i, _p in split), sum(p for _i, p in split))]
    for (date_s, _amount), (interest, principal) in zip(posts, split, strict=False):
        parent_event_id = _post_ytd_transfer(
            con, step_id, date_s, from_account, interest_id, interest,
            tax_char, "ytd_rule", tid,
        )
        _post_ytd_transfer(
            con, step_id, date_s, from_account, to_account, principal,
            tax_char, "ytd_rule", tid,
            parent_event_id=parent_event_id,
        )


def _post_ytd_chain(
    con, step_id: int, date_s: str, graph: ChainGraph, chain_id: int,
    from_id: int, amount: int, tax_char: str, reason_id: int,
) -> None:
    """Expand one balance-neutral YTD amount through a destination chain."""
    if amount <= 0:
        return
    account_id = graph.leaf_lot.get(chain_id)
    if account_id is not None:
        _post_ytd_transfer(
            con, step_id, date_s, from_id, account_id, amount,
            tax_char, "ytd_rule", reason_id,
        )
        return
    remaining = amount
    for draw_into_id, step_maximum, step_percent, *_rest in graph.members.get(chain_id, []):
        if remaining <= 0:
            break
        allocation = remaining * step_percent // 1_000_000
        if step_maximum is not None:
            allocation = min(allocation, step_maximum)
        allocation = min(remaining, allocation)
        if allocation <= 0:
            continue
        _post_ytd_chain(
            con, step_id, date_s, graph, draw_into_id,
            from_id, allocation, tax_char, reason_id,
        )
        remaining -= allocation


def _past_person_lifespan(
    month_s: str, p_person: str | None, person_lifespan: dict[str, str],
) -> bool:
    """True if this rule's ``person`` has died by ``month_s`` (ADR-003 §4.9).
    Only the rule's own ``person`` column counts, not its accounts' owner: a
    survivor keeps paying from, and into, a decedent's accounts (#256)."""
    return p_person in person_lifespan and month_s > person_lifespan[p_person]


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
    basis = params.get("rate_basis", "opening")
    base = (opening_balances if basis == "opening" else current_balances).get(to_id, 0)
    if base <= 0:
        return 0
    if blend := params.get("blend"):
        # A fund's allocation blend (#78): sum weight x each slice's rate, read
        # now rather than at load, since a set_rate event can move any slice.
        # Kept in micros-squared so the blend itself never rounds.
        weighted = sum(
            weight * _annual_rate_micros(aux, rate_kind, slice_series, month_s, tid)
            for slice_series, weight in _blend_slices_at(blend, month_s)
        )
        return (base * weighted) // (12 * 1_000_000 * 1_000_000)
    annual_micros = _annual_rate_micros(aux, rate_kind, params.get("series"), month_s, tid)
    # monthly_rate = annual_micros / 12 / 1_000_000; amount = base * monthly_rate
    return (base * annual_micros) // (12 * 1_000_000)


def _blend_slices_at(blend: list[list[Any]], month_s: str) -> list[list[Any]]:
    """The ``[[series, weight_micros], ...]`` of the blend's latest set on or before
    ``month_s``, else its earliest: the portfolio overview's rule (ADR-003 §4.1b)."""
    chosen = blend[0][1]
    for as_of, slices in blend:
        if as_of <= month_s:
            chosen = slices
    return chosen


def _annual_rate_micros(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    rate_kind: str, rate_series: str | None, month_s: str, tid: int,
) -> int:
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
    return int(annual_micros)


def _apply_mortgage_payment(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str,
    params: dict[str, Any], amount_cents: int | None,
    from_id: int | None, liab_id: int,
    account_id_by_name: dict[str, int],
    tax_char: str, tid: int, *, graph=None, source_chain_id=None,
    account_tax_treatment=None, account_subkind=None, account_name=None,
    refill_ctx: RefillContext | None = None,
) -> None:
    """Multi-leg P&I per ADR-003 §2.7. Interest leg writes first; principal
    leg's ``parent_event_id`` points at the interest leg. The liability
    account is the row's ``to_account``; the interest-expense account is
    named by ``interest_account``.
    """
    if amount_cents is None:
        raise EngineError(
            f"mortgage_payment id={tid}: missing amount (monthly P&I payment)"
        )
    interest_name = params.get("interest_account")
    annual_micros = params.get("rate_micros")
    if not (interest_name and annual_micros is not None):
        raise EngineError(
            f"mortgage_payment id={tid}: requires interest_account and rate"
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
    payment = int(amount_cents)
    principal = payment - interest
    if principal < 0:
        raise EngineError(
            f"mortgage_payment id={tid}: interest {interest} exceeds payment {payment}"
        )
    if principal > outstanding:
        principal = outstanding
    if from_id is not None:
        interest_event_id = _post_transfer(
            con, balances, account_kinds, step_id, month_s,
            from_id, interest_id, interest, tax_char, tid,
            refill_ctx=refill_ctx,
        )
        _post_transfer(
            con, balances, account_kinds, step_id, month_s,
            from_id, liab_id, principal, tax_char, tid,
            parent_event_id=interest_event_id, refill_ctx=refill_ctx,
        )
        return
    if source_chain_id is None:
        raise EngineError(f"mortgage_payment id={tid}: missing source chain")
    # The engine passes these with every chain source.
    assert graph is not None and account_tax_treatment is not None
    assert account_subkind is not None and account_name is not None
    _chain_draw(
        con, balances, account_kinds, account_tax_treatment, account_subkind,
        account_name, step_id, month_s, graph, source_chain_id,
        interest, interest_id, reason_kind="rule", reason_id=tid,
        refill_ctx=refill_ctx,
    )
    _chain_draw(
        con, balances, account_kinds, account_tax_treatment, account_subkind,
        account_name, step_id, month_s, graph, source_chain_id,
        principal, liab_id, reason_kind="rule", reason_id=tid,
        refill_ctx=refill_ctx,
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
    params: dict[str, Any], from_id: int | None, to_id: int | None,
    person_birthdates: dict[str, date],
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    tax_char: str, tid: int, *, chain_graph=None, to_chain_id=None,
    account_tax_treatment=None, account_subkind=None, account_name=None,
    inflation_factor_cache=None, chain_base_date=None, source_tax_year=None,
    refill_ctx: RefillContext | None = None,
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
        if from_id is None:
            raise EngineError(f"ss_benefit id={tid}: the source must be an account, not an interior chain")
        if to_id is not None:
            _post_transfer(
                con, balances, account_kinds, step_id, month_s,
                from_id, to_id, amount, tax_char, tid, refill_ctx=refill_ctx,
            )
        elif to_chain_id is not None:
            # The engine passes these with every chain destination.
            assert chain_graph is not None and account_tax_treatment is not None
            assert account_subkind is not None and account_name is not None
            _chain_sweep(
                con, balances, account_kinds, account_tax_treatment,
                account_subkind, account_name, step_id, month_s,
                chain_graph, to_chain_id, amount, from_id,
                aux=aux, chain_base_date=chain_base_date,
                inflation_factor_cache=inflation_factor_cache,
                source_tax_character=tax_char,
                source_tax_year=source_tax_year, refill_ctx=refill_ctx,
            )
        else:
            raise EngineError(f"ss_benefit id={tid}: missing destination")


def _apply_rmd(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str, month: date,
    params: dict[str, Any], from_id: int | None, to_id: int,
    person_birthdates: dict[str, date],
    prior_year_end_balances: dict[int, int],
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    tax_char: str, tid: int, *, subject_lots: Sequence[int],
    graph: ChainGraph | None = None, source_chain_id: int | None = None,
    account_tax_treatment: Mapping[int, str | None] | None = None,
    account_subkind: dict[int, str] | None = None,
    account_name: dict[int, str] | None = None,
    chain_context: dict[str, Any] | None = None,
    refill_ctx: RefillContext | None = None,
) -> None:
    """ADR-003 §2.3, ADR-005 §8. Annual December RMD; age-gated by
    ``start_age_series``.

    The subject account (``subject_lots`` are all of its lots) is the account the
    row's ``from_account`` draws from, or ``params["subject_account"]`` when the
    source is a chain. The amount is its total balance on the previous Dec 31
    (from ``lot_state``, or the lots' ``prior_year_end_balance`` when no such
    step exists) over the age factor. It is drawn from a single lot, or through
    the source chain in the chain's own order.
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
    age_year_end = month.year - bd.year
    start_payload = _lookup_aux(aux, start_age_series, None, month_s)
    if start_payload is None or "start_age" not in start_payload:
        raise EngineError(
            f"rmd id={tid}: missing {start_age_series!r} aux with start_age"
        )
    if age_year_end < int(start_payload["start_age"]):
        return
    prev_year = month.year - 1
    marks = ",".join("?" * len(subject_lots))
    total = con.execute(
        "SELECT SUM(ls.balance) FROM lot_state ls "
        "JOIN simulation_step s ON s.id = ls.step_id "
        f"WHERE ls.lot_id IN ({marks}) AND s.month = ?",
        (*subject_lots, f"{prev_year}-12-31"),
    ).fetchone()[0]
    seeded = [prior_year_end_balances[lot] for lot in subject_lots if lot in prior_year_end_balances]
    if total is not None:
        prev_bal = int(total)
    elif seeded:
        prev_bal = sum(seeded)
    else:
        raise EngineError(
            f"rmd id={tid}: no Dec-{prev_year} lot_state and no "
            f"prior_year_end_balance for the subject account's lots {list(subject_lots)}"
        )
    factor_payload = _lookup_rmd_factor(aux, factor_table, age_year_end, month_s)
    if factor_payload is None:
        raise EngineError(
            f"rmd id={tid}: no {factor_table!r} factor for age {age_year_end} "
            f"effective <= {month_s}"
        )
    factor_micros = int(factor_payload["factor_micros"])
    amount = prev_bal * 1_000_000 // factor_micros
    if amount <= 0:
        return
    if from_id is not None:
        _post_transfer(
            con, balances, account_kinds, step_id, month_s,
            from_id, to_id, amount, tax_char, tid, refill_ctx=refill_ctx,
        )
        return
    if graph is None or source_chain_id is None:
        raise EngineError(f"rmd id={tid}: from_account is neither a lot nor a chain")
    _chain_draw(
        con, balances, account_kinds, account_tax_treatment or {}, account_subkind or {},
        account_name or {}, step_id, month_s, graph, source_chain_id, amount, to_id,
        reason_kind="rule", reason_id=tid, refill_ctx=refill_ctx, **(chain_context or {}),
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


def _next_month_end(date_s: str) -> str:
    """Month-end date one calendar month after ``date_s`` (itself a month-end)."""
    d = _parse_date(date_s)
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day).isoformat()


def _apply_on_death(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str,
    params: dict[str, Any], from_id: int | None, to_id: int | None,
    person_lifespan: dict[str, str],
    tax_char: str, tid: int, *, refill_ctx: RefillContext | None = None,
) -> None:
    """ADR-003 \u00a74.9-adjacent: fires once, the first month after the named
    person's lifespan.csv ``final_month``, sweeping 100% of ``from_id``'s
    balance into ``to_id`` (e.g. selling a car, or a life-insurance payout --
    for now, at its current book value, not a separately declared death
    benefit). A no-op if the person has no declared lifespan, or the balance
    is already zero; not an error either way.
    """
    person = params.get("person")
    if not person:
        raise EngineError(f"on_death id={tid}: requires person")
    if from_id is None or to_id is None:
        raise EngineError(
            f"on_death id={tid}: from_account and to_account must both be "
            "concrete accounts, not interior chains"
        )
    final_month = person_lifespan.get(person)
    if final_month is None or month_s != _next_month_end(final_month):
        return
    amount = max(0, balances.get(from_id, 0))
    if amount > 0:
        _post_transfer(
            con, balances, account_kinds, step_id, month_s,
            from_id, to_id, amount, tax_char, tid, refill_ctx=refill_ctx,
        )


def _lookup_by_series_key(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    kind: str, key_field: str, key_value: str, month_s: str,
) -> dict[str, Any] | None:
    """Generic version of ``_lookup_rmd_factor`` for aux kinds keyed by a
    string payload field (e.g. ``filing_status``, ``category``) rather than
    an integer age -- ``capital_loss_limit`` and ``energy_credits`` both
    fit this shape, as do the existing ``federal_brackets``/
    ``standard_deduction`` tables if they're ever driven through here
    instead of their current bespoke lookups.

    Same "most recent row with effective_date <= month_s" convention as
    ``_lookup_rmd_factor``.
    """
    rows = aux.get((kind, None))
    if not rows:
        return None
    chosen: dict[str, Any] | None = None
    for effective, payload in rows:
        if effective <= month_s and payload.get(key_field) == key_value:
            chosen = payload
    return chosen


def _lookup_energy_credit(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    category: str, month_s: str,
) -> dict[str, Any] | None:
    """Federal/state credit or rebate rate for ``category`` (e.g.
    "heat_pump", "ev_charger", "induction_range") effective as of
    ``month_s``, or ``None`` if nothing in the ``energy_credits`` aux table
    covers that category at that date -- callers must treat that as "no
    credit available," not as a default rate. Returns the raw payload
    (``rate_micros``, ``cap`` in cents); it's up to the caller to apply it
    to whatever project cost is being credited. Not wired to a call site
    yet -- see ADR note on where credits should apply (tax_accumulator
    reduction vs. a standalone income-like leg).
    """
    return _lookup_by_series_key(aux, "energy_credits", "category", category, month_s)


type ChainStep = tuple[int, int | None, int, str | None, str | None, str | None, int | None]
"""One ``chain_steps`` row: ``(draw_from_chain_id, maximum, percent, series,
cadence, tax_character, basis_of_lot_id)``."""


class ChainGraph:
    """Resolved ``chain``/``chain_steps`` reference graph (ADR-003 §4.7).

    Each ``members`` entry is a ``(draw_from_chain_id, maximum, percent,
    series, cadence, tax_character, basis_of_lot_id)`` tuple; callers (tests
    especially) may pass a shorter tuple and have it padded with the same
    defaults the loader writes (``maximum=None``, ``percent=100%``, and None
    for the rest).
    """

    __slots__ = ("members", "leaf_lot", "minimum_of", "refill_chain_of",
                 "refill_target_of", "refill_amount_of",
                 "maximum_of", "overflow_chain_of")

    _STEP_DEFAULTS: tuple = (None, 1_000_000, None, None, None, None)

    def __init__(
        self,
        members: Mapping[int, Sequence[tuple[Any, ...]]],
        leaf_lot: dict[int, int],
        minimum_of: dict[int, int],
        refill_chain_of: dict[int, int | None],
        refill_target_of: dict[int, int | None],
        refill_amount_of: dict[int, int | None],
        maximum_of: dict[int, int | None],
        overflow_chain_of: dict[int, int | None],
    ) -> None:
        self.members: dict[int, list[ChainStep]] = {
            chain_id: [(*step, *self._STEP_DEFAULTS[len(step) - 1:]) for step in steps]
            for chain_id, steps in members.items()
        }
        self.leaf_lot = leaf_lot
        self.minimum_of = minimum_of
        self.refill_chain_of = refill_chain_of
        self.refill_target_of = refill_target_of
        self.refill_amount_of = refill_amount_of
        self.maximum_of = maximum_of
        self.overflow_chain_of = overflow_chain_of


def _load_chain_graph(con) -> ChainGraph:
    members: dict[int, list[ChainStep]] = {}
    for chain_id, *step in con.execute(
        "SELECT chain_id, draw_from_chain_id, "
        "       maximum_cents, percent_micros, series, cadence, "
        "       tax_character, basis_of_lot_id "
        "FROM chain_steps ORDER BY chain_id, step_order"
    ):
        members.setdefault(chain_id, []).append(tuple(step))
    leaf_lot: dict[int, int] = {
        r[0]: r[1] for r in con.execute(
            "SELECT id, lot_id FROM chain WHERE lot_id IS NOT NULL"
        )
    }
    minimum_of: dict[int, int] = {}
    refill_chain_of: dict[int, int | None] = {}
    refill_target_of: dict[int, int | None] = {}
    refill_amount_of: dict[int, int | None] = {}
    maximum_of: dict[int, int | None] = {}
    overflow_chain_of: dict[int, int | None] = {}
    for a_id, minimum, refill, refill_target, refill_amount, maximum, overflow in con.execute(
        "SELECT id, minimum_balance_cents, refill_chain_id, "
        "       refill_target_cents, refill_amount_cents, "
        "       maximum_balance_cents, overflow_chain_id "
        "FROM account_lot"
    ):
        minimum_of[a_id] = minimum
        refill_chain_of[a_id] = refill
        refill_target_of[a_id] = refill_target
        refill_amount_of[a_id] = refill_amount
        maximum_of[a_id] = maximum
        overflow_chain_of[a_id] = overflow
    return ChainGraph(members, leaf_lot, minimum_of, refill_chain_of,
                       refill_target_of, refill_amount_of,
                       maximum_of, overflow_chain_of)


def _chain_leaves(chain_id: int, graph: ChainGraph, seen: set[int]) -> set[int]:
    """Every real account reachable by walking a chain's steps recursively."""
    if chain_id in seen:
        return set()  # graph is validated acyclic at load time; guard anyway
    seen.add(chain_id)
    account_id = graph.leaf_lot.get(chain_id)
    if account_id is not None:
        return {account_id}
    out: set[int] = set()
    for draw_from_id, *_allocation in graph.members.get(chain_id, []):
        out |= _chain_leaves(draw_from_id, graph, seen)
    return out


def _settle_order(graph: ChainGraph) -> list[int]:
    """Accounts with a refill/overflow policy, ordered so a source account's
    own policy settles before anything downstream draws from or sweeps into
    it (ADR-003 §4.7) -- a topological sort of the policy-account subgraph.
    """
    policy_accounts = {
        a_id for a_id in graph.minimum_of
        if graph.refill_chain_of.get(a_id) or graph.overflow_chain_of.get(a_id)
    }
    depends_on: dict[int, set[int]] = {a_id: set() for a_id in policy_accounts}
    for a_id in policy_accounts:
        for chain_id in (graph.refill_chain_of.get(a_id), graph.overflow_chain_of.get(a_id)):
            if chain_id is None:
                continue
            for leaf in _chain_leaves(chain_id, graph, set()):
                if leaf in policy_accounts and leaf != a_id:
                    depends_on[a_id].add(leaf)

    order: list[int] = []
    visited: set[int] = set()

    def visit(node: int) -> None:
        if node in visited:
            return
        visited.add(node)
        for dep in depends_on.get(node, ()):
            visit(dep)
        order.append(node)

    for node in policy_accounts:
        visit(node)
    return order


def _post_withdrawal(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    account_tax_treatment: Mapping[int, str | None], account_subkind: dict[int, str],
    account_name: dict[int, str], cost_bases: dict[int, int | None] | None,
    step_id: int, month_s: str, from_id: int, to_id: int, amount: int,
    reason_id: int, *, parent_event_id: int | None = None,
    reason_kind: str = "rule", tax_year: int | None = None,
    refill_ctx: RefillContext | None = None,
    requested: int | None = None, remainder: int | None = None,
    tax_character: str | None = None, refill_mark: int | None = None,
) -> None:
    """Post one withdrawal leg out of ``from_id``, splitting it into a
    ``return_of_capital`` + ``ltcg`` pair (v1's account-level running
    cost-basis approach, ADR-001's "cost basis tracked via basis-tagged
    transfer events") when the source needs cost-basis tracking (today: a
    taxable non-cash account such as ``brokerage_taxable``) and no caller-
    supplied ``tax_character`` already settles the question.

    The split is pro-rata to the fraction of the account's balance withdrawn:
    ``basis_out = cost_basis * amount // balance``. A realized loss (basis
    exceeds proceeds) falls out naturally as a negative ``ltcg`` leg -- the
    same convention year_to_date.csv already uses for loss carryforwards.
    Then the ``return_of_capital`` leg alone exceeds ``amount``, so the refill
    look-ahead, lot tracking and negative-balance check run once on the net
    ``amount`` rather than per leg (issue #101): per leg, the overshoot drew
    a phantom refill or warned of a negative balance the pair never reaches.
    """
    if tax_character is None and _leaves_roth(refill_ctx, from_id, to_id):
        assert refill_ctx is not None
        _post_roth_withdrawal(
            con, balances, account_kinds, refill_ctx, step_id, month_s, from_id, to_id,
            amount, reason_id, parent_event_id=parent_event_id, reason_kind=reason_kind,
            tax_year=tax_year, requested=requested, remainder=remainder, refill_mark=refill_mark,
        )
        return
    if tax_character is None and refill_ctx is not None and from_id in refill_ctx.roth:
        tax_character = "none"  # a rollover into another retirement lot (_leaves_roth)
    tax_char = tax_character or registries.withdrawal_tax_character(
        account_tax_treatment.get(from_id), account_subkind.get(from_id, ""),
        account_tax_treatment.get(to_id),
    )
    if tax_char is not None:
        _post_transfer(
            con, balances, account_kinds, step_id, month_s, from_id, to_id, amount,
            tax_char, reason_id, parent_event_id, reason_kind=reason_kind,
            tax_year=tax_year, refill_ctx=refill_ctx,
            requested=requested, remainder=remainder, refill_mark=refill_mark,
        )
        return
    basis = None if cost_bases is None else cost_bases.get(from_id)
    if cost_bases is None or basis is None:
        raise EngineError(
            f"account {account_name.get(from_id)!r}: withdrawal needs cost-basis "
            "tracking (taxable, non-cash) but opening_cost_basis was never set -- "
            "add it to account_lots.csv"
        )
    if account_kinds[to_id] == "liability":
        amount = min(amount, max(0, balances.get(to_id, 0)))
        if amount == 0:
            return
    if refill_ctx is not None and amount > 0:
        if refill_mark is None:  # see _post_transfer's refill_mark
            refill_mark = _ensure_min_balance(
                con, balances, account_kinds, step_id, month_s, refill_ctx,
                from_id, amount,
            )
        # A refill into the account is a purchase that raised its basis.
        basis = cost_bases[from_id] or 0
    prior_balance = balances.get(from_id, 0)
    basis_out = 0 if prior_balance <= 0 else min(basis, basis * amount // prior_balance)
    gain = amount - basis_out
    if refill_ctx is not None:
        _track_lots(refill_ctx, account_kinds, balances, from_id, to_id, amount, _parse_date(month_s))
    basis_leg_id = _post_transfer(
        con, balances, account_kinds, step_id, month_s, from_id, to_id, basis_out,
        "return_of_capital", reason_id, parent_event_id, reason_kind=reason_kind,
        tax_year=tax_year, refill_ctx=refill_ctx,
        requested=requested, remainder=remainder, net_effects=False,
        refill_mark=refill_mark,
    )
    gain_character = (
        "stcg"
        if refill_ctx is not None
        and not _is_long_term(refill_ctx.acquired.get(from_id), _parse_date(month_s))
        else "ltcg"
    )
    _post_transfer(
        con, balances, account_kinds, step_id, month_s, from_id, to_id, gain,
        gain_character, reason_id, basis_leg_id if basis_leg_id is not None else parent_event_id,
        reason_kind=reason_kind, tax_year=tax_year, refill_ctx=refill_ctx,
        requested=requested, remainder=remainder, net_effects=False,
        refill_mark=refill_mark if basis_leg_id is None else None,
    )
    cost_bases[from_id] = basis - basis_out
    if refill_ctx is not None:
        _warn_if_negative(refill_ctx, account_kinds, from_id, balances, month_s)


def _post_roth_withdrawal(
    con, balances: dict[int, int], account_kinds: dict[int, str], ctx: RefillContext,
    step_id: int, month_s: str, from_id: int, to_id: int, amount: int, reason_id: int,
    *, parent_event_id: int | None, reason_kind: str, tax_year: int | None,
    requested: int | None, remainder: int | None, refill_mark: int | None,
) -> None:
    """Post a distribution out of a Roth lot (#217): basis as ``tax_free``,
    and earnings as ``tax_free`` when the distribution is qualified, else as
    ``roth_nonqualified`` (``RothLot``). The lot's basis falls by the basis
    paid out. As in ``_post_withdrawal``'s split, the refill, lot tracking
    and negative-balance check run once, on the whole amount."""
    if account_kinds[to_id] == "liability":
        amount = min(amount, max(0, balances.get(to_id, 0)))
        if amount == 0:
            return
    if amount > 0 and refill_mark is None:
        refill_mark = _ensure_min_balance(
            con, balances, account_kinds, step_id, month_s, ctx, from_id, amount,
        )
    taxed = _roth_taxed(ctx, balances, from_id, amount, _parse_date(month_s))
    # _track_lots spends the lot's basis by the same rule, qualified or not.
    _track_lots(ctx, account_kinds, balances, from_id, to_id, amount, _parse_date(month_s))
    free_leg = _post_transfer(
        con, balances, account_kinds, step_id, month_s, from_id, to_id, amount - taxed,
        "tax_free", reason_id, parent_event_id, reason_kind=reason_kind, tax_year=tax_year,
        refill_ctx=ctx, requested=requested, remainder=remainder, net_effects=False,
        refill_mark=refill_mark,
    )
    _post_transfer(
        con, balances, account_kinds, step_id, month_s, from_id, to_id, taxed,
        "roth_nonqualified", reason_id, free_leg if free_leg is not None else parent_event_id,
        reason_kind=reason_kind, tax_year=tax_year, refill_ctx=ctx,
        requested=requested, remainder=remainder, net_effects=False,
        refill_mark=refill_mark if free_leg is None else None,
    )
    _warn_if_negative(ctx, account_kinds, from_id, balances, month_s)


def _roth_draw_taxed(
    ctx: RefillContext | None, balances: Mapping[int, int], graph: ChainGraph,
    step_chain: int, target: int, allocation: int, month_s: str,
) -> bool:
    """Whether a chain step on a Roth lot would pay out taxed earnings (#217):
    such a step is skipped unless it is its chain's last."""
    lot = graph.leaf_lot.get(step_chain)
    if lot is None or ctx is None or not _leaves_roth(ctx, lot, target):
        return False
    take = min(allocation, max(0, balances.get(lot, 0) - graph.minimum_of.get(lot, 0)))
    return take > 0 and _roth_taxed(ctx, balances, lot, take, _parse_date(month_s)) > 0


def _chain_step_allocation(
    remaining: int, maximum: int | None, percent: int, series: str | None,
    cadence: str | None, month_s: str,
    aux, chain_base_date: date | None, inflation_factor_cache: dict | None,
) -> int:
    """One chain_steps row's allocation out of ``remaining`` (ADR-003 §4.7):
    ``percent`` of what's still outstanding (default 100%, so an unallocated
    step consumes all of it), capped at ``maximum`` if set -- the two may be
    combined -- with ``series``/``cadence`` inflating that cap over time.
    """
    allocation = remaining * percent // 1_000_000
    if maximum is not None:
        cap = maximum
        if series:
            if aux is None or chain_base_date is None or inflation_factor_cache is None:
                raise EngineError("inflated chain step requires runtime inflation context")
            cap = cap * _cumulative_inflation_factor(
                aux, series, chain_base_date, _parse_date(month_s),
                inflation_factor_cache, cadence=cadence or "monthly",
            ) // 1_000_000
        allocation = min(allocation, cap)
    return min(remaining, allocation)


class _ChainProgress:
    """Tracks one top-level ``_chain_draw``/``_chain_sweep`` call tree's
    original ask and how much of it has been fulfilled so far, so every leg's
    transfer_event can record the SAME requested amount (the rule's original
    ask, not that leg's own recursively-allocated slice) plus a running
    remainder -- letting the ledger show how one request broke down across
    accounts without having to trace the recursion.
    """

    __slots__ = ("requested", "fulfilled")

    def __init__(self, requested: int) -> None:
        self.requested = requested
        self.fulfilled = 0

    def record(self, amount: int) -> int:
        """Register ``amount`` fulfilled and return the remainder after it."""
        self.fulfilled += amount
        return self.requested - self.fulfilled


def _chain_draw(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    account_tax_treatment: Mapping[int, str | None], account_subkind: dict[int, str],
    account_name: dict[int, str],
    step_id: int, month_s: str, graph: ChainGraph,
    chain_id: int, amount_needed: int, target_account_id: int,
    *, refill_ctx: RefillContext | None = None,
    reason_kind: str = "refill", reason_id: int | None = None,
    tax_year: int | None = None,
    aux=None, chain_base_date: date | None = None,
    inflation_factor_cache: dict | None = None,
    _progress: _ChainProgress | None = None,
    _fallback: bool = False,
) -> int:
    """Recursively draw up to ``amount_needed`` into ``target_account_id``,
    scanning ``chain_id``'s steps in order (ADR-003 §4.7).

    Each step's account supplies as much of its ``_chain_step_allocation`` as
    it can without dropping -- after any refill_chain draw -- at or below its
    own minimum_balance_cents; whatever it can't cover falls through to the
    next step, whether because it's exhausted or because the step's own
    percent/maximum allocation was smaller than what's still needed. Each leg
    is posted with the concrete source account's own withdrawal tax
    character -- derived here, at the point the money actually moves, since
    it depends on which account actually supplied it. ``_progress`` (created
    here at the top-level call, then threaded through the recursion) is what
    lets every leg's transfer_event report the ORIGINAL ``amount_needed`` as
    ``requested`` rather than its own shrunk-down allocation. ``_fallback``
    says a step after this chain, in a chain above it, can still pay, so a
    taxed Roth step here is skipped even when it is this chain's last.
    """
    if amount_needed <= 0:
        return 0
    if _progress is None:
        _progress = _ChainProgress(amount_needed)
    if refill_ctx is not None:
        if aux is None:
            aux = refill_ctx.aux
        if chain_base_date is None:
            chain_base_date = refill_ctx.chain_base_date
        if inflation_factor_cache is None:
            inflation_factor_cache = refill_ctx.inflation_factor_cache
    account_id = graph.leaf_lot.get(chain_id)
    if account_id is not None:
        if account_id == target_account_id:
            return 0
        # A liability accepts only what's owed; capping here, before the
        # refill and the progress record, keeps both to what actually moves.
        if account_kinds[target_account_id] == "liability":
            amount_needed = min(amount_needed, max(0, balances.get(target_account_id, 0)))
            if amount_needed <= 0:
                return 0
        refill_mark = None
        if refill_ctx is not None:
            refill_mark = _ensure_min_balance(
                con, balances, account_kinds, step_id, month_s, refill_ctx,
                account_id, amount_needed,
            )
        floor = graph.minimum_of.get(account_id, 0)
        available = max(0, balances.get(account_id, 0) - floor)
        take = min(amount_needed, available)
        if take <= 0:
            return 0
        _post_withdrawal(
            con, balances, account_kinds, account_tax_treatment, account_subkind,
            account_name, refill_ctx.cost_bases if refill_ctx is not None else None,
            step_id, month_s, account_id, target_account_id, take,
            reason_id or account_id,
            parent_event_id=None, reason_kind=reason_kind, tax_year=tax_year,
            refill_ctx=refill_ctx, requested=_progress.requested,
            remainder=_progress.record(take), refill_mark=refill_mark,
        )
        return take
    total = 0
    members = graph.members.get(chain_id, [])
    for number, (draw_from_id, step_maximum, step_percent, step_series, step_cadence, *_tax) in (
            enumerate(members, 1)):
        remaining = amount_needed - total
        if remaining <= 0:
            break
        allocation = _chain_step_allocation(
            remaining, step_maximum, step_percent, step_series, step_cadence,
            month_s, aux, chain_base_date, inflation_factor_cache,
        )
        if allocation <= 0:
            continue
        fallback = _fallback or number < len(members)
        if fallback and _roth_draw_taxed(
            refill_ctx, balances, graph, draw_from_id, target_account_id, allocation, month_s,
        ):
            continue  # a later step can pay without tax; a Roth last step still pays (#217)
        total += _chain_draw(
            con, balances, account_kinds, account_tax_treatment, account_subkind,
            account_name, step_id, month_s, graph,
            draw_from_id, allocation, target_account_id,
            refill_ctx=refill_ctx, reason_kind=reason_kind,
            reason_id=reason_id, tax_year=tax_year,
            aux=aux, chain_base_date=chain_base_date,
            inflation_factor_cache=inflation_factor_cache,
            _progress=_progress, _fallback=fallback,
        )
    return total


class _SaleBasis:
    """The basis a sweep out of a lot carries into a destination chain that
    has a basis step for that lot (#199): the lot's basis, pro-rata to the
    share of its balance withdrawn, until the basis step uses it."""

    __slots__ = ("recovered", "share")

    def __init__(self, share: int) -> None:
        self.share = share
        self.recovered = False


def _has_basis_step(graph: ChainGraph, chain_id: int, lot_id: int) -> bool:
    """Whether ``chain_id``, or a chain under it, recovers ``lot_id``'s basis."""
    for draw_into_id, *_allocation, character, basis_of in graph.members.get(chain_id, []):
        if basis_of == lot_id and character in registries.BASIS_TAX_CHARACTERS:
            return True
        if draw_into_id not in graph.leaf_lot and _has_basis_step(graph, draw_into_id, lot_id):
            return True
    return False


def _sale_basis(
    refill_ctx: RefillContext | None, balances: dict[int, int], lot_id: int, amount: int,
) -> _SaleBasis:
    """The basis ``amount`` withdrawn from ``lot_id`` carries: the same pro-rata
    share as ``_post_withdrawal``'s split, so a sale of the whole balance
    carries all of it."""
    basis = None if refill_ctx is None else refill_ctx.cost_bases.get(lot_id)
    if refill_ctx is None or basis is None:
        name = None if refill_ctx is None else refill_ctx.account_name.get(lot_id)
        raise EngineError(
            f"account {name!r}: a chain step recovers its basis, but it has none -- "
            "set opening_cost_basis in account_lots.csv"
        )
    prior = balances.get(lot_id, 0)
    return _SaleBasis(basis if prior <= amount else basis * amount // prior)


def _chain_sweep(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    account_tax_treatment: Mapping[int, str | None], account_subkind: dict[int, str],
    account_name: dict[int, str],
    step_id: int, month_s: str, graph: ChainGraph,
    chain_id: int, amount_available: int, source_account_id: int,
    *, aux=None, chain_base_date: date | None = None,
    inflation_factor_cache: dict | None = None,
    source_tax_character: str | None = None,
    source_tax_year: int | None = None,
    refill_ctx: RefillContext | None = None,
    reason_kind: str = "overflow", reason_id: int | None = None,
    _progress: _ChainProgress | None = None,
    _sale: _SaleBasis | None = None,
) -> int:
    """Recursively sweep up to ``amount_available`` out of ``source_account_id``.

    Each leg is an ``overflow`` traced to the source lot, unless a rule's
    sweep passes its own ``reason_kind='rule'`` and ``reason_id``.

    Mirrors ``_chain_draw``, including ``_progress`` tracking so every leg
    reports the original ``amount_available`` as ``requested``: each
    destination's own ``maximum_balance_cents`` caps how much it accepts, and
    any remainder falls through to the next step.

    A step's ``tax_character`` characterizes the money it places, and every
    later step's until another sets one (#199, ADR-003 §4.7). A basis step
    (``basis_of`` the source lot, character ``return_of_capital`` or
    ``home_sale``) is sized by the basis the withdrawal carries
    (``_SaleBasis``) and uses it up; a ``capital_improvement`` step adds what
    it places to its ``basis_of`` lot's basis.
    """
    if amount_available <= 0:
        return 0
    if _progress is None:
        _progress = _ChainProgress(amount_available)
        if (graph.leaf_lot.get(chain_id) is None
                and _has_basis_step(graph, chain_id, source_account_id)):
            sale = _sale_basis(refill_ctx, balances, source_account_id, amount_available)
            placed = _chain_sweep(
                con, balances, account_kinds, account_tax_treatment, account_subkind,
                account_name, step_id, month_s, graph,
                chain_id, amount_available, source_account_id,
                aux=aux, chain_base_date=chain_base_date,
                inflation_factor_cache=inflation_factor_cache,
                source_tax_character=source_tax_character,
                source_tax_year=source_tax_year, refill_ctx=refill_ctx,
                reason_kind=reason_kind, reason_id=reason_id,
                _progress=_progress, _sale=sale,
            )
            # Sale mode bypassed the withdrawal's own split, so a basis step
            # that earlier steps starved would leave the basis where it was,
            # and money left in the lot would keep a gain the sale's basis
            # share already priced as sold.
            name = None if refill_ctx is None else refill_ctx.chain_name.get(chain_id)
            if not sale.recovered:
                raise EngineError(
                    f"chain {name!r}: its basis step for "
                    f"{account_name.get(source_account_id)!r} was never reached; "
                    "an earlier step took the whole amount"
                )
            if placed < amount_available:
                raise EngineError(
                    f"chain {name!r}: placed only {placed / 100:.2f} of the "
                    f"{amount_available / 100:.2f} sale of "
                    f"{account_name.get(source_account_id)!r}; add a step after "
                    "the basis step, with its own tax_character, to take the gain"
                )
            return placed
    account_id = graph.leaf_lot.get(chain_id)
    if account_id is not None:
        if account_id == source_account_id:
            return 0
        cap = graph.maximum_of.get(account_id)
        room = (
            amount_available if cap is None
            else max(0, cap - max(0, balances.get(account_id, 0)))
        )
        if account_kinds[account_id] == "liability":
            # Room in a liability is what's still owed (see _chain_draw).
            room = min(room, max(0, balances.get(account_id, 0)))
        put = min(amount_available, room)
        if put <= 0:
            return 0
        _post_withdrawal(
            con, balances, account_kinds, account_tax_treatment, account_subkind,
            account_name, refill_ctx.cost_bases if refill_ctx is not None else None,
            step_id, month_s, source_account_id, account_id, put,
            source_account_id if reason_id is None else reason_id,
            parent_event_id=None, reason_kind=reason_kind,
            tax_year=source_tax_year, refill_ctx=refill_ctx,
            requested=_progress.requested, remainder=_progress.record(put),
            tax_character=source_tax_character,
        )
        return put
    total = 0
    character = source_tax_character
    # Money out of a basis-tracked lot keeps _post_withdrawal's pro-rata
    # split unless a basis step for that lot says how much is basis; a
    # chain's characters alone would tax the basis as gain.
    keeps_split = _sale is None and registries.withdrawal_tax_character(
        account_tax_treatment.get(source_account_id), account_subkind.get(source_account_id, ""),
    ) is None
    for number, (draw_into_id, step_maximum, step_percent, step_series, step_cadence,
                 step_character, basis_of) in enumerate(graph.members.get(chain_id, []), 1):
        if step_character is not None and not keeps_split:
            character = step_character
        recovers = basis_of is not None and step_character in registries.BASIS_TAX_CHARACTERS
        remaining = amount_available - total
        if remaining <= 0 and not recovers:
            break
        if recovers:
            if _sale is None or basis_of != source_account_id:
                name = None if refill_ctx is None else refill_ctx.chain_name.get(chain_id)
                raise EngineError(
                    f"chain {name!r} step {number}: recovers the basis of "
                    f"{account_name.get(basis_of)!r}, but the money comes from "
                    f"{account_name.get(source_account_id)!r}"
                )
            if _progress.fulfilled > 0:
                name = None if refill_ctx is None else refill_ctx.chain_name.get(chain_id)
                raise EngineError(
                    f"chain {name!r} step {number}: a basis step must take the sale's "
                    f"first money, but earlier steps already placed "
                    f"{_progress.fulfilled / 100:.2f}"
                )
            allocation = min(remaining, _sale.share)
        else:
            allocation = _chain_step_allocation(
                remaining, step_maximum, step_percent, step_series, step_cadence,
                month_s, aux, chain_base_date, inflation_factor_cache,
            )
        placed = 0 if allocation <= 0 else _chain_sweep(
            con, balances, account_kinds, account_tax_treatment, account_subkind,
            account_name, step_id, month_s, graph,
            draw_into_id, allocation, source_account_id,
            aux=aux, chain_base_date=chain_base_date,
            inflation_factor_cache=inflation_factor_cache,
            source_tax_character=character,
            source_tax_year=source_tax_year, refill_ctx=refill_ctx,
            reason_kind=reason_kind, reason_id=reason_id,
            _progress=_progress, _sale=_sale,
        )
        total += placed
        if recovers:
            assert _sale is not None and refill_ctx is not None  # checked above, by _sale_basis
            _recover_basis(
                con, refill_ctx, _sale, source_account_id, placed, allocation,
                step_id, month_s, step_character, source_tax_year,
                reason_kind, source_account_id if reason_id is None else reason_id,
            )
        elif basis_of is not None and placed > 0:
            _add_basis(refill_ctx, account_name, basis_of, placed)
    return total


def _recover_basis(
    con, ctx: RefillContext, sale: _SaleBasis, lot_id: int, placed: int, allocation: int,
    step_id: int, month_s: str, character: str | None, tax_year: int | None,
    reason_kind: str, reason_id: int,
) -> None:
    """Close a basis step (#199): the withdrawal's basis share leaves the lot,
    whether or not the sale recovered it. What it didn't recover is a capital
    loss for ``return_of_capital``, recorded for the tax accumulator; for
    ``home_sale`` it is a personal loss, which is not deductible."""
    if placed < allocation:
        raise EngineError(
            f"account {ctx.account_name.get(lot_id)!r}: a basis step placed only "
            f"{placed / 100:.2f} of {allocation / 100:.2f}; give its destinations room "
            "(a lot with no maximum_balance) so the gain isn't taken as basis"
        )
    share, sale.share = sale.share, 0
    sale.recovered = True
    ctx.cost_bases[lot_id] = (ctx.cost_bases[lot_id] or 0) - share
    lost = share - placed
    if lost > 0 and character == "return_of_capital":
        long_term = _is_long_term(ctx.acquired.get(lot_id), _parse_date(month_s))
        con.execute(
            "INSERT INTO capital_loss(step_id, date, lot_id, amount, tax_character, "
            "tax_year, reason_kind, reason_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (step_id, month_s, lot_id, lost, "ltcg" if long_term else "stcg",
             tax_year, reason_kind, reason_id),
        )


def _add_basis(
    ctx: RefillContext | None, account_name: dict[int, str], lot_id: int, amount: int,
) -> None:
    """A ``capital_improvement`` step (#199): what it placed raises the lot's basis."""
    basis = None if ctx is None else ctx.cost_bases.get(lot_id)
    if ctx is None or basis is None:
        raise EngineError(
            f"account {account_name.get(lot_id)!r}: a capital_improvement step adds "
            "to its basis, but it has none -- set opening_cost_basis in account_lots.csv"
        )
    ctx.cost_bases[lot_id] = basis + amount


def _settle_accounts(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    account_subkind: dict[int, str], account_tax_treatment: Mapping[int, str | None],
    account_name: dict[int, str],
    step_id: int, month_s: str, graph: ChainGraph, settle_order: list[int],
    *, aux, chain_base_date: date, inflation_factor_cache: dict,
    refill_ctx: RefillContext | None = None,
) -> None:
    """Overflow pass (ADR-003 §4.7), run once after each month's rules.

    Refill is handled proactively per-debit (see ``_ensure_min_balance``, called
    from ``_post_transfer``) rather than here -- by the time this runs, no
    account should still be below its minimum_balance from this month's
    activity. Overflow has no per-transaction analogue (a balance can only
    exceed maximum_balance because of accumulated credits over the month), so
    it stays a month-end sweep. ``settle_order`` is precomputed once per run
    so an account's own overflow already reflects this month's activity before
    anything downstream sweeps into it.
    """
    for account_id in settle_order:
        maximum = graph.maximum_of.get(account_id)
        overflow_chain = graph.overflow_chain_of.get(account_id)
        if maximum is not None and overflow_chain is not None:
            excess = balances.get(account_id, 0) - maximum
            if excess > 0:
                _chain_sweep(
                    con, balances, account_kinds, account_tax_treatment, account_subkind,
                    account_name, step_id, month_s, graph,
                    overflow_chain, excess, account_id,
                    aux=aux, chain_base_date=chain_base_date,
                    inflation_factor_cache=inflation_factor_cache,
                    refill_ctx=refill_ctx,
                )


@dataclass(frozen=True)
class _TaxEstimate:
    """One tax year's income tax as ``tax_accumulator`` computes it, from
    the transfers posted so far, with ways to ask what more income would do
    (#251's after-tax drawdown, #252's fill_bracket)."""
    scope: str
    filing_status: str
    brackets: list[tuple[int, int]]
    ordinary_income: int
    ltcg_income: int
    ss_income: int
    taxable_ss: int
    business: Any
    capital: Any
    standard_deduction: int
    itemized_deduction: int
    deduction: int
    nol_in: int
    nol_used: int
    nol_new: int
    taxable_income: int
    tax_liability: int
    tax_with: Callable[[int, int], int]
    """The year's tax with more ordinary income and long-term gain on top."""
    taxable_with: Callable[[int, int], tuple[int, int]]
    """The year's taxable income, and the ordinary part of it, with more
    ordinary income and long-term gain on top."""
    agi_with: Callable[[int, int], int]
    """The year's AGI (and so IRMAA's MAGI, #253) with more ordinary income
    and long-term gain on top."""


def _year_tax_estimate(
    con, params: dict[str, Any], *,
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    tax_year: int, tid: int,
    person_lifespan: dict[str, str] | None = None,
    taxpayers: Collection[str] = (),
) -> _TaxEstimate:
    """``tax_year``'s tax for a ``tax_accumulator`` rule's ``params`` (see
    ``_apply_tax_accumulator``), from the transfers posted so far. When
    ``tax_with``/``taxable_with`` add income, they work out again how much of
    a carried-in net operating loss it can use under the rule's limit, and
    how much more Social Security becomes taxable."""
    scope = str(params.get("scope", "federal"))
    filing_status_aux = str(params.get("filing_status_aux", "filing_status"))
    standard_deduction_aux = str(params.get(
        "standard_deduction_aux",
        "standard_deduction" if scope == "federal" else registries.STATE_STANDARD_DEDUCTION,
    ))
    ss_threshold_aux = str(params.get("ss_threshold_aux", "ss_taxation_thresholds"))
    ltcg_bracket_aux = str(params.get("ltcg_bracket_aux", "ltcg_brackets"))
    bracket_aux = str(
        params.get(
            "bracket_aux",
            "federal_brackets" if scope == "federal" else "state_brackets",
        )
    )
    bracket_series = params.get("bracket_series")

    as_of = f"{tax_year}-12-31"
    filing_status = _active_filing_status(
        aux, filing_status_aux, tax_year, tid,
        person_lifespan=person_lifespan, taxpayers=taxpayers,
    )

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

    def loss_limit(year: int) -> int:
        # A carryover from a year before the filing_status table starts (a
        # year-to-date loss) is limited as if filed with this year's status.
        status = filing_status if year == tax_year or not _active_aux_rows(
            aux, filing_status_aux, None, f"{year}-12-31",
        ) else _active_filing_status(
            aux, filing_status_aux, year, tid,
            person_lifespan=person_lifespan, taxpayers=taxpayers,
        )
        row = _lookup_by_series_key(
            aux, "capital_loss_limit", "filing_status", status, f"{year}-12-31",
        )
        if row is None:
            raise EngineError(
                f"tax_accumulator id={tid}: {year} has a net capital loss, but no "
                f"capital_loss_limit row for filing_status={status!r} effective on "
                f"or before {year}-12-31 (aux/capital_loss_limit.csv)"
            )
        return int(row["amount"])

    other_ordinary, ss_income = _tax_year_income_buckets(con, tax_year=tax_year)
    business = _business_result(con, tax_year=tax_year)
    capital = _capital_gains(con, tax_year=tax_year, loss_limit=loss_limit)
    # A Schedule C loss offsets the year's other income (#231).
    ordinary_income = other_ordinary + business.net + capital.stcg - capital.loss_deduction
    ltcg_income = capital.ltcg

    ss_tiers: tuple[int, int] | None = None
    if scope == "federal" and ss_income > 0:
        thresholds = _active_aux_rows(aux, ss_threshold_aux, None, as_of)
        thresholds = [r for r in thresholds if r.get("filing_status") == filing_status]
        if not thresholds:
            raise EngineError(
                f"tax_accumulator id={tid}: missing ss thresholds for filing_status={filing_status!r}"
            )
        ss_tiers = (int(thresholds[0]["tier1_floor"]), int(thresholds[0]["tier2_floor"]))

    # A state's standard deduction is keyed by state, like its brackets
    # (state_standard_deduction__CA.csv), so it follows bracket_series.
    deduction_series = (
        bracket_series if standard_deduction_aux == registries.STATE_STANDARD_DEDUCTION
        else None
    )
    if deduction_series is not None and not aux.get((standard_deduction_aux, deduction_series)):
        # The loader warned: the federal figure stands in for the state's.
        standard_deduction_aux, deduction_series = "standard_deduction", None
    deduction_rows = _active_aux_rows(aux, standard_deduction_aux, deduction_series, as_of)
    deduction_rows = [r for r in deduction_rows if r.get("filing_status") == filing_status]
    if not deduction_rows:
        raise EngineError(
            f"tax_accumulator id={tid}: missing standard deduction for filing_status={filing_status!r}"
        )
    standard_deduction = int(deduction_rows[0]["amount"])

    # Itemized deductions (optional): a dated total, no filing_status split.
    # state_deductions falls back to federal_deductions when the scenario has
    # no state_deductions rows at all -- most states don't need their own file.
    itemized_deduction_aux = str(params.get(
        "itemized_deduction_aux",
        "federal_deductions" if scope == "federal" else "state_deductions",
    ))
    if scope == "state" and not aux.get((itemized_deduction_aux, None)):
        itemized_deduction_aux = "federal_deductions"
    itemized_rows = _active_aux_rows(aux, itemized_deduction_aux, None, as_of)
    itemized_deduction = int(itemized_rows[0]["amount"]) if itemized_rows else 0
    deduction = max(standard_deduction, itemized_deduction)

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
    ltcg_rows = [
        r for r in _active_aux_rows(aux, ltcg_bracket_aux, None, as_of)
        if r.get("filing_status") == filing_status
    ] if scope == "federal" else []
    ltcg_brackets = sorted(
        (int(r["floor"]), int(r["marginal_rate_micros"])) for r in ltcg_rows
    )

    def taxable_ss_for(ordinary: int, ltcg: int) -> int:
        if ss_tiers is None:
            return 0
        return _taxable_social_security_cents(
            ordinary_income=ordinary, ltcg_income=ltcg, ss_income=ss_income,
            tier1=ss_tiers[0], tier2=ss_tiers[1],
        )

    def tax_on(taxable: int, ltcg: int) -> int:
        """The brackets' tax on ``taxable`` income, ``ltcg`` of it long-term gain."""
        if not (ltcg_brackets and ltcg > 0):
            return _progressive_tax_cents(taxable, brackets)
        # Long-term gains sit on top of ordinary taxable income and are taxed at
        # the preferential rates for the band each part lands in.
        gain_taxable = min(ltcg, taxable)
        ordinary_taxable = taxable - gain_taxable
        return (
            _progressive_tax_cents(ordinary_taxable, brackets)
            + _progressive_tax_cents(taxable, ltcg_brackets)
            - _progressive_tax_cents(ordinary_taxable, ltcg_brackets)
        )

    taxable_ss = taxable_ss_for(ordinary_income, ltcg_income)
    income_before_nol = ordinary_income + ltcg_income + taxable_ss - deduction
    # A net operating loss carried in offsets part of this year's taxable
    # income (#231). It doesn't lower AGI first, as the NOL deduction really
    # does, so taxable Social Security can come out a little high in a year
    # that uses one.
    nol_limit_percent = int(params.get("nol_limit_percent", 80 if scope == "federal" else 100))
    if not 0 <= nol_limit_percent <= 100:
        raise EngineError(
            f"tax_accumulator nol_limit_percent must be 0 to 100, got {nol_limit_percent}"
        )
    nol_in = _summary_amount(con, tax_year - 1, f"{scope}_nol_carryover")
    nol_used = min(nol_in, max(0, income_before_nol) * nol_limit_percent // 100)
    taxable_income = max(0, income_before_nol - nol_used)
    # The loss that carries: the business loss less the income it absorbed.
    # The deduction never adds to it (a nonbusiness deduction offsets only
    # nonbusiness income, §172(d)(4)), nor does the capital-loss deduction.
    nonbusiness_income = other_ordinary + capital.stcg + ltcg_income + taxable_ss
    nol_new = max(0, -business.net - max(0, nonbusiness_income - deduction))
    tax_liability = tax_on(taxable_income, ltcg_income)

    def taxable_with(extra_ordinary: int, extra_ltcg: int) -> tuple[int, int]:
        """This year's taxable income, and its ordinary part, with more
        ordinary income and long-term gain on top. More income can use more
        of a carried-in NOL under its limit, so that is worked out again."""
        ordinary, ltcg = ordinary_income + extra_ordinary, ltcg_income + extra_ltcg
        before_nol = ordinary + ltcg + taxable_ss_for(ordinary, ltcg) - deduction
        used = min(nol_in, max(0, before_nol) * nol_limit_percent // 100)
        taxable = max(0, before_nol - used)
        return taxable, taxable - min(ltcg, taxable)

    def tax_with(extra_ordinary: int, extra_ltcg: int) -> int:
        """This year's tax with more ordinary income and long-term gain on top."""
        return tax_on(taxable_with(extra_ordinary, extra_ltcg)[0], ltcg_income + extra_ltcg)

    def agi_with(extra_ordinary: int, extra_ltcg: int) -> int:
        ordinary, ltcg = ordinary_income + extra_ordinary, ltcg_income + extra_ltcg
        return ordinary + ltcg + taxable_ss_for(ordinary, ltcg)

    return _TaxEstimate(
        scope=scope, filing_status=filing_status, brackets=brackets,
        ordinary_income=ordinary_income, ltcg_income=ltcg_income, ss_income=ss_income,
        taxable_ss=taxable_ss, business=business, capital=capital,
        standard_deduction=standard_deduction, itemized_deduction=itemized_deduction,
        deduction=deduction, nol_in=nol_in, nol_used=nol_used, nol_new=nol_new,
        taxable_income=taxable_income, tax_liability=tax_liability,
        tax_with=tax_with, taxable_with=taxable_with, agi_with=agi_with,
    )


def _apply_tax_accumulator(
    con,
    balances: dict[int, int],
    account_kinds: dict[int, str],
    step_id: int | None,
    month_s: str,
    month: date,
    params: dict[str, Any],
    *,
    from_id: int | None,
    to_id: int,
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    tax_char: str,
    tid: int,
    post_transfer: bool = True,
    graph=None, source_chain_id=None, account_tax_treatment=None,
    account_subkind=None, account_name=None, tax_year=None,
    person_lifespan: dict[str, str] | None = None,
    taxpayers: Collection[str] = (),
    refill_ctx: RefillContext | None = None,
) -> None:
    """Compute and optionally pay forecasted income tax for a target year.

    Supported params:
    - year: "previous" (default), "current", or explicit year integer
    - scope: "federal" (default) or "state"
    - filing_status_aux: defaults to "filing_status"
    - bracket_aux: defaults to "federal_brackets" for federal, "state_brackets" for state
    - bracket_series: optional, required when state_brackets has multiple series
    - standard_deduction_aux: defaults to "standard_deduction" (federal) or
      "state_standard_deduction" (state), whose series is bracket_series's
      (state_standard_deduction__CA.csv); with no such file the state falls
      back to "standard_deduction"
    - itemized_deduction_aux: defaults to "federal_deductions" (federal) or
      "state_deductions" (state); the greater of it and the standard deduction
      is used. "state_deductions" falls back to "federal_deductions" when the
      scenario has no "state_deductions" rows at all.
    - ss_threshold_aux: defaults to "ss_taxation_thresholds" (federal only)
    - nol_limit_percent: how much of a year's taxable income a carried net
      operating loss may offset (#231); defaults to 80 (federal, IRC §172) or
      100 (state; California, for one, has no 80% limit)

    ``post_transfer=False`` (used for the trailing-year tax tail; see
    ``run_scenario``) computes and writes ``tax_year_summary`` as normal but
    skips actually moving money -- the tail exists only to let the final
    scenario year's reconciliation compute, not to simulate a real month.
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

    est = _year_tax_estimate(
        con, params, aux=aux, tax_year=tax_year, tid=tid,
        person_lifespan=person_lifespan, taxpayers=taxpayers,
    )
    ordinary_income = est.ordinary_income
    ltcg_income = est.ltcg_income
    ss_income = est.ss_income
    taxable_ss = est.taxable_ss
    business = est.business
    capital = est.capital
    standard_deduction = est.standard_deduction
    itemized_deduction = est.itemized_deduction
    deduction = est.deduction
    nol_in = est.nol_in
    nol_used = est.nol_used
    nol_new = est.nol_new
    taxable_income = est.taxable_income
    tax_liability = est.tax_liability
    tax_with = est.tax_with

    drawdown_base, drawdown_tax = _drawdown_tax(con, tax_year, tax_with)

    paid_transfers = con.execute(
        "SELECT COALESCE(SUM(amount), 0) "
        "FROM transfer_event "
        "WHERE to_lot_id = ? "
        "  AND COALESCE(tax_year, CAST(substr(date, 1, 4) AS INTEGER)) = ?",
        (to_id, tax_year),
    ).fetchone()[0]
    already_paid = int(paid_transfers or 0)
    tax_due = max(0, tax_liability - already_paid)
    tax_overpaid = max(0, already_paid - tax_liability)

    if post_transfer and tax_due > 0:
        assert step_id is not None  # only the post-end tail runs without a step, and it never posts
        if from_id is not None:
            _post_transfer(
                con, balances, account_kinds, step_id, month_s,
                from_id, to_id, tax_due, tax_char, tid, tax_year=tax_year,
                refill_ctx=refill_ctx,
            )
        else:
            # The engine passes these with every chain source.
            assert graph is not None and source_chain_id is not None and account_tax_treatment is not None
            assert account_subkind is not None and account_name is not None
            _chain_draw(
                con, balances, account_kinds, account_tax_treatment,
                account_subkind, account_name, step_id, month_s, graph,
                source_chain_id, tax_due, to_id, reason_kind="rule", reason_id=tid,
                tax_year=tax_year, refill_ctx=refill_ctx,
            )

    computed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary_rows = {
        f"{scope}_ordinary_income": ordinary_income,
        f"{scope}_schedule_c_net": business.net,
        f"{scope}_hobby_expense_disallowed": business.disallowed,
        f"{scope}_nol_used": nol_used,
        f"{scope}_nol_carryover": nol_in - nol_used + nol_new,
        f"{scope}_ltcg_income": ltcg_income,
        f"{scope}_capital_loss_deduction": capital.loss_deduction,
        f"{scope}_capital_loss_carryover": capital.carryover_st + capital.carryover_lt,
        f"{scope}_ss_income": ss_income,
        f"{scope}_taxable_ss": taxable_ss,
        f"{scope}_adjusted_gross_income": ordinary_income + ltcg_income + taxable_ss,
        f"{scope}_standard_deduction": standard_deduction,
        f"{scope}_itemized_deduction": itemized_deduction,
        f"{scope}_deduction_used": deduction,
        f"{scope}_taxable_income": taxable_income,
        f"{scope}_tax_liability": tax_liability,
        f"{scope}_tax_paid": already_paid,
        f"{scope}_tax_due": tax_due,
        f"{scope}_tax_overpaid": tax_overpaid,
    }
    if scope == "federal":
        # IRMAA's MAGI (#253): AGI plus tax-exempt interest, which the model
        # has no character for yet (#265), so it is AGI.
        summary_rows["federal_magi"] = summary_rows["federal_adjusted_gross_income"]
    if drawdown_base is not None:
        summary_rows[f"{scope}_drawdown_base"] = drawdown_base
        summary_rows[f"{scope}_drawdown_tax"] = drawdown_tax

    for bucket, amount in summary_rows.items():
        con.execute(
            "INSERT OR REPLACE INTO tax_year_summary(year, bucket, amount, computed_at) "
            "VALUES (?, ?, ?, ?)",
            (tax_year, bucket, int(amount), computed_at),
        )


FILL_BRACKET_STEP = 100_00
"""How far ``max_marginal_rate`` looks ahead (#252): the tax on the next
$100 converted. Small enough to stop near a bracket edge, large enough that
a whole IRA takes a few thousand steps at most."""


def _fill_bracket_tax_rule(
    tax_rules: Sequence[tuple[str, dict[str, Any]]], params: Mapping[str, Any],
    tid: int, name: str,
) -> dict[str, Any]:
    """The ``tax_accumulator`` rule whose settings a fill_bracket measures
    with: ``tax_rule`` by name, or the one rule of its scope."""
    scope = str(params.get("scope", "federal"))
    wanted = params.get("tax_rule")
    matches = [rule for rule_name, rule in tax_rules
               if str(rule.get("scope", "federal")) == scope and (rule_name == wanted or not wanted)]
    if len(matches) != 1:
        raise EngineError(
            f"fill_bracket id={tid} ({name!r}): needs one {scope} tax_accumulator rule "
            f"to measure with (found {len(matches)}); name it with tax_rule in params_json"
        )
    return {**matches[0], "scope": scope}


def _largest_within(measure: Callable[[int], int], target: int, cap: int) -> int:
    """The largest amount from 0 to ``cap`` whose ``measure`` (never falling
    as the amount grows) is at or under ``target``, by bisection; 0 when even
    nothing is over it."""
    if measure(0) > target:
        return 0
    low, high = 0, cap
    if measure(high) <= target:
        return high
    while high - low > 1:
        middle = (low + high) // 2
        if measure(middle) <= target:
            low = middle
        else:
            high = middle
    return low


def _within_marginal_rate(
    tax_with: Callable[[int, int], int], cliff: Callable[[int], int] | None,
    amount: int, limit: int,
) -> int:
    """How much of ``amount`` to convert, in ``FILL_BRACKET_STEP`` steps from
    zero, while the next step's tax costs no more than ``limit`` (micros) of
    it (#252). ``cliff``, the IRMAA surcharge an amount's MAGI costs two years
    later (#253), jumps at a tier floor, so a step that crosses one isn't
    judged alone: the span from here to where stepping would stop without
    another floor is, cliff included. Crossing pays when its average cost is
    within the limit; otherwise the conversion stops at the floor."""
    def cost(x: int) -> int:
        return tax_with(x, 0) + (cliff(x) if cliff else 0)

    def tax_step_ok(x: int, step: int) -> bool:
        return (tax_with(x + step, 0) - tax_with(x, 0)) * 1_000_000 <= limit * step

    taken = 0
    while taken < amount:
        step = min(FILL_BRACKET_STEP, amount - taken)
        if not tax_step_ok(taken, step):
            break
        if cliff is None or cliff(taken + step) == cliff(taken):
            taken += step
            continue
        # The step crosses a floor: the last amount still under it, then how
        # far the tax alone would let the conversion go before the next floor.
        below = _largest_within(cliff, cliff(taken), taken + step)
        end, crossing = below, True
        while end < amount:
            next_step = min(FILL_BRACKET_STEP, amount - end)
            if not tax_step_ok(end, next_step) or (
                    not crossing and cliff(end + next_step) != cliff(end)):
                break
            end, crossing = end + next_step, False
        if (cost(end) - cost(below)) * 1_000_000 <= limit * (end - below):
            taken = end
        else:
            return below
    return taken


def _apply_fill_bracket(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str, month: date, params: dict[str, Any],
    from_lot: int, to_lot: int, tax_char: str, tid: int, name: str, *,
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    tax_rules: Sequence[tuple[str, dict[str, Any]]],
    inflation: tuple[str, date, str] | None,
    inflation_factor_cache: dict[tuple[str, date, date, str], int],
    person_lifespan: dict[str, str], taxpayers: Collection[str],
    refill_ctx: RefillContext, irmaa: IrmaaContext | None = None,
) -> None:
    """Convert the most from ``from_lot`` (traditional) to ``to_lot`` (Roth)
    that keeps this year's taxable income at or under the rule's target
    (#252): the top of the bracket taxed at ``rate_micros``, measured against
    ordinary taxable income, or ``amount_cents`` grown by its series,
    measured against all taxable income. Taxable income never falls as the
    conversion grows (more of it can make more Social Security taxable), so
    the largest amount under the target is found by bisection. Then
    ``max_amount`` and the lot's balance cap it, and ``max_marginal_rate``
    stops it where the next $100's tax would cost more than that rate.
    ``irmaa_tier`` keeps the year's MAGI within that IRMAA tier (#253), and
    ``max_marginal_rate`` counts the surcharge a tier floor adds two years
    later, spread over the money converted past it."""
    tax_year = month.year
    est = _year_tax_estimate(
        con, _fill_bracket_tax_rule(tax_rules, params, tid, name), aux=aux,
        tax_year=tax_year, tid=tid, person_lifespan=person_lifespan, taxpayers=taxpayers,
    )
    rate = params.get("rate_micros")
    if rate is not None:
        floors = [floor for floor, bracket_rate in est.brackets if bracket_rate == rate]
        above = [floor for floor, _ in est.brackets if floors and floor > floors[0]]
        if not above:
            rates = ", ".join(units.micros_to_percent(r) for _, r in est.brackets[:-1])
            raise EngineError(
                f"fill_bracket id={tid} ({name!r}): no {est.scope} {tax_year} bracket for "
                f"{est.filing_status} is taxed at {units.micros_to_percent(rate)} below the "
                f"top one (brackets with a top: {rates})"
            )
        target, ordinary_only = min(above), True
    else:
        target, ordinary_only = int(params["amount_cents"]), False
        if inflation is not None:
            series, base_date, cadence = inflation
            target = target * _cumulative_inflation_factor(
                aux, series, base_date, month, inflation_factor_cache, cadence=cadence,
            ) // 1_000_000

    def measured(amount: int) -> int:
        taxable, ordinary = est.taxable_with(amount, 0)
        return ordinary if ordinary_only else taxable

    cap = max(0, balances.get(from_lot, 0))
    if (max_amount := params.get("max_amount")) is not None:
        cap = min(cap, units.json_dollars_to_cents(max_amount))
    if measured(0) >= target or cap == 0:
        return
    amount = _largest_within(measured, target, cap)
    # IRMAA (#253): a tier to stay within, and the surcharge a floor costs
    # two years later when the rate limit weighs it (if anyone will pay it).
    magi_est = None
    if irmaa is not None and ("irmaa_tier" in params or (
            "max_marginal_rate" in params and irmaa.rules)):
        # MAGI is federal whatever scope the target is measured in.
        magi_est = est if est.scope == "federal" else _year_tax_estimate(
            con, _fill_bracket_tax_rule(tax_rules, {"scope": "federal"}, tid, name), aux=aux,
            tax_year=tax_year, tid=tid, person_lifespan=person_lifespan, taxpayers=taxpayers,
        )
    if "irmaa_tier" in params and irmaa is not None and magi_est is not None:
        floors = [t.floor for t in irmaa.tiers(tax_year, magi_est.filing_status, tid)
                  if t.tier > int(params["irmaa_tier"])]
        if floors:
            amount = _largest_within(
                lambda x, e=magi_est: e.agi_with(x, 0), min(floors), amount)
    if (max_rate := params.get("max_marginal_rate")) is not None:
        limit = units.percent_to_micros(str(max_rate))
        cliff = None
        if magi_est is not None and irmaa is not None:
            def cliff(x: int, e: _TaxEstimate = magi_est) -> int:
                return irmaa.annual_cost(tax_year, e.agi_with(x, 0), month_s, tid)
        amount = _within_marginal_rate(est.tax_with, cliff, amount, limit)
    event_id = _post_transfer(
        con, balances, account_kinds, step_id, month_s, from_lot, to_lot, amount,
        tax_char, tid, tax_year=tax_year, refill_ctx=refill_ctx,
    )
    if event_id is not None:
        # What the conversion aimed at and reached, so a report or a reader
        # of the run can see why it is the size it is.
        con.execute(
            "UPDATE transfer_event SET meta_json = ? WHERE id = ?",
            (json.dumps({"fill_bracket": {
                "target": target, "measure": "ordinary" if ordinary_only else "taxable",
                "reached": measured(amount)}}), event_id),
        )


class IrmaaTier(NamedTuple):
    """One ``irmaa_tiers`` row for a premium year, grown by its series: the
    tier number, the MAGI it is charged above, and its monthly add-ons."""
    tier: int
    floor: int
    part_b: int
    part_d: int


IRMAA_YEARS_BACK = 2
"""A premium year's IRMAA reads the MAGI on the return from two years
before it (42 USC 1395r(i)(4)(B))."""

IRMAA_PARTS = {"B+D": ("part_b", "part_d"), "B": ("part_b",), "D": ("part_d",)}
"""``params_json`` ``parts`` of an ``irmaa`` rule: the add-ons it charges."""


def _prior_magi(con) -> dict[int, int]:
    """``scenario.meta_json`` ``irmaa.magi`` (#253): the household's MAGI for
    years before the run, in cents, keyed by year."""
    from analysis.sim.units import json_dollars_to_cents
    meta = json.loads(con.execute("SELECT meta_json FROM scenario").fetchone()[0] or "{}")
    magi = (meta.get("irmaa") or {}).get("magi") or {}
    return {int(year): json_dollars_to_cents(amount) for year, amount in magi.items()}


class IrmaaContext:
    """What an ``irmaa`` rule (#253) needs to price a premium year: the aux
    tables, the household's MAGI before the run, and the people whose deaths
    change the filing status. A premium year's tier is the same for every
    rule and month, so it is worked out once."""

    def __init__(
        self, aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
        inflation_factor_cache: dict[tuple[str, date, date, str], int], *,
        start_year: int, prior_magi: Mapping[int, int],
        person_lifespan: Mapping[str, str], taxpayers: Collection[str],
        rules: Sequence[Sequence[Any]] = (),
    ) -> None:
        self.aux = aux
        # The run's irmaa rows, as run_scenario holds them, so a calendar
        # event's change to one's end is seen.
        self.rules = rules
        self.inflation_factor_cache = inflation_factor_cache
        self.start_year = start_year
        self.prior_magi = prior_magi
        self.person_lifespan = dict(person_lifespan)
        self.taxpayers = taxpayers
        self._charged: dict[tuple[int, str], IrmaaTier | None] = {}

    def tiers(self, year: int, filing_status: str, tid: int) -> list[IrmaaTier]:
        """``filing_status``'s tiers for premium ``year``, lowest first: the
        table in force that January, each figure grown from the row's
        effective date by its own series, a grown floor rounded to $1,000
        as the law rounds it. A blank series holds the figure flat."""
        january = date(year, 1, 31)
        rows = [
            r for r in _active_aux_rows(self.aux, registries.IRMAA_TIERS, None, january.isoformat())
            if r.get("filing_status") == filing_status
        ]
        effective = _parse_date(max(
            (e for e, _ in self.aux.get((registries.IRMAA_TIERS, None), [])
             if e <= january.isoformat()), default=january.isoformat()))

        def grown(amount: int, series: object) -> tuple[int, bool]:
            if not series:
                return amount, False
            factor = _cumulative_inflation_factor(
                self.aux, str(series), effective, january, self.inflation_factor_cache)
            return amount * factor // 1_000_000, True

        tiers = []
        for row in rows:
            floor, indexed = grown(int(row["magi_floor"]), row.get("floor_series"))
            if indexed:
                floor = (floor + 50_000) // 100_000 * 100_000
            tiers.append(IrmaaTier(
                int(row["tier"]), floor,
                grown(int(row["part_b"]), row.get("part_b_series"))[0],
                grown(int(row["part_d"]), row.get("part_d_series"))[0],
            ))
        if not tiers:
            raise EngineError(
                f"irmaa id={tid}: no {registries.IRMAA_TIERS} rows for "
                f"filing_status={filing_status!r} effective on or before {january}"
            )
        return sorted(tiers)

    def magi(self, con, year: int, tid: int) -> int:
        """The household's MAGI for tax ``year``: from ``meta_json`` before
        the run (none given is no surcharge, as the loader warned), then the
        federal tax rule's summary."""
        if year < self.start_year:
            return self.prior_magi.get(year, 0)
        row = con.execute(
            "SELECT amount FROM tax_year_summary WHERE year = ? AND bucket = 'federal_magi'",
            (year,),
        ).fetchone()
        if row is None:
            raise EngineError(
                f"irmaa id={tid}: no federal MAGI for {year}; a federal tax_accumulator "
                f"rule must compute {year} before January {year + IRMAA_YEARS_BACK}"
            )
        return int(row[0])

    def filing_status(self, year: int, aux_kind: str, tid: int) -> str:
        """The filing status on tax ``year``'s return, as the tax rule finds
        it; a year before the filing_status table starts takes its first row,
        though a death before ``year`` is still judged against ``year``."""
        table_year = year
        if not _active_aux_rows(self.aux, aux_kind, None, f"{year}-12-31"):
            rows = self.aux.get((aux_kind, None)) or []
            if rows:
                table_year = int(rows[0][0][:4])
        return _active_filing_status(
            self.aux, aux_kind, year, tid,
            person_lifespan=self.person_lifespan, taxpayers=self.taxpayers,
            table_year=table_year,
        )

    def tier(self, con, year: int, aux_kind: str, tid: int) -> IrmaaTier | None:
        """The tier premium ``year`` is charged at, None below the first.
        Records it in ``tax_year_summary`` under the MAGI's year, for a year
        the run computed, as ``federal_irmaa_tier``."""
        key = (year, aux_kind)
        if key not in self._charged:
            magi_year = year - IRMAA_YEARS_BACK
            magi = self.magi(con, magi_year, tid)
            tiers = self.tiers(year, self.filing_status(magi_year, aux_kind, tid), tid)
            charged = None
            for tier in tiers:
                if magi > tier.floor:
                    charged = tier
            self._charged[key] = charged
            if magi_year >= self.start_year:
                con.execute(
                    "INSERT OR REPLACE INTO tax_year_summary(year, bucket, amount, computed_at) "
                    "VALUES (?, 'federal_irmaa_tier', ?, ?)",
                    (magi_year, charged.tier if charged else 0,
                     datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
                )
        return self._charged[key]

    def enrolled(
        self, premium_year: int, month_s: str,
    ) -> list[tuple[tuple[str, ...], str, int]]:
        """The add-ons (``parts``), filing-status table and months charged in
        ``premium_year`` of each irmaa rule that will charge then, as a
        planner sees it in ``month_s``: the months its start and end allow,
        if its person is still alive. A death not yet happened isn't
        foreseen, as a planner can't foresee it."""
        enrolled = []
        for row in self.rules:
            person, start, end, ext = row[8], row[16], row[17], row[13]
            if person in self.person_lifespan and self.person_lifespan[person] < month_s:
                continue
            months = sum(
                1 for m in range(1, 13)
                if start <= (month_end := date(
                    premium_year, m, calendar.monthrange(premium_year, m)[1]).isoformat())
                and (end is None or month_end <= end)
            )
            if not months:
                continue
            params = json.loads(ext) if ext else {}
            enrolled.append((IRMAA_PARTS[str(params.get("parts", "B+D"))],
                             str(params.get("filing_status_aux", "filing_status")), months))
        return enrolled

    def annual_cost(self, year: int, magi: int, month_s: str, tid: int) -> int:
        """What ``magi`` on ``year``'s return costs in surcharges two years
        later, for everyone who will be enrolled then, priced with the table
        in force in ``year`` (what a planner knows when it decides). Each
        rule's tier follows its own filing-status table, as its charge will,
        for the months it will charge."""
        cost = 0
        for parts, status_aux, months in self.enrolled(year + IRMAA_YEARS_BACK, month_s):
            tiers = self.tiers(year, self.filing_status(year, status_aux, tid), tid)
            charged = [t for t in tiers if magi > t.floor]
            if charged:
                cost += months * sum(getattr(charged[-1], part) for part in parts)
        return cost

    def charge(self, con, year: int, params: Mapping[str, Any], tid: int) -> int:
        """One month's surcharge for one enrollee in premium ``year``: the
        add-ons ``params`` ``parts`` names, at the tier MAGI two years back
        set."""
        tier = self.tier(con, year, str(params.get("filing_status_aux", "filing_status")), tid)
        if tier is None:
            return 0
        return sum(getattr(tier, part) for part in IRMAA_PARTS[str(params.get("parts", "B+D"))])


DEFAULT_DRAWDOWN_YEARS = 10
"""Years an after-tax drawdown spreads a traditional balance over (#251):
the inherited-IRA rule's ten (SECURE Act, 2019)."""


def _drawdown_tax(
    con, tax_year: int, tax_with: Callable[[int, int], int],
) -> tuple[int | None, int]:
    """The money ``tax_year``'s after-tax net worth taxes and the extra tax on
    it (#251): its year-end traditional (tax_deferred) balances, and the
    unrealized gain of its taxable lots with a basis unless ``after_tax``
    says ``step_up``, drawn down evenly over ``drawdown_years`` with this
    year's brackets, filing status and other income. ``(None, 0)`` for a year
    the run has no month of (a year-to-date one)."""
    step = con.execute(
        "SELECT id FROM simulation_step WHERE substr(month, 1, 4) = ? "
        "ORDER BY month DESC LIMIT 1", (str(tax_year),),
    ).fetchone()
    if step is None:
        return None, 0
    settings = json.loads(
        con.execute("SELECT meta_json FROM scenario").fetchone()[0] or "{}"
    ).get("after_tax") or {}
    years = int(settings.get("drawdown_years", DEFAULT_DRAWDOWN_YEARS))
    deferred = gains = 0
    for treatment, subkind, balance, basis in con.execute(
        "SELECT m.tax_treatment, a.subkind, ls.balance, ls.cost_basis "
        "FROM lot_state ls JOIN account_lot m ON m.id = ls.lot_id "
        "JOIN account a ON a.id = m.account_id "
        "WHERE ls.step_id = ? AND a.kind = 'asset'", (step[0],),
    ):
        if treatment == "tax_deferred":
            deferred += max(0, balance)
        elif (treatment == "taxable" and basis is not None
              and subkind not in registries.CASH_SUBKINDS):
            gains += max(0, balance - basis)
    if settings.get("step_up"):
        gains = 0
    if deferred + gains == 0:
        return 0, 0
    # Each year's share carries its cent of any remainder, so the shares sum
    # to the whole base; only a few distinct shares need a tax computed.
    (deferred_share, deferred_extra), (gains_share, gains_extra) = (
        divmod(deferred, years), divmod(gains, years))
    shares = Counter(
        (deferred_share + (i < deferred_extra), gains_share + (i < gains_extra))
        for i in range(years))
    base_tax = tax_with(0, 0)
    extra = sum(count * (tax_with(d, g) - base_tax) for (d, g), count in shares.items())
    return deferred + gains, extra


def _tax_year_income_buckets(con, *, tax_year: int) -> tuple[int, int]:
    """A tax year's ordinary income and Social Security benefits. Capital
    gains and losses are ``_capital_gains``'s."""
    rows = con.execute(
        "SELECT tax_character, COALESCE(SUM(amount), 0) "
        "FROM transfer_event "
        "WHERE COALESCE(tax_year, CAST(substr(date, 1, 4) AS INTEGER)) = ? "
        "GROUP BY tax_character",
        (tax_year,),
    ).fetchall()
    by_char = {k: int(v or 0) for k, v in rows}
    ordinary = (
        by_char.get("ordinary", 0)
        + by_char.get("tax_deferred_out", 0)
        + by_char.get("roth_nonqualified", 0)
        - by_char.get("tax_deferred_in", 0)
    )
    return ordinary, by_char.get("ss_benefit", 0)


class BusinessResult(NamedTuple):
    """One tax year's Schedule C result (#231): receipts less deductible
    expenses, a loss negative, and the expenses not deducted because the
    business is a hobby."""
    net: int
    disallowed: int


def _business_result(con, *, tax_year: int) -> BusinessResult:
    """``tax_year``'s ``business_income`` legs into, less its
    ``business_expense`` legs out of, ``business`` accounts. A business whose
    account's meta_json says ``"hobby": true`` isn't engaged in for profit
    (IRC §183): its receipts are still income, but none of its expenses are
    deductible."""
    net = disallowed = 0
    for event_id, character, amount, from_name, from_subkind, from_meta, to_name, to_subkind in con.execute(
        "SELECT te.id, te.tax_character, te.amount, fa.name, fa.subkind, fa.meta_json, "
        "       ta.name, ta.subkind "
        "FROM transfer_event te "
        "JOIN account fa ON fa.id = te.from_account JOIN account ta ON ta.id = te.to_account "
        "WHERE te.tax_character IN (?, ?) "
        "  AND COALESCE(te.tax_year, CAST(substr(te.date, 1, 4) AS INTEGER)) = ?",
        (registries.BUSINESS_INCOME, registries.BUSINESS_EXPENSE, tax_year),
    ):
        income = character == registries.BUSINESS_INCOME
        name, subkind = (to_name, to_subkind) if income else (from_name, from_subkind)
        if subkind != registries.BUSINESS_SUBKIND:
            # The loader checks the rows it can; this catches the rest.
            side = "into" if income else "out of"
            raise EngineError(
                f"transfer_event id={event_id}: {character} must be paid {side} a "
                f"{registries.BUSINESS_SUBKIND} account, not '{name}'"
            )
        if income:
            net += int(amount)
        elif json.loads(from_meta or "{}").get("hobby") is True:
            disallowed += int(amount)
        else:
            net -= int(amount)
    return BusinessResult(net, disallowed)


def _summary_amount(con, year: int, bucket: str) -> int:
    """A ``tax_year_summary`` amount, 0 if that year wasn't computed."""
    row = con.execute(
        "SELECT amount FROM tax_year_summary WHERE year = ? AND bucket = ?", (year, bucket),
    ).fetchone()
    return int(row[0]) if row else 0


class CapitalGains(NamedTuple):
    """One tax year's capital gains after netting (IRC §1211(b), §1212(b)):
    the short- and long-term gains taxed, the loss deducted from ordinary
    income, and the loss carried into the next year, by its character."""
    stcg: int
    ltcg: int
    loss_deduction: int
    carryover_st: int
    carryover_lt: int


def _net_capital(st: int, lt: int, limit: Callable[[], int]) -> CapitalGains:
    """Net a year's short- and long-term results, each already less its own
    carryover: a loss in one offsets a gain in the other, then up to the
    limit of what's left offsets ordinary income, short-term loss first, and
    the rest carries forward keeping its character."""
    if st < 0 < lt or lt < 0 < st:
        net = st + lt
        if net >= 0:
            st, lt = (net, 0) if st > 0 else (0, net)
        else:
            st, lt = (net, 0) if st < 0 else (0, net)
    loss_st, loss_lt = max(0, -st), max(0, -lt)
    deduction = min(limit(), loss_st + loss_lt) if loss_st + loss_lt else 0
    used_st = min(loss_st, deduction)
    return CapitalGains(
        max(0, st), max(0, lt), deduction,
        loss_st - used_st, loss_lt - (deduction - used_st),
    )


def _capital_gains(
    con, *, tax_year: int, loss_limit: Callable[[int], int],
) -> CapitalGains:
    """``tax_year``'s capital gains: its ``stcg``/``ltcg`` legs (a withdrawal at
    a loss posts a negative one) less its ``capital_loss`` records, netted
    with the carryover from every earlier year, each of which used up to its
    own year's ``loss_limit``."""
    year_of = "COALESCE(tax_year, CAST(substr(date, 1, 4) AS INTEGER))"
    totals: dict[int, list[int]] = {}
    for table, sign in (("transfer_event", 1), ("capital_loss", -1)):
        for year, character, amount in con.execute(
            f"SELECT {year_of} AS y, tax_character, COALESCE(SUM(amount), 0) "
            f"FROM {table} WHERE tax_character IN ('stcg', 'ltcg') AND {year_of} <= ? "
            "GROUP BY y, tax_character",
            (tax_year,),
        ):
            totals.setdefault(int(year), [0, 0])[1 if character == "ltcg" else 0] += sign * int(amount)
    carry_st = carry_lt = 0
    result = CapitalGains(0, 0, 0, 0, 0)
    for year in range(min(totals, default=tax_year), tax_year + 1):
        st, lt = totals.get(year, (0, 0))
        result = _net_capital(
            st - carry_st, lt - carry_lt, functools.partial(loss_limit, year),
        )
        carry_st, carry_lt = result.carryover_st, result.carryover_lt
    return result


def _active_filing_status(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    filing_status_aux: str,
    tax_year: int,
    tid: int,
    *, person_lifespan: dict[str, str] | None = None,
    taxpayers: Collection[str] = (),
    table_year: int | None = None,
) -> str:
    """Filing status for ``tax_year``: the filing_status aux table's latest row on
    or before the year's end, except that a joint or separate filer becomes Single
    the year after a taxpayer's ``lifespan.csv`` ``final_month`` (the survivor may
    still file jointly for the year of death). The lifespan alone is enough; the
    aux table needs no row for it. ``table_year`` reads the table as of another
    year (an IRMAA return from before the table starts) without moving the
    death test.
    """
    table_year = tax_year if table_year is None else table_year
    died = any(
        int(final_month[:4]) < tax_year
        for person, final_month in (person_lifespan or {}).items()
        if person in taxpayers
    )
    rows = _active_aux_rows(aux, filing_status_aux, None, f"{table_year}-12-31")
    if not rows:
        if died:
            return "Single"
        raise EngineError(
            f"tax_accumulator id={tid}: no filing_status row effective <= {table_year}-12-31"
        )
    status = rows[0].get("filing_status")
    if not status:
        raise EngineError(
            f"tax_accumulator id={tid}: filing_status payload missing filing_status"
        )
    return "Single" if died and status in ("MFJ", "MFS") else str(status)


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
    cache: dict[tuple[str, date, date, str], int],
    cadence: str = "monthly",
) -> int:
    """ADR-003 §2.1: cumulative inflation factor (micros; 1_000_000 = 1.0x)
    from ``from_date`` (exclusive) through ``to_date`` (inclusive). Returns
    the inverse factor if ``to_date`` precedes ``from_date``.

    ``cadence='monthly'`` (default) compounds every month -- appropriate for
    things that genuinely drift smoothly (rent, salaries). ``cadence='annual'``
    steps once per calendar year crossed (each January), matching how real
    COLA-style adjustments (e.g. Social Security) actually apply: flat within
    a year, then a single jump.
    """
    if to_date == from_date:
        return 1_000_000
    key = (series, from_date, to_date, cadence)
    cached = cache.get(key)
    if cached is not None:
        return cached
    lo, hi = (from_date, to_date) if from_date < to_date else (to_date, from_date)
    factor = 1_000_000
    match cadence:
        case "monthly":
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
        case "annual":
            for year in range(lo.year + 1, hi.year + 1):
                jan_s = f"{year}-01-31"
                payload = _lookup_aux(aux, "inflation", series, jan_s)
                if payload is None:
                    raise EngineError(
                        f"fixed_amount inflation: no aux row for series={series!r} "
                        f"effective <= {jan_s}"
                    )
                annual_micros = int(payload.get("annual_rate_micros", 0))
                factor = factor * (1_000_000 + annual_micros) // 1_000_000
        case _:
            raise EngineError(f"unknown inflation_cadence: {cadence!r}")
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


def _parse_scheduled_date(
    value: str | None, transaction_id: int, name: str, field: str,
) -> date:
    """Parse a scheduled date with enough context to fix its CSV row."""
    try:
        if value is None:
            raise TypeError("no date")
        return _parse_date(value)
    except (TypeError, ValueError) as exc:
        raise EngineError(
            f"scheduled_transaction id={transaction_id} ({name!r}): invalid "
            f"{field} date {value!r}; expected YYYY-MM-DD in scheduled.csv"
        ) from exc


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


def _calendar_tax_character(
    payload: Mapping[str, Any], from_chain: int, to_chain: int, ctx: RefillContext,
) -> str | None:
    """The tax character a ``one_off_transfer`` or ``transfer_balance`` row
    moves its money with (#201). One the row sets is used as written. Left
    blank, money out of a lot that tracks cost basis (a brokerage lot, say)
    is a sale, and money out of a traditional retirement lot is taxed by
    where each leg lands (#217): a rollover into another traditional lot is
    untaxed, anywhere else (a Roth lot, checking) it is ``tax_deferred_out``.
    Money out of a Roth lot that leaves the wrapper is a distribution, under
    the five-year rule. All of these return None, so ``_post_withdrawal``
    decides per leg, as for a sweep out of that lot, even through a
    destination chain. From anywhere else a blank is ``none``, as it is for
    a Roth rollover straight into another retirement lot.
    """
    if character := payload.get("tax_character"):
        return character
    lot = ctx.graph.leaf_lot.get(from_chain)
    if lot is None:
        return "none"
    to_lot = ctx.graph.leaf_lot.get(to_chain)
    if lot in ctx.roth and (to_lot is None or _leaves_roth(ctx, lot, to_lot)):
        # A Roth distribution, or money a chain may carry out of the wrapper:
        # _post_withdrawal applies the five-year rule to each leg that leaves.
        return None
    treatment = ctx.account_tax_treatment.get(lot)
    if treatment == "tax_deferred" or registries.withdrawal_tax_character(
            treatment, ctx.account_subkind.get(lot, "")) is None:
        return None
    return "none"


def _split_rule_character(from_chain: int, ctx: RefillContext) -> str:
    """What the synthesized rule of a calendar row whose legs each decide
    their own character records: traditional money's ``tax_deferred_out``,
    a Roth lot's ``tax_free`` (its basis comes out first), or a sale's first
    leg, ``return_of_capital``."""
    lot = ctx.graph.leaf_lot.get(from_chain)
    if lot is not None and ctx.account_tax_treatment.get(lot) == "tax_deferred":
        return "tax_deferred_out"
    if lot is not None and lot in ctx.roth:
        return "tax_free"
    return "return_of_capital"


def _synthesize_one_off_rule(
    con, name: str, from_id: int, to_id: int, amount: int,
    tax_char: str | None, ev_date: str, ev_id: int, description: str | None,
    split_character: str = "return_of_capital",
) -> int:
    """Insert the cadence='one_off' fixed_amount rule a rule-synthesizing
    calendar event fires through (ADR-003 \u00a73), and return its id.

    A ``tax_char`` of None (each leg decides, see ``_calendar_tax_character``)
    is recorded as ``split_character``; the legs themselves carry the
    characters that are taxed."""
    cur = con.execute(
        "INSERT INTO scheduled_transaction("
        "  name, from_account, to_account, cadence, "
        "  amount_rule_kind, amount_cents, tax_character, start, \"end\", "
        "  priority, created_by_event_id, description"
        ") VALUES (?, ?, ?, 'one_off', 'fixed_amount', ?, ?, ?, ?, "
        "  200, ?, ?)",
        (name, from_id, to_id, amount, tax_char or split_character,
         ev_date, ev_date, ev_id, description),
    )
    return inserted_id(cur)


# Aux kinds a run looks up for months already past: an inflation factor
# compounds from a rule's base_date. Every other kind is only ever read for
# the month being stepped (or later).
_LOOKED_BACK_AUX_KINDS = frozenset({"inflation"})


def _swap_aux_table(
    con, ev_id: int, ev_date: str, payload: dict[str, Any],
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
) -> None:
    """ADR-003 \u00a73.5 (#197): from ``ev_date`` on, every lookup of
    (``aux_kind``, ``series``) sees what a lookup of (``aux_kind``,
    ``source_series``) would. Swapping ``state_brackets`` swaps the state's
    standard deduction with it.

    Lookups only move forward, so the target simply becomes a copy of the
    source -- which keeps each lookup style exact: the latest row, the
    latest date's row set (brackets), and the latest row per key (RMD
    factors, energy credits). An inflation series is also read for past
    months, so it keeps its own rows before ``ev_date``, with the source's
    row in force on ``ev_date`` taking over then; inflation tables have one
    row per date, so that splice is exact too.
    """
    aux_kind = payload.get("aux_kind")
    source_series = payload.get("source_series")
    series = payload.get("series")
    if not aux_kind or not source_series:
        raise EngineError(
            f"swap_aux_table event id={ev_id}: requires params_json aux_kind "
            "and source_series"
        )
    source = aux.get((aux_kind, source_series))
    if not source:
        raise EngineError(
            f"swap_aux_table event id={ev_id}: no aux rows for "
            f"kind={aux_kind!r} series={source_series!r}"
        )
    if source[0][0] > ev_date:
        raise EngineError(
            f"swap_aux_table event id={ev_id}: series {source_series!r} has no "
            f"{aux_kind} row effective on or before {ev_date}, so there is "
            "nothing to swap in yet"
        )
    if aux_kind in _LOOKED_BACK_AUX_KINDS:
        kept = [row for row in aux.get((aux_kind, series), []) if row[0] < ev_date]
        in_force = [(ev_date, row[1]) for row in source if row[0] <= ev_date][-1:]
        written = in_force + [row for row in source if row[0] > ev_date]
        con.execute(
            "DELETE FROM aux_table WHERE kind = ? AND series IS ? AND effective_date >= ?",
            (aux_kind, series, ev_date),
        )
    else:
        kept, written = [], list(source)
        con.execute(
            "DELETE FROM aux_table WHERE kind = ? AND series IS ?", (aux_kind, series),
        )
    # The run file's aux_table follows, so what reads it after the run (CPI-
    # constant dollars) sees the table the run actually used.
    con.executemany(
        "INSERT INTO aux_table(kind, series, effective_date, payload_json, description) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (aux_kind, series, effective, json.dumps(row_payload),
             f"swap_aux_table event id={ev_id} from series {source_series!r}")
            for effective, row_payload in written
        ],
    )
    aux[(aux_kind, series)] = kept + written
    if aux_kind == "state_brackets":
        # A move to another state takes its standard deduction too (#248):
        # the new state's state_standard_deduction__<state>, or, with none,
        # the federal fallback rather than the old state's figure.
        deduction = registries.STATE_STANDARD_DEDUCTION
        if aux.get((deduction, source_series)):
            _swap_aux_table(con, ev_id, ev_date, {**payload, "aux_kind": deduction}, aux)
        elif aux.pop((deduction, series), None):
            con.execute("DELETE FROM aux_table WHERE kind = ? AND series IS ?",
                        (deduction, series))


def _apply_calendar_event(
    con,
    ev_id: int, ev_date: str, ev_kind: str, payload: dict[str, Any],
    txns: list[list[Any]],
    txn_by_name: dict[str, list[Any]],
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    chain_id_by_name: dict[str, int],
    account_kinds: dict[int, str],
    balances: dict[int, int],
    step_id: int, month_s: str,
    *, refill_ctx: RefillContext, vary_distribution: str | None = None,
) -> None:
    """Expand one calendar_event per ADR-003 \u00a73."""
    if vary_distribution is not None:
        raise EngineError(
            f"calendar_event id={ev_id} still has vary_distribution={vary_distribution!r} "
            "set -- an ensemble recipe must be resolved (the 'vary' step) before it can be run"
        )
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
                    "a new amount (scheduled_name, amount)"
                )
            rec = txn_by_name.get(name)
            if rec is None:
                raise EngineError(
                    f"set_amount event id={ev_id}: unknown scheduled_name {name!r}"
                )
            rec[_SCHED_AMOUNT_CENTS] = int(new_amount)
            new_base_date = payload.get("param_date")
            if new_base_date:
                rec[_SCHED_BASE_DATE] = new_base_date
            con.execute(
                "UPDATE scheduled_transaction SET amount_cents = ?, "
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
            rec[_SCHED_END] = ev_date
            con.execute(
                'UPDATE scheduled_transaction SET "end" = ? WHERE id = ?',
                (ev_date, rec[0]),
            )
        case "swap_aux_table":
            _swap_aux_table(con, ev_id, ev_date, payload, aux)
        case "one_off_transfer":
            frm_name = payload.get("account_1")
            to_name = payload.get("account_2")
            amount = payload.get("amount")
            if frm_name is None or to_name is None or amount is None:
                raise EngineError(
                    f"one_off_transfer event id={ev_id}: requires from_account, "
                    "to_account, amount (account_1, account_2, amount)"
                )
            # Either side may be a lot's leaf chain or an interior chain (#98).
            from_id = chain_id_by_name.get(frm_name)
            to_id = chain_id_by_name.get(to_name)
            if from_id is None or to_id is None:
                raise EngineError(
                    f"one_off_transfer event id={ev_id}: unknown account or chain "
                    f"(account_1={frm_name!r} or account_2={to_name!r})"
                )
            tax_char = _calendar_tax_character(payload, from_id, to_id, refill_ctx)
            tid = _synthesize_one_off_rule(
                con, f"oneoff_evt_{ev_id}", from_id, to_id, int(amount),
                tax_char, ev_date, ev_id, payload.get("description"),
                split_character=_split_rule_character(from_id, refill_ctx),
            )
            _post_fixed_amount(
                con, balances, account_kinds, step_id, month_s, refill_ctx,
                from_id, to_id, int(amount), tax_char, tid,
                what=f"one_off_transfer event id={ev_id}",
            )
        case "transfer_balance":
            frm_name = payload.get("account_1")
            to_name = payload.get("account_2")
            if frm_name is None or to_name is None:
                raise EngineError(
                    f"transfer_balance event id={ev_id}: requires from_account and "
                    "to_account (account_1, account_2)"
                )
            from_id = chain_id_by_name.get(frm_name)
            to_id = chain_id_by_name.get(to_name)
            if from_id is None or to_id is None:
                raise EngineError(
                    f"transfer_balance event id={ev_id}: unknown account or chain "
                    f"(account_1={frm_name!r} or account_2={to_name!r})"
                )
            lot = con.execute("SELECT lot_id FROM chain WHERE id = ?", (from_id,)).fetchone()
            if lot is None or lot[0] is None:
                raise EngineError(
                    f"transfer_balance event id={ev_id}: account_1={frm_name!r} is a "
                    "chain; the balance moved must be one lot's"
                )
            # Applied before the month's rules, so this is the previous
            # month-end balance (plus any calendar event earlier this month)
            # and the month's growth accrues at the destination. Nothing to
            # move is a no-op, as for on_death.
            lot_id = lot[0]
            tax_char = _calendar_tax_character(payload, from_id, to_id, refill_ctx)
            amount = max(0, balances.get(lot_id, 0))
            if amount == 0:
                return
            description = con.execute(
                "SELECT description FROM calendar_event WHERE id = ?", (ev_id,),
            ).fetchone()[0]
            # A fixed_amount of the balance, so it routes exactly as the same
            # one_off_transfer would and its transfer_event traces to the row.
            tid = _synthesize_one_off_rule(
                con, f"balance_evt_{ev_id}", from_id, to_id, amount,
                tax_char, ev_date, ev_id, description,
                split_character=_split_rule_character(from_id, refill_ctx),
            )
            # Emptying the lot is the point, so its refill look-ahead must not
            # top it back up to its minimum_balance first: marked as already
            # refilling, the lot is skipped as a refill cycle would be.
            refill_ctx.refilling.add(lot_id)
            try:
                _post_fixed_amount(
                    con, balances, account_kinds, step_id, month_s, refill_ctx,
                    from_id, to_id, amount, tax_char, tid,
                    what=f"transfer_balance event id={ev_id}",
                )
            finally:
                refill_ctx.refilling.discard(lot_id)
        case _:  # pragma: no cover
            raise EngineError(f"supported but unhandled calendar kind: {ev_kind}")
