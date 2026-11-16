"""``python -m analysis.videos build|check|publish|setup``: ``analysis video``
on this machine, never in the container (it is what the container runs;
see docs/videos/README.md)."""

from __future__ import annotations

from analysis.videos.cli import LOCAL_ONLY, video_group_command


def main(argv: list[str] | None = None) -> None:
    video_group_command.main(args=argv, prog_name="python -m analysis.videos", obj=LOCAL_ONLY)


if __name__ == "__main__":
    main()
