"""A scenario's latent factors as the trees they form (ADR-008, #369), and
the varying rows that hang from them: what ``sim ensemble factors`` lists
and draws.

A latent factor is a shared influence with no value of its own; each one
names at most one parent and its ρ to it, so the factors form a forest. A
varying row (or lifespan, or strategy parameter) correlated into that
forest belongs to its nearest factor above it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from analysis.sim.correlation import Link, implied_correlation
from analysis.sim.draws_db import row_affects
from analysis.sim.ensemble import correlation_links, strategy_vary_rows
from analysis.sim.registries import LATENT_FACTOR_KIND, LIFESPAN_NODE_PREFIX

if TYPE_CHECKING:
    from matplotlib.figure import Figure

__all__ = ["Factor", "FactorTree", "Varying", "factor_tree", "factor_tree_figure", "mermaid"]


@dataclass(frozen=True)
class Factor:
    name: str
    parent: str | None
    rho: float | None
    description: str
    # On a tree's root: the Student-t copula's degrees of freedom (#68).
    nu: float | None = None
    # The varying rows (or lifespans) between this factor and its parent,
    # nearest first: a factor may hang from a row (ADR-008 §4), and its
    # ``rho`` is then the product along the way.
    via: tuple[str, ...] = ()


@dataclass(frozen=True)
class Varying:
    """A varying node: what it moves, in the author's names, and how it is drawn."""

    name: str
    varies: str
    distribution: str


@dataclass(frozen=True)
class FactorTree:
    factors: dict[str, Factor]
    """In the order authored, parents before children."""
    varying: list[Varying]
    links: dict[str, Link] = field(repr=False)

    def children(self, name: str | None) -> list[Factor]:
        return [f for f in self.factors.values() if f.parent == name
                or (name is None and f.parent not in self.factors)]

    def walk(self) -> Iterator[tuple[Factor, int]]:
        """Every factor with its depth, each tree root first, depth first."""
        def down(factor: Factor, depth: int) -> Iterator[tuple[Factor, int]]:
            yield factor, depth
            for child in self.children(factor.name):
                yield from down(child, depth + 1)
        for root in self.children(None):
            yield from down(root, 0)

    def factor_of(self, node: str) -> str | None:
        """The nearest factor above ``node``, or ``None`` when it is
        independent or correlated only with other rows."""
        seen: set[str] = set()
        while node in self.links and node not in seen:
            seen.add(node)
            node = self.links[node].parent
            if node in self.factors:
                return node
        return None

    def root(self, name: str) -> str:
        """The root of the factor tree ``name`` is in."""
        while (parent := self.factors[name].parent) in self.factors:
            name = parent
        return name

    def rho(self, a: str, b: str) -> float:
        """The correlation the tree implies between two nodes' scores."""
        return implied_correlation(self.links, a, b)


def factor_tree(con: sqlite3.Connection) -> FactorTree:
    """The factor forest and the varying nodes of a loaded intent."""
    links = correlation_links(con)
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    columns = {row[1] for row in con.execute("PRAGMA table_info(calendar_event)")}
    nu = "vary_tail_dependence" if "vary_tail_dependence" in columns else "NULL"
    rows = cur.execute(
        f"SELECT name, description, {nu} AS nu FROM calendar_event WHERE kind = ? ORDER BY id",
        (LATENT_FACTOR_KIND,),
    ).fetchall()
    names = {row["name"] for row in rows}
    factors: dict[str, Factor] = {}
    for row in rows:
        parent, via = _factor_parent(links, names, row["name"])
        factors[row["name"]] = Factor(
            row["name"], parent,
            None if parent is None else implied_correlation(links, row["name"], parent),
            row["description"] or "", row["nu"], via)
    varying = [
        Varying(row["name"], _row_varies(row), _distribution(row))
        for row in cur.execute(
            "SELECT * FROM calendar_event WHERE vary_distribution IS NOT NULL AND name IS NOT NULL "
            "ORDER BY id")
    ]
    if "vary_distribution" in {row[1] for row in con.execute("PRAGMA table_info(person_lifespan)")}:
        varying += [
            Varying(LIFESPAN_NODE_PREFIX + row["person"], f"{row['person']}'s age at death",
                    row["vary_distribution"])
            for row in cur.execute(
                "SELECT * FROM person_lifespan WHERE vary_distribution IS NOT NULL ORDER BY person")
        ]
    varying += [Varying(row["name"], f"strategy {row['strategy']}", row["vary_distribution"])
                for row in strategy_vary_rows(con)]
    return FactorTree(factors, varying, links)


