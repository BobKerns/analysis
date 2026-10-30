"""``sim init``: a new data repo built from the code repository's
``templates/`` (#289).

The template scenarios (``t_*``) and the aux tables aren't copied: the loader
reads them from ``templates/`` itself (ADR-003 §4.5a, #317), so a copy would
only hide their later updates. What is copied is what a household edits:
``persons.csv``, the shared CSVs, ``activate-analysis`` and ``scenarios/base``.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from analysis.sim import registries
from analysis.sim.lineage import TEMPLATE_PREFIX, TEMPLATE_SCENARIOS_DIR
from analysis.sim.loader import write_current_scenario
from analysis.sim.retention import DEFAULT_RETENTION, RETENTION_FILENAME

TEMPLATES_DIR = TEMPLATE_SCENARIOS_DIR.parent
CODE_REPO_DIR = TEMPLATES_DIR.parent
# The scenario a new data repo starts from, and is current in.
START_SCENARIO = "base"

# Per-machine files under sim/, kept out of the data repo's history as
# activate-analysis and link-data.sh keep them out.
_IGNORED = (".analysis", ".current_scenario", ".analysis_user", RETENTION_FILENAME)

# templates/ paths not copied: the code repository's own documentation of
# them (it describes the t_ scenarios and tables left behind), and the
# template's sample user and ignore list, which are written fresh.
_NOT_COPIED = frozenset({
    "README.md", "aux/README.md", "scenarios/README.md",
    ".analysis_user", ".gitignore", RETENTION_FILENAME, "persons.csv",
})


class InitError(Exception):
    """A data repo can't be created where asked."""


def is_data_repo(root: Path) -> bool:
    """Whether ``root`` already holds a data repo: the test link-data.sh uses."""
    return (root / "sim" / "persons.csv").exists()


def _copied(relative: Path) -> bool:
    if relative.as_posix() in _NOT_COPIED:
        return False
    parts = relative.parts
    if parts[0] == "scenarios" and len(parts) > 1 and parts[1].startswith(TEMPLATE_PREFIX):
        return False
    # Every aux table is read from templates/aux but the household's status.
    return not (parts[0] == "aux" and relative.name != "filing_status.csv")


def _copy_templates(sim: Path) -> None:
    for path in sorted(TEMPLATES_DIR.rglob("*")):
        relative = path.relative_to(TEMPLATES_DIR)
        if path.is_file() and _copied(relative):
            target = sim / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(header), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _start_scenario(sim: Path, person_id: str) -> str:
    """Credit ``scenarios/base`` to the operator; return its start month
    (``YYYY-MM``), from which the filing status takes effect."""
    path = sim / "scenarios" / START_SCENARIO / "scenario.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or ())
        rows = list(reader)
    for row in rows:
        row["created_by"] = person_id
    _write_csv(path, header, rows)
    return rows[0]["start"][:7]


def create_data_repo(
    root: Path, person: Mapping[str, str], filing_status: str, *, commit: bool = True,
) -> str | None:
    """Build a data repo at ``root`` (``root/sim`` is the data dir), with
    ``person`` (persons.csv's columns but role) as its one primary person
    and operator, ``filing_status`` from the base scenario's start, and
    ``base`` current. With ``commit``, ``root`` is made a git repository if
    it isn't one and the new files are committed, so provenance works from
    the first run.

    Returns git's complaint when the commit failed (the files stay), else
    ``None``. Raises InitError, before writing anything, when ``root``
    already has a data repo or a ``sim/`` folder with anything in it.
    """
    sim = root / "sim"
    if is_data_repo(root):
        raise InitError(f"{root} already holds a data repo")
    if sim.exists() and any(sim.iterdir()):
        raise InitError(f"{sim} already exists and isn't empty")
    if filing_status not in registries.FILING_STATUSES:
        raise InitError(f"filing status must be one of {', '.join(registries.FILING_STATUSES)}: "
                        f"{filing_status!r}")

    sim.mkdir(parents=True, exist_ok=True)
    _copy_templates(sim)
    _write_csv(sim / "persons.csv", registries.PERSONS_CSV_HEADER,
               [{**person, "role": "primary", "meta_json": person.get("meta_json") or "{}"}])
    start = _start_scenario(sim, person["id"])
    _write_csv(sim / "aux" / "filing_status.csv", registries.AUX_KIND_COLUMNS["filing_status"],
               [{"effective_date": start, "filing_status": filing_status}])
    (sim / ".gitignore").write_text("".join(f"{name}\n" for name in _IGNORED), encoding="utf-8")
    (sim / RETENTION_FILENAME).write_text(DEFAULT_RETENTION, encoding="utf-8")
    # activate-analysis finds the code repo and the user from these, so it
    # needn't ask for either.
    (sim / ".analysis").write_text(f"{CODE_REPO_DIR}\n", encoding="utf-8")
    (sim / ".analysis_user").write_text(f"{person['id']}\n", encoding="utf-8")
    write_current_scenario(sim, START_SCENARIO)
    # state/ (beside sim/) holds runs, rebuilt from the data; the code repo's
    # cspell.json reads .cspell/data-words.txt for the household's words.
    _ignore(root / ".gitignore", "/state/")
    (root / ".cspell").mkdir(exist_ok=True)
    (root / ".cspell" / "data-words.txt").touch()
    if commit:
        return _commit(root)
    return None


def _ignore(path: Path, pattern: str) -> None:
    """Add ``pattern`` to the ignore file at ``path`` unless it's there:
    ``root`` may be an existing repository with its own."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if pattern in text.splitlines():
        return
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(f"{text}{pattern}\n", encoding="utf-8")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


def _commit(root: Path) -> str | None:
    if not (root / ".git").exists():
        done = _git(root, "init", "-q")
        if done.returncode:
            return done.stderr.strip() or "git init failed"
    paths = ("sim", ".gitignore", ".cspell")
    for step in (("add", "--", *paths),
                 ("commit", "-q", "-m", "Start a data repo with sim init", "--", *paths)):
        done = _git(root, *step)
        if done.returncode:
            return (done.stderr or done.stdout).strip() or f"git {step[0]} failed"
    return None
