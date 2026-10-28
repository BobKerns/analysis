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
from collections.abc import Callable, Mapping
from datetime import date
from types import MappingProxyType
from typing import Any, NamedTuple

from analysis.sim.asset_classes import class_weights, weights_at
from analysis.sim.strategy_api import (
    ChainView, Context, Decision, LotRef, LotView, Put, StepView, Strategy, StrategyError, Take,
)
from analysis.sim.strategy_code import StrategyKinds

__all__ = ["Owned", "StrategyRuntime"]


class Owned(NamedTuple):
    """One strategy row, ready to ask."""

    id: int
    name: str
    kind: str
    strategy: Strategy
    chain_id: int
    role: str
    start: str | None
    end: str | None


class _LotInfo(NamedTuple):
    ref: LotRef
    kind: str
    subkind: str
    lot_subkind: str | None
    tax_treatment: str | None
    owner: str | None
    tags: frozenset[str]
    symbol: str | None


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
    ) -> None:
        rows = con.execute(
            'SELECT id, name, kind, chain_id, start, "end", params_json FROM strategy ORDER BY id'
        ).fetchall()
        self.owned: dict[int, Owned] = {}
        self.memory: dict[int, dict[str, Any]] = {}
        if not rows:
            return  # nothing to ask: skip building the views
        self._balances = balances
        self._cost_bases = cost_bases
        self._minimum_of = minimum_of
        self._leaf_lot = leaf_lot
        self._members = members
        self._roth_qualified = roth_qualified
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
        sets: dict[str, dict[str, dict[str, float]]] = {}
        for symbol, as_of, asset_class, weight in con.execute(
            "SELECT symbol, as_of, asset_class, weight_micros FROM security_allocation"
        ):
            sets.setdefault(symbol, {}).setdefault(as_of, {})[asset_class] = weight / 1_000_000
        self._sets = {s: sorted(by_date.items()) for s, by_date in sets.items()}
        targets: dict[str, dict[str, dict[str, float]]] = {}
        for target_set, as_of, asset_class, weight in con.execute(
            "SELECT target_set, as_of, asset_class, weight_micros FROM allocation_target"
        ):
            targets.setdefault(target_set, {}).setdefault(as_of, {})[asset_class] = weight / 1_000_000
        self._targets = {t: sorted(by_date.items()) for t, by_date in targets.items()}
        self._weights_cache: dict[tuple[int, str], dict[str, float]] = {}
        self._memory_changed = False

        sources = dict(con.execute("SELECT module, source FROM strategy_source").fetchall())
        kinds = StrategyKinds(sources)
        if kinds.problems:
            raise StrategyError("; ".join(kinds.problems))
        for sid, name, kind, chain_id, start, end, params_json in rows:
            cls = kinds.get(kind)
            params = cls.Params(**json.loads(params_json))
            role = self.chain_role.get(chain_id) or ""
            self.owned[chain_id] = Owned(sid, name, kind, cls(name, params), chain_id, role, start, end)
            self.memory[sid] = {}

    def __bool__(self) -> bool:
        return bool(self.owned)

    def owner(self, chain_id: int, month_s: str) -> Owned | None:
        """The strategy owning ``chain_id`` this month, if any."""
        owned = self.owned.get(chain_id)
        if owned is None:
            return None
        if owned.start and month_s < owned.start:
            return None
        if owned.end and month_s > owned.end:
            return None
        return owned

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

    def context(self, owned: Owned, month: date) -> Context:
        month_s = month.isoformat()
        views: dict[LotRef, LotView] = {}
        for lot_id, info in self.lots.items():
            if info.kind not in ("asset", "liability"):
                continue
            views[info.ref] = LotView(
                ref=info.ref, kind=info.kind, subkind=info.subkind, lot_subkind=info.lot_subkind,
                tax_treatment=info.tax_treatment, owner=info.owner, tags=info.tags,
                symbol=info.symbol, balance=self._balances.get(lot_id, 0),
                cost_basis=self._cost_bases.get(lot_id), minimum=self._minimum_of.get(lot_id, 0),
                roth_qualified=self._roth_qualified(lot_id, month),
            )
        return Context(
            month=month, lots=MappingProxyType(views),
            weights=lambda ref: MappingProxyType(self._weights(self.lot_id[ref], month_s)),
            targets=lambda name: self._target(name, month_s),
            memory=self.memory[owned.id],
        )

    def chain_view(self, chain_id: int) -> ChainView:
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
        ctx = self.context(owned, month)
        before = json.dumps(self.memory[owned.id], sort_keys=True)
        try:
            decision = getattr(owned.strategy, hook)(ctx, chain, amount)
        except Exception as e:
            raise StrategyError(f"{what}: {type(e).__name__}: {e}") from e
        try:
            after = json.dumps(self.memory[owned.id], sort_keys=True)
        except (TypeError, ValueError) as e:
            raise StrategyError(f"{what}: memory must hold JSON values: {e}") from e
        self._memory_changed = after != before
        if decision is None:
            return None
        if not isinstance(decision, Decision):
            raise StrategyError(f"{what}: returned {type(decision).__name__}, not a Decision or None")
        move_type = Take if hook == "draw" else Put
        leaves = set(chain.leaves)
        moves: list[tuple[int, int]] = []
        total = 0
        for move in decision.moves:
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
        try:
            decision.detail_json()
        except (TypeError, ValueError) as e:
            raise StrategyError(f"{what}: detail must hold JSON values: {e}") from e
        return decision, moves

    def record(
        self, con: sqlite3.Connection, owned: Owned, hook: str, *, step_id: int, chain_id: int,
        asked: int, decision: Decision,
    ) -> int:
        """Insert the decision row (amounts filled in by :meth:`finish`)."""
        cur = con.execute(
            "INSERT INTO strategy_decision(step_id, strategy_id, hook, chain_id, asked_cents, "
            "chosen_cents, fallback_cents, note, detail_json, memory_json) "
            "VALUES (?,?,?,?,?,0,0,?,?,?)",
            (step_id, owned.id, hook, chain_id, asked, decision.note, decision.detail_json(),
             json.dumps(self.memory[owned.id], sort_keys=True) if self._memory_changed else None),
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
