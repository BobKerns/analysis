"""notebook_ui.styled: Markdown output that carries notebook.css (issue #61)."""

from __future__ import annotations

from IPython.core.formatters import DisplayFormatter

from analysis.sim.notebook_ui.pickers import warning_html
from analysis.sim.notebook_ui.styled import StyledMarkdown, styled_html


def test_styled_markdown_renders_inline_code_inside_the_styled_wrapper() -> None:
    html = StyledMarkdown("parents: `base`, `mixin`")._repr_html_()
    assert '<div class="analysis-md"><p>parents: <code>base</code>, <code>mixin</code></p>' in html
    assert ".analysis-md code" in html and "color: inherit" in html


def test_styled_markdown_offers_html_and_keeps_the_markdown_source() -> None:
    md = StyledMarkdown("**bold** `code`")
    bundle, _ = DisplayFormatter().format(md)
    assert "text/html" in bundle
    assert bundle["text/markdown"] == md.data == "**bold** `code`"


def test_styled_markdown_renders_tables() -> None:
    html = StyledMarkdown("| a | b |\n|---|---|\n| `x` | y |")._repr_html_()
    assert "<table>" in html and "<td><code>x</code></td>" in html


def test_stylesheet_is_sent_without_its_comments() -> None:
    assert "/*" not in styled_html("")


def test_warning_html_carries_the_stylesheet() -> None:
    html = warning_html("run `sim load`")
    assert html.startswith("<style>") and 'class="analysis-md"' in html
