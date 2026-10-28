"""
Scenario management for the simulation.

This module provides functions to create, load, and manipulate scenarios
within the simulation framework.
"""

import csv
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from analysis.sim.loader import LoadError
from analysis.sim import chain_edit, registries, units
from analysis.sim.dates import month_end_iso
from analysis.sim.lineage import (
    MIXIN_ONLY_DESCRIPTION,
    default_description,
    load_order,
    parse_parents,
    is_mixin_only,
    lineage_order,
    is_template,
    read_parents,
    scenario_dir,
    template_names,
)
from analysis.sim.loader import ScenarioRef, list_scenario_names, resolve_scenario_name
from analysis.sim.readme import parse_readme_summary
from analysis.sim import cli


# Written into every new scenario's series/ so the folder isn't empty (git
# doesn't keep an empty folder) and says what belongs there. It names its
# references in prose: a link wouldn't resolve from inside the data repo.
SERIES_README = """\
# Series

Growth-rate and inflation series for this scenario, one CSV file per
series. A file named `rate__<series>.csv` is a growth rate (for an
account's or lot's `series`, or a fund's asset class);
`inflation__<series>.csv` is an inflation rate (for a scheduled
transaction's `series`).

Each file has two columns:

- `effective_date`: the month the rate takes effect; it holds until the
  next row's date.
- `annual_rate`: the annual rate as a percent, with its sign (`4.5%`).

A file here replaces the parent scenario's file of the same name; a series
with no file here is inherited from the parent. CPI is the exception: it
lives in `aux/inflation__cpi.csv`, not here.

Details: ADR-003 sections 4.1d and 4.4 in the analysis code repository.
"""


# The placeholder section of a new scenario's README.md (#190), for the
# author to replace with what the scenario is for.
README_PURPOSE = """\
## Purpose

_What is this scenario for, and what does it change from what it builds on?
Replace this paragraph; the title and first paragraph above are what scenario
listings and notebooks show._
"""


def readme_paragraph(description: str, parents: list[str]) -> str:
    """A generated README.md's first paragraph: the scenario's description,
    the default one with its parents in `code`, so a rename follows them."""
    if description == default_description(parents):
        return f"Derived from {' + '.join(f'`{p}`' for p in parents)}."
    return description


def readme_parents_line(parents: list[str]) -> str:
    """A generated README.md's line naming what the scenario builds on."""
    if not parents:
        return "Builds on no other scenario."
    line = f"Builds on `{parents[0]}`"
    if parents[1:]:
        line += f", with mix-in{'s' if len(parents) > 2 else ''} " + ", ".join(f"`{m}`" for m in parents[1:])
    return line + "."


def scenario_readme(name: str, description: str, parents: list[str]) -> str:
    """The README.md ``scenario create`` writes (ADR-003 §4.1e): the name as
    its title, the description as its first paragraph (which the loader then
    takes as ``scenario.description``), the parents, and a placeholder for
    what the scenario is for."""
    return "\n\n".join((
        f"# {name}", readme_paragraph(description, parents), readme_parents_line(parents), README_PURPOSE,
    ))


def current_user() -> str:
    """Who a new scenario's ``created_by`` names."""
    return os.environ.get("ANALYSIS_USER") or os.environ.get("USER") or "unknown"


