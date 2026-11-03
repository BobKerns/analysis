"""What ``analysis video build|check|publish|setup`` do on this machine (see
docs/videos/README.md); ``analysis.videos.cli`` is the command line in
front of them, and decides when the container runs them instead."""

from __future__ import annotations

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
from analysis.videos import youtube
from analysis.videos.build import GIB, available_memory, build_languages, default_jobs, repo_root

# VHS archives by (platform.system(), platform.machine()), named as upstream's.
_VHS_PLATFORMS = {
    ("Linux", "x86_64"): "Linux_x86_64",
    ("Linux", "AMD64"): "Linux_x86_64",
    ("Linux", "aarch64"): "Linux_arm64",
    ("Linux", "arm64"): "Linux_arm64",
    ("Darwin", "x86_64"): "Darwin_x86_64",
    ("Darwin", "arm64"): "Darwin_arm64",
}
TTYD_URL = "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64"


def vhs_url(system: str, machine: str) -> str | None:
    """The pinned VHS build for a platform, or None if there isn't one."""
    name = _VHS_PLATFORMS.get((system, machine))
    return name and f"{render.VHS_RELEASE}/vhs_{render.VHS_VERSION}_{name}.tar.gz"


def _install_vhs(url: str, bin_dir: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "vhs.tar.gz"
        _download(url, archive)
        with tarfile.open(archive) as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("/vhs"))
            member.name = "vhs"
            tar.extract(member, bin_dir, filter="data")
    (bin_dir / "vhs").chmod(0o755)


def _download(url: str, dest: Path) -> None:
    print(f"  downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(dest)


def setup() -> int:
    """Fetch the Kokoro model, VHS and (on Linux) ttyd into the tool cache."""
    home = render.cache_home()
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    print(f"tool cache: {home}")
    for name in render.KOKORO_FILES:
        if not (home / name).is_file():
            _download(f"{render.KOKORO_RELEASE}/{name}", home / name)
    linux = platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64")
    try:
        render.need_vhs()
    except (render.ToolMissing, RuntimeError):
        # Missing, or a stock VHS (brew's), which can't record terminal images.
        url = vhs_url(platform.system(), platform.machine())
        if url:
            _install_vhs(url, bin_dir)
        else:
            print(f"  vhs: no build of VHS {render.VHS_VERSION} for this platform; see {render.VHS_RELEASE}")
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
                youtube.check(s)
                print(f"ok   {folder.name} [{lang}]: {len(s.scenes)} scenes")
        except scripts.ScriptError as e:
            failed += 1
            print(f"FAIL {e}", file=sys.stderr)
    return 1 if failed else 0


def publish(folders: list[Path], *, lang: str | None = None, out: Path | None = None,
            dry_run: bool = False, refresh: bool = False, new_upload: bool = False) -> int:
    """Upload built videos to YouTube, or (refresh) update what's there;
    with refresh and no folders, every published video."""
    repo = repo_root(Path.cwd())
    if not folders:
        if not refresh:
            print("error: name the videos to publish (or --refresh for every published one)", file=sys.stderr)
            return 2
        folders = youtube.published_folders(repo / "docs" / "videos")
    if refresh and new_upload:
        print("error: --refresh and --new-upload don't go together", file=sys.stderr)
        return 2
    try:
        youtube.publish(folders, langs=[lang] if lang else None, out_dir=out or repo / "out" / "videos",
                        repo=repo, dry_run=dry_run, new_upload=new_upload, refresh_only=refresh)
    except scripts.ScriptError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def video_folders(folders: list[Path]) -> list[Path]:
    """The folders named, or every video in docs/videos."""
    if folders:
        return folders
    root = repo_root(Path.cwd()) / "docs" / "videos"
    return sorted(p for p in root.iterdir() if (p / "script.md").is_file())


def _memory_note(asked: int | None) -> str:
    """Why the default is lower than the CPUs allow, if memory is why."""
    if asked is not None or default_jobs(memory=1 << 60) == default_jobs():
        return ""
    free = available_memory() or 0
    return f" (fewer for memory: {free / GIB:.1f} GB free; Docker Desktop: Settings > Resources > Memory)"


def build(folder: Path, *, lang: str | None = None, out: Path | None = None, silent: bool = False,
          rerecord: bool = False, jobs: int | None = None) -> int:
    """Build one video's languages (or just lang): only what changed since the last build."""
    langs = [lang] if lang else scripts.languages(folder)
    try:
        print(f"{folder.name} [{', '.join(langs)}]")
        tasks = jobs or default_jobs()
        print(f"  {tasks} task{'s' if tasks > 1 else ''} at once, besides recordings{_memory_note(jobs)}")
        results = build_languages(folder, langs, jobs=tasks, out_dir=out,
                                  engine="silent" if silent else None, rerecord=rerecord)
        for result in results:
            print(f"  -> {result.video} ({result.length:.1f}s)\n  -> {result.captions}")
            if result.description:
                print(f"  -> {result.description}")
    except (scripts.ScriptError, render.ToolMissing) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
