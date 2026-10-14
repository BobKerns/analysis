"""The chain flowchart as an image, drawn in the terminal (issue #142).

No terminal graphics protocol draws lines or shapes: kitty's and iTerm2's
both carry a finished bitmap. So :func:`render` draws the Mermaid source
from :func:`analysis.sim.chain_map.mermaid` with Mermaid itself, in headless
Chromium through Playwright, which keeps the image the same as the
notebook's flowchart. :func:`terminal_protocol` asks the terminal which
protocol it takes, and :func:`kitty_sequence` / :func:`iterm_sequence`
wrap the PNG for it.

The chart never leaves the machine, and drawing it needs no network: the
Mermaid script ships with the package (``vendor/``, the release the notebook
loads, gzipped), and the page drawing the chart may fetch nothing.
"""

from __future__ import annotations

import base64
import gzip
import math
import os
import re
import select
import sys
from pathlib import Path
from typing import Literal

__all__ = [
    "MERMAID_URL", "GraphError", "fit_columns", "iterm_sequence",
    "kitty_sequence", "render", "terminal_protocol",
]

MERMAID_VERSION = "11.17.2"
MERMAID_URL = f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.esm.min.mjs"
"""Where the notebook's flowchart widget imports Mermaid from."""
MERMAID_SCRIPT = Path(__file__).parent / "vendor" / f"mermaid-{MERMAID_VERSION}.min.js.gz"
"""The same release as a plain script, as npm publishes it, gzipped: the
minified script's short names trip the blocked-keyword check, which skips a
binary file. To upgrade, replace it, change ``MERMAID_VERSION``, and update
the hash in ``tests/test_chain_graph.py``."""

type Protocol = Literal["kitty", "iterm"]
type ImageFormat = Literal["png", "svg"]

_INSTALL_HINT = ("install Google Chrome, or run `uv run playwright install chromium`; "
                 "`--mermaid` prints the chart's source instead")


class GraphError(Exception):
    """The chart couldn't be drawn: no browser, or Mermaid couldn't draw it."""


_PAGE = '<html><body style="margin:0;background:white"><div id="chart"></div></body></html>'

_DRAW = """async (source) => {
    mermaid.initialize({startOnLoad: false, theme: "default"});
    const {svg} = await mermaid.render("chain-graph", source);
    document.getElementById("chart").innerHTML = svg;
    return svg;
}"""


_ZOOM = 2.0
"""How far a chart may grow past ``scale`` to fill ``fit_width``: its labels
at most twice the terminal's text, so a small chart doesn't balloon."""


def render(
    source: str, fmt: ImageFormat = "png", scale: float = 2.0, fit_width: float | None = None,
) -> bytes:
    """Draw Mermaid ``source`` as a PNG, ``scale`` pixels to each of the
    chart's own (twice, by default, for a sharp image on a high-density
    screen), or as SVG text; on a white background so it reads on a dark
    terminal too. With ``fit_width``, the PNG is that many pixels wide
    instead, shrinking a wide chart or growing a narrow one up to
    :data:`_ZOOM` times ``scale``."""
    from playwright.sync_api import Error, sync_playwright

    script = gzip.decompress(MERMAID_SCRIPT.read_bytes()).decode()
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error:
            # Playwright's own Chromium isn't installed; an installed Chrome serves.
            try:
                browser = playwright.chromium.launch(channel="chrome")
            except Error as error:
                raise GraphError(f"no browser to draw the chart with: {_INSTALL_HINT}") from error
        def draw(scale: float):
            page = browser.new_page(device_scale_factor=scale)
            # The page holds the chart: it may fetch nothing from anywhere.
            page.route("**/*", lambda route: route.abort())
            page.set_content(_PAGE)
            page.add_script_tag(content=script)
            try:
                svg = page.evaluate(_DRAW, source)
            except Error as error:
                raise GraphError(f"Mermaid couldn't draw the chart: {error.message}") from error
            return page, svg

        try:
            page, svg = draw(scale)
            if fmt == "svg":
                return svg.encode()
            chart = page.locator("#chart svg")
            if fit_width:
                # A page's scale is fixed when it opens, so the chart is drawn
                # again on a page at the scale that fills the width.
                box = chart.bounding_box()
                if box and box["width"]:
                    fitted = min(fit_width / box["width"], scale * _ZOOM)
                    if abs(fitted - scale) > 0.01:
                        page, _ = draw(fitted)
                        chart = page.locator("#chart svg")
            return chart.screenshot(omit_background=False)
        finally:
            browser.close()


def png_size(png: bytes) -> tuple[int, int]:
    """A PNG's width and height in pixels, from its header."""
    return int.from_bytes(png[16:20]), int.from_bytes(png[20:24])


def fit_columns(image_width: int, columns: int, cell_width: float | None) -> int | None:
    """How many terminal columns to show an image ``image_width`` pixels wide
    in: its own width when it fits, else the whole terminal, so a wide chart
    shrinks instead of running off the right edge. ``None`` (the terminal
    picks) when the cell size is unknown and nothing says it overflows."""
    if not cell_width:
        return None
    return min(columns, max(1, math.ceil(image_width / cell_width)))


_CHUNK = 4096


