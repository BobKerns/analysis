"""A scenario's chains as a graph, for reading them rather than running them (issue #129).

A chain (ADR-003 §4.7) is an ordered list of steps, each a lot or another
chain; every lot is also a one-step *leaf* chain of its own. What a chain
means depends on where it is used: a rule drawing from it, or a lot's
``refill_chain``, takes money out of its steps in turn (a *draw*); a rule
paying into it, or a lot's ``overflow_chain``, puts money into them (a
*sweep*). :func:`chain_map` gathers the steps, the lot policies the steps
meet (minimum, refill, maximum, overflow) and every use of each chain, from
a loaded scenario; :func:`chain_tree` and :func:`mermaid` draw it, for
``sim chain show|flow`` and the scenario notebook alike.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from analysis.frames import named_rows
from analysis.sim import registries
from analysis.sim.connection import ScenarioConnection

__all__ = [
    "ChainMap", "LotPolicy", "Step", "Use", "chain_map", "chain_tree", "mermaid", "share_text",
]

type UseKind = Literal["draw", "sweep", "refill", "overflow", "step"]


@dataclass(frozen=True)
class Step:
    order: int
    target: str
    """The chain the step visits: a leaf (one lot) or an interior chain."""
    maximum_cents: int | None
    percent_micros: int
    series: str | None
    cadence: str | None


@dataclass(frozen=True)
class LotPolicy:
    minimum_cents: int
    maximum_cents: int | None
    refill_chain: str | None
    refill_target_cents: int | None
    refill_amount_cents: int | None
    overflow_chain: str | None


@dataclass(frozen=True)
class Use:
    kind: UseKind
    by: str
    """Who uses the chain: a rule's name, a lot's label, or a chain's name."""
    detail: str = ""
    """For a rule, the chain at the other end of the transfer; for a step, its number."""


@dataclass
class ChainMap:
    kinds: dict[str, str] = field(default_factory=dict)
    """Interior chain name to its kind (``asset``, ``income``)."""
    steps: dict[str, list[Step]] = field(default_factory=dict)
    """Interior chain name to its steps, in order."""
    leaves: dict[str, tuple[str, str]] = field(default_factory=dict)
    """Leaf chain name to its ``(account, lot)``."""
    policies: dict[str, LotPolicy] = field(default_factory=dict)
    """Leaf chain name to the lot's balance policy, for lots that have one."""
    uses: dict[str, list[Use]] = field(default_factory=dict)
    """Chain name (interior or leaf) to everything that uses it."""
    multi_lot: frozenset[str] = frozenset()
    """Accounts whose lot must be named to say which one is meant."""

    def is_interior(self, name: str) -> bool:
        return name in self.steps

    def label(self, name: str) -> str:
        """``name`` as the CSVs spell it: an account, ``account (lot)``, or a chain."""
        if name in self.leaves:
            account, lot = self.leaves[name]
            return f"{account} ({lot})" if account in self.multi_lot else account
        return name

    def descendants(self, name: str) -> list[str]:
        """``name`` and every chain reachable through its steps, depth first."""
        seen: list[str] = []

        def walk(node: str) -> None:
            if node in seen:
                return
            seen.append(node)
            for step in self.steps.get(node, ()):
                walk(step.target)

        walk(name)
        return seen


def _cents(value: float | None) -> int | None:
    """A nullable integer column's cell: pandas reads NULL back as NaN."""
    return None if value is None or pd.isna(value) else int(value)


def _text(value: object) -> str | None:
    return None if value is None or pd.isna(value) or value == "" else str(value)


