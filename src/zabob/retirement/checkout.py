"""What a code checkout of this program is, said once (#654).

``sim init`` pairs a data repo with the checkout it is run in, the videos'
builder finds the checkout a script belongs to, and ``activate-analysis``
checks the one it is about to activate. A folder one took for a checkout
and another didn't would leave the pairing in doubt, so all three go by
the files here (the script by ``_analysis_validate_repo_dir``, which lists
the same ones; ``tests/test_sim_init.py`` holds the two together).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["MARKERS", "checkout_of", "is_checkout"]

MARKERS = (
    "pyproject.toml",
    "src/zabob/retirement/cli.py",
    "src/zabob/retirement/amazon/cli.py",
    "src/zabob/retirement/sim/cli.py",
)
"""The files a folder has when it is a checkout of this program."""


def is_checkout(folder: Path) -> bool:
    return all((folder / marker).is_file() for marker in MARKERS)


def checkout_of(path: Path) -> Path | None:
    """The code checkout ``path`` is in: the nearest folder at or above it
    that is one. None outside any."""
    return next((folder for folder in (path, *path.parents) if is_checkout(folder)), None)
