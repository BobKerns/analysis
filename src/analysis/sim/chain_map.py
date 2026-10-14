"""A scenario's chains as a graph, for reading them rather than running them (issue #129).

A chain (ADR-003 §4.7) is an ordered list of steps, each a lot or another
chain; every lot is also a one-step *leaf* chain of its own. A named chain
has a role: a *source* chain is drawn from, by a rule or as a lot's
``refill_chain``, taking money out of its steps in turn (a *draw*); a
*destination* chain is paid into, by a rule or as a lot's
``overflow_chain``, putting money into them (a *sweep*). :func:`chain_map` gathers the steps, the lot policies the steps
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
    roles: dict[str, str] = field(default_factory=dict)
    """Interior chain name to its role (``source``, ``destination``)."""
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
            cmap.roles[row.name] = row.role
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
    # A calendar transfer becomes a rule only when the engine runs, so the
    # intent's calendar rows are read directly; only these two kinds move money.
    for row in named_rows(sc.items("calendar_event")):
        if row.kind not in ("one_off_transfer", "transfer_balance"):
            continue
        what = f"calendar {row.name or row.kind} ({row.date})"
        source, destination = _text(row.account_1), _text(row.account_2)
        if source in cmap.steps:
            cmap.uses.setdefault(source, []).append(Use("draw", what, destination or ""))
        if destination in cmap.steps:
            cmap.uses.setdefault(destination, []).append(Use("sweep", what, source or ""))
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
            return f"[bold]{escape(node)}[/bold] [dim]({cmap.roles[node]} chain)[/dim]"
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


_PICKED = "fill:#ffe08a,stroke:#b8860b,stroke-width:3px,color:#000"
"""The highlight on the chains a chart was drawn for: dark text on amber
reads in a light or a dark theme."""
_REFILL = '"refill, before a debit"'
_OVERFLOW = '"overflow, month end"'


def mermaid(cmap: ChainMap, names: Iterable[str] | None = None, *, rules: bool = False) -> str:
    """A Mermaid flowchart of ``names`` (default: every interior chain) and
    everything money passes through on its way through them, every arrow
    pointing the way money moves, left to right (issue #143).

    A source chain is drawn from, so money flows from its steps into it and
    out along the rules that draw from it; the lots it reaches bring their
    refill chains, which feed them. A destination chain is paid into, so
    money flows in along the rules that pay into it and out to its steps;
    the lots it reaches bring their overflow chains, which they feed. A
    source chain never follows overflow, nor a destination chain refill: a
    lot's other policy is about money that never passes through the chain.
    Steps and rules are solid arrows, refill and overflow dotted ones.
    The chains ``names`` asks for are highlighted, so they stand out in a
    tangled chart; with no ``names``, every chain was asked for and none is.

    The rules and calendar transfers using a shown chain are left out unless
    ``rules`` asks for them: they are many, often fire once, and each adds a
    node and perhaps a loop, where the chains are what the chart is for.
    """
    roots = list(cmap.steps) if names is None else list(names)
    shown: list[str] = []
    edges: list[tuple[str, str, str, str]] = []  # (from, arrow, label, to), in the order drawn

    def show(node: str) -> None:
        if node not in shown:
            shown.append(node)

    def edge(source: str, arrow: str, label: str, target: str) -> None:
        if (source, arrow, label, target) not in edges:
            edges.append((source, arrow, label, target))

    expanded: set[tuple[str, str | None]] = set()

    def expand(node: str, role: str | None) -> None:
        """Show ``node`` and what money passes through it, reached as a
        ``role`` chain's step (a root lot has no role, so it brings both
        its policies)."""
        role = cmap.roles.get(node, role)
        show(node)
        if (node, role) in expanded:  # a refill or overflow may lead back here
            return
        expanded.add((node, role))
        for step in cmap.steps.get(node, ()):
            label = _quoted(f"{step.order}: {share_text(step)}")
            if role == "source":
                edge(step.target, "-->", label, node)
            else:
                edge(node, "-->", label, step.target)
            expand(step.target, role)
        policy = cmap.policies.get(node)
        if policy is None:
            return
        if role != "destination" and policy.refill_chain:
            edge(policy.refill_chain, "-.->", _REFILL, node)
            expand(policy.refill_chain, "source")
        if role != "source" and policy.overflow_chain:
            edge(node, "-.->", _OVERFLOW, policy.overflow_chain)
            expand(policy.overflow_chain, "destination")

    for root in roots:
        expand(root, None)
    # One step further along the money from what is shown so far: the lots
    # that refill from a shown source chain or overflow into a shown
    # destination, the chains a root is a step of, and (with ``rules``) the
    # far end of each rule using a shown chain. None is followed on from there.
    reached = list(shown)
    parents: list[str] = []
    for root in roots:
        for use in cmap.uses.get(root, ()):
            if use.kind != "step":
                continue
            step = next(s for s in cmap.steps[use.by] if str(s.order) == use.detail)
            label = _quoted(f"{step.order}: {share_text(step)}")
            show(use.by)
            parents.append(use.by)
            if cmap.roles.get(use.by) == "source":
                edge(root, "-->", label, use.by)
            else:
                edge(use.by, "-->", label, root)
    for leaf, policy in cmap.policies.items():
        if policy.refill_chain in reached and cmap.roles.get(policy.refill_chain) == "source":
            show(leaf)
            edge(policy.refill_chain, "-.->", _REFILL, leaf)
        if policy.overflow_chain in reached and cmap.roles.get(policy.overflow_chain) == "destination":
            show(leaf)
            edge(leaf, "-.->", _OVERFLOW, policy.overflow_chain)
    # A root's parent chains bring their rules too, but nothing further.
    for node in dict.fromkeys([*reached, *parents]) if rules else ():
        for use in cmap.uses.get(node, ()):
            if use.kind == "draw":
                show(use.detail)
                edge(node, "-->", _quoted(use.by), use.detail)
            elif use.kind == "sweep":
                show(use.detail)
                edge(use.detail, "-->", _quoted(use.by), node)
    return _render(cmap, shown, edges, roots if names is not None else [])


def _render(
    cmap: ChainMap, shown: list[str], edges: list[tuple[str, str, str, str]], picked: list[str],
) -> str:
    """The Mermaid text for ``shown`` and ``edges``, ``picked`` highlighted.

    Mermaid lays a flowchart out left to right only while it has no loop:
    given one, it turns an edge around and the chart reads both ways. Money
    can go round one (a rule paying into a lot of the very chain it draws
    from, as an IRA contribution from the cash an IRA refills), so an edge
    that would close a loop goes to a copy of its target instead, a node of
    its own (``c`` in place of the node id's ``n``) that leads nowhere
    further.

    The lots of an account whose lots are named sit in a box titled with the
    account (issue #144). A copy gets a box of its own: in its original's,
    Mermaid would pull it back beside the original and turn its edge around."""
    ids = _node_ids(shown)
    lines = ["flowchart LR", *_grouped(cmap, [(ids[node], node) for node in shown], "a", with_policy=True)]
    after: dict[str, set[str]] = {}  # node id -> the ids its edges lead to so far

    def leads_to(start: str, goal: str) -> bool:
        seen, todo = set(), [start]
        while todo:
            if (node := todo.pop()) == goal:
                return True
            if node not in seen:
                seen.add(node)
                todo.extend(after.get(node, ()))
        return False

    copies: dict[str, str] = {}  # copy id -> the node it copies
    drawn: list[str] = []
    for a, arrow, label, b in edges:
        source, target = ids[a], ids[b]
        if leads_to(target, source):
            target = "c" + target[1:]  # one copy per node, however many loops end there
            copies.setdefault(target, b)
        after.setdefault(source, set()).add(target)
        drawn.append(f"    {source} {arrow}|{label}| {target}")
    lines += _grouped(cmap, list(copies.items()), "ac", with_policy=False)
    lines += drawn
    if picked:
        lines.append(f"    classDef picked {_PICKED}")
        lines.append(f"    class {','.join(ids[root] for root in dict.fromkeys(picked))} picked")
    return "\n".join(lines)


def _boxed(cmap: ChainMap, node: str) -> str | None:
    """The account whose box ``node`` is drawn in: one whose lots are named."""
    if node in cmap.leaves and (account := cmap.leaves[node][0]) in cmap.multi_lot:
        return account
    return None


def _grouped(cmap: ChainMap, nodes: list[tuple[str, str]], prefix: str, *, with_policy: bool) -> list[str]:
    """The Mermaid lines declaring ``nodes`` (``(id, node)`` pairs), the lots
    of each boxed account in one ``subgraph`` where its first lot comes."""
    boxes: dict[str, list[tuple[str, str]]] = {}
    for nid, node in nodes:
        if (account := _boxed(cmap, node)) is not None:
            boxes.setdefault(account, []).append((nid, node))
    box_ids = {account: f"{prefix}{i}" for i, account in enumerate(boxes)}
    lines: list[str] = []
    for nid, node in nodes:
        account = _boxed(cmap, node)
        if account is None:
            lines.append(f"    {nid}{_shape(cmap, node, with_policy=with_policy)}")
        elif (lots := boxes.pop(account, None)) is not None:  # None: its box is already drawn
            lines.append(f"    subgraph {box_ids[account]}[{_quoted(account)}]")
            lines += [f"        {lot_id}{_shape(cmap, lot, with_policy=with_policy, boxed=True)}"
                      for lot_id, lot in lots]
            lines.append("    end")
    return lines


def _shape(cmap: ChainMap, node: str, *, with_policy: bool, boxed: bool = False) -> str:
    """A node's Mermaid shape and label: a rounded box for a chain, a box for
    a lot, with the lot's policy under its name when ``with_policy``. A lot
    drawn in its account's box (``boxed``) is named by its lot alone."""
    label = cmap.leaves[node][1] if boxed else cmap.label(node)
    if cmap.is_interior(node):
        return f"([{_quoted(label)}])"
    policy = cmap.policies.get(node) if with_policy else None
    text = label if policy is None else f"{label}<br/><small>{policy_text(cmap, policy)}</small>"
    return f"[{_quoted(text)}]"