def chain_map(sc: ScenarioConnection) -> ChainMap:
    """Every chain of the scenario open on ``sc``, its steps, the policies of
    the lots it reaches, and its uses."""
    chains = sc.items("chain")
    members = sc.items("account_model_member")
    lot_counts = members.groupby("account").size()
    subkinds = dict(zip(members["account"], members["subkind"], strict=False))
    cmap = ChainMap(multi_lot=frozenset(
        account for account, subkind in subkinds.items()
        if lot_counts.get(account, 0) > 1 or subkind in registries.INVESTMENT_ASSET_SUBKINDS
    ))
    for row in named_rows(chains):
        if _text(row.account_name) is not None:
            cmap.leaves[row.name] = (row.account_name, row.lot or registries.DEFAULT_LOT)
        else:
            cmap.kinds[row.name] = row.kind
            cmap.steps[row.name] = []
    for row in named_rows(sc.items("chain_steps").sort_values(["chain_name", "step_order"])):
        cmap.steps[row.chain_name].append(Step(
            int(row.step_order), row.draw_from_chain_name, _cents(row.maximum_cents),
            int(row.percent_micros), _text(row.series), _text(row.cadence),
        ))
        cmap.uses.setdefault(row.draw_from_chain_name, []).append(
            Use("step", row.chain_name, str(int(row.step_order))))

    def chain_of(name: object, lot: object) -> str | None:
        """A ``refill_chain``/``overflow_chain`` cell and its lot, as a chain name."""
        if (text := _text(name)) is None:
            return None
        if text in cmap.steps:
            return text
        lot_text = _text(lot) or registries.DEFAULT_LOT
        return registries.leaf_chain_name(text, lot_text, subkinds.get(text, ""))

    for row in named_rows(members):
        leaf = registries.leaf_chain_name(row.account, row.lot, row.subkind or "")
        refill, overflow = chain_of(row.refill_chain, row.refill_lot), chain_of(row.overflow_chain, row.overflow_lot)
        minimum = _cents(row.minimum_balance_cents) or 0
        maximum = _cents(row.maximum_balance_cents)
        if refill or overflow or minimum or maximum is not None:
            cmap.policies[leaf] = LotPolicy(
                minimum, maximum, refill, _cents(row.refill_target_cents),
                _cents(row.refill_amount_cents), overflow,
            )
        label = cmap.label(leaf)
        if refill:
            cmap.uses.setdefault(refill, []).append(Use("refill", label))
        if overflow:
            cmap.uses.setdefault(overflow, []).append(Use("overflow", label))

    for row in named_rows(sc.items("scheduled")):
        what = f"rule {row.name}"
        if _cents(row.created_by_event_id) is not None:
            what = f"calendar {row.name}"
        if row.from_name in cmap.steps:
            cmap.uses.setdefault(row.from_name, []).append(Use("draw", what, row.to_name))
        if row.to_name in cmap.steps:
            cmap.uses.setdefault(row.to_name, []).append(Use("sweep", what, row.from_name))
    return cmap


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.0f}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


def share_text(step: Step) -> str:
    """What a step takes of the amount still outstanding when it is reached."""
    if step.percent_micros == 1_000_000:
        share = "all that's left"
    else:
        share = f"{step.percent_micros / 10_000:g}% of what's left"
    if step.maximum_cents is not None:
        share += f", up to {_dollars(step.maximum_cents)}"
        if step.series:
            share += f" growing with {step.series} ({step.cadence or 'monthly'})"
    return share


def policy_text(cmap: ChainMap, policy: LotPolicy) -> str:
    """A lot's balance policy in a line."""
    parts = []
    if policy.minimum_cents or policy.refill_chain:
        parts.append(f"keeps {_dollars(policy.minimum_cents)}")
    if policy.refill_chain:
        refill = f"refills from {cmap.label(policy.refill_chain)}"
        if policy.refill_target_cents is not None:
            refill += f" up to {_dollars(policy.refill_target_cents)}"
        elif policy.refill_amount_cents is not None:
            refill += f" {_dollars(policy.refill_amount_cents)} at a time"
        parts.append(refill)
    if policy.maximum_cents is not None:
        cap = f"holds at most {_dollars(policy.maximum_cents)}"
        if policy.overflow_chain:
            cap += f", extra to {cmap.label(policy.overflow_chain)}"
        parts.append(cap)
    return "; ".join(parts)


_USE_TEXT: dict[UseKind, str] = {
    "draw": "drawn from by", "sweep": "paid into by", "refill": "refills",
    "overflow": "takes the overflow of", "step": "a step of",
}


def use_text(cmap: ChainMap, use: Use) -> str:
    text = f"{_USE_TEXT[use.kind]} {use.by}"
    if use.kind == "draw" and use.detail:
        text += f" (to {cmap.label(use.detail)})"
    elif use.kind == "sweep" and use.detail:
        text += f" (from {cmap.label(use.detail)})"
    elif use.kind == "step":
        text += f" (step {use.detail})"
    return text