def create_scenario(data_dir: Path, name: str, parent: str | ScenarioRef | None,
                    description: str | None = None,
                    intent_dir: Path | None = None,
                    mixins: Sequence[str] = (),
                    ensemble_size: int | None = None) -> Path:
    """
    Create a new scenario with the given name, parent, and optional description.

    ``mixins`` are further parents applied after ``parent``'s line, in order
    (ADR-003 §4.5); ``parent_scenario`` records them colon-separated. With
    no ``parent`` (and no mix-ins), or only mix-in only ones, it is a mix-in
    only scenario (#259, #324).

    ``ensemble_size`` is written to ``meta_json`` when given; left out, it is
    inherited from the parents (ADR-006 §7), and 0 makes a scenario that is
    not an ensemble even under an ensemble parent. The seed is left blank,
    so it inherits.

    Returns the path to the created scenario.
    """
    if not re.match(registries.SCENARIO_NAME_RE, name):
        raise LoadError(f"invalid scenario name: {name}")
    match parent:
        case ScenarioRef():
            parent_names = [parent.name]
        case str():
            parent_names = [resolve_scenario_name(data_dir, parent, intent_dir=intent_dir).name]
        case None if not mixins:
            parent_names = []
        case None:
            raise LoadError("mix-ins need a parent to be mixed into")
        case _:
            raise LoadError(f"invalid parent type: {type(parent)}")
    mixin_names = [
        resolve_scenario_name(data_dir, mixin, intent_dir=intent_dir).name for mixin in mixins
    ]
    if ensemble_size is not None and ensemble_size < 0:
        raise LoadError(f"ensemble_size must be 0 or positive (got {ensemble_size})")
    target = data_dir / "scenarios" / name
    if target.exists():
        raise LoadError(f"scenario already exists: {target}")
    target.mkdir(parents=True, exist_ok=True)
    parents = [*parent_names, *mixin_names]

    skeletons: dict[str, tuple[str, ...]] = {
        "accounts.csv": registries.ACCOUNTS_CSV_HEADER,
        "account_lots.csv": registries.ACCOUNT_LOTS_CSV_HEADER,
        "scheduled.csv": registries.SCHEDULED_CSV_HEADER,
        "calendar.csv": registries.CALENDAR_HEADER,
    }

    series_dir = target / "series"
    series_dir.mkdir()
    (series_dir / "README.md").write_text(SERIES_README, encoding="utf-8")

    # Both role folders, kept in git by an empty .gitignore, so it's plain
    # where a new chain goes (ADR-003 §4.7).
    for role in registries.CHAIN_ROLES:
        role_dir = target / "chains" / role
        role_dir.mkdir(parents=True)
        (role_dir / ".gitignore").write_text("", encoding="utf-8")
    description = description or (default_description(parents) if parents else MIXIN_ONLY_DESCRIPTION)
    (target / "README.md").write_text(scenario_readme(name, description, parents), encoding="utf-8")

    pd.DataFrame(columns=[  # the example is a source chain, which takes no tax columns
        c for c in chain_edit.CHAIN_COLUMNS if c not in chain_edit.TAX_COLUMNS
    ]).to_csv(
        target / "chains" / "source" / "_example.csv", index=False,
    )
    cli._append_csv_row(
        target / "scenario.csv",
        registries.SCENARIO_HEADER,
        {
            "name": name,
            "description": description,
            "parent_scenario": ":".join(parents),
            "created_by": current_user(),
            "meta_json": json.dumps(
                {} if ensemble_size is None else {"ensemble_size": ensemble_size},
                separators=(",", ":"),
            ),
        },
    )
    for filename, header in skeletons.items():
        pd.DataFrame(columns=list(header)).to_csv(target / filename, index=False)

    return target


@dataclass(frozen=True)
class ScenarioDate:
    """A scenario's ``start`` or ``end`` month as the load would merge it,
    and whether it came from an ancestor rather than the scenario itself."""

    month: str
    inherited: bool


@dataclass(frozen=True)
class ScenarioValue:
    """A ``scenario.csv`` value as the load would merge it, and whether it
    came from an ancestor rather than the scenario itself."""

    value: int
    inherited: bool


@dataclass(frozen=True)
class ScenarioSummary:
    """One scenario folder, as ``sim scenario list --verbose`` shows it."""

    name: str
    parents: list[str]
    description: str
    start: ScenarioDate | None = None
    end: ScenarioDate | None = None
    seed: ScenarioValue | None = None
    ensemble_size: ScenarioValue | None = None
    """``None`` when the merged ``meta_json.ensemble_size`` is absent or 0:
    the scenario is not an ensemble (ADR-006 §7)."""
    mixin_only: bool = False
    """No start or end anywhere in its lineage: only for mixing into others
    (#259, #324)."""

    @property
    def is_ensemble(self) -> bool:
        return self.ensemble_size is not None


def scenario_list_names(
    data_dir: Path, scenarios_dir: Path | None = None, *, templates: bool = False,
) -> list[str]:
    """``sim scenario list``'s names: the data repo's own, and with
    ``templates`` the template scenarios it has no folder for, sorted."""
    scenarios = scenarios_dir if scenarios_dir is not None else Path(data_dir) / "scenarios"
    names = list_scenario_names(data_dir, scenarios)
    return sorted([*names, *template_names(scenarios)]) if templates else names


