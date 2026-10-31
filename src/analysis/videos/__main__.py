"""``python -m analysis.videos build|check|setup`` (see docs/videos/README.md)."""

from __future__ import annotations

import argparse
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from analysis.videos import notebook, render
from analysis.videos import script as scripts
from analysis.videos import theme as themes
from analysis.videos.build import build, repo_root

VHS_URL = "https://github.com/charmbracelet/vhs/releases/download/v0.10.0/vhs_0.10.0_Linux_x86_64.tar.gz"
TTYD_URL = "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64"


def _download(url: str, dest: Path) -> None:
    print(f"  downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(dest)


def setup() -> int:
    """Fetch the Kokoro model and, on Linux, VHS and ttyd, into the tool cache."""
    home = render.cache_home()
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    print(f"tool cache: {home}")
    for name in render.KOKORO_FILES:
        if not (home / name).is_file():
            _download(f"{render.KOKORO_RELEASE}/{name}", home / name)
    linux = platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64")
    if not shutil.which("vhs", path=render.tool_path()):
        if linux:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "vhs.tar.gz"
                _download(VHS_URL, archive)
                with tarfile.open(archive) as tar:
                    member = next(m for m in tar.getmembers() if m.name.endswith("/vhs"))
                    member.name = "vhs"
                    tar.extract(member, bin_dir, filter="data")
            (bin_dir / "vhs").chmod(0o755)
        else:
            print("  vhs: install it with `brew install vhs` (brings ttyd too)")
    if not shutil.which("ttyd", path=render.tool_path()):
        if linux:
            _download(TTYD_URL, bin_dir / "ttyd")
            (bin_dir / "ttyd").chmod(0o755)
        else:
            print("  ttyd: install it with `brew install ttyd`")
    for tool in ("ffmpeg", "ffprobe", "uv"):
        if not shutil.which(tool):
            print(f"  {tool}: not on PATH; install it")
    print("Slides use Playwright's Chromium: `uv run playwright install chromium` if it isn't installed.")
    return 0


def check(folders: list[Path]) -> int:
    """Parse every script (and translation) and render every slide's page; no media."""
    failed = 0
    for folder in folders:
        try:
            theme = themes.load(themes.videos_root(folder))
            for lang in scripts.languages(folder):
                s = scripts.load(folder, lang)
                theme.narration(lang)
                for scene in s.scenes:
                    if scene.visual.kind == "slide":
                        render.slide_html(theme, s.meta, scene.visual.fields, lang)
                    elif scene.visual.kind == "notebook":
                        notebook.check(notebook.parse(scene.visual.source), repo_root(folder) / "notebooks")
                print(f"ok   {folder.name} [{lang}]: {len(s.scenes)} scenes")
        except scripts.ScriptError as e:
            failed += 1
            print(f"FAIL {e}", file=sys.stderr)
    return 1 if failed else 0


def _video_folders(args: list[Path]) -> list[Path]:
    if args:
        return args
    root = repo_root(Path.cwd()) / "docs" / "videos"
    return sorted(p for p in root.iterdir() if (p / "script.md").is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m analysis.videos", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build a video (only what changed since the last build)")
    b.add_argument("folder", type=Path, help="the video's folder, e.g. docs/videos/00-pilot")
    b.add_argument("--lang", default=None, help="language (default: every language the script has)")
    b.add_argument("--out", type=Path, default=None, help="output folder (default: out/videos)")
    b.add_argument("--silent", action="store_true", help="timed silence instead of a voice (fast drafts)")
    b.add_argument("--rerecord", action="store_true", help="re-record tapes and notebook scenes (after a code change)")
    c = sub.add_parser("check", help="check scripts parse and slides render (no media)")
    c.add_argument("folders", type=Path, nargs="*", help="video folders (default: all in docs/videos)")
    sub.add_parser("setup", help="download the voice model and recording tools")
    args = parser.parse_args(argv)

    if args.command == "setup":
        return setup()
    if args.command == "check":
        return check(_video_folders(args.folders))
    langs = [args.lang] if args.lang else scripts.languages(args.folder)
    try:
        for lang in langs:
            print(f"{args.folder.name} [{lang}]")
            result = build(args.folder, lang=lang, out_dir=args.out,
                           engine="silent" if args.silent else None, rerecord=args.rerecord)
            print(f"  -> {result.video} ({result.length:.1f}s)\n  -> {result.captions}")
    except (scripts.ScriptError, render.ToolMissing) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
