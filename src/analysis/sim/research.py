"""`sim symbol research`: a fund tear sheet from the finance-research plugin,
its asset-class weights for ``allocations.csv``, and a page to read it (#344).

The plugin's ``fund-tear-sheet`` skill (#345, docs/guides/finance-research.md)
researches a fund from public sources and, with no docs connector, writes its
tabs as Markdown files plus ``<TICKER>-allocations-sim.csv``: rows already in
``allocations.csv``'s format. This module runs the skill headless with
``claude -p``, keeps what it wrote as a *bundle* under the data repo's
``research/<SYMBOL>/<date>/``, reads the weights out of it, and renders the
tabs into one HTML page. Writing the weights is the caller's job, through
``symbols.add_symbol``, so the loader's checks apply.

``research/`` is outside the provenance scope (``provenance._scoped_paths``):
saving a bundle never makes the data dirty or changes a scenario's tree.
"""

from __future__ import annotations

import csv
import html
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from analysis.sim import registries, units
from analysis.sim.dates import month_end_iso

RESEARCH_DIR = "research"
REPORT_PAGE = "report.html"
SKILL = "fund-tear-sheet"
DEFAULT_TIMEOUT_MINUTES = 20
# The tools the skill needs. The run reads untrusted web pages, so a page
# carrying a prompt injection must not reach a shell or files outside the
# run: see claude_command for how each is scoped.
TOOLS = ("Skill", "WebSearch", "WebFetch", "Read", "Write", "Edit", "Glob", "Grep", "Bash")
# The skill's working files sit in fund-work/ beside the tabs; a bundle keeps
# only what a reader or the importer needs.
_KEPT_SUFFIXES = (".md", ".json", ".csv")


class ResearchError(Exception):
    """The skill couldn't be run, or its bundle can't be imported."""


# ---------------------------------------------------------------------------
# Reading a bundle


@dataclass
class Allocation:
    """The weights a bundle proposes for one symbol, with what's known about them."""

    symbol: str
    as_of: str
    """``YYYY-MM``, or blank when the export carries no date."""
    weights: dict[str, str]
    """Asset class to weight in ``allocations.csv``'s form (``97.3%``)."""
    method: str = ""
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _find(bundle: Path, symbol: str, suffix: str) -> Path | None:
    """``<symbol><suffix>`` in ``bundle``, matching the ticker in any case."""
    want = f"{symbol}{suffix}".lower()
    return next((p for p in sorted(bundle.iterdir()) if p.name.lower() == want), None)


def _allocation_doc(bundle: Path, symbol: str) -> dict[str, Any]:
    path = _find(bundle, symbol, "-allocation.json")
    if path is None:
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResearchError(f"{path.name}: not readable JSON: {exc}") from None
    return doc if isinstance(doc, dict) else {}


