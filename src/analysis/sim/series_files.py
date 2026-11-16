"""A scenario's rate and inflation series as the files that hold them (#471).

A series is a file, ``<kind>__<name>.csv`` with ``kind`` ``rate`` or
``inflation``, in a scenario's ``series/`` folder (or ``aux/``, where the
shared ``inflation__cpi.csv`` lives). The most specific file of a name
replaces the rest whole (ADR-003 §4.1d), so what a scenario reads is one
file, found here the way the loader finds it.

A file with only its header is an empty series: the scenario has no such
series, just as if there were no file, except that the ancestor's file is
hidden too, so a scenario that reads the series has to supply its own
(``sim series delete``).

Only ``csv`` and ``lineage`` are imported: completion reads this on TAB.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from analysis.sim.lineage import TEMPLATE_AUX_DIR, load_order, scenario_dir

SERIES_KINDS = ("rate", "inflation")
"""The aux kinds whose ``<kind>__<name>.csv`` files are a series' rates."""

HEADER = ["effective_date", "annual_rate"]

SHARED = "shared aux/"
TEMPLATE = "templates/aux/"

_FOLDERS = ("series", "aux")
"""Where a series' file may be, ``series/`` first: it is where one belongs,
and where a new one is written."""


@dataclass(frozen=True)
class Tier:
    folder: Path
    """The folder holding ``<kind>__<name>.csv`` files."""
    label: str
    """The scenario's name, or ``SHARED``/``TEMPLATE``."""


@dataclass(frozen=True)
class SeriesFile:
    """One file of a series, and what it holds."""

    kind: str
    name: str
    path: Path
    folder: str
    """``series`` or ``aux``."""
    defined_in: str
    """The scenario whose folder holds it, or ``SHARED``/``TEMPLATE``."""
    header: list[str]
    rows: list[dict[str, str]]

    @property
    def empty(self) -> bool:
        return not self.rows

    @property
    def filename(self) -> str:
        return f"{self.kind}__{self.name}.csv"


@dataclass(frozen=True)
class Series:
    """A series as a scenario reads it: the files of its name, most specific
    first, in the folder it is read from."""

    files: list[SeriesFile]
    own: bool
    """Whether the first file is the scenario's own."""
    others: tuple[Series, ...] = ()
    """The same name's files in another folder (``aux/`` beside ``series/``),
    which the loader reads too."""

    @property
    def read(self) -> SeriesFile:
        """The file the scenario reads."""
        return self.files[0]

    @property
    def below(self) -> SeriesFile | None:
        """What the scenario would read without its own file."""
        rest = self.files[1:] if self.own else self.files
        return rest[0] if rest else None

    @property
    def kind(self) -> str:
        return self.read.kind

    @property
    def name(self) -> str:
        return self.read.name

    @property
    def parts(self) -> tuple[Series, ...]:
        """This folder's files and those of every other folder the loader
        reads for the name."""
        return (self, *self.others)

    @property
    def rows(self) -> list[dict[str, str]]:
        """Every row the loader reads for the name, ``aux/``'s before
        ``series/``'s, as it reads them."""
        return [row for part in reversed(self.parts) for row in part.read.rows]

    @property
    def any_own(self) -> bool:
        """Whether the scenario has its own file of the name, in any folder."""
        return any(part.own for part in self.parts)

    @property
    def inherited(self) -> SeriesFile | None:
        """The first file of the name the scenario inherits, in any folder:
        what it reads, in part, without its own files."""
        return next((file for file in (part.below if part.own else part.read for part in self.parts)
                     if file is not None), None)

    @property
    def revertible(self) -> bool:
        """Whether the scenario has its own file of the name and inherits
        one, in any folder (its own ``aux/`` file beside an inherited
        ``series/`` one counts): what ``sim series revert`` drops."""
        return self.any_own and self.inherited is not None

    @property
    def empty(self) -> bool:
        """No rates here: every folder's file of the name is empty."""
        return self.read.empty and all(other.empty for other in self.others)


def _rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """``path``'s header and its non-blank rows, stripped; nothing if it
    can't be read (the loader reports a bad file)."""
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = [{k: (v or "").strip() for k, v in row.items() if k is not None} for row in reader]
            header = list(reader.fieldnames or [])
    except (OSError, UnicodeDecodeError, csv.Error):
        return [], []
    return header, [row for row in rows if any(row.values())]


def tiers(data_dir: Path, scenario: str, folder: str) -> list[Tier]:
    """The folders ``folder`` files are looked up in for ``scenario``, most
    specific first: its own, its ancestors' (later in the load order
    first), then for ``aux`` the data repo's shared folder and the code
    repository's templates."""
    scenarios = data_dir / "scenarios"
    found = [Tier(scenario_dir(scenarios, name) / folder, name)
             for name in reversed(load_order(scenarios, scenario))]
    if folder == "aux":
        found += [Tier(data_dir / "aux", SHARED), Tier(TEMPLATE_AUX_DIR, TEMPLATE)]
    return found


def _files(data_dir: Path, scenario: str) -> Iterator[tuple[str, str, str, Tier, Path]]:
    """(kind, name, folder, tier, path) for every series file the scenario
    can see, most specific tier first within each folder."""
    for folder in _FOLDERS:
        for tier in tiers(data_dir, scenario, folder):
            if not tier.folder.is_dir():
                continue
            for path in sorted(tier.folder.glob("*__*.csv")):
                kind, _, name = path.stem.partition("__")
                if kind in SERIES_KINDS:
                    yield kind, name, folder, tier, path


def scenario_series(data_dir: Path, scenario: str) -> dict[tuple[str, str], Series]:
    """Every rate and inflation series ``scenario`` sees, by (kind, name),
    empty ones included. One with files in both ``series/`` and ``aux/`` is
    shown and edited from ``series/``; the loader reads both, so its
    ``aux/`` files are its ``others``."""
    stacks: dict[tuple[str, str, str], list[tuple[Tier, Path]]] = {}
    for kind, name, folder, tier, path in _files(data_dir, scenario):
        stacks.setdefault((kind, name, folder), []).append((tier, path))
    by_name: dict[tuple[str, str], list[Series]] = {}
    for (kind, name, folder), stack in stacks.items():  # series/ first
        files = []
        for tier, path in stack:
            header, rows = _rows(path)
            files.append(SeriesFile(kind, name, path, folder, tier.label, header, rows))
        by_name.setdefault((kind, name), []).append(Series(files, own=stack[0][0].label == scenario))
    found = {key: Series(first.files, first.own, tuple(rest)) for key, (first, *rest) in by_name.items()}
    return dict(sorted(found.items(), key=lambda item: (item[0][1], item[0][0])))


def live_series_names(data_dir: Path, scenario: str) -> set[str]:
    """The names of the rate and inflation series the scenario has rates for
    (not the empty ones)."""
    return {name for (_, name), series in scenario_series(data_dir, scenario).items()
            if not series.empty}


def write(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    """Write a series file: ``header`` (``HEADER``'s columns first), then
    ``rows``. No rows leaves only the header: an empty series."""
    columns = [*HEADER, *(c for c in header if c not in HEADER)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows({c: row.get(c, "") for c in columns} for row in rows)