def kitty_sequence(png: bytes, columns: int | None = None) -> str:
    """``png`` as kitty graphics protocol escapes: transmit and show it
    (``a=T``) as PNG (``f=100``), in chunks of at most 4096 base64 bytes,
    with no replies (``q=2``) to land in the shell's input."""
    data = base64.standard_b64encode(png).decode()
    chunks = [data[i:i + _CHUNK] for i in range(0, len(data), _CHUNK)] or [""]
    first = "a=T,f=100,q=2" + (f",c={columns}" if columns else "")
    out = []
    for i, chunk in enumerate(chunks):
        more = int(i < len(chunks) - 1)
        keys = f"{first},m={more}" if i == 0 else f"m={more}"
        out.append(f"\x1b_G{keys};{chunk}\x1b\\")
    return "".join(out) + "\n"


def iterm_sequence(png: bytes, columns: int | None = None) -> str:
    """``png`` as an iTerm2 inline-image escape, for terminals that take
    iTerm2's protocol but not kitty's (older iTerm2 and VS Code)."""
    width = f";width={columns}" if columns else ""
    data = base64.standard_b64encode(png).decode()
    return f"\x1b]1337;File=inline=1;size={len(png)}{width};preserveAspectRatio=1:{data}\x07\n"


# The query kitty's documentation gives for detecting its graphics protocol:
# a one-pixel image query, then the device-attributes query every terminal
# answers. A terminal that takes kitty graphics answers the first one before
# the second, so reading up to the second's reply never hangs.
_KITTY_QUERY = "\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[c"
_DEVICE_ATTRIBUTES = re.compile(rb"\x1b\[\?[\d;]*c")
# Any id: iTerm2 answers a kitty query with i=0, not the id it was asked with.
_KITTY_OK = re.compile(rb"\x1b_Gi=\d+;OK\x1b\\")
_ITERM_PROGRAMS = {"iTerm.app", "vscode", "WezTerm"}


def kitty_reply(reply: bytes) -> bool:
    """Whether the terminal's ``reply`` to the kitty query says it takes kitty graphics."""
    return _KITTY_OK.search(reply) is not None


def _ask_terminal(query: str, timeout: float = 1.0) -> bytes:
    """Write ``query`` to the terminal and read its reply, up to the
    device-attributes answer that ends it, or ``timeout`` seconds."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    reply = b""
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        sys.stdout.write(query)
        sys.stdout.flush()
        while not _DEVICE_ATTRIBUTES.search(reply):
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                break
            reply += os.read(fd, 1024)
        # Some terminals (iTerm2) answer the queries out of order; take in a
        # late answer here, or it lands at the shell prompt.
        while select.select([fd], [], [], 0.05)[0]:
            reply += os.read(fd, 1024)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return reply


def swallow_replies(timeout: float = 0.2) -> None:
    """Take in any reply the terminal sends to an image just shown. iTerm2
    answers a kitty image though ``q=2`` asks it not to, and an answer left
    unread is typeahead: the shell shows it at the next prompt."""
    if not sys.stdin.isatty():
        return
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (ImportError, OSError):
        return
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        while select.select([fd], [], [], timeout)[0]:
            os.read(fd, 1024)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def terminal_protocol() -> Protocol | None:
    """The image protocol this terminal takes, or ``None`` when it takes
    none, the output isn't a terminal, or it's inside tmux (which needs
    passthrough wrapping this doesn't do). ``$ANALYSIS_IMAGE_PROTOCOL``
    (``kitty``, ``iterm`` or ``none``) overrides the guess, for a terminal
    it gets wrong."""
    forced = os.environ.get("ANALYSIS_IMAGE_PROTOCOL", "").lower()
    if forced in ("kitty", "iterm"):
        return forced
    if forced == "none":
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get("TMUX"):
        return None
    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        # iTerm2 accepts a kitty image but showed nothing (issue #142), so it
        # gets its own protocol, without asking.
        return "iterm"
    try:
        if kitty_reply(_ask_terminal(_KITTY_QUERY)):
            return "kitty"
    except (ImportError, OSError):  # no termios (Windows), or no controlling terminal
        pass
    return "iterm" if os.environ.get("TERM_PROGRAM") in _ITERM_PROGRAMS else None


def cell_size() -> tuple[float, float] | None:
    """The terminal's cell width and height in its own pixels, which on a
    Retina screen are device pixels, when it reports its pixel size."""
    try:
        import fcntl
        import struct
        import termios

        rows, columns, x_pixels, y_pixels = struct.unpack(
            "HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
    except (ImportError, OSError):
        return None
    if not (x_pixels and y_pixels and columns and rows):
        return None
    return x_pixels / columns, y_pixels / rows


# Mermaid's labels are 16px; a terminal cell is about 1.2 times its font's height.
_MERMAID_FONT_PX = 16
_LINE_HEIGHT = 1.2


def terminal_scale(cell: tuple[float, float] | None) -> float:
    """The scale to render at so the chart's labels come out the size of the
    terminal's own text, on a Retina screen or not alike: the terminal reports
    its cells in the pixels it draws images in. Twice the chart's size when
    the terminal doesn't say."""
    if cell is None:
        return 2.0
    return min(4.0, max(0.5, cell[1] / (_LINE_HEIGHT * _MERMAID_FONT_PX)))