def read_allocation(bundle: Path, symbol: str) -> Allocation | None:
    """The weights in ``bundle``'s ``<SYMBOL>-allocations-sim.csv``, checked
    against the simulator's classes and summing to exactly 100%.

    None when the bundle has no allocation export at all (a company's tear
    sheet). Raises ``ResearchError`` when it has the JSON but no sim rows,
    which the skill does when part of the fund is ``unclassified``: the
    warnings say why."""
    doc = _allocation_doc(bundle, symbol)
    warnings = [str(w) for w in doc.get("warnings") or []]
    sources = [str(s) for s in doc.get("sources") or []]
    method = str(doc.get("method") or "")
    path = _find(bundle, symbol, "-allocations-sim.csv")
    if path is None:
        if not doc:
            return None
        unclassified = (doc.get("allocation_pct") or {}).get("unclassified")
        detail = "".join(f"\n  - {w}" for w in warnings)
        if isinstance(unclassified, (int, float)) and unclassified > 0:
            why = f"the research left {unclassified}% of {symbol} unclassified"
        else:
            # The skill records why it wrote no rows, e.g. weights over 100%.
            why = f"the research wrote no allocations rows for {symbol}"
            if reason := str(doc.get("sim_export") or ""):
                why += f" ({reason})"
        raise ResearchError(f"{why}, so there are no weights to write" + (f":{detail}" if detail else ""))
    with path.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if any((v or "").strip() for v in r.values())]
    if not rows:
        raise ResearchError(f"{path.name} has no rows")
    weights: dict[str, str] = {}
    months = set()
    total = 0
    for n, row in enumerate(rows, start=2):
        where = f"{path.name} line {n}"
        row_symbol = (row.get("symbol") or "").strip().upper()
        if row_symbol != symbol:
            raise ResearchError(f"{where}: symbol {row_symbol!r}, expected {symbol}")
        cls = (row.get("asset_class") or "").strip()
        if cls not in registries.ASSET_CLASSES:
            raise ResearchError(
                f"{where}: unknown asset class {cls!r}; allowed: {', '.join(registries.ASSET_CLASSES)}"
            )
        if cls in weights:
            raise ResearchError(f"{where}: {cls} is given twice")
        try:
            micros = units.percent_to_micros((row.get("weight") or "").strip())
        except ValueError as exc:
            raise ResearchError(f"{where}: weight {exc}") from None
        if not 0 <= micros <= 1_000_000:  # the rate parser takes any sign and size
            raise ResearchError(f"{where}: weight {units.micros_to_percent(micros)} is not between 0% and 100%")
        weights[cls] = units.micros_to_percent(micros)
        total += micros
        months.add((row.get("as_of") or "").strip())
    if total != 1_000_000:
        raise ResearchError(f"{path.name}: the weights add up to {units.micros_to_percent(total)}, not 100%")
    if len(months) > 1:
        raise ResearchError(f"{path.name}: rows carry several as_of months {sorted(months)}")
    as_of = months.pop()
    if as_of:
        try:
            as_of = month_end_iso(as_of)[:7]
        except ValueError:
            raise ResearchError(f"{path.name}: as_of {as_of!r} is not a YYYY-MM month") from None
    return Allocation(symbol, as_of, weights, method, sources, warnings)


def tear_sheet_title(bundle: Path, symbol: str) -> str | None:
    """The tear sheet's first heading, as a default name for the fund."""
    path = _find(bundle, symbol, "-tear-sheet.md")
    if path is None:
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            # "Fund Name (TICKER) — tear sheet" reads better as just the name.
            for cut in (" — ", " - ", " – ", f" ({symbol})", ":"):
                head, sep, _ = title.partition(cut)
                if sep and head.strip():
                    title = head.strip()
            return title or None
    return None


# ---------------------------------------------------------------------------
# Keeping a bundle


def bundle_dir(data_dir: Path, symbol: str, today: date | None = None) -> Path:
    """A fresh ``research/<SYMBOL>/<YYYY-MM-DD>/``; a second run the same day
    gets ``-2``, ``-3`` and so on, so no earlier bundle is overwritten."""
    base = data_dir / RESEARCH_DIR / symbol / (today or date.today()).isoformat()
    target, n = base, 1
    while target.exists():
        n += 1
        target = base.with_name(f"{base.name}-{n}")
    return target


def keep_bundle(source: Path, target: Path) -> list[Path]:
    """Copy ``source``'s tabs and exports (not its working files) to ``target``."""
    files = [p for p in sorted(source.iterdir()) if p.is_file() and p.suffix.lower() in _KEPT_SUFFIXES]
    if not files:
        raise ResearchError(f"{source} has no Markdown, JSON or CSV files to keep")
    target.mkdir(parents=True)
    return [Path(shutil.copy2(p, target / p.name)) for p in files]


def is_under(path: Path, parent: Path) -> bool:
    return path.resolve().is_relative_to(parent.resolve())


# ---------------------------------------------------------------------------
# The report page


@dataclass(frozen=True)
class Tab:
    """One tab of a bundle: its Markdown file (``None`` for a price chart
    with no text) in one language. ``chart`` is the price CSV to draw above
    the text."""

    label: str
    path: Path | None
    key: str = ""
    lang: str = "en"
    chart: Path | None = None


# A language code as a file suffix: ja, pt-br, zh-hant.
_LANG_CODE = re.compile(r"[a-z]+(?:-[a-z]+)*")


