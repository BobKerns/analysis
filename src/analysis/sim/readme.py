"""A scenario folder's optional ``README.md``: title + first paragraph,
baked into the intent's ``scenario.description`` at load time so it travels
with every run file (a README.md on disk isn't something a portable run
file can read back later) -- ``scenario.csv``'s own ``description`` column
is the fallback when there's no ``README.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from analysis.sim.connection import Connection

from IPython.display import Markdown

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

    Deliberately simple: a block is whatever blank lines separate, with no
    attempt to distinguish a sub-heading or a list from an ordinary
    paragraph -- good enough for "is there more than this one paragraph,"
    which is all a summary needs to know.
    """
    blocks = _blocks(text)
    if not blocks:
        return ReadmeSummary("", "", False)
    title = ""
    rest = blocks
    if blocks[0].lstrip().startswith("#"):
        title = blocks[0].lstrip("#").strip()
        rest = blocks[1:]
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


def readme_description(scenario_dir: Path, data_dir: Path) -> str | None:
    """``scenario_dir/README.md``'s title + first paragraph, formatted for
    ``scenario.description`` -- ``None`` if there's no README.md (or it has
    nothing to extract), so the caller keeps ``scenario.csv``'s own
    description unchanged.
    """
    path = Path(scenario_dir) / "README.md"
    if not path.is_file():
        return None
    summary = parse_readme_summary(path.read_text(encoding="utf-8"))
    if not summary.title and not summary.paragraph:
        return None
    parts = []
    if summary.title:
        parts.append(f"# {summary.title}")
    if summary.paragraph:
        parts.append(summary.paragraph)
    if summary.has_more:
        readme = data_dir / scenario_dir / "README.md"
        if not readme.exists():
            parts.append(f"_See `scenarios/{Path(scenario_dir).name}/README.md` for more._")
        else:
            rel_readme = readme.relative_to(data_dir)
            parts.append(f"_See [{rel_readme}]({readme}) for more._")
    return "\n\n".join(parts)


def description_view(sc: Connection, *,
                     heading_level: int = 3,
                     data_dir: Path | str | None = None) -> Markdown:
    """The scenario's description -- a README.md's title + first paragraph,
    baked in at load time, or scenario.csv's own description when there's
    no README.md (ADR-003 §4.1e) -- rendered as markdown, with any leading
    heading re-levelled to ``heading_level`` so it fits inside a notebook
    section rather than carrying whatever level happened to be first in the
    source README.
    """
    description = sc.items("scenario").iloc[0].get("description") or ""
    return Markdown(relevel_heading(description, heading_level))
