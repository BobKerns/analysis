"""The steps that make media: narration, tapes, slides, and ffmpeg assembly.

Each step writes one file. ``build.py`` decides which steps a build needs,
from the cache; nothing here caches.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from analysis.videos.script import ScriptError
from analysis.videos.theme import Theme

KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
# Words a narrator says per second, for timing silent drafts.
_SILENT_WORDS_PER_SECOND = 2.6
_SILENT_CJK_CHARS_PER_SECOND = 7.0
_CONTAINER_CHROMIUM = "/opt/pw-browsers/chromium"


class ToolMissing(RuntimeError):
    """A program the build needs isn't installed; the message says how to get it."""


def cache_home() -> Path:
    """Where downloaded models and tools live (not the build cache, which is under out/)."""
    env = os.environ.get("ANALYSIS_VIDEO_CACHE")
    return Path(env) if env else Path.home() / ".cache" / "analysis-videos"


def tool_path() -> str:
    """PATH with the downloaded tools (cache_home()/bin) first."""
    return os.pathsep.join([str(cache_home() / "bin"), os.environ.get("PATH", "")])


def need(tool: str, hint: str) -> str:
    found = shutil.which(tool, path=tool_path())
    if not found:
        raise ToolMissing(f"{tool} not found: {hint}")
    return found


def digest(*parts: Any) -> str:
    """A short, stable hash of JSON-able inputs: the cache key."""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:20]


