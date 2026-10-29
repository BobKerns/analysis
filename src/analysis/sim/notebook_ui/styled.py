"""Markdown and HTML output styled by ``notebook.css``.

A front end's theme decides how plain ``Markdown`` output looks, and VS
Code's makes inline code too light to read. A linked stylesheet can't fix
that: VS Code's output webview can't fetch a file from the repo, and it
renders each Markdown output in its own shadow DOM, out of reach of a style
injected once. So each output carries the stylesheet itself, scoped to an
``analysis-md`` wrapper, and ``notebook.css`` stays the one place the
styling is defined.
"""

from __future__ import annotations

import re
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING

from IPython.display import Markdown
from markdown_it import MarkdownIt

if TYPE_CHECKING:
    from matplotlib.figure import Figure

__all__ = ["StyledMarkdown", "show_figure", "styled_html"]


@cache
def _stylesheet() -> str:
    # The comments are for whoever edits the file; every output carries the
    # sheet, so they'd only add weight there.
    css = files(__package__).joinpath("notebook.css").read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/\s*", "", css, flags=re.DOTALL).strip()


@cache
def _renderer() -> MarkdownIt:
    # Tables for ScenarioRef.describe(); strikethrough to match what the
    # front ends' own Markdown renderers accept.
    return MarkdownIt("commonmark").enable(["table", "strikethrough"])


def styled_html(body: str) -> str:
    """Wrap an HTML fragment so ``notebook.css`` applies to it."""
    return f'<style>{_stylesheet()}</style><div class="analysis-md">{body}</div>'


class StyledMarkdown(Markdown):
    """``IPython.display.Markdown`` that also offers a styled HTML rendering.

    Front ends prefer ``text/html`` over ``text/markdown``, so the styled
    rendering is what shows; ``data`` stays the Markdown source, and the
    Markdown rendering remains for front ends that can't show HTML.
    """

    def _repr_html_(self) -> str:
        return styled_html(_renderer().render(self.data or ""))


def show_figure(fig: Figure) -> None:
    """Display a plain matplotlib ``Figure`` (one of the shared chart modules',
    drawn without pyplot) as a PNG, as matplotlib-inline would show it."""
    from IPython.display import Image, display

    from analysis.sim.ensemble_plots import figure_bytes

    display(Image(data=figure_bytes(fig), format="png"))
