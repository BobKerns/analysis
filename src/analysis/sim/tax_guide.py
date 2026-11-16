"""The fields of ``sim tax add`` and ``sim tax edit`` (#507): a tax
jurisdiction file's row, ``taxes/<jurisdiction>.csv`` (ADR-003 §4.15), as
questions for ``calendar_guide.Guide``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from analysis.sim import registries
from analysis.sim.calendar_guide import Answers, Field, KindSpec

__all__ = ["FIELDS", "answers_of", "row_of", "spec"]

FIELDS = ("kind", "source_chain", "destination", "start", "end", "params_json", "description")
"""The row's columns the guide asks, in order; ``upstream`` follows from the
kind, and ``schedule`` has one value for now."""


def spec(
    command: str, *, endpoints: Sequence[str], expenses: Sequence[str],
    jurisdictions: Sequence[str] = (), source: str | None = None,
    destination: str | None = None, subkind: str | None = None,
    kinds: Sequence[str] = (), kind: str | None = None,
) -> KindSpec:
    """The questions for ``sim tax <command>``. ``endpoints`` complete the
    source (accounts and source chains), ``expenses`` the destination (the
    scenario's expense accounts, of the jurisdiction's own ``subkind`` when
    it is known, #586). ``jurisdictions``, for ``add``, are the ones the
    scenario lacks; ``source`` and ``destination`` are the defaults offered.
    ``kinds`` are the calculators that compute the jurisdiction, the data
    repo's included (#510), and ``kind`` the one offered."""
    into = f"Its own account, of subkind {subkind}." if subkind else ""
    asked: tuple[Field, ...] = ()
    if jurisdictions:
        asked = (Field("jurisdiction", "jurisdiction", strict=True,
                       choices=lambda ctx, answers: list(jurisdictions)),)  # noqa: ARG005
    return KindSpec(
        command, "a tax jurisdiction", words=("sim", "tax"),
        fields=(
            *asked,
            Field("kind", "calculator kind", option="--kind", default=kind, strict=bool(kinds),
                  required=False, help="Blank: the built-in one. `sim tax kinds` lists them.",
                  choices=lambda ctx, answers: list(kinds),  # noqa: ARG005
                  # Asked only when the data repo has a calculator for the
                  # jurisdiction; offered at the review either way.
                  ask=lambda ctx, answers: len(kinds) > 1),  # noqa: ARG005
            Field("source_chain", "paid from (an account or source chain)", option="--source-chain",
                  default=source,
                  choices=lambda ctx, answers: list(endpoints)),  # noqa: ARG005
            Field("destination", "paid into (an expense account)", option="--destination",
                  default=destination, strict=True, help=into,
                  choices=lambda ctx, answers: list(expenses)),  # noqa: ARG005
            Field("start", "first month it settles in", type="month", option="--start",
                  required=False, help="Blank: the scenario's start."),
            Field("end", "last month it settles in", type="month", option="--end", required=False,
                  help="Blank: the scenario's end."),
            Field("params_json", "params_json", type="json", option="--params-json", required=False,
                  help="The calculator's parameters, a JSON object; blank for none."),
            Field("description", "description", option="--description", required=False,
                  suggest=lambda ctx, answers: (  # noqa: ARG005
                      f"{answers['jurisdiction']} income tax for the year before"
                      if answers.get("jurisdiction") else None)),
        ),
    )


def answers_of(row: Mapping[str, str]) -> Answers:
    """A jurisdiction file's row as answers to :func:`spec`'s fields."""
    return {column: row.get(column, "") for column in FIELDS}


def row_of(jurisdiction: str, answers: Mapping[str, str], base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The file's row from ``answers``, over ``base`` (the row being edited)."""
    row = {**dict.fromkeys(registries.TAX_CSV_HEADER, ""), **(base or {})}
    row.update({column: (answers.get(column) or "").strip() for column in FIELDS})
    # Blank is the built-in calculator for the jurisdiction.
    row["kind"] = row["kind"] or registries.TAX_DEFAULT_KINDS[jurisdiction]
    return {column: row[column] for column in registries.TAX_CSV_HEADER}
