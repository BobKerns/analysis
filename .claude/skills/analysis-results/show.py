# /// script
# requires-python = ">=3.11"
# dependencies = ["iterm2"]
# ///
"""Show results to the user beside this agent session: a "view pane".

    uv run -q .claude/skills/analysis-results/show.py --cmd 'COMMAND'
    uv run -q .claude/skills/analysis-results/show.py IMAGE.png [...]
    uv run -q .claude/skills/analysis-results/show.py --read [LINES]
    uv run -q .claude/skills/analysis-results/show.py --fallback IMAGE.png [...]

An agent's commands have no terminal, so `sim ... --graph` can't draw for
them. This opens a pane next to the agent's own (once; later calls reuse it)
through the terminal's control interface, and types into it, so a command
there runs in a real terminal: `--graph` draws inline, and the user can
scroll and type in it too.

--cmd    type COMMAND into the view pane, then Return
IMAGE    show image files in the view pane (or, with no pane, the browser)
--read   print the pane's last LINES (default 40) of text, to check a
         command worked (images don't show in it)
--fallback  open the images in the default browser instead

Backends, picked from the environment:
  iterm   iTerm2's Python API ($ITERM_SESSION_ID)       tested
  kitty   remote control ($KITTY_LISTEN_ON)              UNTESTED
  wezterm `wezterm cli` ($WEZTERM_PANE)                  UNTESTED
  tmux    split-window / send-keys ($TMUX), no images    UNTESTED
  none    images open in the browser; --cmd can't run    tested

An UNTESTED backend that fails exits 3 with the error: tell the user, and
ask whether to use the browser fallback (--fallback) instead.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

STATE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "analysis-viewpane"
UNTESTED = {"kitty", "wezterm", "tmux"}


def backend() -> str:
    if os.environ.get("TERM_PROGRAM") == "iTerm.app" and os.environ.get("ITERM_SESSION_ID"):
        return "iterm"
    if os.environ.get("KITTY_LISTEN_ON") and shutil.which("kitty"):
        return "kitty"
    if os.environ.get("WEZTERM_PANE") and shutil.which("wezterm"):
        return "wezterm"
    if os.environ.get("TMUX") and shutil.which("tmux"):
        return "tmux"
    return "none"


def _state(name: str) -> Path:
    return STATE_DIR / name


def _remembered(name: str) -> str | None:
    try:
        return _state(name).read_text().strip() or None
    except OSError:
        return None


def _remember(name: str, pane: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state(name).write_text(pane)


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


# --- iTerm2 (tested) --------------------------------------------------------

def _iterm(action: str, payload: str) -> None:
    import iterm2

    async def main(connection: iterm2.Connection) -> None:
        app = await iterm2.async_get_app(connection)
        known = _remembered("iterm")
        pane = app.get_session_by_id(known) if known else None
        if action == "read":
            if pane is None:
                sys.exit("show: no view pane open")
            contents = await pane.async_get_screen_contents()
            lines = [contents.line(i).string.rstrip() for i in range(contents.number_of_lines)]
            while lines and not lines[-1]:
                lines.pop()
            print("\n".join(lines[-int(payload):]))
            return
        if pane is None:
            origin = app.get_session_by_id(os.environ["ITERM_SESSION_ID"].partition(":")[2])
            if origin is None:
                sys.exit("show: this iTerm2 session isn't visible to the API")
            pane = await origin.async_split_pane(vertical=True)
            _remember("iterm", pane.session_id)
        await pane.async_send_text(payload + "\n")

    iterm2.run_until_complete(main)


# --- kitty, WezTerm, tmux (UNTESTED) ----------------------------------------

def _kitty(action: str, payload: str) -> None:
    pane = _remembered("kitty")
    if pane and f'"id": {pane},' not in _run("kitty", "@", "ls"):
        pane = None
    if action == "read":
        if pane is None:
            sys.exit("show: no view pane open")
        print("\n".join(_run("kitty", "@", "get-text", "--match", f"id:{pane}").rstrip().splitlines()[-int(payload):]))
        return
    if pane is None:
        pane = _run("kitty", "@", "launch", "--location=vsplit", "--cwd=current").strip()
        _remember("kitty", pane)
    _run("kitty", "@", "send-text", "--match", f"id:{pane}", payload + "\n")


def _wezterm(action: str, payload: str) -> None:
    pane = _remembered("wezterm")
    if pane and f'"pane_id": {pane},' not in _run("wezterm", "cli", "list", "--format", "json").replace("\n", " "):
        pane = None
    if action == "read":
        if pane is None:
            sys.exit("show: no view pane open")
        print("\n".join(_run("wezterm", "cli", "get-text", "--pane-id", pane).rstrip().splitlines()[-int(payload):]))
        return
    if pane is None:
        pane = _run("wezterm", "cli", "split-pane", "--right").strip()
        _remember("wezterm", pane)
    _run("wezterm", "cli", "send-text", "--pane-id", pane, "--no-paste", payload + "\n")


def _tmux(action: str, payload: str) -> None:
    pane = _remembered("tmux")
    if pane and subprocess.run(["tmux", "display", "-p", "-t", pane], capture_output=True).returncode:
        pane = None
    if action == "read":
        if pane is None:
            sys.exit("show: no view pane open")
        print("\n".join(_run("tmux", "capture-pane", "-p", "-t", pane).rstrip().splitlines()[-int(payload):]))
        return
    if pane is None:
        pane = _run("tmux", "split-window", "-h", "-d", "-P", "-F", "#{pane_id}").strip()
        _remember("tmux", pane)
    _run("tmux", "send-keys", "-t", pane, payload, "Enter")


PANES = {"iterm": _iterm, "kitty": _kitty, "wezterm": _wezterm, "tmux": _tmux}

# How each pane draws an image file; tmux has none (sim turns images off in
# tmux too), so images go to the browser there.
IMAGE_COMMANDS = {
    "iterm": lambda f: f"{Path.home() / '.iterm2' / 'imgcat'} -W 90% {f}"
    if (Path.home() / ".iterm2" / "imgcat").exists() else f"imgcat {f}",
    "kitty": lambda f: f"kitty +kitten icat {f}",
    "wezterm": lambda f: f"wezterm imgcat {f}",
}


def browser(images: list[Path]) -> None:
    for image in images:
        webbrowser.open(image.resolve().as_uri())
        print(f"show: opened {image} in the browser")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    which = backend()
    if argv[0] == "--fallback":
        browser([Path(a) for a in argv[1:]])
        return 0
    try:
        if argv[0] == "--cmd":
            if which == "none":
                print("show: no view pane in this terminal; give the user the command to run", file=sys.stderr)
                return 4
            PANES[which]("send", " ".join(argv[1:]))
        elif argv[0] == "--read":
            if which == "none":
                print("show: no view pane in this terminal", file=sys.stderr)
                return 4
            PANES[which]("read", argv[1] if len(argv) > 1 else "40")
        else:
            images = [Path(a) for a in argv]
            missing = [str(i) for i in images if not i.is_file()]
            if missing:
                print(f"show: no such file: {', '.join(missing)}", file=sys.stderr)
                return 2
            if which not in IMAGE_COMMANDS:
                browser(images)
                return 0
            for image in images:
                PANES[which]("send", IMAGE_COMMANDS[which](shlex.quote(str(image.resolve()))))
    except Exception as exc:  # noqa: BLE001 -- report any failure the same way
        if which in UNTESTED:
            print(f"show: the {which} backend is UNTESTED and failed: {exc}\n"
                  "Tell the user, and ask whether to use the browser fallback (--fallback).",
                  file=sys.stderr)
            return 3
        raise
    print(f"show: {which}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