def _tab_files(bundle: Path, symbol: str) -> dict[tuple[str, str], Path]:
    """``(language, tab key) -> file`` for each tab file in ``bundle``:
    ``<symbol>-<key>.md`` is English, ``<symbol>-<key>-<lang>.md`` a translation."""
    from analysis.research_plugin import TABS

    found: dict[tuple[str, str], Path] = {}
    stem = f"{symbol}-".lower()
    for p in sorted(bundle.iterdir()):
        name = p.name.lower()
        if not (name.startswith(stem) and name.endswith(".md")):
            continue
        rest = name[len(stem):-3]
        for key, _ in TABS:
            if rest == key:
                found[("en", key)] = p
            elif rest.startswith(key + "-") and _LANG_CODE.fullmatch(rest[len(key) + 1:]):
                found[(rest[len(key) + 1:], key)] = p
    return found


def bundle_tabs(bundle: Path, symbol: str, languages: dict[str, dict[str, Any]] | None = None) -> list[Tab]:
    """The bundle's tabs: English in reading order (Overview, Tear sheet,
    Price chart, Detail, Glossary), then each translated language's. A
    missing file is just a missing tab. ``languages`` maps a language code to
    its ``language.yaml`` (from the plugin's overlays), which names its tabs;
    an unknown code's tabs are labelled by the English name and the code."""
    from analysis.research_plugin import TABS, tab_names

    languages = languages or {}
    files: dict[tuple[str, str], Path | None] = dict(_tab_files(bundle, symbol))
    prices = _find(bundle, symbol, "-prices.csv")
    if prices is not None and ("en", "price") not in files:
        files[("en", "price")] = None  # a chart with no text
    codes = ["en"] + sorted({lang for lang, _ in files} - {"en"})
    english = tab_names()
    tabs = []
    for code in codes:
        if code == "en":
            names = english
        elif code in languages:
            names = tab_names(languages[code])
        else:
            names = {key: f"{name} ({code})" for key, name in english.items()}
        for key, _ in TABS:
            if (code, key) in files:
                tabs.append(Tab(names[key], files[(code, key)], key=key, lang=code,
                                chart=prices if key == "price" else None))
    return tabs


# cspell:ignore Segoe Hiragino Noto
_PAGE_CSS = """
:root { --bg: #fff; --fg: #1f2328; --muted: #59636e; --line: #d1d9e0; --accent: #0969da; --code: #f6f8fa; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --line: #3d444d; --accent: #4493f8; --code: #151b23; }
}
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif; }
header { padding: 16px 16px 0; max-width: 960px; margin: 0 auto; }
header h1 { margin: 0 0 4px; font-size: 1.4em; }
header p { margin: 0 0 12px; color: var(--muted); font-size: 0.9em; }
.tabs { max-width: 960px; margin: 0 auto; padding: 0 16px 32px; }
.tabs > input { position: absolute; opacity: 0; }
.tabs nav label { display: inline-block; padding: 8px 14px; cursor: pointer; color: var(--muted);
  border-bottom: 2px solid transparent; }
.tabs nav.langs { text-align: right; }
.tabs nav.langs label { padding: 4px 10px; font-size: 0.9em; }
.tabs nav.row { display: none; }
.tabs > section { display: none; border-top: 1px solid var(--line); padding-top: 8px; }
label.tablink { color: var(--accent); text-decoration: underline; cursor: pointer; }
p.note { color: var(--muted); font-style: italic; }
svg.chart { width: 100%%; height: auto; margin: 8px 0; }
svg.chart .line { fill: none; stroke: var(--accent); stroke-width: 2; }
svg.chart .dot { fill: var(--accent); }
svg.chart .grid { stroke: var(--line); stroke-width: 1; }
svg.chart text { fill: var(--muted); font-size: 12px; }
%(show)s
a { color: var(--accent); }
table { border-collapse: collapse; display: block; overflow-x: auto; margin: 12px 0; }
th, td { border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }
code, pre { background: var(--code); border-radius: 4px; }
code { padding: 1px 4px; } pre { padding: 8px; overflow-x: auto; }
"""
_ACTIVE = "{ color: var(--fg); border-bottom-color: var(--accent); font-weight: 600; }"
_FOCUS = "{ outline: 2px solid var(--accent); }"


