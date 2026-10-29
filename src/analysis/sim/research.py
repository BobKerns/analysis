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
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
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
# What the skill needs, and nothing more: `claude -p` can't ask for approval.
# Its scripts run as `python3 scripts/...` inside the plugin.
ALLOWED_TOOLS = (
    "Skill", "WebSearch", "WebFetch", "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(python3:*)", "Bash(python:*)",
)
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
        why = f" ({unclassified}% unclassified)" if unclassified else ""
        detail = "".join(f"\n  - {w}" for w in warnings)
        raise ResearchError(
            f"the research left part of {symbol} unclassified{why}, so there are no weights to write"
            + (f":{detail}" if detail else "")
        )
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
    label: str
    path: Path


def bundle_tabs(bundle: Path, symbol: str, languages: dict[str, str] | None = None) -> list[Tab]:
    """The bundle's tabs in reading order: Overview, each translated overview,
    Tear sheet, Detail, Glossary. A missing file is just a missing tab.
    ``languages`` maps a language code to its tab label (from the plugin's
    overlays); an unknown code is labelled by the code."""
    languages = languages or {}
    tabs = []
    if p := _find(bundle, symbol, "-overview.md"):
        tabs.append(Tab("Overview", p))
    prefix = f"{symbol}-overview-".lower()
    for p in sorted(bundle.iterdir()):
        name = p.name.lower()
        if name.startswith(prefix) and name.endswith(".md"):
            code = p.name[len(prefix):-3]
            tabs.append(Tab(languages.get(code, code), p))
    for suffix, label in (("-tear-sheet.md", "Tear sheet"), ("-detail.md", "Detail"), ("-glossary.md", "Glossary")):
        if p := _find(bundle, symbol, suffix):
            tabs.append(Tab(label, p))
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
.tabs > label { display: inline-block; padding: 8px 14px; cursor: pointer; color: var(--muted);
  border-bottom: 2px solid transparent; }
.tabs > input:checked + label { color: var(--fg); border-bottom-color: var(--accent); font-weight: 600; }
.tabs > input:focus-visible + label { outline: 2px solid var(--accent); }
.tabs > section { display: none; border-top: 1px solid var(--line); padding-top: 8px; }
%(show)s
a { color: var(--accent); }
table { border-collapse: collapse; display: block; overflow-x: auto; margin: 12px 0; }
th, td { border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }
code, pre { background: var(--code); border-radius: 4px; }
code { padding: 1px 4px; } pre { padding: 8px; overflow-x: auto; }
"""


def render_page(tabs: list[Tab], *, title: str, subtitle: str = "") -> str:
    """One self-contained HTML page showing ``tabs`` (Markdown) as tabs.
    The tabs are radio buttons styled by CSS, so the page needs no script."""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    show = "\n".join(f"#tab{i}:checked ~ #panel{i} {{ display: block; }}" for i in range(len(tabs)))
    parts = [
        "<!doctype html>",
        '<html><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title>",
        f"<style>{_PAGE_CSS % {'show': show}}</style>",
        "</head><body>",
        f"<header><h1>{html.escape(title)}</h1>",
        f"<p>{html.escape(subtitle)}</p>" if subtitle else "",
        "</header>",
        '<div class="tabs">',
    ]
    for i, tab in enumerate(tabs):
        checked = " checked" if i == 0 else ""
        parts.append(f'<input type="radio" name="tabs" id="tab{i}"{checked}>'
                     f'<label for="tab{i}">{html.escape(tab.label)}</label>')
    for i, tab in enumerate(tabs):
        body = md.render(tab.path.read_text(encoding="utf-8"))
        parts.append(f'<section id="panel{i}">{body}</section>')
    if not tabs:
        parts.append("<p>This bundle has no tabs to show.</p>")
    parts += ["</div>", "</body></html>", ""]
    return "\n".join(parts)


def write_page(bundle: Path, symbol: str, languages: dict[str, str] | None = None) -> Path:
    tabs = bundle_tabs(bundle, symbol, languages)
    page = bundle / REPORT_PAGE
    subtitle = f"Research bundle {bundle.name}" if bundle.name else ""
    page.write_text(render_page(tabs, title=f"{symbol} research", subtitle=subtitle), encoding="utf-8")
    return page


# ---------------------------------------------------------------------------
# Running the skill


def prompt_for(symbol: str) -> str:
    return (
        f"Fund tear sheet on {symbol}. There is no docs connector: write the Markdown files "
        "and the allocation export into the current folder, as the skill describes. "
        "Don't ask questions; make reasonable choices and note them."
    )


def claude_command(symbol: str, plugin_dir: Path, claude: str = "claude") -> list[str]:
    return [
        claude, "-p", prompt_for(symbol),
        "--plugin-dir", str(plugin_dir),
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        "--allowedTools", *ALLOWED_TOOLS,
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
    command = claude_command(symbol, plugin_dir, executable)
    start = time.monotonic()
    # stderr goes to a file: a pipe nobody reads until the end could fill and
    # stall the run.
    errors = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        command, cwd=work_dir, stdout=subprocess.PIPE, stderr=errors,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
    )
    timed_out = threading.Event()

    def stop() -> None:
        timed_out.set()
        proc.kill()

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
        proc.kill()
        proc.wait()
        raise
    finally:
        timer.cancel()
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


def language_tabs(languages: Iterable[tuple[str, dict[str, str]]]) -> dict[str, str]:
    return {code: str(meta.get("tab") or meta.get("name") or code) for code, meta in languages}
