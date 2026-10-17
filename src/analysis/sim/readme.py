"""A scenario folder's optional ``README.md``: title + first paragraph,
baked into the intent's ``scenario.description`` at load time so it travels
with every run file (a README.md on disk isn't something a portable run
file can read back later) -- ``scenario.csv``'s own ``description`` column
is the fallback when there's no ``README.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from analysis.sim.connection import Connection

from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = ["ReadmeSummary", "parse_readme_summary", "readme_description"]


@dataclass(frozen=True)
class ReadmeSummary:
    title: str
    paragraph: str
    has_more: bool


def _blocks(text: str) -> list[str]:
    """Blank-line-separated blocks; a block's own lines are joined with a
    single space, so a paragraph wrapped across several source lines reads
    as one line."""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line.strip())
        elif current:
            blocks.append(" ".join(current))
            current = []
    if current:
        blocks.append(" ".join(current))
    return blocks


def parse_readme_summary(text: str) -> ReadmeSummary:
    """The title (first ATX heading, if any), the first paragraph after it,
    and whether anything follows that paragraph.

    The title line is pulled off before blockifying the rest, not treated
    as the first block: an ATX heading is a single line by definition, and
    markdown doesn't require a blank line after one, so `# Title` directly
    followed by `First paragraph.` on the very next line must not get
    joined into one block (reading the whole thing as a title with no
    paragraph at all). The remaining body is otherwise deliberately simple:
    a block is whatever blank lines separate, with no attempt to
    distinguish a sub-heading or a list from an ordinary paragraph -- good
    enough for "is there more than this one paragraph," which is all a
    summary needs to know.
    """
    lines = text.splitlines()
    title = ""
    body = text
    if lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip("#").strip()
        body = "\n".join(lines[1:])
    rest = _blocks(body)
    if not rest:
        return ReadmeSummary(title, "", False)
    return ReadmeSummary(title, rest[0], len(rest) > 1)


_LEADING_HEADING_RE = re.compile(r"^(#+)(\s.*)?$")


def relevel_heading(text: str, level: int) -> str:
    """If ``text`` starts with a markdown ATX heading, rewrite its level to
    ``level`` (one or more ``#``) -- a summary's own stored heading level
    (always 1, from ``readme_description``) isn't necessarily right for
    wherever it ends up displayed."""
    lines = text.splitlines()
    if lines:
        m = _LEADING_HEADING_RE.match(lines[0])
        if m:
            lines[0] = "#" * level + (m.group(2) or "")
    return "\n".join(lines)


_MORE_LINK_RE = re.compile(
    r"^_See (?:`(scenarios/[^`]+/README\.md)`|\[(scenarios/[^\]]+/README\.md)\]\([^)]+\)) for more\._$"
)


def _more_link(scenario_name: str, data_dir: Path) -> str:
    """The "see more" trailer line for a README with more than one
    paragraph: a markdown link to the full README.md when it's found under
    ``data_dir``, else a plain backtick reference. Always keyed off the
    same canonical relative path (``scenarios/<name>/README.md``), which is
    also what ``_MORE_LINK_RE`` matches back out of a stored description.
    """
    rel = f"scenarios/{scenario_name}/README.md"
    readme = Path(data_dir) / rel
    if readme.exists():
        return f"_See [{rel}]({readme}) for more._"
    return f"_See `{rel}` for more._"


def _regenerate_more_link(description: str, data_dir: Path) -> str:
    """If ``description``'s last line is a previously-generated "see more"
    link, rebuild it against ``data_dir``.

    The absolute path such a link points at is only ever correct for the
    environment it was generated in (ADR-003 §4.1e: the description is
    baked into the intent/run file once, at load time, so it travels to
    wherever that file is later opened) -- regenerating it against the
    *current* ``data_dir`` is what makes the link work again there, instead
    of carrying a stale, possibly non-existent path from wherever it was
    first built. A description whose last line doesn't match (hand-written
    in scenario.csv, or a README with only one paragraph) is returned
    unchanged.
    """
    lines = description.splitlines()
    if not lines:
        return description
    m = _MORE_LINK_RE.match(lines[-1])
    if not m:
        return description
    rel = m.group(1) or m.group(2)
    scenario_name = rel.split("/")[1]
    lines[-1] = _more_link(scenario_name, data_dir)
    return "\n".join(lines)


def readme_description(scenario_dir: Path, data_dir: Path) -> str | None:
    """``scenario_dir/README.md``'s title + first paragraph, formatted for
    ``scenario.description`` -- ``None`` if there's no README.md, so the
    caller keeps ``scenario.csv``'s own description. A README.md with nothing
    to extract still wins: its description is "" (ADR-003 §4.1e), not
    scenario.csv's.
    """
    path = Path(scenario_dir) / "README.md"
    if not path.is_file():
        return None
    summary = parse_readme_summary(path.read_text(encoding="utf-8"))
    if not summary.title and not summary.paragraph:
        return ""
    parts = []
    if summary.title:
        parts.append(f"# {summary.title}")
    if summary.paragraph:
        parts.append(summary.paragraph)
    if summary.has_more:
        parts.append(_more_link(Path(scenario_dir).name, data_dir))
    return "\n\n".join(parts)


def description_view(conn: Connection, *,
                     heading_level: int = 3,
                     data_dir: Path | str | None = None) -> StyledMarkdown:
    """The scenario's description -- a README.md's title + first paragraph,
    baked in at load time, or scenario.csv's own description when there's
    no README.md (ADR-003 §4.1e) -- rendered as markdown, with any leading
    heading re-levelled to ``heading_level`` so it fits inside a notebook
    section rather than carrying whatever level happened to be first in the
    source README. When ``data_dir`` is given, a generated "see more" link
    (``_MORE_LINK_RE``) is regenerated against it first, so a description
    baked in under one data_dir still links correctly when later viewed
    against another.
    """
    row = conn.items("scenario").iloc[0]
    description = row.get("description") or ""
    if data_dir is not None:
        description = _regenerate_more_link(description, Path(data_dir))
    parts = [relevel_heading(description, heading_level), lineage_markdown(row)]
    return StyledMarkdown("\n\n".join(part for part in parts if part))


def lineage_markdown(scenario_row: Mapping[str, Any]) -> str:
    """What a scenario inherits from (ADR-003 §4.5), from its intent/run
    file's ``scenario`` row: its direct parents -- the main parent, then any
    mix-ins -- and, when it has any, the whole load order the loader overlaid,
    root first. "" for a file written before schema 31, which didn't record
    it."""
    parents_json = scenario_row.get("parents_json")
    if parents_json is None:
        return ""
    parents = json.loads(parents_json)
    if not parents:
        return "**Inherits from:** nothing (a root scenario)"
    main, *mixins = parents
    lines = [f"**Parent:** `{main}`"]
    if mixins:
        lines[0] += "; **mix-ins:** " + ", ".join(f"`{m}`" for m in mixins)
    order = json.loads(scenario_row.get("load_order_json") or "[]")
    lines.append("**Load order:** " + " → ".join(f"`{n}`" for n in order))
    return "  \n".join(lines)