def chain_tree(cmap: ChainMap, name: str):
    """``name`` as a rich ``Tree``: each step under its chain, a sub-chain's
    steps nested under it like a parenthesized expression, and each lot with
    the policy it brings."""
    from rich.markup import escape
    from rich.tree import Tree

    def heading(node: str) -> str:
        if cmap.is_interior(node):
            return f"[bold]{escape(node)}[/bold] [dim]({cmap.kinds[node]} chain)[/dim]"
        return f"[bold]{escape(cmap.label(node))}[/bold] [dim](one lot)[/dim]"

    tree = Tree(heading(name))

    def add(branch, node: str, path: tuple[str, ...]) -> None:
        if node in cmap.policies:
            branch.add(f"[dim]{escape(policy_text(cmap, cmap.policies[node]))}[/dim]")
        for step in cmap.steps.get(node, ()):
            target = step.target
            title = f"{step.order}. {escape(cmap.label(target))}"
            if cmap.is_interior(target):
                title += " [dim](chain)[/dim]"
            child = branch.add(f"{title}  [cyan]{escape(share_text(step))}[/cyan]")
            if target in path:
                child.add("[red]cycle[/red]")  # the loader rejects one; never loop here
            else:
                add(child, target, (*path, target))

    add(tree, name, (name,))
    return tree


def _node_ids(names: Iterable[str]) -> dict[str, str]:
    return {name: f"n{i}" for i, name in enumerate(names)}


def _quoted(text: str) -> str:
    return '"' + text.replace('"', "#quot;") + '"'


def mermaid(cmap: ChainMap, names: Iterable[str] | None = None) -> str:
    """A Mermaid flowchart of ``names`` (default: every interior chain) and
    everything under them: steps as plain lines numbered in order, refill and
    overflow as arrows showing which way money moves, and the rules that draw
    from or pay into a chain."""
    roots = list(cmap.steps) if names is None else list(names)
    shown: list[str] = []
    for root in roots:
        for node in cmap.descendants(root):
            if node not in shown:
                shown.append(node)
    # A lot whose refill or overflow chain is shown belongs on the chart too,
    # and so does where a shown lot refills from or overflows to.
    for leaf, policy in cmap.policies.items():
        if leaf not in shown and (policy.refill_chain in shown or policy.overflow_chain in shown):
            shown.append(leaf)
    for leaf in list(shown):
        policy = cmap.policies.get(leaf)
        for chain in (policy.refill_chain, policy.overflow_chain) if policy else ():
            for node in cmap.descendants(chain) if chain else ():
                if node not in shown:
                    shown.append(node)
    # The far end of each rule using a shown chain, unless it is shown already.
    for node in list(shown):
        for use in cmap.uses.get(node, ()):
            if use.kind in ("draw", "sweep") and use.detail not in shown:
                shown.append(use.detail)
    ids = _node_ids(shown)
    lines = ["flowchart LR"]
    for node in shown:
        label = cmap.label(node)
        if cmap.is_interior(node):
            lines.append(f"    {ids[node]}([{_quoted(label)}])")
        else:
            policy = cmap.policies.get(node)
            text = label if policy is None else f"{label}<br/><small>{policy_text(cmap, policy)}</small>"
            lines.append(f"    {ids[node]}[{_quoted(text)}]")
    for node in shown:
        for step in cmap.steps.get(node, ()):
            if step.target in ids:
                share = share_text(step)
                lines.append(f"    {ids[node]} ---|{_quoted(f'{step.order}: {share}')}| {ids[step.target]}")
    for leaf, policy in cmap.policies.items():
        if leaf not in ids:
            continue
        if policy.refill_chain in ids:
            lines.append(f"    {ids[policy.refill_chain]} -.->|\"refill, before a debit\"| {ids[leaf]}")
        if policy.overflow_chain in ids:
            lines.append(f"    {ids[leaf]} ==>|\"overflow, month end\"| {ids[policy.overflow_chain]}")
    for node in shown:
        for use in cmap.uses.get(node, ()):
            if use.kind == "draw":
                lines.append(f"    {ids[node]} -->|{_quoted(use.by)}| {ids[use.detail]}")
            elif use.kind == "sweep":
                lines.append(f"    {ids[use.detail]} -->|{_quoted(use.by)}| {ids[node]}")
    return "\n".join(lines)
