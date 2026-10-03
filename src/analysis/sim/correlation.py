"""ADR-008: correlated parameters -- the Gaussian-copula tree math, pure
functions with no I/O.

A correlation structure is a forest: each node (a varying row or a latent
factor) names at most one parent, with a correlation ``rho`` between their
standard normal scores::

    z_child = rho * z_parent + sqrt(1 - rho**2) * epsilon_child

Every node's ``z`` is standard normal on its own, so mapping it through
``Phi`` gives a uniform probability ``u``, and a row's value is its own
distribution's ``value_at(u)``: the authored distribution is preserved
exactly, and only the joint behavior changes (ADR-008 §1).
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass
from statistics import NormalDist

__all__ = [
    "CorrelationError", "Link", "tree_members", "topological_order", "resolve_scores",
    "standard_normal", "probability", "expected_rank_correlation", "implied_correlation",
    "conditional_scores",
]

_STANDARD = NormalDist()


class CorrelationError(ValueError):
    """A correlation structure that isn't a forest (a cycle) or names a
    missing parent. The loader rejects these before resolution ever sees
    them; this is the defensive check."""


@dataclass(frozen=True)
class Link:
    """A node's parent in its correlation tree, and rho between their scores."""

    parent: str
    rho: float


def tree_members(links: Mapping[str, Link]) -> set[str]:
    """Every node in some correlation tree: each child and each parent. A
    varying row outside this set resolves exactly as before (ADR-008 §5)."""
    return set(links) | {link.parent for link in links.values()}


def topological_order(links: Mapping[str, Link]) -> list[str]:
    """The tree members, parents before children (roots first, in sorted
    order, then breadth-first; deterministic). Raises ``CorrelationError``
    on a cycle or a dangling parent."""
    members = tree_members(links)
    children: dict[str, list[str]] = {}
    for child, link in links.items():
        children.setdefault(link.parent, []).append(child)
    roots = sorted(m for m in members if m not in links)
    order: list[str] = []
    queue = list(roots)
    while queue:
        node = queue.pop(0)
        order.append(node)
        queue.extend(sorted(children.get(node, [])))
    if len(order) != len(members):
        stuck = sorted(members - set(order))
        raise CorrelationError(f"correlation cycle among {stuck}")
    return order


def resolve_scores(links: Mapping[str, Link], noise: Mapping[str, float]) -> dict[str, float]:
    """Each tree member's standard normal score from its own independent
    ``noise`` (a standard normal draw per member): a root's score is its
    noise; a child's is ``rho * z_parent + sqrt(1 - rho**2) * noise``."""
    scores: dict[str, float] = {}
    for node in topological_order(links):
        link = links.get(node)
        if link is None:
            scores[node] = noise[node]
        else:
            rho = link.rho
            scores[node] = rho * scores[link.parent] + math.sqrt(max(0.0, 1 - rho * rho)) * noise[node]
    return scores


def conditional_scores(links: Mapping[str, Link], node: str, z: float) -> dict[str, float]:
    """Every descendant of ``node`` at its conditional median given
    ``node``'s score is ``z``: the product of rho along the path down to it,
    times ``z`` (ADR-008 §5). Intermediate nodes are marginalized out, not
    held at their own medians: a grandchild's conditional mean given its
    grandparent is ``rho_1 * rho_2 * z``. ``node`` itself is not included."""
    scores = {node: z}
    for member in topological_order(links):
        link = links.get(member)
        if link is not None and link.parent in scores and member not in scores:
            scores[member] = link.rho * scores[link.parent]
    del scores[node]
    return scores


def standard_normal(rng: random.Random) -> float:
    """One standard normal draw by inverse CDF from ``rng.random()``, so a
    node's noise is a single call on its own keyed stream (ADR-006 §3)."""
    u = rng.random()
    while u == 0.0:  # inv_cdf rejects 0
        u = rng.random()
    return _STANDARD.inv_cdf(u)


def probability(z: float) -> float:
    """``Phi(z)``: the uniform probability a score maps a row's value through."""
    return _STANDARD.cdf(z)


def expected_rank_correlation(rho: float) -> float:
    """The Spearman rank correlation two rows' *values* get from a copula
    correlation ``rho`` between their scores: ``(6/pi) * asin(rho / 2)``,
    whatever their (continuous, untied) distributions. Within 0.02 of rho.
    Tied values (a short date range) correlate less; compare scores there
    (ADR-008 §1)."""
    return 6 / math.pi * math.asin(rho / 2)


def implied_correlation(links: Mapping[str, Link], a: str, b: str) -> float:
    """The correlation between two nodes' scores: the product of rho along
    the path joining them through their nearest common ancestor, 1 for a
    node with itself, 0 for nodes in different trees."""
    if a == b:
        return 1.0

    def ancestry(node: str) -> list[tuple[str, float]]:
        """``(ancestor, product of rho from node up to it)``, nearest first."""
        path = [(node, 1.0)]
        product = 1.0
        while node in links:
            product *= links[node].rho
            node = links[node].parent
            path.append((node, product))
        return path

    up_a = dict(ancestry(a))
    for ancestor, product_b in ancestry(b):
        if ancestor in up_a:
            return up_a[ancestor] * product_b
    return 0.0
