# cspell:ignore Collapser MPLCONFIGDIR apputils ipywidget jupyterapp linejoin mousedown mousemove mouseup
"""Notebook scenes: Playwright driving JupyterLab, recorded frame by frame.

A ```` ```notebook ```` block is a list of steps, one per line, written like
a tape (``docs/videos/README.md`` has the full description)::

    Set DataDir "home/retirement/data/sim"   # optional, before the first step
    Hide
    Open "net_worth.ipynb"
    RunAll
    Show
    Scroll "Net worth over time"
    ScrollBy 300
    Click "Pre-tax"
    Drag "Years:" 2035 2045
    Select "Account:" "Asset_Checking"
    Sleep 3s

JupyterLab runs on a fresh copy of the prepared demo, with this checkout's
notebooks copied in and ``ANALYSIS_DATA_DIR`` set, so a notebook reads the
demo's runs and nothing else. Chromium's screencast sends a frame whenever
the page changes; ffmpeg holds each frame until the next, which gives the
clip its real timing at the video's full resolution.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analysis.videos.script import ScriptError
from analysis.videos.theme import Theme

# step name -> (fewest, most) arguments
STEPS: dict[str, tuple[int, int]] = {
    "Open": (1, 1),
    "RunAll": (0, 0),
    "Wait": (0, 0),
    "Hide": (0, 0),
    "Show": (0, 0),
    "Sleep": (1, 1),
    "Scroll": (1, 2),
    "ScrollBy": (1, 1),
    "Click": (1, 1),
    "Select": (2, 2),
    "Drag": (3, 3),
}
SETTINGS = ("DataDir", "Zoom")
# How much larger than JupyterLab's own size the page is drawn, so its
# text reads at 1080p. A setting can change it for one scene.
DEFAULT_ZOOM = 1.4
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s)$")
# How long a step may wait for the kernel (a first run imports pandas).
_KERNEL_TIMEOUT_MS = 180_000


@dataclass(frozen=True)
class Step:
    name: str
    args: tuple[str, ...]
    line: int  # within the block, from 1


@dataclass(frozen=True)
class Plan:
    data_dir: str | None
    zoom: float
    steps: tuple[Step, ...]

    @property
    def notebooks(self) -> tuple[str, ...]:
        return tuple(s.args[0] for s in self.steps if s.name == "Open")


def seconds(text: str) -> float:
    m = _DURATION.match(text)
    if not m:
        raise ValueError(f"a duration is like 2s or 500ms, not {text!r}")
    return float(m[1]) / (1000 if m[2] == "ms" else 1)


def parse(source: str) -> Plan:
    """The block's settings and steps; a ScriptError names the bad line."""
    data_dir: str | None = None
    zoom = DEFAULT_ZOOM
    steps: list[Step] = []
    for n, raw in enumerate(source.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip() if not raw.strip().startswith("#") else ""
        if not line:
            continue
        try:
            words = shlex.split(line)
        except ValueError as e:
            raise ScriptError(f"notebook line {n}: {e}") from e
        name, args = words[0], tuple(words[1:])
        if name == "Set":
            if steps:
                raise ScriptError(f"notebook line {n}: Set comes before the first step")
            if len(args) != 2 or args[0] not in SETTINGS:
                raise ScriptError(f"notebook line {n}: Set takes one of {', '.join(SETTINGS)} and a value")
            if args[0] == "DataDir":
                if Path(args[1]).is_absolute() or ".." in Path(args[1]).parts:
                    raise ScriptError(f"notebook line {n}: DataDir is a path inside the demo, like home/x/sim")
                data_dir = args[1]
            else:
                try:
                    zoom = float(args[1])
                except ValueError:
                    raise ScriptError(f"notebook line {n}: Zoom is a number, like 1.4") from None
            continue
        if name not in STEPS:
            raise ScriptError(f"notebook line {n}: unknown step {name!r} (one of {', '.join(STEPS)})")
        fewest, most = STEPS[name]
        if not fewest <= len(args) <= most:
            want = str(fewest) if fewest == most else f"{fewest} to {most}"
            raise ScriptError(f"notebook line {n}: {name} takes {want} argument(s), not {len(args)}")
        if name == "Sleep":
            try:
                seconds(args[0])
            except ValueError as e:
                raise ScriptError(f"notebook line {n}: {e}") from e
        if name == "Scroll" and len(args) == 2 and not (args[1].isdigit() and int(args[1]) > 0):
            raise ScriptError(f"notebook line {n}: Scroll's second argument counts matches from 1")
        if name == "ScrollBy" and not args[0].lstrip("-").isdigit():
            raise ScriptError(f"notebook line {n}: ScrollBy takes a whole number of pixels")
        if name == "Drag" and not all(a.lstrip("-").isdigit() for a in args[1:]):
            raise ScriptError(f"notebook line {n}: Drag takes a label and two whole numbers")
        if name == "Open" and (not args[0].endswith(".ipynb") or "/" in args[0]):
            raise ScriptError(f"notebook line {n}: Open names a notebook in notebooks/, like net_worth.ipynb")
        if name != "Open" and not steps and name not in ("Hide", "Show", "Sleep"):
            raise ScriptError(f"notebook line {n}: the first step opens a notebook")
        steps.append(Step(name, args, n))
    if not any(s.name == "Open" for s in steps):
        raise ScriptError("a notebook scene needs an Open step")
    return Plan(data_dir, zoom, tuple(steps))


def check(plan: Plan, notebooks: Path) -> None:
    for name in plan.notebooks:
        if not (notebooks / name).is_file():
            raise ScriptError(f"notebook scene opens {name}, which isn't in {notebooks}")


# --- recording ------------------------------------------------------------------

# A pointer drawn into the page: headless Chromium's screencast has none.
# It follows the mouse Playwright moves, and pulses on a click.
_POINTER_JS = """
(() => {
  const install = () => {
    if (document.getElementById('video-pointer')) return;
    const p = document.createElement('div');
    p.id = 'video-pointer';
    p.innerHTML = '<svg width="28" height="28" viewBox="0 0 28 28">'
      + '<path d="M3 2 L3 22 L9 17 L13 26 L17 24 L13 15 L21 15 Z" fill="#10242f"'
      + ' stroke="#ffffff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    Object.assign(p.style, {position: 'fixed', left: '-40px', top: '-40px', zIndex: 2147483647,
                            pointerEvents: 'none', transition: 'transform 120ms'});
    document.documentElement.appendChild(p);
    addEventListener('mousemove', e => {
      p.style.left = e.clientX - 3 + 'px';
      p.style.top = e.clientY - 2 + 'px';
    }, true);
    addEventListener('mousedown', () => { p.style.transform = 'scale(0.8)'; }, true);
    addEventListener('mouseup', () => { p.style.transform = ''; }, true);
    // No hover toolbar or active-cell bar: the viewer isn't editing.
    const css = document.createElement('style');
    css.textContent = '.jp-cell-toolbar, .jp-Collapser { visibility: hidden !important; }';
    document.head.appendChild(css);
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', install); else install();
})();
"""

# JupyterLab settings for a clean picture: every cell drawn (not just the
# ones in view, so scrolling never shows blanks), no news prompt.
_LAB_SETTINGS = {
    "@jupyterlab/notebook-extension/tracker.jupyterlab-settings": '{"windowingMode": "none"}',
    "@jupyterlab/apputils-extension/notification.jupyterlab-settings":
        '{"fetchNews": "false", "checkForUpdates": false}',
}

_BUSY_JS = """() => {
  const nb = window.jupyterapp?.shell.currentWidget;
  const kernel = nb?.sessionContext?.session?.kernel;
  if (!kernel || kernel.status !== 'idle') return false;
  return ![...document.querySelectorAll('.jp-InputPrompt')].some(e => e.textContent.includes('*'));
}"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Lab:
    """A JupyterLab server on the demo copy, for one recording."""

    def __init__(self, root: Path, data_dir: Path):
        self.root = root
        self.port = _free_port()
        self.token = secrets.token_hex(16)
        jupyter_dir = root / ".jupyter-video"
        settings = jupyter_dir / "settings"
        for rel, text in _LAB_SETTINGS.items():
            (settings / rel).parent.mkdir(parents=True, exist_ok=True)
            (settings / rel).write_text(text, encoding="utf-8")
        home = root / "home"
        home.mkdir(exist_ok=True)
        env = {
            **os.environ,
            "HOME": str(home),
            "ANALYSIS_DATA_DIR": str(data_dir),
            "ANALYSIS_NO_BUILDER": "1",
            "JUPYTER_CONFIG_DIR": str(jupyter_dir / "config"),
            "JUPYTER_RUNTIME_DIR": str(jupyter_dir / "runtime"),
            "JUPYTER_DATA_DIR": str(jupyter_dir / "data"),
            "JUPYTERLAB_SETTINGS_DIR": str(settings),
            "JUPYTERLAB_WORKSPACES_DIR": str(jupyter_dir / "workspaces"),
            # Matplotlib's font cache, built once, outside the demo copy.
            "MPLCONFIGDIR": str(Path(tempfile.gettempdir()) / "analysis-video-mpl"),
        }
        self.log = (jupyter_dir / "lab.log").open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "jupyterlab", "--no-browser", f"--port={self.port}",
             f"--IdentityProvider.token={self.token}", f"--ServerApp.root_dir={root / 'notebooks'}",
             "--LabApp.expose_app_in_browser=True", "--ServerApp.open_browser=False",
             *(["--allow-root"] if hasattr(os, "geteuid") and os.geteuid() == 0 else [])],
            cwd=root, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        self._wait_up()

    def _wait_up(self) -> None:
        import urllib.request

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api?token={self.token}", timeout=2)
                return
            except OSError:
                time.sleep(0.3)
        self.close()
        tail = (self.root / ".jupyter-video" / "lab.log").read_text(errors="replace").splitlines()[-15:]
        raise ScriptError("JupyterLab didn't start:\n" + "\n".join(tail))

    def url(self, notebook: str) -> str:
        return f"http://127.0.0.1:{self.port}/lab/tree/{notebook}?token={self.token}&reset"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


class _Recorder:
    """Screencast frames, kept only while showing, with their times."""

    def __init__(self, page: Any, cdp: Any, width: int, height: int):
        self.page = page
        self.cdp = cdp
        self.showing = True
        self.frames: list[tuple[float, bytes]] = []  # (time, jpeg) while showing
        self.last: bytes | None = None
        self.shown_at: float | None = None
        cdp.on("Page.screencastFrame", self._frame)
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": width,
                                          "maxHeight": height, "everyNthFrame": 1})

    def _frame(self, event: dict[str, Any]) -> None:
        self.cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
        data = base64.b64decode(event["data"])
        self.last = data
        if self.showing:
            self.frames.append((time.time(), data))

    def show(self) -> None:
        if not self.showing:
            self.showing = True
            if self.last is not None:
                self.frames.append((time.time(), self.last))

    def hide(self) -> None:
        self.showing = False

    def stop(self) -> float:
        self.cdp.send("Page.stopScreencast")
        return time.time()


def _move_to(page: Any, box: dict[str, float], steps: int = 18) -> tuple[float, float]:
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y, steps=steps)
    return x, y


def _visible(page: Any, selector: str, text: str) -> Any:
    """The first element matching ``selector`` with ``text`` whose box is on screen."""
    loc = page.locator(selector).filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$"))
    height = page.viewport_size["height"]
    for i in range(loc.count()):
        box = loc.nth(i).bounding_box()
        if box and box["height"] > 0 and 0 <= box["y"] and box["y"] + box["height"] <= height:
            return loc.nth(i)
    raise ScriptError(f"nothing on screen labeled {text!r}")


def _widget(page: Any, kind: str, label: str) -> Any:
    """The on-screen ipywidget of ``kind`` (a CSS class) whose description is ``label``."""
    boxes = page.locator(f".{kind}").filter(
        has=page.locator(".widget-label", has_text=re.compile(rf"^\s*{re.escape(label)}\s*$")))
    height = page.viewport_size["height"]
    for i in range(boxes.count()):
        box = boxes.nth(i).bounding_box()
        if box and 0 <= box["y"] and box["y"] + box["height"] <= height:
            return boxes.nth(i)
    raise ScriptError(f"no {kind} labeled {label!r} on screen")


# Scrolls the notebook (the nearest scrolling box around ``el``) by ``by``
# pixels, or so ``el`` is at the top when ``by`` is null, eased in and out.
_SMOOTH_SCROLL_JS = """([el, by]) => new Promise(done => {
  let box = el.parentElement;
  while (box && !(box.scrollHeight > box.clientHeight && getComputedStyle(box).overflowY !== 'visible'))
    box = box.parentElement;
  if (!box) { el.scrollIntoView({block: 'start'}); return done(); }
  const from = box.scrollTop;
  const to = by === null ? from + el.getBoundingClientRect().top - box.getBoundingClientRect().top - 12
                         : from + by;
  const ms = Math.min(1600, 500 + Math.abs(to - from) / 3), t0 = performance.now();
  const step = now => {
    const t = Math.min(1, (now - t0) / ms), ease = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
    box.scrollTop = from + (to - from) * ease;
    t < 1 ? requestAnimationFrame(step) : done();
  };
  requestAnimationFrame(step);
})"""


def _scroll_to(page: Any, text: str, nth: int = 1) -> None:
    """Smoothly bring a heading (or other text, the ``nth`` that reads so) to the top of the notebook."""
    # A heading's text ends with JupyterLab's ¶ anchor link.
    target = page.locator(".jp-Notebook").locator("h1, h2, h3, h4").filter(
        has_text=re.compile(rf"^\s*{re.escape(text)}\s*¶?\s*$"))
    if target.count() == 0:
        target = page.locator(".jp-Notebook").get_by_text(text, exact=True)
    if target.count() < nth:
        raise ScriptError(f"the notebook has {target.count()} of {text!r}, not {nth}")
    target.nth(nth - 1).evaluate(f"(el) => ({_SMOOTH_SCROLL_JS})([el, null])")


def _scroll_by(page: Any, pixels: int) -> None:
    """Smoothly scroll the notebook down (or up, negative) by ``pixels`` of its layout."""
    cell = page.locator(".jp-Notebook .jp-Cell").first
    cell.evaluate(f"(el, by) => ({_SMOOTH_SCROLL_JS})([el, by])", pixels)


def _wait_idle(page: Any) -> None:
    page.wait_for_timeout(400)  # let a click's request reach the kernel
    page.wait_for_function(_BUSY_JS, timeout=_KERNEL_TIMEOUT_MS)
    page.wait_for_timeout(600)  # and its output draw


def _drag(page: Any, label: str, low: int, high: int) -> None:
    slider = _widget(page, "widget-slider", label)
    handles = slider.locator(".noUi-handle")
    base = slider.locator(".noUi-base").bounding_box()
    if base is None or handles.count() == 0:
        raise ScriptError(f"slider {label!r} has no handles")
    first = handles.first
    vmin = float(first.get_attribute("aria-valuemin") or 0)
    vmax = float(first.get_attribute("aria-valuemax") or 0)
    if handles.count() == 2:
        vmin = float(first.get_attribute("aria-valuemin") or vmin)
        vmax = float(handles.nth(1).get_attribute("aria-valuemax") or vmax)
    span = vmax - vmin or 1.0
    wanted = [low, high] if handles.count() == 2 else [high]
    for handle, value in zip([handles.nth(i) for i in range(handles.count())], wanted, strict=False):
        hb = handle.bounding_box()
        if hb is None:
            continue
        x, y = _move_to(page, hb)
        page.wait_for_timeout(250)
        page.mouse.down()
        target = base["x"] + base["width"] * (value - vmin) / span
        page.mouse.move(target, y, steps=30)
        page.mouse.up()
        page.wait_for_timeout(300)


def record(source: str, theme: Theme, prepared: Path, notebooks: Path, out: Path) -> None:
    """Record a notebook scene from a fresh copy of the prepared demo into ``out`` (MP4)."""
    from playwright.sync_api import sync_playwright

    from analysis.videos import render

    plan = parse(source)
    check(plan, notebooks)
    ffmpeg = render.need("ffmpeg", "install ffmpeg")
    root = render.DEMO_ROOT
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(prepared, root, symlinks=True)
    lab: _Lab | None = None
    frames_dir = Path(tempfile.mkdtemp(prefix="analysis-video-frames-"))
    try:
        shutil.copytree(notebooks, root / "notebooks", ignore=shutil.ignore_patterns(".ipynb_checkpoints"))
        data_dir = root / plan.data_dir if plan.data_dir else root / "data"
        if not data_dir.is_dir():
            raise ScriptError(f"the notebook scene's DataDir {plan.data_dir} isn't in the demo")
        lab = _Lab(root, data_dir)
        out_w, out_h = theme.out_width, theme.out_height
        # The page is laid out smaller than the video and drawn at a higher
        # pixel ratio, so JupyterLab's text is larger and still sharp.
        view_w, view_h = round(theme.width / plan.zoom), round(theme.height / plan.zoom)
        ratio = out_w / view_w
        args = [f"--force-device-scale-factor={ratio}"]
        with sync_playwright() as p:
            browser = render.launch_chromium(p, args)
            try:
                context = browser.new_context(viewport={"width": view_w, "height": view_h})
                context.add_init_script(_POINTER_JS)
                page = context.new_page()
                page.set_default_timeout(30_000)
                recorder = _Recorder(page, context.new_cdp_session(page), out_w, out_h)
                end = _play(page, plan, lab, recorder)
            finally:
                browser.close()
        if not recorder.frames:
            raise ScriptError("the notebook scene showed nothing (Hide with no Show?)")
        _encode(ffmpeg, recorder.frames, end, frames_dir, theme, out)
    finally:
        if lab is not None:
            lab.close()
        shutil.rmtree(frames_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def _play(page: Any, plan: Plan, lab: _Lab, recorder: _Recorder) -> float:
    for step in plan.steps:
        a = step.args
        try:
            if step.name == "Open":
                page.goto(lab.url(a[0]))
                page.wait_for_function("() => window.jupyterapp?.shell.currentWidget?.sessionContext",
                                       timeout=_KERNEL_TIMEOUT_MS)
                page.evaluate("""async () => {
                  const app = window.jupyterapp;
                  await app.restored;
                  await app.commands.execute('application:set-mode', {mode: 'single-document'});
                  if (!app.shell.leftCollapsed) await app.commands.execute('application:toggle-left-area');
                  if (!app.shell.rightCollapsed) await app.commands.execute('application:toggle-right-area');
                }""")
                page.wait_for_function(_BUSY_JS, timeout=_KERNEL_TIMEOUT_MS)
            elif step.name == "RunAll":
                page.evaluate("() => window.jupyterapp.commands.execute('notebook:run-all-cells')")
                _wait_idle(page)
                # Run all leaves the last cell selected and in view: start from the top.
                page.evaluate("() => { const nb = window.jupyterapp.shell.currentWidget.content;"
                              " nb.activeCellIndex = 0; nb.deselectAll(); nb.mode = 'command'; nb.scrollToItem(0); }")
                page.wait_for_timeout(500)
            elif step.name == "Wait":
                _wait_idle(page)
            elif step.name == "Hide":
                recorder.hide()
            elif step.name == "Show":
                page.wait_for_timeout(300)
                recorder.show()
            elif step.name == "Sleep":
                page.wait_for_timeout(seconds(a[0]) * 1000)
            elif step.name == "Scroll":
                _scroll_to(page, a[0], int(a[1]) if len(a) > 1 else 1)
            elif step.name == "ScrollBy":
                _scroll_by(page, int(a[0]))
            elif step.name == "Click":
                target = _visible(page, "button, .widget-toggle-button, .jp-Button, a, label", a[0])
                _move_to(page, target.bounding_box())
                page.wait_for_timeout(250)
                page.mouse.down()
                page.wait_for_timeout(80)
                page.mouse.up()
                _wait_idle(page)
            elif step.name == "Select":
                # A dropdown, or a list that takes several (each picks just this one).
                box = _widget(page, "widget-inline-hbox", a[0]).locator("select")
                _move_to(page, box.bounding_box())
                page.wait_for_timeout(300)
                box.select_option(label=a[1])
                _wait_idle(page)
            elif step.name == "Drag":
                _drag(page, a[0], int(a[1]), int(a[2]))
                _wait_idle(page)
        except ScriptError as e:
            raise ScriptError(f"notebook line {step.line} ({step.name}): {e}") from e
    page.wait_for_timeout(200)
    return recorder.stop()


def _encode(ffmpeg: str, frames: list[tuple[float, bytes]], end: float, folder: Path, theme: Theme,
            out: Path) -> None:
    """The frames as an MP4, each held until the next one (and the last until ``end``)."""
    from analysis.videos import render

    listing = folder / "frames.txt"
    with listing.open("w") as f:
        for i, (at, jpeg) in enumerate(frames):
            name = folder / f"{i:06d}.jpg"
            name.write_bytes(jpeg)
            until = frames[i + 1][0] if i + 1 < len(frames) else end
            f.write(f"file '{name}'\nduration {max(until - at, 0.001):.4f}\n")
        f.write(f"file '{folder / f'{len(frames) - 1:06d}.jpg'}'\n")  # concat needs the last one again
    w, h, fps = theme.out_width, theme.out_height, theme.fps
    partial = out.with_name(f"{out.stem}.partial{out.suffix}")
    fit = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=white"
    render.run([ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                "-vf", f"{fit},fps={fps}",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", str(partial)])
    partial.replace(out)
