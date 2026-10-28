"""``rebalance``: draw from what is overweight, place into what is underweight."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from analysis.sim.strategy_api import (
    Allocation, ChainView, Context, Decision, LotRef, Percent, Put, Strategy, Tag, Take, TargetSet,
    percent, register,
)


@register
class Rebalance(Strategy):
    """Keep a tagged portfolio near its target allocation through the money
    that flows anyway. While every asset class is within ``band`` of its
    target, the chain's own order decides (the household's tax-driven
    preference). Beyond it, each slice of a draw (``step`` of the amount)
    comes from the leaf that brings the portfolio's class mix closest to the
    targets, and each slice placed goes where it helps most. Ties go to the
    leaf earlier in the chain. A leaf outside the portfolio moves no class,
    so it is used only when no portfolio lot would help."""

    kind = "rebalance"

    @dataclass(frozen=True)
    class Params:
        portfolio: Tag
        """Account tag whose asset lots are the portfolio."""
        targets: TargetSet
        """Target set in targets.csv."""
        band: Percent = percent("5%")
        """Drift from target, per class, tolerated before allocation matters."""
        step: Percent = percent("1%")
        """Size of each slice, as a share of the amount (at least $1)."""

    def draw(self, ctx: Context, chain: ChainView, need: int) -> Decision | None:
        return self._decide(ctx, chain, need, sign=-1)

    def place(self, ctx: Context, chain: ChainView, amount: int) -> Decision | None:
        return self._decide(ctx, chain, amount, sign=+1)

    def _decide(self, ctx: Context, chain: ChainView, amount: int, *, sign: int) -> Decision | None:
        p = self.params
        portfolio = ctx.allocation(ctx.tagged(p.portfolio))
        targets = ctx.targets(p.targets)
        if portfolio.total <= 0 or amount <= 0:
            return None
        classes = sorted(set(portfolio.dollars) | set(targets))
        shares = portfolio.shares
        drift = {c: shares.get(c, 0.0) - targets.get(c, 0.0) for c in classes}
        band = p.band / 1_000_000
        if max(abs(d) for d in drift.values()) <= band:
            return None
        # The class this move works on: the most overweight for a draw, the
        # most underweight for a place.
        worst = max(classes, key=lambda c: (-sign * drift[c], c))
        taken = _greedy(ctx, chain, portfolio, targets, classes, amount, sign, p.step)
        if not taken:
            return None
        verb, prep = ("took", "from") if sign < 0 else ("put", "into")
        moved = ", ".join(f"{_dollars(a)} {prep} {ref}" for ref, a in taken.items())
        after = portfolio.after({ref: sign * a for ref, a in taken.items()}).shares
        note = (
            f"{worst} {shares.get(worst, 0.0):.1%} vs {targets.get(worst, 0.0):.1%} target "
            f"(band {band:.0%}): {verb} {moved}"
        )
        move = Take if sign < 0 else Put
        return Decision(
            [move(ref, a) for ref, a in taken.items()],
            note=note,
            detail={
                "before": {c: round(shares.get(c, 0.0), 6) for c in classes},
                "after": {c: round(after.get(c, 0.0), 6) for c in classes},
                "targets": {c: round(targets.get(c, 0.0), 6) for c in classes},
            },
        )


def _greedy(
    ctx: Context, chain: ChainView, portfolio: Allocation, targets: Mapping[str, float],
    classes: Sequence[str], amount: int, sign: int, step_micros: int,
) -> dict[LotRef, int]:
    """Slice by slice, the leaf whose move most reduces the squared distance
    between the portfolio's class dollars and the targets' share of the
    portfolio once the whole ``amount`` has moved.

    With ``r`` the dollars each class still has to move (positive: toward
    the target) and ``w`` a leaf's class weights, moving ``a`` from that leaf
    changes the distance ``|r - a w|^2`` by ``-(2 a r.w - a^2 |w|^2)``, so the
    best leaf has the largest improvement ``a (2 r.w - a |w|^2)``; a leaf
    outside the portfolio scores 0 (it moves no class)."""
    final_total = portfolio.total + sign * amount
    # Toward the target is "take out" for a draw and "put in" for a place.
    residual = [
        -sign * (portfolio.dollars.get(c, 0) - targets.get(c, 0.0) * final_total) for c in classes
    ]
    leaves = [ref for ref in chain.leaves if ref in ctx.lots]
    vectors = {
        ref: [portfolio.weights[ref].get(c, 0.0) for c in classes] if ref in portfolio.weights else None
        for ref in leaves
    }
    room = {ref: (ctx.lots[ref].available if sign < 0 else _room(ctx, ref)) for ref in leaves}
    slice_size = max(100, amount * step_micros // 1_000_000)
    remaining = amount
    taken: dict[LotRef, int] = {}
    while remaining > 0:
        size = min(slice_size, remaining)
        best: LotRef | None = None
        best_score = 0.0
        for ref in leaves:
            if room[ref] <= 0:
                continue
            a = min(size, room[ref])
            w = vectors[ref]
            score = 0.0 if w is None else a * (
                2 * sum(r * x for r, x in zip(residual, w, strict=True))
                - a * sum(x * x for x in w)
            )
            if best is None or score > best_score + 1e-9:
                best, best_score = ref, score
        if best is None:
            break
        a = min(size, room[best])
        taken[best] = taken.get(best, 0) + a
        room[best] -= a
        remaining -= a
        if (w := vectors[best]) is not None:
            residual = [r - a * x for r, x in zip(residual, w, strict=True)]
    return taken


def _room(ctx: Context, ref: LotRef) -> int:
    """How much a lot can receive: unbounded here (the engine applies a
    destination's maximum balance when it posts)."""
    del ctx, ref
    return 1 << 62


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.0f}"