def _factor_parent(
    links: dict[str, Link], factors: set[str], name: str,
) -> tuple[str | None, tuple[str, ...]]:
    """The nearest factor above ``name`` and the other nodes passed on the
    way; ``None`` when nothing above it is a factor (a tree's root)."""
    via: list[str] = []
    while name in links:
        name = links[name].parent
        if name in factors:
            return name, tuple(via)
        if name in via:  # the loader refuses cycles; don't loop on one
            break
        via.append(name)
    return None, ()


def _row_varies(row: sqlite3.Row) -> str:
    what = " ".join(part for part in (row["kind"], row_affects(row)) if part)
    return what if row["vary_target"] in (None, "amount") else f"{what} ({row['vary_target']})"


def _distribution(row: sqlite3.Row) -> str:
    process = row["vary_process"] if "vary_process" in row.keys() else None
    return f"{row['vary_distribution']}, {process} path" if process else row["vary_distribution"]


def mermaid(tree: FactorTree) -> str:
    """A Mermaid flowchart of the factors, top down, each arrow carrying the
    child's ρ to its parent, as the template READMEs draw them."""
    ids = {name: f"f{i}" for i, name in enumerate(tree.factors)}

    def node(factor: Factor) -> str:
        label = factor.name.replace('"', "#quot;")
        if factor.nu is not None:
            label += f"<br/>t copula, ν {factor.nu:g}"
        return f'{ids[factor.name]}(["{label}"])'

    # Every node first, depth first, so each subtree's nodes sit together
    # and the layout keeps its arrows from crossing; then the arrows.
    walked = [factor for factor, _depth in tree.walk()]
    lines = ["flowchart TB"]
    lines += [f"    {node(factor)}" for factor in walked]
    # A link through varying rows is dotted, and names them.
    lines += [f"    {ids[f.parent]} -->|{f.rho:.3g}| {ids[f.name]}" if not f.via else
              f"    {ids[f.parent]} -.->|\"{f.rho:.3g} via {', '.join(f.via)}\"| {ids[f.name]}"
              for f in walked if f.parent in tree.factors]
    return "\n".join(lines)


_SPINE = 0.22
"""How far short of a column of children their parent's spine runs, in columns."""


