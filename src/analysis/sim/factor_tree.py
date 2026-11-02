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
    factors: dict[str, Factor] = {}
    for row in cur.execute(
        f"SELECT name, description, {nu} AS nu FROM calendar_event WHERE kind = ? ORDER BY id",
        (LATENT_FACTOR_KIND,),
    ):
        link = links.get(row["name"])
        factors[row["name"]] = Factor(
            row["name"], link.parent if link else None, link.rho if link else None,
            row["description"] or "", row["nu"])
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
    lines += [f"    {ids[f.parent]} -->|{f.rho:g}| {ids[f.name]}"
              for f in walked if f.parent in tree.factors]
    return "\n".join(lines)


_SPINE = 0.22
"""How far short of a column of children their parent's spine runs, in columns."""


def factor_tree_figure(
    tree: FactorTree, title: str | None = None, size: tuple[float, float] | None = None,
) -> Figure:
    """The factors drawn left to right, each root at the left and its
    children to its right, each arrow labeled with the child's ρ to its
    parent. Mermaid's layout crosses arrows on a tree this shape, and its
    top-down chart runs six times wider than tall; a left-to-right tree
    stacks its leaves, so it fits 16:9 with the text still readable.
    ``size`` is the figure's in inches (default 12 wide, 16:9)."""
    from matplotlib.figure import Figure

    from analysis.sim.plotting import GRID, INK, MUTED

    width, height = size or (12.0, 12.0 * 9 / 16)
    fig = Figure(figsize=(width, height))
    ax = fig.subplots()
    ax.set_axis_off()
    # A tidy tree: leaves take the next row down, each parent sits midway
    # between its first and last child, and a blank row parts two trees.
    place: dict[str, tuple[float, float]] = {}
    row = 0.0

    def lay(factor: Factor, depth: int) -> float:
        nonlocal row
        children = tree.children(factor.name)
        if children:
            ys = [lay(child, depth + 1) for child in children]
            y = (ys[0] + ys[-1]) / 2
        else:
            y, row = row, row + 1
        place[factor.name] = (depth, y)
        return y

    for root in tree.children(None):
        lay(root, 0)
        row += 1
    depth = max((x for x, _ in place.values()), default=0)
    ax.set_xlim(-0.05, depth + 0.85)
    ax.set_ylim(max(row - 1.5, 0.5), -0.6)
    for name, (x, y) in place.items():
        factor = tree.factors[name]
        label = name if factor.nu is None else f"{name} (t copula, ν {factor.nu:g})"
        ax.text(x, y, label, ha="left", va="center", fontsize=10, color=INK, zorder=3,
                bbox={"boxstyle": "round,pad=0.35,rounding_size=0.8", "facecolor": "#eef1fb",
                      "edgecolor": "#7d8fd6"})
        children = tree.children(name)
        if not children:
            continue
        # Elbows: right from the parent to a spine just short of its
        # children, then across to each; each child's ρ sits on its own
        # arm, so a parent with many children doesn't stack them up.
        spine = x + 1 - _SPINE
        ys = [place[child.name][1] for child in children]
        ax.plot([x, spine], [y, y], color=MUTED, lw=1, zorder=1)
        ax.plot([spine, spine], [min(ys + [y]), max(ys + [y])], color=MUTED, lw=1, zorder=1)
        for child, cy in zip(children, ys, strict=True):
            ax.plot([spine, x + 1], [cy, cy], color=MUTED, lw=1, zorder=1)
            ax.text(spine + _SPINE / 2, cy, f"{child.rho:g}", ha="center", va="center",
                    fontsize=9, color=INK, zorder=2,
                    bbox={"boxstyle": "square,pad=0.15", "facecolor": GRID, "edgecolor": "none"})
    if title:
        ax.set_title(title, loc="left", color=INK, fontsize=11)
    return fig