def scenario_summaries(
    data_dir: Path, scenarios_dir: Path | None = None, *, templates: bool = False,
) -> list[ScenarioSummary]:
    """Every scenario folder's name, parents, one-line description, and start
    and end months, sorted; with ``templates``, the template scenarios the
    data repo reads from the code repository too (ADR-003 §4.5a).

    Read straight from the folders rather than loaded, so it is quick and
    still lists a scenario that wouldn't load. The description follows the
    loader's rule (ADR-003 §4.1e), cut to one line: the README.md title
    (unless it just restates the name), else its first paragraph, else
    scenario.csv's own ``description``. The dates follow the loader's
    field-by-field merge down the load order (ADR-003 §4.5).
    """
    scenarios = scenarios_dir if scenarios_dir is not None else Path(data_dir) / "scenarios"
    summaries = []
    for name in scenario_list_names(data_dir, scenarios, templates=templates):
        mixin_only = is_mixin_only(scenarios, name)
        # A mix-in only scenario's own lineage's values, not those of the
        # template it is checked on: mixed in, only its lineage applies.
        order = lineage_order(scenarios, name) if mixin_only else load_order(scenarios, name)
        rows = {n: _scenario_row(scenario_dir(scenarios, n)) for n in order}
        summaries.append(ScenarioSummary(
            mixin_only=mixin_only,
            name=name,
            parents=read_parents(scenario_dir(scenarios, name)),
            description=_one_line_description(scenario_dir(scenarios, name), rows[name]),
            start=_merged_date(_templates_first(scenarios, rows), name, "start"),
            end=_merged_date(_templates_first(scenarios, rows), name, "end"),
            seed=_merged_seed(_templates_first(scenarios, rows), name),
            ensemble_size=_merged_ensemble_size(rows, name),
        ))
    return summaries


def ensemble_settings(
    data_dir: Path, name: str,
) -> tuple[ScenarioValue | None, ScenarioValue | None]:
    """Scenario ``name``'s merged ``(seed, ensemble_size)``, as
    :func:`scenario_summaries` reports them."""
    scenarios = Path(data_dir) / "scenarios"
    rows = {n: _scenario_row(scenario_dir(scenarios, n)) for n in load_order(scenarios, name)}
    return _merged_seed(_templates_first(scenarios, rows), name), _merged_ensemble_size(rows, name)


