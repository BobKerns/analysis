"""Guided entry for ``sim scenario edit`` (issue #190).

A scenario's own settings are its ``scenario.csv`` row and two of its
``meta_json`` keys. They are described here as data for
``calendar_guide.Guide`` to ask, as a lot's or a scheduled rule's fields are.
Every field is optional: a blank one inherits, except the description, where
a blank restores the default ``Derived from <parents>``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from analysis.sim.calendar_guide import Answers, Field, KindSpec

NEW_SEED = "new"  # typed as the seed to draw a fresh one


def spec(scenarios: Sequence[str]) -> KindSpec:
    """The fields of ``sim scenario edit``; ``scenarios`` complete the parents."""
    return KindSpec(
        "edit", "a scenario's own settings", words=("sim", "scenario"),
        fields=(
            Field("description", "description", option="--description", required=False,
                  help="Blank restores the default, 'Derived from <parents>'."),
            Field("start", "first month (YYYY-MM)", type="month", option="--start", required=False,
                  help="Blank inherits it."),
            Field("end", "last month (YYYY-MM)", type="month", option="--end", required=False,
                  help="Blank inherits it."),
            Field("parents", "builds on (parent, then any mix-ins, colon-separated)",
                  option="--parents", required=False,
                  choices=lambda ctx, answers: list(scenarios),  # noqa: ARG005
                  help="Blank makes it a root scenario."),
            Field("seed", "ensemble master seed", type="seed", option="--seed", required=False,
                  help=f"Blank inherits it; '{NEW_SEED}' draws a fresh one."),
            Field("size", "ensemble member count", type="count", option="--size", required=False,
                  help="0 makes it not an ensemble; blank inherits it."),
            Field("reserve", "reserve in CPI-constant dollars", type="money", option="--reserve",
                  required=False, help="For the ensemble reports; blank inherits it."),
        ),
    )


def answers_of(row: dict[str, str]) -> Answers:
    """A ``scenario.csv`` row as answers to ``spec``'s fields."""
    try:
        meta = json.loads(row.get("meta_json") or "{}")
    except json.JSONDecodeError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "description": row.get("description", ""),
        "start": row.get("start", ""),
        "end": row.get("end", ""),
        "parents": row.get("parent_scenario", ""),
        "seed": row.get("seed", ""),
        "size": "" if meta.get("ensemble_size") is None else str(meta["ensemble_size"]),
        "reserve": "" if meta.get("reserve") is None else str(meta["reserve"]),
    }