def _page_rules(n_langs: int, n_tabs: int) -> str:
    """The CSS that shows the picked language's tab row and the panel for the
    picked (language, tab) pair. Language and tab are separate radio groups,
    so switching language keeps the tab."""
    rules = []
    for li in range(n_langs):
        rules.append(f"#lang{li}:checked ~ nav.l{li} {{ display: block; }}")
        rules.append(f"#lang{li}:checked ~ nav.langs label[for=lang{li}] {_ACTIVE}")
        rules.append(f"#lang{li}:focus-visible ~ nav.langs label[for=lang{li}] {_FOCUS}")
        for ki in range(n_tabs):
            rules.append(f"#lang{li}:checked ~ #tab{ki}:checked ~ #p{li}-{ki} {{ display: block; }}")
    for ki in range(n_tabs):
        rules.append(f"#tab{ki}:checked ~ nav.row label[for=tab{ki}] {_ACTIVE}")
        rules.append(f"#tab{ki}:focus-visible ~ nav.row label[for=tab{ki}] {_FOCUS}")
    return "\n".join(rules)


# markdown-it writes href first, then a title if the link has one.
_LINK = re.compile(r'<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def _tab_links(body: str, targets: dict[str, tuple[int, int]], here: tuple[int, int]) -> str:
    """``body`` with each link to another tab's file turned into a label that
    switches to that tab: the file name means nothing in the page. A link to
    the same tab in another language switches only the language. A
    ``#heading`` on the link is dropped: a label can switch tabs but not scroll."""
    def swap(match: re.Match[str]) -> str:
        href = re.split(r"[#?]", html.unescape(match.group(1)), maxsplit=1)[0]
        target = targets.get(href.rsplit("/", 1)[-1].lower())
        if target is None:
            return match.group(0)
        li, ki = target
        radio = f"lang{li}" if ki == here[1] and li != here[0] else f"tab{ki}"
        return f'<label class="tablink" for="{radio}">{match.group(2)}</label>'
    return _LINK.sub(swap, body)


def render_page(tabs: list[Tab], *, title: str, subtitle: str = "",
                language_labels: dict[str, str] | None = None) -> str:
    """One self-contained HTML page showing ``tabs`` (Markdown) as tabs, with
    a language switch when more than one language is present. Tabs and
    languages are radio buttons styled by CSS, so the page needs no script.
    ``language_labels`` names each language's switch (English and the code
    by default)."""
    from markdown_it import MarkdownIt

    from analysis.research_plugin import tab_names

    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    labels = {"en": "English", **(language_labels or {})}
    english = tab_names()
    langs = list(dict.fromkeys(tab.lang for tab in tabs))
    keys = list(dict.fromkeys(tab.key or tab.label for tab in tabs))
    grid = {(tab.lang, tab.key or tab.label): tab for tab in tabs}
    targets = {tab.path.name.lower(): (langs.index(tab.lang), keys.index(tab.key or tab.label))
               for tab in tabs if tab.path is not None}
    parts = [
        "<!doctype html>",
        '<html><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title>",
        f"<style>{_PAGE_CSS % {'show': _page_rules(len(langs), len(keys))}}</style>",
        "</head><body>",
        f"<header><h1>{html.escape(title)}</h1>",
        f"<p>{html.escape(subtitle)}</p>" if subtitle else "",
        "</header>",
        '<div class="tabs">',
    ]
    if not tabs:
        parts += ["<p>This bundle has no tabs to show.</p>", "</div>", "</body></html>", ""]
        return "\n".join(parts)
    for li, _ in enumerate(langs):
        parts.append(f'<input type="radio" name="lang" id="lang{li}"{" checked" if li == 0 else ""}>')
    for ki, _ in enumerate(keys):
        parts.append(f'<input type="radio" name="tab" id="tab{ki}"{" checked" if ki == 0 else ""}>')
    if len(langs) > 1:
        parts.append('<nav class="langs">' + "".join(
            f'<label for="lang{li}">{html.escape(labels.get(lang, lang))}</label>' for li, lang in enumerate(langs)
        ) + "</nav>")
    def shown(lang: str, key: str) -> Tab | None:
        """The tab for ``(lang, key)``, else the first language that has it."""
        return grid.get((lang, key)) or next((grid[(other, key)] for other in langs if (other, key) in grid), None)

    for li, lang in enumerate(langs):
        row = []
        for ki, key in enumerate(keys):
            # A tab this language lacks: English names its own tabs; another
            # language borrows the name of the tab it shows instead.
            tab = grid.get((lang, key)) or shown(lang, key)
            name = english.get(key) if lang == "en" and (lang, key) not in grid else None
            row.append(f'<label for="tab{ki}">{html.escape(name or (tab.label if tab else key))}</label>')
        parts.append(f'<nav class="row l{li}">{"".join(row)}</nav>')
    for li, lang in enumerate(langs):
        for ki, key in enumerate(keys):
            tab = shown(lang, key)
            note = ""
            if tab is not None and tab.lang != lang:
                note = (f'<p class="note">Not in {html.escape(labels.get(lang, lang))}; '
                        f'showing {html.escape(labels.get(tab.lang, tab.lang))}.</p>')
            body = ""
            if tab is not None:
                if tab.chart is not None:
                    body += price_chart_svg(tab.chart)
                if tab.path is not None:
                    body += _tab_links(md.render(tab.path.read_text(encoding="utf-8")), targets, (li, ki))
            parts.append(f'<section id="p{li}-{ki}">{note}{body}</section>')
    parts += ["</div>", "</body></html>", ""]
    return "\n".join(parts)


def _nice_step(span: float, ticks: int = 4) -> float:
    """A round step (1, 2 or 5 times a power of ten) giving about ``ticks``
    gridlines over ``span``."""
    raw = span / ticks if span > 0 else 1.0
    power = 10 ** math.floor(math.log10(raw))
    return next(m * power for m in (1, 2, 5, 10) if m * power >= raw)


def _month_index(day: str) -> int | None:
    """``YYYY-MM[-DD]`` as a count of months, so points sit by date."""
    try:
        return int(day[:4]) * 12 + int(day[5:7]) - 1
    except ValueError:
        return None


def read_prices(path: Path) -> list[tuple[str, float]]:
    """``(date, close)`` rows from ``path``, oldest first. Header names match
    in any case (Stooq writes ``Date,...,Close``) and a byte-order mark is
    ignored; a row whose date or close doesn't parse, or isn't finite, is
    skipped, so it shows as a gap."""
    points = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = [name.strip().lower() for name in next(reader, [])]
            if "date" not in header or "close" not in header:
                return []
            at_date, at_close = header.index("date"), header.index("close")
            for row in reader:
                try:
                    day, close = row[at_date].strip(), float(row[at_close].replace(",", ""))
                except (IndexError, ValueError):
                    continue
                if math.isfinite(close) and _month_index(day) is not None:
                    points.append((day, close))
    except OSError:
        return []
    return sorted(points)


def price_chart_svg(path: Path) -> str:
    """An inline SVG line chart of ``path``'s ``date,close`` rows (the price
    tab's data), drawn here so the page needs no script or library. Points
    sit by month, and a missing month breaks the line."""
    points = read_prices(path)
    if len(points) < 2:
        return '<p class="note">The price file has too few rows to draw a chart.</p>'
    months = [_month_index(day) or 0 for day, _ in points]
    width, height, left, right, top, bottom = 880, 300, 64, 16, 12, 28
    low, high = min(c for _, c in points), max(c for _, c in points)
    step = _nice_step(high - low)
    y0, y1 = math.floor(low / step) * step, math.ceil(high / step) * step
    if y1 == y0:
        y1 = y0 + step
    plot_w, plot_h = width - left - right, height - top - bottom
    span = max(months[-1] - months[0], 1)

    def x(month: int) -> float:
        return left + plot_w * (month - months[0]) / span

    def y(v: float) -> float:
        return top + plot_h * (1 - (v - y0) / (y1 - y0))

    decimals = 0 if step >= 1 else 2
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="Month-end price, {html.escape(points[0][0])} to {html.escape(points[-1][0])}">']
    tick = y0
    while tick <= y1 + step / 2:
        out.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>')
        out.append(f'<text x="{left - 6}" y="{y(tick) + 4:.1f}" text-anchor="end">${tick:,.{decimals}f}</text>')
        tick += step
    years = range(months[0] // 12, months[-1] // 12 + 1)
    every = max(1, math.ceil(len(years) / 10))
    for n, year in enumerate(years):
        if n % every == 0:
            at = max(year * 12, months[0])
            out.append(f'<text x="{x(at):.1f}" y="{height - 8}" text-anchor="middle">{year}</text>')
    runs: list[list[str]] = []
    for i, (month, (_, close)) in enumerate(zip(months, points, strict=True)):
        if i == 0 or month - months[i - 1] > 1:
            runs.append([])
        runs[-1].append(f"{x(month):.1f},{y(close):.1f}")
    for run in runs:
        if len(run) == 1:  # a lone month between gaps: a dot, since a line needs two points
            cx, cy = run[0].split(",")
            out.append(f'<circle class="dot" cx="{cx}" cy="{cy}" r="2.5"/>')
        else:
            out.append(f'<polyline class="line" points="{" ".join(run)}"/>')
    out.append("</svg>")
    return "".join(out)


def write_page(bundle: Path, symbol: str, languages: dict[str, dict[str, Any]] | None = None) -> Path:
    languages = languages or {}
    tabs = bundle_tabs(bundle, symbol, languages)
    page = bundle / REPORT_PAGE
    labels = {code: str(meta.get("label") or meta.get("name") or code) for code, meta in languages.items()}
    page.write_text(render_page(tabs, title=f"{symbol} research", subtitle=f"Research bundle {bundle.name}",
                                language_labels=labels), encoding="utf-8")
    return page


# ---------------------------------------------------------------------------
# Running the skill


def skill_scripts(plugin_dir: Path) -> list[Path]:
    """The fund skill's helper scripts: the only commands a run may execute."""
    return sorted((plugin_dir / "skills" / SKILL / "scripts").resolve().glob("*.py"))


def prompt_for(symbol: str, scripts: list[Path]) -> str:
    where = f" Its scripts are in {scripts[0].parent}; run each by that absolute path with python3." if scripts else ""
    return (
        f"Fund tear sheet on {symbol}. There is no docs connector: write the Markdown files "
        "and the allocation export into the current folder, as the skill describes."
        f"{where} No other shell commands are allowed. "
        "Don't ask questions; make reasonable choices and note them."
    )


def _path_rule(tool: str, folder: Path) -> str:
    # Permission rules take an absolute path as //path.
    return f"{tool}(/{folder.resolve().as_posix()}/**)"


def claude_command(symbol: str, plugin_dir: Path, work_dir: Path, claude: str = "claude") -> list[str]:
    """The headless run, confined so that a prompt injection in a fetched page
    can't run commands or touch files outside the run:

    - ``--restricted`` ignores the user's and project's settings (their allow
      rules could be broader), confines the file tools to the working folder
      and ``--add-dir``, and drops Bash and WebFetch unless ``--tools`` names
      them; ``--strict-mcp-config`` loads no MCP servers.
    - ``--permission-mode dontAsk`` refuses anything not allowed below,
      instead of waiting on a prompt nobody can answer.
    - Bash may run only the skill's own scripts, by absolute path; writes are
      allowed only in the working folder, so a run can't plant a script to
      run either. The plugin folder is readable, for the skill's references.
    """
    scripts = skill_scripts(plugin_dir)
    allowed = [
        "Skill", "WebSearch", "WebFetch", "Read", "Glob", "Grep",
        # An Edit rule covers Write as well; Claude Code ignores a Write(path)
        # rule and warns on stderr, which a failed run would report as its reason.
        _path_rule("Edit", work_dir),
        *(f"Bash(python3 {script.as_posix()} *)" for script in scripts),
    ]
    return [
        claude, "-p", prompt_for(symbol, scripts),
        "--plugin-dir", str(plugin_dir),
        "--add-dir", str(plugin_dir.resolve()),
        "--restricted", "--strict-mcp-config",
        "--tools", *TOOLS,
        "--permission-mode", "dontAsk",
        "--allowedTools", *allowed,
        "--output-format", "stream-json", "--verbose",
    ]


def describe_event(event: dict[str, Any]) -> list[str]:
    """Progress lines for one ``stream-json`` event: one per tool call."""
    if event.get("type") != "assistant":
        return []
    lines = []
    for block in (event.get("message") or {}).get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name", "?")
        args = block.get("input") or {}
        detail = (args.get("query") or args.get("url") or args.get("command")
                  or args.get("file_path") or args.get("skill") or "")
        detail = " ".join(str(detail).split())
        if len(detail) > 90:
            detail = detail[:87] + "..."
        lines.append(f"{name} {detail}".rstrip())
    return lines


@dataclass
class RunResult:
    ok: bool
    message: str
    """The final ``result`` text, or why the run failed."""
    cost_usd: float | None = None
    seconds: float = 0.0


def run_skill(
    symbol: str, work_dir: Path, plugin_dir: Path, *, timeout_minutes: float = DEFAULT_TIMEOUT_MINUTES,
    claude: str = "claude", progress: Callable[[str], None] = print,
) -> RunResult:
    """Run the fund skill on ``symbol`` with ``work_dir`` as its folder,
    reporting each tool call through ``progress``. Stops the run after
    ``timeout_minutes``."""
    executable = shutil.which(claude)
    if executable is None:
        raise ResearchError(f"{claude!r} isn't on PATH; install Claude Code (docs/guides/finance-research.md)")
    command = claude_command(symbol, plugin_dir, work_dir, executable)
    start = time.monotonic()
    # stderr goes to a file: a pipe nobody reads until the end could fill and
    # stall the run.
    errors = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    # Its own process group, so stopping it stops whatever it started too: a
    # child still holding stdout would keep the read loop below waiting.
    proc = subprocess.Popen(
        command, cwd=work_dir, stdout=subprocess.PIPE, stderr=errors,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        start_new_session=True,
    )
    timed_out = threading.Event()

    def kill() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def stop() -> None:
        timed_out.set()
        kill()

    timer = threading.Timer(timeout_minutes * 60, stop)
    timer.daemon = True
    timer.start()
    result: dict[str, Any] | None = None
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "result":
                result = event
            for text in describe_event(event):
                elapsed = int(time.monotonic() - start)
                progress(f"  [{elapsed // 60:2d}:{elapsed % 60:02d}] {text}")
        proc.wait()
    except BaseException:
        kill()
        proc.wait()
        raise
    finally:
        timer.cancel()
        # Nothing it started outlives the run.
        if proc.poll() is not None:
            kill()
        errors.seek(0)
        stderr = errors.read().strip()
        errors.close()
    seconds = time.monotonic() - start
    if timed_out.is_set():
        return RunResult(False, f"stopped after {timeout_minutes:g} minutes (--timeout)", seconds=seconds)
    if result is None:
        why = stderr.splitlines()[-1] if stderr else f"claude exited with status {proc.returncode}"
        return RunResult(False, why, seconds=seconds)
    cost = result.get("total_cost_usd")
    ok = not result.get("is_error") and proc.returncode == 0
    return RunResult(ok, str(result.get("result") or result.get("subtype") or ""),
                     cost_usd=float(cost) if isinstance(cost, (int, float)) else None, seconds=seconds)


def compare_lines(proposed: Allocation, latest: tuple[str, list[tuple[str, str]]] | None) -> list[str]:
    """The proposed set beside the symbol's latest set, one class per line."""
    old = dict(latest[1]) if latest else {}
    classes = [c for c in registries.ASSET_CLASSES if c in proposed.weights or c in old]
    new_head = f"proposed ({proposed.as_of or 'every date'})"
    old_head = f"now ({latest[0] or 'every date'})" if latest else "now"
    width = max(len(c) for c in [*classes, "class"])
    lines = [f"  {'class':<{width}}  {new_head:>22}  {old_head:>22}"]
    for cls in classes:
        lines.append(f"  {cls:<{width}}  {proposed.weights.get(cls, '-'):>22}  {old.get(cls, '-'):>22}")
    return lines


