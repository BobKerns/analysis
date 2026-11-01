"""``analysis video``: build and check the instructional videos in the
container when Docker is running here, else on this machine; publish and
set up always on this machine.

A thin front for what's already there: ``scripts/videos/docker.sh`` for the
container, ``python -m analysis.videos`` (``analysis.videos.__main__.main``)
for local runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import click
from click.shell_completion import CompletionItem



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
    folder = path if path.is_dir() else _videos_root() / path.name
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
        env["ANALYSIS_VIDEO_OUT"] = str(out)
    script = _videos_root().parent.parent / "scripts" / "videos" / "docker.sh"
    return subprocess.run(["bash", str(script), *args], env=env, check=False).returncode


def _local(args: list[str]) -> int:
    from analysis.videos.__main__ import main

    return main(args)


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
@_where_option
def build_command(video: str, lang: str | None, out: Path | None, silent: bool, rerecord: bool,
                  where: str | None) -> None:
    """Build VIDEO (a name like 04-results, or its folder): only what
    changed since the last build. In the container when Docker is running."""
    folder = _video_folder(video)
    flags = [*(["--lang", lang] if lang else []), *(["--silent"] if silent else []),
             *(["--rerecord"] if rerecord else [])]
    if _use_docker(where):
        code = _docker(["build", folder.name, *flags], out)
    else:
        code = _local(["build", str(folder), *(["--out", str(out)] if out else []), *flags])
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
        code = _local(["check", *(str(f) for f in folders)])
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
# sign-in, and setup fetches the tools a local build uses. Their arguments
# go to the builder's own parser, so its --help is the one shown.
_PASS_THROUGH = {"ignore_unknown_options": True, "allow_extra_args": True, "help_option_names": []}


@video_group_command.command(name="publish", context_settings=_PASS_THROUGH)
@click.argument("args", nargs=-1, type=click.UNPROCESSED, shell_complete=_complete_video)
def publish_command(args: tuple[str, ...]) -> None:
    """Upload built videos to YouTube, or update what's there (--help for its options)."""
    raise SystemExit(_local(["publish", *(_named_folder(a) for a in args)]))


def _named_folder(arg: str) -> str:
    """A video's name as its folder, so publish takes 04-results as build does."""
    if arg.startswith("-") or Path(arg).is_dir():
        return arg
    folder = _videos_root() / arg
    return str(folder) if (folder / "script.md").is_file() else arg


@video_group_command.command(name="setup", context_settings=_PASS_THROUGH)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def setup_command(args: tuple[str, ...]) -> None:
    """Download the voice model and recording tools for local builds."""
    raise SystemExit(_local(["setup", *args]))