def tree_digest(root: Path) -> str:
    """Hash of every file's path and bytes under root (for demo data)."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.relative_to(root).parts:
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:20]


def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(list(argv), text=True, capture_output=True, **kwargs)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).splitlines()[-15:])
        raise RuntimeError(f"{Path(argv[0]).name} failed ({proc.returncode}):\n{tail}")
    return proc


def duration(path: Path) -> float:
    """Seconds of media in path (ffprobe)."""
    ffprobe = need("ffprobe", "install ffmpeg")
    out = run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]).stdout
    return float(out.strip())


# --- narration ---------------------------------------------------------------


def silent_durations(sentences: Sequence[str]) -> list[float]:
    """How long a narrator would take, roughly: for drafts and tests."""
    out = []
    for s in sentences:
        cjk = len(re.findall(r"[぀-ヿ㐀-鿿]", s))
        words = len(s.split())
        seconds = cjk / _SILENT_CJK_CHARS_PER_SECOND if cjk > words else words / _SILENT_WORDS_PER_SECOND
        out.append(round(max(1.0, seconds), 3))
    return out


def synthesize(sentences: Sequence[str], voice: dict[str, Any], gap: float, out: Path, repo: Path) -> list[float]:
    """Write the scene's narration to out (WAV); return each sentence's length."""
    engine = voice.get("engine", "kokoro")
    if engine == "silent":
        durations = silent_durations(sentences)
        total = sum(durations) + gap * max(0, len(durations) - 1)
        ffmpeg = need("ffmpeg", "install ffmpeg")
        silence = ["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{total:.3f}"]
        run([ffmpeg, "-y", "-v", "error", *silence, str(out)])
        return durations
    if engine != "kokoro":
        raise ScriptError(f"unknown narration engine {engine!r} (kokoro or silent)")
    models = cache_home()
    missing = [f for f in KOKORO_FILES if not (models / f).is_file()]
    if missing:
        raise ToolMissing(f"Kokoro model files missing from {models}: run `python -m analysis.videos setup`")
    uv = need("uv", "install uv (https://docs.astral.sh/uv/)")
    request = {
        "model": str(models / KOKORO_FILES[0]),
        "voices": str(models / KOKORO_FILES[1]),
        "voice": voice["voice"],
        "lang": voice.get("lang", "en-us"),
        "speed": voice.get("speed", 1.0),
        "gap": gap,
        "sentences": list(sentences),
        "out": str(out),
    }
    worker = repo / "scripts" / "videos" / "tts_kokoro.py"
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    proc = run([uv, "run", "--quiet", "--script", str(worker)], input=json.dumps(request), env=env)
    return [float(d) for d in json.loads(proc.stdout.strip().splitlines()[-1])["durations"]]


# --- slides ------------------------------------------------------------------


def slide_html(theme: Theme, meta: dict[str, Any], fields: dict[str, Any], lang: str) -> str:
    """The slide's page: its template filled from front matter, then the slide's fields."""
    env = Environment(loader=FileSystemLoader(theme.templates), autoescape=select_autoescape(["html"]))

    def asset(name: str) -> str:
        path = theme.assets / f"{name}.svg"
        if not path.is_file():
            raise ScriptError(f"no asset {name!r} ({path})")
        return path.read_text(encoding="utf-8")

    name = fields["template"]
    if not (theme.templates / f"{name}.html").is_file():
        raise ScriptError(f"no slide template {name!r} in {theme.templates}")
    context = {**meta, **fields, "lang": lang, "theme_css": theme.css(), "asset": asset}
    return env.get_template(f"{name}.html").render(context)


def render_slide(html: str, theme: Theme, out: Path) -> None:
    """Screenshot the slide's page at the video's size (Playwright's Chromium)."""
    from playwright.sync_api import Error, sync_playwright

    with tempfile.TemporaryDirectory() as tmp:
        page_file = Path(tmp) / "slide.html"
        page_file.write_text(html, encoding="utf-8")
        with sync_playwright() as pw:
            # ANALYSIS_VIDEO_CHROMIUM names a Chromium to use when Playwright's
            # own download doesn't match (the cloud container ships one).
            chromium = os.environ.get("ANALYSIS_VIDEO_CHROMIUM") or (
                _CONTAINER_CHROMIUM if Path(_CONTAINER_CHROMIUM).exists() else None
            )
            try:
                browser = pw.chromium.launch(executable_path=chromium)
            except Error:
                browser = pw.chromium.launch(channel="chrome")  # an installed Google Chrome
            try:
                page = browser.new_page(viewport={"width": theme.width, "height": theme.height})
                page.goto(page_file.as_uri())
                page.evaluate("document.fonts.ready")
                page.screenshot(path=str(out))
            finally:
                browser.close()


# --- tapes -------------------------------------------------------------------


def _rgb(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return ";".join(str(int(h[i : i + 2], 16)) for i in (0, 2, 4))


def full_tape(tape: str, theme: Theme, rc: str, out: Path) -> str:
    """The tape VHS runs: output, theme settings, a hidden setup, then the scene's commands."""
    return "\n".join(
        [
            f'Output "{out}"',
            *theme.vhs_settings(),
            "Hide",
            f'Type "source {rc} && clear"',
            "Enter",
            "Sleep 300ms",
            "Show",
            tape,
            "",
        ]
    )


def _sandbox(data: Path, theme: Theme, root: Path) -> Path:
    """A fresh copy of the demo data, committed (the loader refuses dirty data)."""
    shutil.copytree(data, root / "data")
    stamp = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    env = {**os.environ, **stamp}
    ident = ["-c", "user.name=Demo", "-c", "user.email=demo@example.com", "-c", "commit.gpgsign=false"]
    git = need("git", "install git")
    run([git, "init", "-q"], cwd=root / "data")
    run([git, "add", "-A"], cwd=root / "data")
    run([git, *ident, "commit", "-q", "--no-verify", "-m", "demo data"], cwd=root / "data", env=env)
    term = theme.data["terminal"]
    accent = _rgb(theme.data["palette"]["accent"])
    rc = root / ".videorc"
    rc.write_text(
        "\n".join(
            [
                f"cd {root}",
                f"export ANALYSIS_DATA_DIR={root / 'data'}",
                f"export PATH={Path(sys.executable).parent}:$PATH",
                "unset PROMPT_COMMAND HISTFILE",
                f"PS1='\\[\\e[38;2;{accent}m\\]{term['prompt']}\\[\\e[0m\\] $ '",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return rc


def record_tape(tape: str, theme: Theme, data: Path, out: Path) -> None:
    """Record a terminal scene with VHS, against a fresh copy of the demo data."""
    vhs = need("vhs", "run `python -m analysis.videos setup` (Linux) or `brew install vhs` (macOS)")
    need("ttyd", "run `python -m analysis.videos setup` (Linux) or `brew install ttyd` (macOS)")
    # A fixed path, not a random temp name: the CLI prints paths (run files),
    # and they should read the same in every recording.
    root = Path(tempfile.gettempdir()) / "analysis-video-demo"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    try:
        rc = _sandbox(data, theme, root)
        tape_file = root / "scene.tape"
        tape_file.write_text(full_tape(tape, theme, str(rc), out), encoding="utf-8")
        env = {**os.environ, "PATH": tool_path()}
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            env["VHS_NO_SANDBOX"] = "true"  # Chromium refuses to sandbox as root (containers)
        run([vhs, str(tape_file)], cwd=root, env=env)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --- assembly ----------------------------------------------------------------


def compose_args(
    ffmpeg: str,
    theme: Theme,
    *,
    visual: Path,
    still: bool,
    audio: Path | None,
    length: float,
    lead: float,
    out: Path,
    fade_in: bool = True,
) -> list[str]:
    """ffmpeg arguments for one scene: the picture held to ``length``, narration ``lead`` in.

    A still fades in and out through the background color, except that the
    video's first scene doesn't fade in: players use frame 0 as the preview.
    """
    w, h, fps = theme.width, theme.height, theme.fps
    bg = theme.bg_hex()
    args = [ffmpeg, "-y", "-v", "error"]
    if still:
        args += ["-loop", "1", "-framerate", str(fps), "-t", f"{length:.3f}", "-i", str(visual)]
    else:
        args += ["-i", str(visual)]
    if audio is not None:
        args += ["-i", str(audio)]
    else:
        args += ["-f", "lavfi", "-t", f"{length:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    fit = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={bg}"
    video = f"[0:v]fps={fps},{fit},tpad=stop_mode=clone:stop_duration={length:.3f},trim=duration={length:.3f}"
    if still:
        fade = theme.timing("slide_fade")
        if fade_in:
            video += f",fade=t=in:st=0:d={fade}:color={bg}"
        video += f",fade=t=out:st={max(0.0, length - fade):.3f}:d={fade}:color={bg}"
    video += ",setpts=PTS-STARTPTS,format=yuv420p[v]"
    delay = int(round(lead * 1000)) if audio is not None else 0
    sound = (
        f"[1:a]aresample=48000,aformat=channel_layouts=stereo,adelay={delay}:all=1,"
        f"apad,atrim=duration={length:.3f}[a]"
    )
    args += ["-filter_complex", f"{video};{sound}", "-map", "[v]", "-map", "[a]"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(fps), "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-t", f"{length:.3f}", str(out)]
    return args


def thumbnail(source: Path, out: Path) -> None:
    """A 1280x720 JPEG of source (a still or a video's first frame): YouTube's thumbnail size."""
    ffmpeg = need("ffmpeg", "install ffmpeg")
    run([ffmpeg, "-y", "-v", "error", "-i", str(source), "-frames:v", "1", "-vf", "scale=1280:720",
         "-q:v", "3", str(out)])


def concat(parts: Sequence[Path], out: Path, cover: Path | None = None) -> None:
    """Join scene clips (same size, rate and codecs) without re-encoding.

    ``cover`` (a JPEG) is embedded as cover art, which file browsers and
    players show as the video's preview.
    """
    ffmpeg = need("ffmpeg", "install ffmpeg")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write(f"file '{p.resolve()}'\n")
        listing = f.name
    args = [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listing]
    if cover is not None:
        args += ["-i", str(cover), "-map", "0", "-map", "1", "-c", "copy", "-disposition:v:1", "attached_pic"]
    else:
        args += ["-c", "copy"]
    try:
        run([*args, "-movflags", "+faststart", str(out)])
    finally:
        os.unlink(listing)
