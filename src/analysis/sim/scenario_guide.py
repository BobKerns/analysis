"""Guided entry for ``sim scenario edit`` (issue #190).

A scenario's own settings are its ``scenario.csv`` row and two of its
``meta_json`` keys. They are described here as data for
``calendar_guide.Guide`` to ask, as a lot's or a scheduled rule's fields are.
Every field is optional: a blank one inherits, except the description, where
a blank restores the default ``Derived from <parents>``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

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
            # Asked one name at a time (``walk_parents``), not as this one line.
            Field("parents", "builds on (parent, then any mix-ins)",
                  option="--parents", required=False,
                  choices=lambda ctx, answers: list(scenarios)),  # noqa: ARG005
            Field("seed", "ensemble master seed", type="seed", option="--seed", required=False,
                  help=f"Blank inherits it; '{NEW_SEED}' draws a fresh one."),
            Field("size", "ensemble member count", type="count", option="--size", required=False,
                  help="0 makes it not an ensemble; blank inherits it."),
            Field("reserve", "reserve in CPI-constant dollars", type="money", option="--reserve",
                  required=False, help="For the ensemble reports; blank inherits it."),
        ),
    )


def pool_spec(ensembles: Sequence[str]) -> KindSpec:
    """The fields of ``sim ensemble pool edit`` (ADR-009); ``ensembles``
    complete the components."""
    return KindSpec(
        "edit", "a pool's definition", words=("sim", "ensemble", "pool"),
        fields=(
            Field("components", "components (NAME or NAME=PCT, space-separated)",
                  help="Weights add up to 100%; none gives equal shares.",
                  choices=lambda ctx, answers: list(ensembles)),  # noqa: ARG005
            Field("base", "base component (its member 0 is the pool's)", option="--base",
                  required=False, help="Blank takes the first component.",
                  choices=lambda ctx, answers: [  # noqa: ARG005
                      word.partition("=")[0] for word in answers.get("components", "").split()]),
            Field("size", "member count", type="count", option="--size", required=False,
                  help="Blank takes the smallest component's."),
            Field("reserve", "reserve in CPI-constant dollars", type="money", option="--reserve",
                  required=False, help="Blank takes the base component's."),
            Field("description", "description", option="--description", required=False,
                  help="Blank restores the generated 'Pooled from <components>'."),
        ),
    )


def pool_answers(row: dict[str, str]) -> Answers:
    """A pool's ``scenario.csv`` row as answers to :func:`pool_spec`'s fields."""
    try:
        meta = json.loads(row.get("meta_json") or "{}")
    except json.JSONDecodeError:
        meta = {}
    spec = meta.get("pool") if isinstance(meta, dict) else None
    spec = spec if isinstance(spec, dict) else {}
    raw = spec.get("components") or []
    components = " ".join(f"{c}={raw[c]}" if isinstance(raw, dict) else str(c) for c in raw)
    return {
        "components": components,
        "base": str(spec.get("base") or ""),
        "size": "" if meta.get("ensemble_size") is None else str(meta["ensemble_size"]),
        "reserve": "" if meta.get("reserve") is None else str(meta["reserve"]),
        "description": row.get("description", ""),
    }


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


WALK_HELP = "Enter keeps, - removes, +NAME inserts NAME before it, NAME replaces it"


def walk_parents(
    current: Sequence[str], choices: Sequence[str],
    ask: Callable[[str, list[str], str], str], echo: Callable[[str], None],
    *, itself: str | None = None,
) -> list[str]:
    """A scenario's parents, asked one at a time (#259): each of ``current``
    in turn, offered as the default, then more mix-ins until a blank answer.

    ``ask(text, completions, default)`` reads one line. ``choices`` are the
    names a parent may be; ``itself`` (the scenario being edited) is not one,
    nor is a name already in the list. The result may be empty: whether no
    parent at all is meant is the caller's to confirm."""
    allowed = [c for c in choices if c != itself]
    completions = [*allowed, *(f"+{c}" for c in allowed)]
    kept: list[str] = []
    pending = list(current)

    def problem(name: str, others: Sequence[str]) -> str | None:
        if name == itself:
            return f"{name} can't build on itself"
        if name not in allowed:
            return f"not a scenario: {name!r}"
        if name in others:
            return f"{name} is already in the list"
        return None

    def label() -> str:
        return "parent" if not kept else f"mix-in {len(kept)}"

    if pending:
        echo(f"  {WALK_HELP}")
    while pending:
        entry = pending[0]
        answer = ask(f"  {label()}", completions, entry).strip()
        if answer in ("", entry):
            kept.append(pending.pop(0))
        elif answer == "-":
            pending.pop(0)
        elif answer.startswith("+"):
            name = answer[1:].strip()
            if (why := problem(name, [*kept, *pending])) is not None:
                echo(f"  {why}")
            else:
                kept.append(name)
        elif (why := problem(answer, [*kept, *pending[1:]])) is not None:
            echo(f"  {why}")
        else:
            kept.append(answer)
            pending.pop(0)
    while True:
        prompt = "  add a mix-in (Enter when done)" if kept else "  builds on (Enter for none)"
        answer = ask(prompt, list(allowed), "").strip()
        if not answer:
            return kept
        if (why := problem(answer.removeprefix("+").strip(), kept)) is not None:
            echo(f"  {why}")
        else:
            kept.append(answer.removeprefix("+").strip())
