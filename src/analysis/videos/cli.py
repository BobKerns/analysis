"""``analysis video``: build and check the instructional videos in the
container when Docker is running here, else on this machine; publish and
set up always on this machine.

A thin front for what's already there: ``scripts/videos/docker.sh`` for the
container, ``analysis.videos.commands`` for local runs. ``python -m
analysis.videos`` is this same group, run locally only (the container runs
it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import click
from click.shell_completion import CompletionItem

# The context object `python -m analysis.videos` runs the group with: on
# this machine, without looking for Docker (it's what the container runs).
LOCAL_ONLY = {"where": "local"}


def _repo() -> Path | None:
    """The checkout this is run in (the builder's own lookup, imported
    late: `analysis` starts without loading the video builder)."""
    from analysis.videos.build import repo_root
    from analysis.videos.script import ScriptError

    try:
        return repo_root(Path.cwd())
    except ScriptError:
        return None


def _videos_root() -> Path:
    repo = _repo()
    if repo is None:
        raise click.ClickException("run this inside the analysis repository (it has docs/videos)")
    return repo / "docs" / "videos"


def _complete_video(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[CompletionItem]:
    repo = _repo()
    if repo is None:
        return []
    root = repo / "docs" / "videos"
    return [CompletionItem(p.name) for p in sorted(root.iterdir())
            if (p / "script.md").is_file() and p.name.startswith(incomplete)]


def _video_folder(video: str) -> Path:
    """A video named (04-results) or given by path, as its folder."""
    path = Path(video)
    # A bare name is a folder in docs/videos; a path must exist as given.
    folder = path if path.is_dir() or len(path.parts) > 1 else _videos_root() / video
    if not (folder / "script.md").is_file():
        raise click.BadParameter(f"no video {video} (no script.md in {folder})", param_hint="VIDEO")
    return folder


def docker_running() -> bool:
    """Docker is installed and its daemon answers (Docker Desktop may be
    installed but not started)."""
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        return subprocess.run([docker, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=20, check=False).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _use_docker(where: str | None) -> bool:
    obj = click.get_current_context().obj
    where = where or (obj or {}).get("where")
    if where == "local":
        return False
    running = docker_running()
    if where == "docker" and not running:
        raise click.ClickException("--docker: Docker isn't installed here, or isn't running")
    if not running:
        click.echo("Docker isn't running here; running locally.", err=True)
    return running


def _docker(args: list[str], out: Path | None = None) -> int:
    env = dict(os.environ)
    if out is not None:
        env["ANALYSIS_VIDEO_OUT"] = str(out.resolve())
    script = _videos_root().parent.parent / "scripts" / "videos" / "docker.sh"
    return subprocess.run(["bash", str(script), *args], env=env, check=False).returncode


def _where_option(fn: click.decorators.FC) -> click.decorators.FC:
    fn = click.option("--local", "where", flag_value="local",
                      help="Run on this machine, even when Docker is running.")(fn)
    return click.option("--docker", "where", flag_value="docker",
                        help="Run in the container; fail if Docker isn't running.")(fn)


@click.group(name="video")
def video_group_command() -> None:
    """The instructional videos (docs/videos): build, check, publish."""


@video_group_command.command(name="build")
@click.argument("video", shell_complete=_complete_video)
@click.option("--lang", default=None, help="One language (default: every language the script has).")
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Where the videos land (default: out/videos).")
@click.option("--silent", is_flag=True, help="Timed silence instead of a voice (fast drafts).")
@click.option("--rerecord", is_flag=True, help="Re-record tapes and notebook scenes (after a code change).")
@click.option("--jobs", type=click.IntRange(min=1), default=None,
              help="Tasks at once, besides recordings (default: CPUs less 4, at least 2, and what memory holds).")
@_where_option
def build_command(video: str, lang: str | None, out: Path | None, silent: bool, rerecord: bool,
                  jobs: int | None, where: str | None) -> None:
    """Build VIDEO (a name like 04-results, or its folder): only what
    changed since the last build. In the container when Docker is running."""
    folder = _video_folder(video)
    flags = [*(["--lang", lang] if lang else []), *(["--silent"] if silent else []),
             *(["--rerecord"] if rerecord else []), *(["--jobs", str(jobs)] if jobs else [])]
    if _use_docker(where):
        code = _docker(["build", folder.name, *flags], out)
    else:
        from analysis.videos import commands

        code = commands.build(folder, lang=lang, out=out, silent=silent, rerecord=rerecord, jobs=jobs)
    raise SystemExit(code)


@video_group_command.command(name="check")
@click.argument("videos", nargs=-1, shell_complete=_complete_video)
@_where_option
def check_command(videos: tuple[str, ...], where: str | None) -> None:
    """Check that VIDEOS' scripts parse and their slides render, without
    making any media (default: every video)."""
    folders = [_video_folder(v) for v in videos]
    if _use_docker(where):
        code = _docker(["check", *(f.name for f in folders)])
    else:
        from analysis.videos import commands

        code = commands.check(commands.video_folders(folders))
    raise SystemExit(code)


@video_group_command.command(name="shell")
@click.argument("video", shell_complete=_complete_video)
def shell_command(video: str) -> None:
    """Open bash in the container with VIDEO's own data repo at /data."""
    folder = _video_folder(video)
    if not docker_running():
        raise click.ClickException("shell needs Docker, and it isn't installed here or isn't running")
    raise SystemExit(_docker(["shell", folder.name]))


# publish and setup always run here: publishing needs this machine's Google
# sign-in, and setup fetches the tools a local build uses.
@video_group_command.command(name="publish")
@click.argument("videos", nargs=-1, shell_complete=_complete_video)
@click.option("--lang", default=None, help="One language (default: every language the script has).")
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Where the builds are (default: out/videos).")
@click.option("--dry-run", is_flag=True, help="Show what would be sent; send nothing.")
@click.option("--refresh", is_flag=True,
              help="Only update titles and descriptions of videos already up (no VIDEOS: every published one).")
@click.option("--new-upload", is_flag=True,
              help="Upload again as a new video (YouTube can't replace a video's picture).")
def publish_command(videos: tuple[str, ...], lang: str | None, out: Path | None, dry_run: bool,
                    refresh: bool, new_upload: bool) -> None:
    """Upload built VIDEOS to YouTube, or update what's there."""
    from analysis.videos import commands

    folders = [_video_folder(v) for v in videos]
    raise SystemExit(commands.publish(folders, lang=lang, out=out, dry_run=dry_run, refresh=refresh,
                                      new_upload=new_upload))


@video_group_command.command(name="setup")
def setup_command() -> None:
    """Download the voice model and recording tools for local builds."""
    from analysis.videos import commands

    raise SystemExit(commands.setup())
