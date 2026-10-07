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
import json
import warnings
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from analysis.sim import registries
from analysis.sim.runs import finalize_run, open_run_file
from analysis.sim.schema import inserted_id

__all__ = ["run_scenario", "EngineError"]


class EngineError(RuntimeError):
    pass


SUPPORTED_RULE_KINDS = frozenset(
    {"fixed_amount", "percent_of_balance",
    "mortgage_payment", "ss_benefit", "rmd", "tax_accumulator", "on_death",
    "home_sale"}
)

SUPPORTED_CALENDAR_KINDS = frozenset(
    {"set_rate", "set_amount", "retire_scheduled", "one_off_transfer"}
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
            'SELECT start, "end", account_model_id FROM scenario'
        ).fetchone()
        if scenario is None:
            raise EngineError("scenario table is empty in intent snapshot")
        start_s, end_s, model_id = scenario
        months = list(_month_ends(_parse_date(start_s), _parse_date(end_s)))
        if not months:
            raise EngineError(f"scenario range is empty: start={start_s} end={end_s}")

        # The engine's nodes are lots (ADR-005): every dict below is keyed by
        # account_model_member.id, with each lot inheriting its account's kind,
        # subkind, tax treatment, owner and name.
        lots = con.execute(
            "SELECT m.id, m.opening_balance, m.opening_cost_basis, "
            "       m.prior_year_end_balance, a.kind, a.subkind, a.tax_treatment, "
            "       a.owner, m.quantity_micros, a.name, m.acquired "
            "FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id "
            "WHERE m.model_id = ? ORDER BY m.id",
            (model_id,),
        ).fetchall()
        balances: dict[int, int] = {r[0]: r[1] for r in lots}
        cost_bases: dict[int, int | None] = {r[0]: r[2] for r in lots}
        prior_year_end_balances: dict[int, int] = {
            r[0]: r[3] for r in lots if r[3] is not None
        }
        account_kinds: dict[int, str] = {r[0]: r[4] for r in lots}
        account_subkind: dict[int, str] = {r[0]: r[5] for r in lots}
        account_tax_treatment: dict[int, str | None] = {r[0]: r[6] for r in lots}
        account_owner: dict[int, str | None] = {r[0]: r[7] for r in lots}
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
        person_birthdates: dict[str, date] = {
            r[0]: _parse_date(r[1])
            for r in con.execute("SELECT id, birthdate FROM person")
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
                'FROM scheduled_transaction WHERE model_id = ? '
                'ORDER BY priority, id',
                (model_id,),
            ).fetchall()
        ]
        txn_by_name: dict[str, list[Any]] = {
            r[1]: r for r in txns
        }

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
        )

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
            step_id = inserted_id(cur)
            _advance_inflation_indexes(
                con, step_id, month_s, aux, inflation_indexes,
            )

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
                if ev_kind == registries.LATENT_FACTOR_KIND:
                    # ADR-008: a correlation anchor for the ensemble's draws,
                    # with no effect of its own -- not an unsupported kind.
                    applied_event_ids.add(ev_id)
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
                    txns, txn_by_name, aux, chain_id_by_name, account_kinds,
                    balances, step_id, month_s, model_id, refill_ctx=refill_ctx,
                    vary_distribution=ev_vary_distribution,
                )
                con.execute(
                    "INSERT INTO applied_calendar_event(step_id, calendar_event_id) "
                    "VALUES (?, ?)",
                    (step_id, ev_id),
                )
                applied_event_ids.add(ev_id)

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
                # percent_of_balance is asset growth (ADR-003 §2.2: "an asset
                # account's growth is a property of the account"), never a
                # personal service tied to the living owner -- an IRA/checking
                # balance keeps earning interest/appreciating after its owner's
                # death, so it's exempt from lifespan gating. on_death is the
                # rule kind that FIRES on the death transition itself, so it
                # must not be suppressed by the very gating it triggers.
                if (
                    person_lifespan and rule_kind not in ("percent_of_balance", "on_death")
                    and _past_person_lifespan(
                        month_s, p_person, from_account or frm, to, account_owner, person_lifespan,
                    )
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
                            tax_char, tid, subject_lots=lots_by_account[subject],
                            graph=chain_graph, source_chain_id=frm,
                            account_tax_treatment=account_tax_treatment,
                            account_subkind=account_subkind, account_name=account_name,
                            chain_context={
                                "aux": aux, "chain_base_date": _parse_date(start_s),
                                "inflation_factor_cache": inflation_factor_cache,
                            },
                            refill_ctx=refill_ctx,
                        )
                    case "on_death":
                        _apply_on_death(
                            con, balances, account_kinds, step_id, month_s,
                            params, from_account, to, person_lifespan,
                            tax_char, tid, refill_ctx=refill_ctx,
                        )
                    case "home_sale":
                        _apply_home_sale(
                            con, balances, account_kinds, cost_bases, step_id, month_s,
                            params, p_amount_cents, from_account, to, tid, aux,
                            tax_year=tax_year, refill_ctx=refill_ctx,
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
                    case _:  # pragma: no cover
                        raise EngineError(f"supported but unhandled rule_kind: {rule_kind}")

            _settle_accounts(
                con, balances, account_kinds, account_subkind, account_tax_treatment,
                account_name, step_id, month_s, chain_graph, settle_order,
                aux=aux, chain_base_date=_parse_date(start_s),
                inflation_factor_cache=inflation_factor_cache, refill_ctx=refill_ctx,
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
                    person_lifespan=person_lifespan,
                )

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
        if refill_ctx.negative_events:
            con.execute(
                "UPDATE simulation_run SET run_args_json = json_set("
                "  run_args_json, '$.negative_balance_warnings', json(?)) WHERE id = 1",
                (json.dumps(
                    [{"month": m, "account": n, "balance_cents": b}
                     for m, n, b in refill_ctx.negative_events],
                ),),
            )

        finalize_run(con, status="succeeded")
    except Exception as e:
        finalize_run(con, status="failed", failure_reason=f"{type(e).__name__}: {e}")
        raise
    finally:
        con.close()
    return Path(run_path)


class RefillContext:
    """Everything ``_post_transfer`` needs to look-ahead refill a debited
    account and to warn once when an asset actually goes negative (ADR-003
    §2.9). Built once per run in ``run_scenario`` and threaded through.
    """

    __slots__ = ("graph", "aux", "chain_base_date", "inflation_factor_cache",
                 "account_tax_treatment", "account_subkind", "account_name",
                 "cost_bases", "quantities", "growth_sources", "acquired",
                 "refilling", "negative_since", "negative_events")

    def __init__(
        self, graph: ChainGraph, aux, chain_base_date: date,
        inflation_factor_cache: dict,
        account_tax_treatment: Mapping[int, str | None],
        account_subkind: dict[int, str], account_name: dict[int, str],
        cost_bases: dict[int, int | None],
        quantities: dict[int, int | None] | None = None,
        growth_sources: frozenset[int] = frozenset(),
        acquired: dict[int, date | None] | None = None,
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
        self.refilling: set[int] = set()  # reentrancy guard against refill cycles
        self.negative_since: dict[int, bool] = {}
        self.negative_events: list[tuple[str, str, int]] = []


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
    if refill_chain is None or account_id in refill_ctx.refilling:
        return None
    minimum = graph.minimum_of.get(account_id, 0)
    projected = balances.get(account_id, 0) - debit_amount
    if projected >= minimum:
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
        _chain_draw(
            con, balances, account_kinds,
            refill_ctx.account_tax_treatment, refill_ctx.account_subkind,
            refill_ctx.account_name, step_id, month_s, graph,
            refill_chain, needed, account_id,
            refill_ctx=refill_ctx,
        )
    finally:
        refill_ctx.refilling.discard(account_id)
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
    """
    if account_kinds.get(account_id) != "asset":
        return
    is_negative = balances.get(account_id, 0) < 0
    was_negative = refill_ctx.negative_since.get(account_id, False)
    if is_negative and not was_negative:
        name = refill_ctx.account_name.get(account_id, str(account_id))
        warnings.warn(
            f"{month_s}: account {name!r} went negative "
            f"(balance={balances[account_id] / 100:.2f}) after exhausting its refill chain",
            RuntimeWarning,
            stacklevel=2,
        )
        refill_ctx.negative_events.append((month_s, name, balances[account_id]))
    refill_ctx.negative_since[account_id] = is_negative


def _post_transfer(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str,
    from_id: int, to_id: int, amount: int, tax_char: str, reason_id: int,
    parent_event_id: int | None = None, *, reason_kind: str = "rule",
    tax_year: int | None = None, refill_ctx: RefillContext | None = None,
    requested: int | None = None, remainder: int | None = None,
    refill_mark: int | None = None,
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
    this event along with any this call's own look-ahead draws.
    """
    if amount == 0:
        return None
    if requested is None:
        requested = amount
    if account_kinds[to_id] == "liability":
        amount = min(amount, max(0, balances.get(to_id, 0)))
        if amount == 0:
            return None
    if remainder is None:
        remainder = requested - amount
    if refill_ctx is not None and amount > 0:
        mark = _ensure_min_balance(
            con, balances, account_kinds, step_id, month_s, refill_ctx,
            from_id, amount,
        )
        if refill_mark is None:
            refill_mark = mark
    cur = con.execute(
        "INSERT INTO transfer_event("
        "  step_id, date, from_account, to_account, from_lot_id, to_lot_id, "
        "  amount, requested_cents, remainder_cents, tax_character, tax_year, "
        "  reason_kind, reason_id, parent_event_id"
        ") VALUES (?, ?, "
        "  (SELECT account_id FROM account_model_member WHERE id = ?), "
        "  (SELECT account_id FROM account_model_member WHERE id = ?), "
        "  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (step_id, month_s, from_id, to_id, from_id, to_id, amount, requested,
         remainder, tax_char, tax_year, reason_kind, reason_id, parent_event_id),
    )
    if refill_ctx is not None and amount > 0 and account_kinds[from_id] == "asset":
        quantity = refill_ctx.quantities.get(from_id)
        prior_balance = balances.get(from_id, 0)
        if quantity and prior_balance > 0:
            refill_ctx.quantities[from_id] = quantity - min(
                quantity, quantity * amount // prior_balance
            )
    if refill_ctx is not None and amount > 0 and account_kinds[to_id] == "asset":
        _record_purchase(refill_ctx, balances, from_id, to_id, amount)
    balances[from_id] = balances.get(from_id, 0) + _delta(account_kinds[from_id], -amount)
    balances[to_id] = balances.get(to_id, 0) + _delta(account_kinds[to_id], +amount)
    if refill_ctx is not None:
        _warn_if_negative(refill_ctx, account_kinds, from_id, balances, month_s)
    event_id = inserted_id(cur)
    _link_refills(con, refill_mark, event_id)
    return event_id


def _post_fixed_amount(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    step_id: int, month_s: str, ctx: RefillContext,
    from_chain: int, to_chain: int, amount: int, tax_char: str, tid: int,
    *, tax_year: int | None = None, what: str,
) -> None:
    """Move a fixed ``amount`` between two chains (ADR-003 §4.7): lot to lot
    directly, out of a lot through an interior chain's sweep, or into a lot
    through an interior chain's draw. Shared by fixed_amount rules and
    one_off_transfer events, so the two route a chain alike."""
    graph = ctx.graph
    from_lot, to_lot = graph.leaf_lot.get(from_chain), graph.leaf_lot.get(to_chain)
    if from_lot is not None and to_lot is not None:
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
        "  (SELECT account_id FROM account_model_member WHERE id = ?), "
        "  (SELECT account_id FROM account_model_member WHERE id = ?), "
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


def _mortgage_ytd_split(
    ytd_total: int,
    monthly_payment: int | None,
    annual_micros: int | None,
    starting_balance: int,
    tid: int,
) -> tuple[int, int]:
    if monthly_payment is None or annual_micros is None:
        raise EngineError(
            f"mortgage_payment id={tid}: YTD split requires amount and rate_micros"
        )
    remaining_ytd = ytd_total
    outstanding = max(0, starting_balance)
    interest_total = 0
    principal_total = 0
    while remaining_ytd > 0 and outstanding > 0:
        payment = min(int(monthly_payment), remaining_ytd)
        interest = min(payment, (outstanding * int(annual_micros)) // (12 * 1_000_000))
        principal = min(payment - interest, outstanding)
        interest_total += interest
        principal_total += principal
        outstanding -= principal
        remaining_ytd -= payment
    return interest_total, principal_total


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
            if rule_kind == "mortgage_payment" and ytd_amount is not None:
                if account_id_by_name is None or balances is None:
                    raise EngineError(
                        f"scheduled_transaction id={tid}: mortgage YTD requires account context"
                    )
                interest_id = account_id_by_name.get(interest_acct or "")
                if interest_id is None:
                    raise EngineError(
                        f"scheduled_transaction id={tid}: unknown interest_account {interest_acct!r}"
                    )
                starting_balance = (
                    (prior_year_end_balances or {}).get(to_account)
                    or balances.get(to_account, 0)
                )
                interest, principal = _mortgage_ytd_split(
                    posts[0][1], amount_cents, rate_micros, starting_balance, tid,
                )
                parent_event_id = _post_ytd_transfer(
                    con, step_id, date_s, from_account, interest_id, interest,
                    tax_char, "ytd_rule", tid,
                )
                _post_ytd_transfer(
                    con, step_id, date_s, from_account, to_account, principal,
                    tax_char, "ytd_rule", tid,
                    parent_event_id=parent_event_id,
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
    for draw_into_id, step_maximum, step_percent, _series, _cadence in graph.members.get(chain_id, []):
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
    month_s: str,
    p_person: str | None,
    from_id: int,
    to_id: int | None,
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
    an integer age -- ``home_sale_exclusion`` and ``energy_credits`` both
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


def _apply_home_sale(
    con, balances: dict[int, int], account_kinds: dict[int, str],
    cost_bases: dict[int, int | None], step_id: int, month_s: str,
    params: dict[str, Any], amount_cents: int | None,
    from_id: int | None, to_id: int | None, tid: int,
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    *, tax_year: int | None = None, refill_ctx: RefillContext | None = None,
) -> None:
    """One-time full(-ish) liquidation of a cost-basis-tracked asset --
    written for ``real_estate``, but not restricted to it -- applying the
    IRC §121 home-sale exclusion. Reuses ``_post_withdrawal``'s
    return-of-capital/gain split (ADR-001), but where that function's
    brokerage case only ever produces ``return_of_capital`` + ``ltcg``, this
    one can produce a third leg:

      - ``return_of_capital``: sale proceeds up to cost basis (not gain)
      - ``excluded_gain``:     gain covered by the §121 exclusion (real
                                gain, but not taxed)
      - ``ltcg``:              any gain above the exclusion (taxed)

    ``amount_cents`` is the sale price (the row's own amount_cents column).
    The exclusion amount comes from ``params["exclusion_cap_cents"]`` if
    given directly, else from the ``home_sale_exclusion`` aux table keyed
    by ``params["filing_status"]`` (mirrors ``standard_deduction``'s
    shape). One row's tax_character is authored as 'none' (enforced by
    loader validation) since the real split is computed here, not read off
    the row.

    Does NOT model §121 eligibility (ownership/use tests, once-per-2-years,
    partial exclusion for hardship) or the surviving-spouse $500k-within-
    2-years rule under §121(b)(4) -- pass an explicit
    ``exclusion_cap_cents`` override for scenarios needing those, rather
    than teaching this function the full statute for a one-time event.

    Unlike ``_post_withdrawal``, this always liquidates the account's full
    balance -- there's no partial-sale case for a house in this model.
    """
    if from_id is None or to_id is None:
        raise EngineError(
            f"home_sale id={tid}: from_account and to_account must both be "
            "concrete accounts, not interior chains"
        )
    if amount_cents is None:
        raise EngineError(f"home_sale id={tid}: missing amount (sale price)")
    basis = cost_bases.get(from_id)
    if basis is None:
        raise EngineError(
            f"home_sale id={tid}: requires cost-basis tracking, but "
            "opening_cost_basis was never set -- add it to account_values.csv"
        )

    sale_price = int(amount_cents)
    basis_out = min(basis, sale_price)
    gain = max(0, sale_price - basis)

    exclusion_cap_param = params.get("exclusion_cap_cents")
    if exclusion_cap_param is not None:
        exclusion_cap = int(exclusion_cap_param)
    else:
        filing_status = params.get("filing_status")
        if not filing_status:
            raise EngineError(
                f"home_sale id={tid}: requires filing_status (or an "
                "explicit exclusion_cap_cents override)"
            )
        row = _lookup_by_series_key(
            aux, "home_sale_exclusion", "filing_status", filing_status, month_s,
        )
        if row is None:
            raise EngineError(
                f"home_sale id={tid}: no home_sale_exclusion row for "
                f"filing_status={filing_status!r} effective on or before {month_s}"
            )
        exclusion_cap = int(row["amount"])

    excluded = min(gain, exclusion_cap)
    taxable_gain = gain - excluded

    parent_id: int | None = None
    if basis_out > 0:
        parent_id = _post_transfer(
            con, balances, account_kinds, step_id, month_s, from_id, to_id,
            basis_out, "return_of_capital", tid,
            tax_year=tax_year, refill_ctx=refill_ctx,
        )
    if excluded > 0:
        leg_id = _post_transfer(
            con, balances, account_kinds, step_id, month_s, from_id, to_id,
            excluded, "excluded_gain", tid, parent_event_id=parent_id,
            tax_year=tax_year, refill_ctx=refill_ctx,
        )
        parent_id = leg_id if leg_id is not None else parent_id
    if taxable_gain > 0:
        _post_transfer(
            con, balances, account_kinds, step_id, month_s, from_id, to_id,
            taxable_gain, "ltcg", tid, parent_event_id=parent_id,
            tax_year=tax_year, refill_ctx=refill_ctx,
        )
    cost_bases[from_id] = basis - basis_out


type ChainStep = tuple[int, int | None, int, str | None, str | None]
"""One ``chain_steps`` row: ``(draw_from_chain_id, maximum, percent, series, cadence)``."""


class ChainGraph:
    """Resolved ``chain``/``chain_steps`` reference graph (ADR-003 §4.7).

    Each ``members`` entry is a ``(draw_from_chain_id, maximum, percent,
    series, cadence)`` tuple; callers (tests especially) may pass a shorter
    tuple and have it padded with the same defaults the loader writes
    (``maximum=None``, ``percent=100%``, ``series=None``, ``cadence=None``).
    """

    __slots__ = ("members", "leaf_lot", "minimum_of", "refill_chain_of",
                 "refill_target_of", "refill_amount_of",
                 "maximum_of", "overflow_chain_of")

    _STEP_DEFAULTS: tuple = (None, 1_000_000, None, None)

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
    for chain_id, draw_from_id, maximum, percent, series, cadence in con.execute(
        "SELECT chain_id, draw_from_chain_id, "
        "       maximum_cents, percent_micros, series, cadence "
        "FROM chain_steps ORDER BY chain_id, step_order"
    ):
        members.setdefault(chain_id, []).append(
            (draw_from_id, maximum, percent, series, cadence)
        )
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
        "FROM account_model_member"
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
    """
    tax_char = tax_character or registries.withdrawal_tax_character(
        account_tax_treatment.get(from_id), account_subkind.get(from_id, ""),
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
            "add it to account_values.csv"
        )
    prior_balance = balances.get(from_id, 0)
    basis_out = 0 if prior_balance <= 0 else min(basis, basis * amount // prior_balance)
    gain = amount - basis_out
    basis_leg_id = _post_transfer(
        con, balances, account_kinds, step_id, month_s, from_id, to_id, basis_out,
        "return_of_capital", reason_id, parent_event_id, reason_kind=reason_kind,
        tax_year=tax_year, refill_ctx=refill_ctx,
        requested=requested, remainder=remainder, refill_mark=refill_mark,
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
        requested=requested, remainder=remainder,
        refill_mark=refill_mark if basis_leg_id is None else None,
    )
    cost_bases[from_id] = basis - basis_out


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
    ``requested`` rather than its own shrunk-down allocation.
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
    for draw_from_id, step_maximum, step_percent, step_series, step_cadence in graph.members.get(chain_id, []):
        remaining = amount_needed - total
        if remaining <= 0:
            break
        allocation = _chain_step_allocation(
            remaining, step_maximum, step_percent, step_series, step_cadence,
            month_s, aux, chain_base_date, inflation_factor_cache,
        )
        if allocation <= 0:
            continue
        total += _chain_draw(
            con, balances, account_kinds, account_tax_treatment, account_subkind,
            account_name, step_id, month_s, graph,
            draw_from_id, allocation, target_account_id,
            refill_ctx=refill_ctx, reason_kind=reason_kind,
            reason_id=reason_id, tax_year=tax_year,
            aux=aux, chain_base_date=chain_base_date,
            inflation_factor_cache=inflation_factor_cache,
            _progress=_progress,
        )
    return total


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
) -> int:
    """Recursively sweep up to ``amount_available`` out of ``source_account_id``.

    Each leg is an ``overflow`` traced to the source lot, unless a rule's
    sweep passes its own ``reason_kind='rule'`` and ``reason_id``.

    Mirrors ``_chain_draw``, including ``_progress`` tracking so every leg
    reports the original ``amount_available`` as ``requested``: each
    destination's own ``maximum_balance_cents`` caps how much it accepts, and
    any remainder falls through to the next step.
    """
    if amount_available <= 0:
        return 0
    if _progress is None:
        _progress = _ChainProgress(amount_available)
    account_id = graph.leaf_lot.get(chain_id)
    if account_id is not None:
        if account_id == source_account_id:
            return 0
        cap = graph.maximum_of.get(account_id)
        room = (
            amount_available if cap is None
            else max(0, cap - max(0, balances.get(account_id, 0)))
        )
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
    for draw_into_id, step_maximum, step_percent, step_series, step_cadence in graph.members.get(chain_id, []):
        remaining = amount_available - total
        if remaining <= 0:
            break
        allocation = _chain_step_allocation(
            remaining, step_maximum, step_percent, step_series, step_cadence,
            month_s, aux, chain_base_date, inflation_factor_cache,
        )
        if allocation <= 0:
            continue
        total += _chain_sweep(
            con, balances, account_kinds, account_tax_treatment, account_subkind,
            account_name, step_id, month_s, graph,
            draw_into_id, allocation, source_account_id,
            aux=aux, chain_base_date=chain_base_date,
            inflation_factor_cache=inflation_factor_cache,
            source_tax_character=source_tax_character,
            source_tax_year=source_tax_year, refill_ctx=refill_ctx,
            reason_kind=reason_kind, reason_id=reason_id,
            _progress=_progress,
        )
    return total


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
    - standard_deduction_aux: defaults to "standard_deduction"
    - itemized_deduction_aux: defaults to "federal_deductions" (federal) or
      "state_deductions" (state); the greater of it and the standard deduction
      is used. "state_deductions" falls back to "federal_deductions" when the
      scenario has no "state_deductions" rows at all.
    - ss_threshold_aux: defaults to "ss_taxation_thresholds" (federal only)

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

    filing_status_aux = str(params.get("filing_status_aux", "filing_status"))
    standard_deduction_aux = str(params.get("standard_deduction_aux", "standard_deduction"))
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

    taxable_income = max(0, ordinary_income + ltcg_income + taxable_ss - deduction)

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
    ltcg_rows = [
        r for r in _active_aux_rows(aux, ltcg_bracket_aux, None, as_of)
        if r.get("filing_status") == filing_status
    ] if scope == "federal" else []
    if ltcg_rows and ltcg_income > 0:
        # Long-term gains sit on top of ordinary taxable income and are taxed at
        # the preferential rates for the band each part lands in.
        ltcg_brackets = sorted(
            (int(r["floor"]), int(r["marginal_rate_micros"])) for r in ltcg_rows
        )
        gain_taxable = min(ltcg_income, taxable_income)
        ordinary_taxable = taxable_income - gain_taxable
        tax_liability = (
            _progressive_tax_cents(ordinary_taxable, brackets)
            + _progressive_tax_cents(taxable_income, ltcg_brackets)
            - _progressive_tax_cents(ordinary_taxable, ltcg_brackets)
        )

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
        f"{scope}_ltcg_income": ltcg_income,
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
        "WHERE COALESCE(tax_year, CAST(substr(date, 1, 4) AS INTEGER)) = ? "
        "GROUP BY tax_character",
        (tax_year,),
    ).fetchall()
    by_char = {k: int(v or 0) for k, v in rows}
    ordinary = (
        by_char.get("ordinary", 0)
        + by_char.get("stcg", 0)
        + by_char.get("tax_deferred_out", 0)
        - by_char.get("tax_deferred_in", 0)
    )
    ltcg = by_char.get("ltcg", 0)
    ss = by_char.get("ss_benefit", 0)
    return ordinary, ltcg, ss


def _active_filing_status(
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    filing_status_aux: str,
    tax_year: int,
    tid: int,
    *, person_lifespan: dict[str, str] | None = None,
    taxpayers: Collection[str] = (),
) -> str:
    """Filing status for ``tax_year``: the filing_status aux table's latest row on
    or before the year's end, except that a joint or separate filer becomes Single
    the year after a taxpayer's ``lifespan.csv`` ``final_month`` (the survivor may
    still file jointly for the year of death). The lifespan alone is enough; the
    aux table needs no row for it.
    """
    died = any(
        int(final_month[:4]) < tax_year
        for person, final_month in (person_lifespan or {}).items()
        if person in taxpayers
    )
    rows = _active_aux_rows(aux, filing_status_aux, None, f"{tax_year}-12-31")
    if not rows:
        if died:
            return "Single"
        raise EngineError(
            f"tax_accumulator id={tid}: no filing_status row effective <= {tax_year}-12-31"
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


def _apply_calendar_event(
    con,
    ev_id: int, ev_date: str, ev_kind: str, payload: dict[str, Any],
    txns: list[list[Any]],
    txn_by_name: dict[str, list[Any]],
    aux: dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]],
    chain_id_by_name: dict[str, int],
    account_kinds: dict[int, str],
    balances: dict[int, int],
    step_id: int, month_s: str, model_id: int,
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
            # Either side may be a lot's leaf chain or an interior chain (#98).
            from_id = chain_id_by_name.get(frm_name)
            to_id = chain_id_by_name.get(to_name)
            if from_id is None or to_id is None:
                raise EngineError(
                    f"one_off_transfer event id={ev_id}: unknown account or chain "
                    f"(account_1={frm_name!r} or account_2={to_name!r})"
                )
            cur = con.execute(
                "INSERT INTO scheduled_transaction("
                "  model_id, name, from_account, to_account, cadence, "
                "  amount_rule_kind, amount_cents, tax_character, start, \"end\", "
                "  priority, created_by_event_id, description"
                ") VALUES (?, ?, ?, ?, 'one_off', 'fixed_amount', ?, ?, ?, ?, "
                "  200, ?, ?)",
                (model_id, f"oneoff_evt_{ev_id}", from_id, to_id,
                 int(amount), tax_char, ev_date, ev_date, ev_id,
                 payload.get("description")),
            )
            _post_fixed_amount(
                con, balances, account_kinds, step_id, month_s, refill_ctx,
                from_id, to_id, int(amount), tax_char, inserted_id(cur),
                what=f"one_off_transfer event id={ev_id}",
            )
        case _:  # pragma: no cover
            raise EngineError(f"supported but unhandled calendar kind: {ev_kind}")
