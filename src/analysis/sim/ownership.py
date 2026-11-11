"""Whose an account's money is at a death (#255, #277).

The engine's basis step-up (ADR-003 §4.9) and the tax calculators' end of a
decedent's loss carryovers (§2.6) both ask how much of what an account holds
was the decedent's. They answer it here, so the two can't disagree.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

JOINT_OWNERS: frozenset[str | None] = frozenset({None, "", "joint"})
"""An account ``owner`` that means the household's, not one person's."""


@dataclass(frozen=True)
class Household:
    """The household and when each of its people dies.

    ``members`` are the ``primary`` persons, or everyone with a role if none
    is marked; two ``primary`` persons are spouses. ``final_months`` holds
    the last month (``YYYY-MM-DD``) of everyone with a lifespan."""
    members: tuple[str, ...]
    final_months: Mapping[str, str]
    spouses: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, people: Iterable[tuple[str, str | None]],
           final_months: Mapping[str, str]) -> Household:
        """The household of ``people``, each an ``(id, role)``."""
        people = list(people)
        primaries = sorted(p for p, role in people if role == "primary")
        members = primaries or sorted(p for p, role in people if role)
        spouses = dict([primaries, primaries[::-1]]) if len(primaries) == 2 else {}
        return cls(tuple(members), dict(final_months), spouses)

    def alive_after(self, person: str, month: str) -> bool:
        """Whether ``person`` lives past ``month``."""
        return person not in self.final_months or self.final_months[person] > month

    def decedent_part(self, decedent: str, owner: str | None, *,
                      community: bool = False) -> tuple[int, str] | None:
        """How much of an account ``owner`` holds was ``decedent``'s at their
        death: ``(1, rule)`` all of it, ``(2, rule)`` half, None none.

        Their own account is all theirs (``owner``), and so is one they
        inherited from a spouse who died before them (``inherited``). A
        joint account is the household's: all theirs when no one else in it
        is left alive (``last_survivor``), else half (``joint_half``: a
        spouses' joint tenancy, §2040(b), or community property's half).
        ``community`` makes it all of it (``community_property``), which a
        basis step-up wants (§1014(b)(6)) and a loss carryover doesn't: a
        community loss is still half each spouse's."""
        month = self.final_months[decedent]
        if owner in JOINT_OWNERS:
            if decedent not in self.members:
                return None
            if not any(p != decedent and self.alive_after(p, month) for p in self.members):
                return 1, "last_survivor"
            return (1, "community_property") if community else (2, "joint_half")
        if owner == decedent:
            return 1, "owner"
        if self.spouses.get(owner) == decedent and self.final_months.get(owner, month) < month:
            return 1, "inherited"
        return None