def factor_tree_figure(
    tree: FactorTree, title: str | None = None, size: tuple[float, float] | None = None,
    *, varying: bool = False,
) -> Figure:
    """The factors drawn left to right, each root at the left and its
    children to its right, each link labeled with the child's ρ to its
    parent. Mermaid's layout crosses arrows on a tree this shape, and its
    top-down chart runs six times wider than tall; a left-to-right tree
    stacks its leaves, so it fits 16:9 with the text still readable.
    ``size`` is the figure's in inches (default 12 wide, 16:9).

    With ``varying``, each factor's varying rows hang from it too, each a
    plain box saying what it moves and how it is drawn, on a dotted link
    labeled with its ρ to the factor; the rows in no tree hang from an
    unboxed "on their own", with no ρ. Those boxes are two lines each, so
    the figure grows taller than ``size`` when they need the room."""
    from matplotlib.figure import Figure

    from analysis.sim.plotting import GRID, INK, MUTED

    under: dict[str | None, list[Varying]] = {}
    if varying:
        for node in tree.varying:
            under.setdefault(tree.factor_of(node.name), []).append(node)

    # Nodes are ("factor", name), ("row", name) or ("alone", ""), the
    # unboxed parent of the rows in no tree.
    Node = tuple[str, str]

    def children(node: Node) -> list[Node]:
        if node[0] == "factor":
            return ([("factor", f.name) for f in tree.children(node[1])]
                    + [("row", v.name) for v in under.get(node[1], [])])
        if node[0] == "alone":
            return [("row", v.name) for v in under.get(None, [])]
        return []

    # A tidy tree: leaves take the next row down, each parent sits midway
    # between its first and last child, and a blank row parts two trees.
    place: dict[Node, tuple[float, float]] = {}
    row = 0.0

    def lay(node: Node, depth: int) -> float:
        nonlocal row
        kids = children(node)
        if kids:
            ys = [lay(kid, depth + 1) for kid in kids]
            y = (ys[0] + ys[-1]) / 2
        else:
            y, row = row, row + 1
        place[node] = (depth, y)
        return y

    roots: list[Node] = [("factor", f.name) for f in tree.children(None)]
    roots += [("alone", "")] if under.get(None) else []
    for root in roots:
        lay(root, 0)
        row += 1
    width, height = size or (12.0, 12.0 * 9 / 16)
    if varying:
        height = max(height, (row - 1) * _ROW_INCHES)
    fig = Figure(figsize=(width, height))
    ax = fig.subplots()
    ax.set_axis_off()
    depth = max((x for x, _ in place.values()), default=0)
    ax.set_xlim(-0.05, depth + 0.85)
    ax.set_ylim(max(row - 1.5, 0.5), -0.6)
    rows = {v.name: v for v in tree.varying}
    for node, (x, y) in place.items():
        kind, name = node
        if kind == "factor":
            factor = tree.factors[name]
            label = name if factor.nu is None else f"{name} (t copula, ν {factor.nu:g})"
            ax.text(x, y, label, ha="left", va="center", fontsize=10, color=INK, zorder=3,
                    bbox={"boxstyle": "round,pad=0.35,rounding_size=0.8",
                          "facecolor": "#eef1fb", "edgecolor": "#7d8fd6"})
        elif kind == "row":
            ax.text(x, y, _row_label(rows[name]), ha="left", va="center", fontsize=8,
                    color=INK, zorder=3, linespacing=1.15,
                    bbox={"boxstyle": "square,pad=0.35", "facecolor": "white",
                          "edgecolor": MUTED, "linestyle": ":"})
        else:
            ax.text(x, y, "on their own", ha="left", va="center", fontsize=10,
                    color=MUTED, style="italic", zorder=3,
                    bbox={"boxstyle": "square,pad=0.35", "facecolor": "white", "edgecolor": "none"})
        kids = children(node)
        if not kids:
            continue
        # Elbows: right from the parent to a spine just short of its
        # children, then across to each; each child's ρ sits on its own
        # arm, so a parent with many children doesn't stack them up.
        spine = x + 1 - _SPINE
        ys = [place[kid][1] for kid in kids]
        ax.plot([x, spine], [y, y], color=MUTED, lw=1, zorder=1)
        ax.plot([spine, spine], [min(ys + [y]), max(ys + [y])], color=MUTED, lw=1, zorder=1)
        for kid, cy in zip(kids, ys, strict=True):
            # A row's link is dotted, and a factor's link through varying
            # rows dashed: its ρ is the product.
            if kid[0] == "row":
                style = ":"
                rho = None if kind == "alone" else tree.rho(kid[1], name)
            else:
                style = "--" if tree.factors[kid[1]].via else "-"
                rho = tree.factors[kid[1]].rho
            ax.plot([spine, x + 1], [cy, cy], color=MUTED, lw=1, zorder=1, linestyle=style)
            if rho is not None:
                ax.text(spine + _SPINE / 2, cy, f"{rho:.3g}", ha="center", va="center",
                        fontsize=9, color=INK, zorder=2,
                        bbox={"boxstyle": "square,pad=0.15", "facecolor": GRID,
                              "edgecolor": "none"})
    if title:
        ax.set_title(title, loc="left", color=INK, fontsize=11)
    return fig


_ROW_INCHES = 0.6
"""The height a two-line varying row's box needs, so the chart grows to fit them."""


def _row_label(node: Varying) -> str:
    """What a varying row moves, over how it is drawn, in short: a
    ``truncated_normal`` is ``trunc_normal``, and a process is in parentheses."""
    distribution = node.distribution.replace("truncated_normal", "trunc_normal")
    head, comma, process = distribution.partition(", ")
    if comma:
        distribution = f"{head} ({process.removesuffix(' path')})"
    return f"{node.varies}\n{distribution}"
