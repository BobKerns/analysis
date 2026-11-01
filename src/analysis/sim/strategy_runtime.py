"""The engine's side of responsive strategies (#326, ADR-010 §3).

Built once per run from the intent: the scenario's strategies, instantiated
from their kinds (built-in, or imported from the intent's
``strategy_source`` snapshot), and what a :class:`~analysis.sim.strategy_api.Context`
needs to show them. The engine asks :meth:`StrategyRuntime.decide` at a hook,
posts the moves itself, and records the decision.

A strategy sees the lots' balances as they stand at the moment it's asked
(a draw is decided from that month's current state, Bob 2026-10-21), and
nothing from later months: future rates and calendar rows aren't in its view.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Collection, Iterator, Mapping
from datetime import date
from types import MappingProxyType
from typing import Any, NamedTuple

import numpy as np

from analysis.sim import guard_rails, registries
from analysis.sim.asset_classes import class_weights, weight_sets, weights_at
from analysis.sim.measures import RailYears
from analysis.sim.strategy_api import (
    BreachView, ChainView, Context, Decision, History, LotRef, LotView, PersonView, Put, Realize,
    RuleView, Spend, StepView, Strategy, StrategyError, Take, TaxView, Transfer,
)
from analysis.sim.types import DollarMode
from analysis.sim.strategy_code import StrategyKinds

__all__ = ["Owned", "StrategyRuntime"]


class Owned(NamedTuple):
    """One strategy row, ready to ask."""

    id: int
    name: str
    kind: str
    strategy: Strategy
    chain_id: int | None
    role: str
    start: str | None
    end: str | None
    rules: tuple[int, ...] = ()
    """The scheduled rules it owns by tag (#387)."""

    def active(self, month_s: str) -> bool:
        return not (self.start and month_s < self.start) and not (self.end and month_s > self.end)


class _LotInfo(NamedTuple):
    ref: LotRef
    kind: str
    subkind: str
    lot_subkind: str | None
    tax_treatment: str | None
    owner: str | None
    tags: frozenset[str]
    symbol: str | None


class _Lots(Mapping[LotRef, LotView]):
    """``Context.lots`` for one call: each lot's view is made the first time
    it is read, so a strategy pays only for the lots it looks at."""

    def __init__(self, refs: Mapping[LotRef, int], make: Callable[[int], LotView]) -> None:
        self._refs = refs
        self._make = make
        self._views: dict[LotRef, LotView] = {}

    def __getitem__(self, ref: LotRef) -> LotView:
        view = self._views.get(ref)
        if view is None:
            view = self._views[ref] = self._make(self._refs[ref])
        return view

    def __iter__(self) -> Iterator[LotRef]:
        return iter(self._refs)

    def __len__(self) -> int:
        return len(self._refs)

    def __contains__(self, ref: object) -> bool:
        return ref in self._refs


class StrategyRuntime:
    """Everything the engine needs to ask a run's strategies."""

    def __init__(
        self, con: sqlite3.Connection, *,
        balances: Mapping[int, int],
        cost_bases: Mapping[int, int | None],
        minimum_of: Mapping[int, int],
        leaf_lot: Mapping[int, int],
        members: Mapping[int, list[tuple[Any, ...]]],
        roth_qualified: Callable[[int, date], bool | None],
        end: date | None = None,
        rule_budget: Callable[[int, date], int] | None = None,
        growth_sources: frozenset[int] = frozenset(),
        opening_balances: Mapping[int, int] | None = None,
        first_month: date | None = None,
        acquired: Mapping[int, date | None] | None = None,
        roth_lots: Collection[int] = (),
    ) -> None:
        rows = con.execute(
            'SELECT id, name, kind, chain_id, rule_tag, start, "end", params_json '
            "FROM strategy ORDER BY id"
        ).fetchall()
        self.owned: dict[int, Owned] = {}
        """Strategies by the chain they own."""
        self.by_rule: dict[int, Owned] = {}
        """Strategies by the scheduled rule they own."""
        self.strategies: list[Owned] = []
        self.memory: dict[int, dict[str, Any]] = {}
        self.tax: Any = None
        """The engine's ``_StrategyTax``, which ``ctx.tax`` reads (#421)."""
        if not rows:
            return  # nothing to ask: skip building the views
        self._con = con
        self._end = end
        self._rule_budget = rule_budget
        self._growth_sources = growth_sources
        self._opening = dict(opening_balances or balances)
        self._first_month = first_month
        # The CPI index at each month end so far; 1.0 before the first month.
        self._cpi_index: dict[date, int] = {}
        self._started: set[int] = set()
        # ctx.people, made once a month: it changes only with the month.
        self._people_month: tuple[date, Mapping[str, PersonView]] | None = None
        self._people = [
            (pid, role or None, date.fromisoformat(birth) if birth else None)
            for pid, role, birth in con.execute("SELECT id, role, birthdate FROM person ORDER BY id")
        ]
        self._final_month = {
            person: date.fromisoformat(final)
            for person, final in con.execute("SELECT person, final_month FROM person_lifespan")
        }
        self._rules: dict[int, tuple[str, str, int | None, int | None, int, str | None]] = {
            tid: (name, kind, frm, to, amount or 0, series)
            for tid, name, kind, frm, to, amount, series in con.execute(
                "SELECT id, name, amount_rule_kind, from_account, to_account, amount_cents, series "
                "FROM scheduled_transaction"
            )
        }
        self._rule_tags: dict[int, frozenset[str]] = {}
        for tid, role, name in con.execute(
            "SELECT st.scheduled_transaction_id, t.role, t.name FROM scheduled_transaction_tag st "
            "JOIN tag t ON t.id = st.tag_id"
        ):
            self._rule_tags[tid] = self._rule_tags.get(tid, frozenset()) | {f"{role}:{name}"}
        self._rule_views: dict[int, RuleView] = {}
        self._balances = balances
        self._cost_bases = cost_bases
        self._minimum_of = minimum_of
        self._leaf_lot = leaf_lot
        self._members = members
        self._roth_qualified = roth_qualified
        self._acquired = acquired if acquired is not None else {}
        self._roth_lots = frozenset(roth_lots)
        self.chain_name: dict[int, str] = dict(con.execute("SELECT id, name FROM chain").fetchall())
        self.chain_role: dict[int, str | None] = dict(con.execute("SELECT id, role FROM chain").fetchall())
        self.leaf_chain_of: dict[int, int] = {}
        for chain_id, lot_id in sorted(leaf_lot.items()):
            self.leaf_chain_of.setdefault(lot_id, chain_id)
        tags: dict[int, set[str]] = {}
        for account_id, role, name in con.execute(
            "SELECT at.account_id, t.role, t.name FROM account_tag at JOIN tag t ON t.id = at.tag_id"
        ):
            tags.setdefault(account_id, set()).add(f"{role}:{name}")
        self.lots: dict[int, _LotInfo] = {}
        self.lot_id: dict[LotRef, int] = {}
        for lot_id, account_id, account, lot, kind, subkind, lot_subkind, treatment, owner, symbol in con.execute(
            "SELECT m.id, a.id, a.name, m.lot, a.kind, a.subkind, m.subkind, m.tax_treatment, "
            "       a.owner, m.symbol FROM account_lot m JOIN account a ON a.id = m.account_id "
            "ORDER BY m.id"
        ):
            ref = LotRef(account, lot)
            self.lots[lot_id] = _LotInfo(
                ref, kind, subkind, lot_subkind, treatment, owner,
                frozenset(tags.get(account_id, ())), symbol or None,
            )
            self.lot_id[ref] = lot_id
        self._sets = weight_sets(
            (symbol, as_of, asset_class, weight / 1_000_000) for symbol, as_of, asset_class, weight
            in con.execute("SELECT symbol, as_of, asset_class, weight_micros FROM security_allocation")
        )
        self._targets = weight_sets(
            (name, as_of, asset_class, weight / 1_000_000) for name, as_of, asset_class, weight
            in con.execute("SELECT target_set, as_of, asset_class, weight_micros FROM allocation_target")
        )
        self._weights_cache: dict[tuple[int, str], dict[str, float]] = {}
        self._roth_cache: dict[tuple[int, date], bool | None] = {}
        self._chain_views: dict[int, ChainView] = {}
        # The asset and liability lots a strategy sees, in id order.
        self._visible = {
            info.ref: lot_id for lot_id, info in self.lots.items() if info.kind in ("asset", "liability")
        }
        # Each strategy's memory as last written to a decision row.
        self._recorded_memory: dict[int, str] = {}

        # Triggers (#441): each strategy's (rail, margin kind) pairs, the
        # rails they name, each rail's years as last evaluated, and how many
        # years in a row each pair has been past.
        self.triggers: dict[int, list[tuple[str, str]]] = {}
        self._rails: dict[str, guard_rails.GuardRail] = {}
        has_triggers = con.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'strategy_trigger'").fetchone()
        for sid, rail, margin_kind, rail_json in (con.execute(
                "SELECT strategy_id, rail, margin_kind, rail_json FROM strategy_trigger "
                "ORDER BY strategy_id, rowid") if has_triggers else ()):
            self.triggers.setdefault(sid, []).append((rail, margin_kind))
            if rail not in self._rails:
                parsed, problems = guard_rails.parse_cells(json.loads(rail_json))
                if parsed is None:
                    raise StrategyError(f"trigger rail {rail!r}: {'; '.join(problems)}")
                self._rails[rail] = parsed
        self._rail_years: dict[str, RailYears] = {}
        self._years_past: dict[tuple[int, str, str], int] = {}

        sources = dict(con.execute("SELECT module, source FROM strategy_source").fetchall())
        # A copy of the modules for this run alone: no state from another run.
        self._kinds = kinds = StrategyKinds(sources, fresh=True)
        if kinds.problems:
            kinds.unload()
            raise StrategyError("; ".join(kinds.problems))
        for sid, name, kind, chain_id, rule_tag, start, stop, params_json in rows:
            cls = kinds.get(kind)
            params = cls.Params(**json.loads(params_json))
            role = (self.chain_role.get(chain_id) or "") if chain_id is not None else ""
            rules = tuple(sorted(
                tid for tid, tags in self._rule_tags.items() if rule_tag and rule_tag in tags
            ))
            owned = Owned(sid, name, kind, cls(name, params), chain_id, role, start, stop, rules)
            self.strategies.append(owned)
            if chain_id is not None:
                self.owned[chain_id] = owned
            for tid in rules:
                self.by_rule[tid] = owned
            self.memory[sid] = {}
            self._recorded_memory[sid] = "{}"

    def close(self) -> None:
        """Unload this run's copy of the strategy modules."""
        kinds = getattr(self, "_kinds", None)
        if kinds is not None:
            kinds.unload()

    def __bool__(self) -> bool:
        return bool(self.strategies)

    def owner(self, chain_id: int, month_s: str) -> Owned | None:
        """The strategy owning ``chain_id`` this month, if any."""
        owned = self.owned.get(chain_id)
        return owned if owned is not None and owned.active(month_s) else None

    def rule_owner(self, tid: int, month_s: str) -> Owned | None:
        """The strategy owning scheduled rule ``tid`` this month, if any."""
        owned = self.by_rule.get(tid)
        return owned if owned is not None and owned.active(month_s) else None

    def note_cpi(self, month: date, index: int | None) -> None:
        """Keep the month's CPI index (``ctx.cpi`` divides two of them)."""
        if index is not None:
            self._cpi_index[month] = index

    # ---------- the strategy's view ----------

    def _weights(self, lot_id: int, month_s: str) -> dict[str, float]:
        key = (lot_id, month_s)
        cached = self._weights_cache.get(key)
        if cached is None:
            info = self.lots[lot_id]
            cached = class_weights(
                symbol=info.symbol, account=info.ref.account, lot=info.ref.lot,
                subkind=info.subkind, sets=self._sets, date=month_s,
            )
            self._weights_cache[key] = cached
        return cached

    def _target(self, target_set: str, month_s: str) -> Mapping[str, float]:
        sets = self._targets.get(target_set)
        if not sets:
            raise StrategyError(f"no target set {target_set!r}")
        return weights_at(sets, month_s)

    def _roth(self, lot_id: int, month: date) -> bool | None:
        key = (lot_id, month)
        if key not in self._roth_cache:
            self._roth_cache[key] = self._roth_qualified(lot_id, month)
        return self._roth_cache[key]

    def context(self, owned: Owned, month: date) -> Context:
        month_s = month.isoformat()

        def view(lot_id: int) -> LotView:
            # Balances as they stand while the strategy is being asked.
            info = self.lots[lot_id]
            return LotView(
                ref=info.ref, kind=info.kind, subkind=info.subkind, lot_subkind=info.lot_subkind,
                tax_treatment=info.tax_treatment, owner=info.owner, tags=info.tags,
                symbol=info.symbol, balance=self._balances.get(lot_id, 0),
                cost_basis=self._cost_bases.get(lot_id), minimum=self._minimum_of.get(lot_id, 0),
                roth_qualified=self._roth(lot_id, month),
                acquired=self._acquired.get(lot_id),
                taxable_sale=self._sells_with_gain(lot_id),
            )

        return Context(
            month=month, lots=_Lots(self._visible, view),
            weights=lambda ref: MappingProxyType(self._weights(self.lot_id[ref], month_s)),
            targets=lambda name: self._target(name, month_s),
            memory=self.memory[owned.id],
            end=self._end,
            cpi=(lambda since: self._cpi(since, month)) if self._cpi_index else None,
            people=self._people_at(month),
            budget=lambda: self._budget(owned, month),
            annual_return=self._annual_return,
            tax=self._tax_view(month),
            history=History(lambda: self._history_months(month),
                            lambda refs: self._history_balances(refs, month)),
            rail=self._rail if self._rails else None,
        )

    def _history_months(self, month: date) -> tuple[date, ...]:
        return tuple(date.fromisoformat(m) for (m,) in self._con.execute(
            "SELECT month FROM simulation_step WHERE month < ? ORDER BY month", (month.isoformat(),)))

    def _history_balances(self, refs: tuple[LotRef, ...], month: date) -> np.ndarray:
        """``refs``' month-end total before ``month``: one value per step."""
        ids = [self.lot_id[ref] for ref in refs if ref in self.lot_id]
        unknown = [str(ref) for ref in refs if ref not in self.lot_id]
        if unknown:
            raise StrategyError(f"history.balances: no lot {', '.join(unknown)}")
        marks = ",".join("?" * len(ids)) or "NULL"
        rows = self._con.execute(
            f"SELECT s.month, COALESCE(SUM(ls.balance), 0) FROM simulation_step s "
            f"LEFT JOIN lot_state ls ON ls.step_id = s.id AND ls.lot_id IN ({marks}) "
            f"WHERE s.month < ? GROUP BY s.id ORDER BY s.month",
            (*ids, month.isoformat()),
        ).fetchall()
        return np.array([total for _month, total in rows], dtype=np.int64)

    def _rail(self, name: str) -> RailYears:
        if name not in self._rails:
            known = ", ".join(sorted(self._rails))
            raise StrategyError(f"no rail {name!r} is triggered in this run (triggered: {known})")
        years = self._rail_years.get(name)
        if years is None:
            empty = np.empty((1, 0))
            return RailYears(np.empty(0, dtype=np.int64), empty, empty, empty, empty, empty)
        return years

    def _tax_view(self, month: date) -> TaxView | None:
        tax = self.tax
        if tax is None:
            return None
        year = month.year
        return TaxView(
            year=year,
            estimator=lambda scope: tax.estimate(scope, year),
            ytd=lambda: MappingProxyType(tax.ytd(year)),
            last_year=lambda: (lambda found: None if found is None else MappingProxyType(found))(
                tax.summary(year - 1)),
            irmaa_floors=lambda status: tax.irmaa_floors(year, status),
        )

    def _cpi(self, since: date, month: date) -> int:
        now = self._cpi_index.get(month)
        if now is None:
            raise StrategyError(f"no CPI index for {month.isoformat()}")
        then = self._cpi_index.get(since)
        if then is None:
            if self._first_month is not None and since < self._first_month:
                then = 1_000_000  # the index before the run's first month
            else:
                raise StrategyError(f"no CPI index for {since.isoformat()} (use a month end)")
        return now * 1_000_000 // then

    def _people_at(self, month: date) -> Mapping[str, PersonView]:
        if self._people_month is not None and self._people_month[0] == month:
            return self._people_month[1]
        out: dict[str, PersonView] = {}
        for pid, role, birth in self._people:
            age = None
            if birth is not None:
                age = month.year - birth.year - ((month.month, month.day) < (birth.month, birth.day))
            final = self._final_month.get(pid)
            out[pid] = PersonView(
                id=pid, role=role, birthdate=birth, age=age,
                alive=final is None or month <= final, final_month=final,
            )
        view = MappingProxyType(out)
        self._people_month = (month, view)
        return view

    def _budget(self, owned: Owned, month: date) -> int:
        if self._rule_budget is None:
            return 0
        return sum(self._rule_budget(tid, month) for tid in owned.rules)

    def _annual_return(self, refs: tuple[LotRef, ...], year: int) -> float | None:
        """Growth posted to ``refs`` in ``year`` over their balance at its start."""
        if self._first_month is None or year < self._first_month.year:
            return None
        ids = [self.lot_id[ref] for ref in refs if ref in self.lot_id]
        if not ids:
            return None
        if year == self._first_month.year:
            base = sum(self._opening.get(i, 0) for i in ids)
        else:
            marks = ",".join("?" * len(ids))
            row = self._con.execute(
                f"SELECT COALESCE(SUM(ls.balance), 0) FROM lot_state ls "
                f"JOIN simulation_step s ON s.id = ls.step_id "
                f"WHERE s.month = ? AND ls.lot_id IN ({marks})",
                (date(year - 1, 12, 31).isoformat(), *ids),
            ).fetchone()
            base = row[0]
        if base <= 0:
            return None
        growth = self._growth(ids, year)
        return growth / base

    def _growth(self, ids: list[int], year: int) -> int:
        if not self._growth_sources:
            return 0
        marks = ",".join("?" * len(ids))
        sources = ",".join("?" * len(self._growth_sources))
        lo, hi = f"{year}-01-01", f"{year}-12-31"
        src = sorted(self._growth_sources)
        row = self._con.execute(
            f"SELECT "
            f"  COALESCE(SUM(CASE WHEN to_lot_id IN ({marks}) THEN amount ELSE 0 END), 0) - "
            f"  COALESCE(SUM(CASE WHEN from_lot_id IN ({marks}) THEN amount ELSE 0 END), 0) "
            f"FROM transfer_event WHERE date BETWEEN ? AND ? AND "
            f"((from_lot_id IN ({sources}) AND to_lot_id IN ({marks})) OR "
            f" (to_lot_id IN ({sources}) AND from_lot_id IN ({marks})))",
            (*ids, *ids, lo, hi, *src, *ids, *src, *ids),
        ).fetchone()
        return int(row[0])

    def rule_view(self, tid: int) -> RuleView:
        view = self._rule_views.get(tid)
        if view is None:
            name, kind, frm, to, amount, series = self._rules[tid]
            view = self._rule_views[tid] = RuleView(
                name=name, kind=kind, tags=self._rule_tags.get(tid, frozenset()),
                source=self.chain_name.get(frm, "") if frm is not None else "",
                destination=self.chain_name.get(to, "") if to is not None else "",
                base_amount=amount, series=series if series and series != "fixed" else None,
            )
        return view

    def chain_view(self, chain_id: int) -> ChainView:
        """``chain_id`` as a strategy sees it; fixed for the run, so made once."""
        cached = self._chain_views.get(chain_id)
        if cached is None:
            cached = self._chain_views[chain_id] = self._make_chain_view(chain_id)
        return cached

    def _make_chain_view(self, chain_id: int) -> ChainView:
        steps: list[StepView] = []
        for step in self._members.get(chain_id, []):
            source_id, maximum, percent = step[0], step[1], step[2]
            leaf = self._leaf_lot.get(source_id)
            source: LotRef | str = self.lots[leaf].ref if leaf is not None else self.chain_name[source_id]
            steps.append(StepView(source, maximum, percent))
        leaves: list[LotRef] = []
        self._leaves(chain_id, leaves, set())
        return ChainView(
            self.chain_name[chain_id], self.chain_role.get(chain_id) or "", tuple(steps), tuple(leaves),
        )

    def _leaves(self, chain_id: int, out: list[LotRef], seen: set[int]) -> None:
        if chain_id in seen:
            return
        seen.add(chain_id)
        leaf = self._leaf_lot.get(chain_id)
        if leaf is not None:
            ref = self.lots[leaf].ref
            if ref not in out:
                out.append(ref)
            return
        for step in self._members.get(chain_id, []):
            self._leaves(step[0], out, seen)

    # ---------- asking ----------

    def decide(
        self, owned: Owned, hook: str, chain_id: int, amount: int, month: date,
    ) -> tuple[Decision, list[tuple[int, int]]] | None:
        """Ask ``owned``'s ``hook`` about ``amount`` cents through ``chain_id``:
        None for "no opinion", else the decision and its moves as
        ``(lot id, cents)``, checked. A strategy's exception, or a move it
        isn't allowed, is raised as :class:`StrategyError` naming it."""
        what = f"strategy {owned.name!r} ({owned.kind}) {hook} in {month.isoformat()}"
        chain = self.chain_view(chain_id)
        decision = self._ask(owned, hook, month, chain, amount)
        if decision is None:
            return None
        move_type = Take if hook == "draw" else Put
        leaves = set(chain.leaves)
        moves: list[tuple[int, int]] = []
        total = 0
        try:
            given = list(decision.moves)
        except TypeError as e:
            raise StrategyError(f"{what}: Decision.moves must be a list of {move_type.__name__}: {e}") from e
        for move in given:
            if not isinstance(move, move_type):
                raise StrategyError(f"{what}: a {hook} answers with {move_type.__name__}, not {move!r}")
            if move.lot not in leaves:
                raise StrategyError(f"{what}: {move.lot} is not a leaf of chain {chain.name!r}")
            if not isinstance(move.amount, int) or isinstance(move.amount, bool) or move.amount <= 0:
                raise StrategyError(f"{what}: amount {move.amount!r} for {move.lot} must be whole cents > 0")
            total += move.amount
            moves.append((self.lot_id[move.lot], move.amount))
        if total > amount:
            raise StrategyError(f"{what}: moves total {total} cents, more than the {amount} asked")
        return decision, moves

    def _ask(self, owned: Owned, hook: str, month: date, *args: Any) -> Decision | None:
        """Call ``hook``; its exception, a non-JSON memory or a wrong answer
        type is a :class:`StrategyError` naming the strategy."""
        what = f"strategy {owned.name!r} ({owned.kind}) {hook} in {month.isoformat()}"
        ctx = self.context(owned, month)
        try:
            decision = getattr(owned.strategy, hook)(ctx, *args)
        except StrategyError as e:
            raise StrategyError(f"{what}: {e}") from e
        except Exception as e:
            raise StrategyError(f"{what}: {type(e).__name__}: {e}") from e
        try:
            json.dumps(self.memory[owned.id], sort_keys=True)
        except (TypeError, ValueError) as e:
            raise StrategyError(f"{what}: memory must hold JSON values: {e}") from e
        if decision is None:
            return None
        if not isinstance(decision, Decision):
            raise StrategyError(f"{what}: returned {type(decision).__name__}, not a Decision or None")
        try:
            decision.detail_json()
        except (TypeError, ValueError) as e:
            raise StrategyError(f"{what}: detail must hold JSON values: {e}") from e
        return decision

    def year_start(self, con: sqlite3.Connection, step_id: int, month: date) -> None:
        """Ask each active strategy's ``year_start`` in its first month and
        each January after (#387), and record what it said."""
        month_s = month.isoformat()
        for owned in self.strategies:
            if "year_start" not in owned.strategy.hooks or not owned.active(month_s):
                continue
            if owned.id in self._started and month.month != 1:
                continue
            self._started.add(owned.id)
            decision = self._ask(owned, "year_start", month)
            if decision is None:
                continue
            if list(decision.moves):
                raise StrategyError(
                    f"strategy {owned.name!r} ({owned.kind}) year_start in {month_s}: "
                    "year_start moves nothing; keep the year's course in ctx.memory"
                )
            self.record(con, owned, "year_start", step_id=step_id, asked=0, decision=decision)

    def year_end(
        self, con: sqlite3.Connection, step_id: int, month: date,
        post: Callable[[str, int, int, int, bool, int], int],
    ) -> None:
        """Ask each active strategy's ``year_end`` in December (#421), record
        what it said, and post its moves in order through ``post(move,
        from lot, to lot, cents, in_kind, decision id)``, which returns the
        cents it moved."""
        self._act(con, step_id, month, "year_end", post)

    def month_start(
        self, con: sqlite3.Connection, step_id: int, month: date,
        post: Callable[[str, int, int, int, bool, int], int],
    ) -> None:
        """Ask each active strategy's ``month_start`` (#441), as ``year_end``."""
        self._act(con, step_id, month, "month_start", post)

    def _act(
        self, con: sqlite3.Connection, step_id: int, month: date, hook: str,
        post: Callable[[str, int, int, int, bool, int], int],
    ) -> None:
        month_s = month.isoformat()
        for owned in self.strategies:
            if hook not in owned.strategy.hooks or not owned.active(month_s):
                continue
            decision = self._ask(owned, hook, month)
            if decision is not None:
                self._post_decision(con, owned, hook, decision, step_id, month, post)

    def _post_decision(
        self, con: sqlite3.Connection, owned: Owned, hook: str, decision: Decision,
        step_id: int, month: date, post: Callable[[str, int, int, int, bool, int], int],
        breach_id: int | None = None,
    ) -> None:
        """Record ``decision`` and post its moves in order, linking their legs."""
        moves = self._moves(owned, hook, decision, month)
        decision_id = self.record(con, owned, hook, step_id=step_id, asked=0, decision=decision,
                                  breach_id=breach_id)
        moved = 0
        for move, frm, to, amount, in_kind in moves:
            mark = con.execute("SELECT COALESCE(MAX(id), 0) FROM transfer_event").fetchone()[0]
            moved += post(move, frm, to, amount, in_kind, decision_id)
            self.link_legs(con, decision_id, mark=mark, from_lot=frm, to_lot=to)
        self.finish(con, decision_id, chosen=moved, fallback=0)

    def breaches(
        self, con: sqlite3.Connection, step_id: int, month: date,
        post: Callable[[str, int, int, int, bool, int], int],
    ) -> None:
        """In January, evaluate the triggered rails over the run so far, as
        ``sim report guard-rails`` does, and tell each strategy whose trigger
        names one about the year just ended when it is past the line, and
        once more the year it comes back (#441). Each telling is a
        ``breach_event`` row and its answer a decision linked to it."""
        if not self._rails or month.month != 1:
            return
        from analysis.sim.connection import RunConnection

        names = list(self._rails)
        if not con.execute("SELECT 1 FROM simulation_step WHERE month < ? LIMIT 1",
                           (month.isoformat(),)).fetchone():
            return  # nothing finished yet
        results = guard_rails.evaluate_run(
            RunConnection.live(con), [self._rails[n] for n in names], DollarMode.CPI_CONSTANT)
        for name, result in zip(names, results, strict=True):
            self._rail_years[name] = result.years
        year = month.year - 1
        month_s = month.isoformat()
        for owned in self.strategies:
            pairs = self.triggers.get(owned.id, [])
            if not pairs or not owned.active(month_s):
                continue
            for rail, margin_kind in pairs:
                years = self._rail_years[rail]
                at = np.flatnonzero(years.years == year)
                values = years.cumulative if margin_kind == "cumulative" else years.margin
                margin = float(values[0, at[0]]) if len(at) else float("nan")
                key = (owned.id, rail, margin_kind)
                before = self._years_past.get(key, 0)
                if np.isnan(margin):
                    self._years_past.pop(key, None)  # the rail doesn't cover the year
                    continue
                if margin < 0:
                    state, past = ("continued" if before else "entered"), before + 1
                elif before:
                    state, past = "left", 0
                else:
                    continue
                self._years_past[key] = past
                rule = self._rails[rail]
                breach = BreachView(
                    rail=rail, description=rule.description or rule.describe(),
                    margin_kind=margin_kind, year=year,
                    measure=round(float(years.measure[0, at[0]]) * 100),
                    threshold=round(float(years.threshold[0, at[0]]) * 100),
                    margin=round(margin * 100), sense=rule.sense, state=state, years_past=past,
                )
                cur = con.execute(
                    "INSERT INTO breach_event(step_id, strategy_id, rail, margin_kind, year, state, "
                    "years_past, measure_cents, threshold_cents, margin_cents) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (step_id, owned.id, rail, margin_kind, year, state, past,
                     breach.measure, breach.threshold, breach.margin),
                )
                decision = self._ask(owned, "on_breach", month, breach)
                if decision is not None:
                    self._post_decision(con, owned, "on_breach", decision, step_id, month, post,
                                        breach_id=cur.lastrowid)

    def _moves(
        self, owned: Owned, hook: str, decision: Decision, month: date,
    ) -> list[tuple[str, int, int, int, bool]]:
        """``decision``'s moves as ``(move, from lot, to lot, cents, in_kind)``,
        checked: :class:`Transfer` out of an asset lot into another asset lot
        or an expense account, ``in_kind`` only from a lot that tracks basis
        into an expense; :class:`Realize` on a taxable lot that tracks basis."""
        what = f"strategy {owned.name!r} ({owned.kind}) {hook} in {month.isoformat()}"
        try:
            given = list(decision.moves)
        except TypeError as e:
            raise StrategyError(f"{what}: Decision.moves must be a list of Transfer and Realize: {e}") from e
        out: list[tuple[str, int, int, int, bool]] = []
        for move in given:
            if not isinstance(move, (Transfer, Realize)):
                raise StrategyError(f"{what}: a {hook} answers with Transfer and Realize, not {move!r}")
            if not isinstance(move.amount, int) or isinstance(move.amount, bool) or move.amount <= 0:
                raise StrategyError(f"{what}: amount {move.amount!r} in {move!r} must be whole cents > 0")
            if isinstance(move, Realize):
                lot = self._asset_lot(move.lot, what)
                if not self._sells_with_gain(lot):
                    raise StrategyError(
                        f"{what}: {move.lot} doesn't track a taxable gain; Realize needs a taxable "
                        "lot with a cost basis")
                out.append(("realize", lot, lot, move.amount, False))
                continue
            source = self._asset_lot(move.source, what)
            to = self._destination(move.to, what)
            if to == source:
                raise StrategyError(f"{what}: {move.source} can't transfer to itself (use Realize)")
            to_kind = self.lots[to].kind
            if move.in_kind:
                if to_kind != "expense":
                    raise StrategyError(f"{what}: in_kind gives into an expense account, not {move.to}")
                if not self._sells_with_gain(source):
                    raise StrategyError(f"{what}: in_kind gives a lot's shares; {move.source} has no cost basis")
            out.append(("transfer", source, to, move.amount, bool(move.in_kind)))
        return out

    def _asset_lot(self, ref: object, what: str) -> int:
        lot = self.lot_id.get(ref) if isinstance(ref, LotRef) else None
        if lot is None or self.lots[lot].kind != "asset":
            raise StrategyError(f"{what}: {ref!r} is not an asset lot")
        return lot

    def _destination(self, to: object, what: str) -> int:
        """A move's ``to``: a lot, or an account by name that has one lot."""
        if isinstance(to, LotRef):
            lot = self.lot_id.get(to)
        elif isinstance(to, str):
            found = [i for ref, i in self.lot_id.items() if ref.account == to]
            lot = found[0] if len(found) == 1 else None
            if len(found) > 1:
                raise StrategyError(f"{what}: account {to!r} has several lots; name one as a LotRef")
        else:
            lot = None
        if lot is None or self.lots[lot].kind not in ("asset", "expense"):
            raise StrategyError(f"{what}: {to!r} is not an asset lot or an expense account")
        return lot

    def _sells_with_gain(self, lot: int) -> bool:
        """A sale out of ``lot`` realizes a gain: a taxable, non-cash lot with a basis."""
        info = self.lots[lot]
        return (
            lot not in self._roth_lots and self._cost_bases.get(lot) is not None
            and info.tax_treatment == "taxable"
            and registries.withdrawal_tax_character(info.tax_treatment, info.subkind) is None
        )

    def scale(
        self, con: sqlite3.Connection, owned: Owned, tid: int, amount: int, *,
        step_id: int, month: date,
    ) -> int:
        """Ask ``owned`` to scale rule ``tid``'s ``amount`` cents; record the
        baseline (and the decision, if it gave one). Returns what to post."""
        what = f"strategy {owned.name!r} ({owned.kind}) scale in {month.isoformat()}"
        decision = self._ask(owned, "scale", month, self.rule_view(tid), amount)
        delivered = amount
        decision_id = None
        if decision is not None:
            moves = list(decision.moves)
            if len(moves) != 1 or not isinstance(moves[0], Spend):
                raise StrategyError(f"{what}: a scale answers with one Spend, not {moves!r}")
            delivered = moves[0].amount
            if not isinstance(delivered, int) or isinstance(delivered, bool) or delivered < 0:
                raise StrategyError(f"{what}: Spend amount {delivered!r} must be whole cents >= 0")
            decision_id = self.record(
                con, owned, "scale", step_id=step_id, rule_id=tid, asked=amount, decision=decision,
            )
            self.finish(con, decision_id, chosen=delivered, fallback=0)
        con.execute(
            "INSERT INTO spending_baseline(step_id, scheduled_transaction_id, strategy_id, "
            "strategy_decision_id, budgeted_cents, delivered_cents) VALUES (?,?,?,?,?,?)",
            (step_id, tid, owned.id, decision_id, amount, delivered),
        )
        return delivered

    def record(
        self, con: sqlite3.Connection, owned: Owned, hook: str, *, step_id: int,
        chain_id: int | None = None, rule_id: int | None = None,
        asked: int, decision: Decision, breach_id: int | None = None,
    ) -> int:
        """Insert the decision row (amounts filled in by :meth:`finish`). Its
        memory is written when it differs from the last row's, so changes made
        while answering None show on the next decision."""
        memory = json.dumps(self.memory[owned.id], sort_keys=True)
        changed = memory != self._recorded_memory[owned.id]
        self._recorded_memory[owned.id] = memory
        if breach_id is None:
            cur = con.execute(
                "INSERT INTO strategy_decision(step_id, strategy_id, hook, chain_id, "
                "scheduled_transaction_id, asked_cents, chosen_cents, fallback_cents, note, "
                "detail_json, memory_json) VALUES (?,?,?,?,?,?,0,0,?,?,?)",
                (step_id, owned.id, hook, chain_id, rule_id, asked, decision.note,
                 decision.detail_json(), memory if changed else None),
            )
        else:
            cur = con.execute(
                "INSERT INTO strategy_decision(step_id, strategy_id, hook, asked_cents, chosen_cents, "
                "fallback_cents, note, detail_json, memory_json, breach_event_id) "
                "VALUES (?,?,?,?,0,0,?,?,?,?)",
                (step_id, owned.id, hook, asked, decision.note, decision.detail_json(),
                 memory if changed else None, breach_id),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    @staticmethod
    def finish(con: sqlite3.Connection, decision_id: int, *, chosen: int, fallback: int) -> None:
        con.execute(
            "UPDATE strategy_decision SET chosen_cents = ?, fallback_cents = ? WHERE id = ?",
            (chosen, fallback, decision_id),
        )

    @staticmethod
    def link_legs(
        con: sqlite3.Connection, decision_id: int, *, mark: int, from_lot: int, to_lot: int,
    ) -> None:
        """Point the legs one move posted (every leg after ``mark`` between its
        two lots: a withdrawal's basis and gain legs alike, not the refills
        it set off) at the decision that chose them."""
        con.execute(
            "UPDATE transfer_event SET strategy_decision_id = ? "
            "WHERE id > ? AND from_lot_id = ? AND to_lot_id = ?",
            (decision_id, mark, from_lot, to_lot),
        )