def _templates_first(scenarios: Path, rows: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """``rows`` with the template scenarios moved ahead of the rest, each
    group in load order: a template's start, end and seed are fallbacks
    that any data repo scenario in the lineage overrides (ADR-003 §4.5a), as
    the loader merges them."""
    return dict(sorted(rows.items(), key=lambda item: not is_template(scenarios, item[0])))


def _scenario_row(scenario_dir: Path) -> dict[str, str]:
    """scenario.csv's data row, or {} -- tolerant, like read_parents:
    reporting a malformed scenario.csv is the loader's job, and a listing
    shouldn't fail on one."""
    try:
        with (scenario_dir / "scenario.csv").open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                return {k: v or "" for k, v in row.items() if k is not None}
    except (OSError, UnicodeDecodeError, csv.Error):
        pass
    return {}


def _merged_date(rows: dict[str, dict[str, str]], name: str, col: str) -> ScenarioDate | None:
    """The last valid ``col`` in load order (``rows`` is root first), as
    ``YYYY-MM``; the loader pins a month column to its last day, so the day
    adds nothing. Like the loader, which reports and blanks a date that
    doesn't parse, an invalid one lets the ancestor's show through."""
    for owner, row in reversed(rows.items()):
        raw = (row.get(col) or "").strip()
        if not raw:
            continue
        try:
            month = month_end_iso(raw)[:len("YYYY-MM")]
        except ValueError:
            continue
        return ScenarioDate(month, inherited=owner != name)
    return None


def _merged_seed(rows: dict[str, dict[str, str]], name: str) -> ScenarioValue | None:
    """The last ``seed`` in load order, merged field by field like the
    loader's; one that isn't an integer is the loader's to report."""
    for owner, row in reversed(rows.items()):
        raw = (row.get("seed") or "").strip()
        if not raw:
            continue
        try:
            return ScenarioValue(int(raw), inherited=owner != name)
        except ValueError:
            return None
    return None


def _merged_ensemble_size(
    rows: dict[str, dict[str, str]], name: str,
) -> ScenarioValue | None:
    """The last ``meta_json.ensemble_size`` in load order, merged key by key
    like the loader's (ADR-003 §4.5), or ``None`` when it is absent or 0:
    then the scenario is not an ensemble. An invalid one is the loader's to
    report; here it is ``None`` too."""
    for owner, row in reversed(rows.items()):
        try:
            meta = json.loads(row.get("meta_json") or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(meta, dict) or "ensemble_size" not in meta:
            continue
        try:
            size = units.json_ensemble_size(meta["ensemble_size"])
        except ValueError:
            return None
        return ScenarioValue(size, inherited=owner != name) if size > 0 else None
    return None


def _one_line_description(scenario_dir: Path, row: dict[str, str]) -> str:
    readme = scenario_dir / "README.md"
    if readme.is_file():
        try:
            summary = parse_readme_summary(readme.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            summary = None
        if summary is not None:
            # A title that only restates the folder name ("# Base") says
            # nothing the name column doesn't; the paragraph does.
            title = summary.title
            if title and _name_key(title) == _name_key(scenario_dir.name):
                title = ""
            # A README.md wins even when blank (ADR-003 §4.1e).
            return title or summary.paragraph
    return " ".join(row.get("description", "").split())


def _name_key(text: str) -> str:
    return re.sub(r"[\s_-]+", "", text).casefold()


# ---------- editing and deleting a scenario (#190) ----------

@dataclass(frozen=True)
class FieldChange:
    """One ``scenario.csv`` value (or ``meta_json`` key) an edit changed; a
    blank one inherits."""

    field: str
    old: str
    new: str


def edit_scenario_row(
    scenario_dir: Path, values: dict[str, str], meta: dict[str, object | None],
) -> list[FieldChange]:
    """Set ``scenario_dir/scenario.csv``'s ``values`` (a blank one inherits)
    and its ``meta_json`` keys (``None`` drops one, so it inherits); what
    changed. A column the file lacks is added. The default description
    follows a change of parents, as it does a rename's."""
    path = scenario_dir / "scenario.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    if len(rows) != 1:
        raise LoadError(f"{path}: expected exactly one row, found {len(rows)}")
    row = {k: v or "" for k, v in rows[0].items() if k is not None}
    before = dict(row)
    values = dict(values)
    new_parents = parse_parents(values.get("parent_scenario"))
    # A root scenario derives from nothing, so the old description stays.
    if new_parents and "description" not in values:
        old_parents = parse_parents(row.get("parent_scenario"))
        if row.get("description", "").strip() == default_description(old_parents):
            values["description"] = default_description(new_parents)
    row.update(values)
    if meta:
        try:
            meta_json = json.loads(row.get("meta_json") or "{}")
        except json.JSONDecodeError as exc:
            raise LoadError(f"{path}: meta_json is not valid JSON: {exc}") from None
        if not isinstance(meta_json, dict):
            raise LoadError(f"{path}: meta_json must be a JSON object")
        for key, value in meta.items():
            if value is None:
                meta_json.pop(key, None)
            else:
                meta_json[key] = value
        row["meta_json"] = json.dumps(meta_json, separators=(",", ":"))
    for column in registries.SCENARIO_HEADER:
        if column in row and column not in header:
            header.append(column)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerow(row)
    changes = [FieldChange(column, before.get(column, ""), row[column])
               for column in header if column != "meta_json" and before.get(column, "") != row.get(column, "")]
    if meta:
        old_meta = json.loads(before.get("meta_json") or "{}")
        new_meta = json.loads(row["meta_json"])
        changes += [FieldChange(f"meta_json {key}", _shown(old_meta.get(key)), _shown(new_meta.get(key)))
                    for key in meta if old_meta.get(key) != new_meta.get(key)]
    return changes


def _shown(value: object) -> str:
    return "" if value is None else str(value)


def follow_readme(scenario_dir: Path, old: dict[str, str], new: dict[str, str]) -> bool | None:
    """Keep a README.md that ``scenario create`` wrote in step with an edit
    of ``scenario.csv`` (``old`` and ``new`` are its rows): its generated
    first paragraph follows the description, and its "Builds on" line the
    parents, while they still read as generated. Text somebody wrote is left
    alone. ``None`` if there is no README.md or nothing it shows changed,
    else whether it now matches."""
    readme = scenario_dir / "README.md"
    old_parents, new_parents = (parse_parents(row.get("parent_scenario")) for row in (old, new))
    swaps = [
        (readme_paragraph(old.get("description", ""), old_parents),
         readme_paragraph(new.get("description", ""), new_parents)),
        (readme_parents_line(old_parents), readme_parents_line(new_parents)),
    ]
    swaps = [(was, now) for was, now in swaps if was != now]
    if not readme.is_file() or not swaps:
        return None
    # Only whole paragraphs, as generated: one line, blank lines around. If
    # any has been reworded, the README is somebody's own; none is touched.
    blocks = readme.read_text(encoding="utf-8").split("\n\n")
    if not all(was in blocks for was, _ in swaps):
        return False
    for was, now in swaps:
        blocks[blocks.index(was)] = now
    readme.write_text("\n\n".join(blocks), encoding="utf-8")
    return True


def dependents(scenarios_dir: Path, name: str) -> list[str]:
    """The scenarios naming ``name`` as their parent or a mix-in, sorted."""
    return sorted(
        other for other in list_scenario_names(scenarios_dir.parent, scenarios_dir)
        if other != name and name in read_parents(scenarios_dir / other)
    )
