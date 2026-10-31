"""``python -m analysis.videos build|check|publish|setup`` (see docs/videos/README.md)."""

from __future__ import annotations

import argparse
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from analysis.videos import render
from analysis.videos import script as scripts
from analysis.videos import theme as themes
from analysis.videos import youtube
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
                youtube.check(s)
                print(f"ok   {folder.name} [{lang}]: {len(s.scenes)} scenes")
        except scripts.ScriptError as e:
            failed += 1
            print(f"FAIL {e}", file=sys.stderr)
    return 1 if failed else 0


def publish(args: argparse.Namespace) -> int:
    repo = repo_root(Path.cwd())
    folders: list[Path] = args.folders
    if not folders:
        if not args.refresh:
            print("error: name the videos to publish (or --refresh for every published one)", file=sys.stderr)
            return 2
        folders = youtube.published_folders(repo / "docs" / "videos")
    if args.refresh and args.new_upload:
        print("error: --refresh and --new-upload don't go together", file=sys.stderr)
        return 2
    try:
        youtube.publish(folders, langs=[args.lang] if args.lang else None, out_dir=args.out or repo / "out" / "videos",
                        repo=repo, dry_run=args.dry_run, new_upload=args.new_upload, refresh_only=args.refresh)
    except scripts.ScriptError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


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
    b.add_argument("--rerecord", action="store_true", help="re-record tapes (after a CLI change)")
    c = sub.add_parser("check", help="check scripts parse and slides render (no media)")
    c.add_argument("folders", type=Path, nargs="*", help="video folders (default: all in docs/videos)")
    p = sub.add_parser("publish", help="upload built videos to YouTube, or update what's there")
    p.add_argument("folders", type=Path, nargs="*", help="video folders (with --refresh, default: every published one)")
    p.add_argument("--lang", default=None, help="language (default: every language the script has)")
    p.add_argument("--out", type=Path, default=None, help="where the builds are (default: out/videos)")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent; send nothing")
    p.add_argument("--refresh", action="store_true",
                   help="only update titles and descriptions of videos already up")
    p.add_argument("--new-upload", action="store_true",
                   help="upload again as a new video (YouTube can't replace a video's picture)")
    sub.add_parser("setup", help="download the voice model and recording tools")
    args = parser.parse_args(argv)

    if args.command == "setup":
        return setup()
    if args.command == "check":
        return check(_video_folders(args.folders))
    if args.command == "publish":
        return publish(args)
    langs = [args.lang] if args.lang else scripts.languages(args.folder)
    try:
        for lang in langs:
            print(f"{args.folder.name} [{lang}]")
            result = build(args.folder, lang=lang, out_dir=args.out,
                           engine="silent" if args.silent else None, rerecord=args.rerecord)
            print(f"  -> {result.video} ({result.length:.1f}s)\n  -> {result.captions}")
            if result.description:
                print(f"  -> {result.description}")
    except (scripts.ScriptError, render.ToolMissing) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
