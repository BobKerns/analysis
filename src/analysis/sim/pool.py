"""ADR-009: pooled ensembles, one result set drawn from several ensembles.

A pool is a scenario folder holding only ``scenario.csv`` (and its README):
its ``meta_json.pool`` names the component ensembles and their weights. It
has no lineage and nothing to load. ``sim run`` brings each component's
latest Monte Carlo batch up to date, then makes the pool's batch from them:
pool member *j* is member *j* of one component, linked from that
component's batch, so nothing is simulated twice and every future appears
once (components that share a seed draw the same values at the same index,
ADR-006 §3, so taking the same index from two of them would repeat one).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analysis.sim import units
from analysis.sim.lineage import (
    POOL_KEY,
    is_mixin_only,
    is_pool,
    lineage_order,
    read_meta,
    read_parents,
    scenario_dir,
)
from analysis.sim.loader import BATCH_MANIFEST_FILENAME, latest_ensemble_batch

__all__ = [
    "POOL_KEY", "Pool", "PoolError", "read_pool", "pool_issues", "pool_size", "assign_members",
    "build_pool_batch", "pool_record", "member_components", "pools_naming", "is_pool",
    "default_description", "renamed_spec", "readme_pools_line", "refresh_readme_pools_line",
]

# 100% in the micros `units.percent_to_micros` returns.
_WHOLE = 100 * units.MICROS_PER_PERCENT
# What a pool folder may hold besides dotfiles: everything else is a
# scenario's, and would be silently ignored. Its guard rails are read by
# the reports, over its base component's (ADR-009 §6).
_POOL_FILES = {"scenario.csv", "README.md", "guard_rails.csv"}


def default_description(components: Sequence[str]) -> str:
    """What ``ensemble pool create`` describes a pool as when not told."""
    return f"Pooled from {' + '.join(components)}"


def renamed_spec(spec: Mapping[str, Any], old: str, new: str) -> dict[str, Any] | None:
    """``meta_json.pool`` with component ``old`` renamed ``new`` (its key or
    list entry, keeping its place, and ``base``); ``None`` when it doesn't
    name ``old``."""
    components = spec.get("components")
    if not isinstance(components, (dict, list)) or old not in components:
        return None
    out = dict(spec)
    if isinstance(components, dict):
        out["components"] = {new if c == old else c: w for c, w in components.items()}
    else:
        out["components"] = [new if c == old else c for c in components]
    if out.get("base") == old:
        out["base"] = new
    return out


class PoolError(ValueError):
    """A pool's definition or components don't hold up; ``issues`` lists why."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = issues


@dataclass(frozen=True)
class Pool:
    """A pool's own definition, as its ``scenario.csv`` states it."""

    name: str
    components: tuple[str, ...]
    """In the order listed: ties in member assignment go to the earlier one."""
    weights: tuple[int, ...]
    """Each component's share, in micros of a percent (100% = 1_000_000)."""
    base: str
    """The component whose member 0 is the pool's member 0 (ADR-006 §9)."""
    size: int | None
    """The authored ``ensemble_size``; ``None`` means the smallest component's."""
    reserve: object = None
    """The pool's own ``meta_json`` ``reserve``, as written; ``None`` keeps
    the base component's."""

    def weight_text(self, component: str) -> str:
        return units.micros_to_percent(self.weights[self.components.index(component)])

    def definition(self) -> dict[str, Any]:
        """What the pool's batch records, to tell later whether it changed."""
        out: dict[str, Any] = {"components": {c: self.weight_text(c) for c in self.components},
                               "base": self.base, "size": self.size}
        if self.reserve is not None:
            out["reserve"] = self.reserve
        return out


def read_pool(scenarios_dir: Path, name: str) -> Pool:
    """Pool ``name``'s definition. Raises :class:`PoolError` when it isn't a
    pool or its own ``scenario.csv`` is malformed; its components are
    :func:`pool_issues`'s to check."""
    folder = scenario_dir(scenarios_dir, name)
    meta = read_meta(folder)
    if POOL_KEY not in meta:
        raise PoolError([f"{name} is not a pool (its meta_json has no '{POOL_KEY}')"])
    issues: list[str] = []
    spec = meta[POOL_KEY]
    if not isinstance(spec, dict):
        raise PoolError([f"{name}: meta_json '{POOL_KEY}' must be an object with 'components'"])
    if unknown := sorted(set(spec) - {"components", "base"}):
        issues.append(f"{name}: meta_json '{POOL_KEY}' has unknown key(s) {', '.join(unknown)}; "
                      "it takes 'components' and 'base'")
    raw = spec.get("components")
    components: list[str] = []
    weights: list[int] = []
    if isinstance(raw, list) and all(isinstance(c, str) for c in raw):
        components = [c.strip() for c in raw]
    elif isinstance(raw, dict):
        for component, weight in raw.items():
            components.append(component.strip())
            try:
                if not isinstance(weight, str):
                    raise ValueError(f"write {weight!r} as a percent string, like \"40%\"")
                micros = units.percent_to_micros(weight)
                if micros <= 0:
                    raise ValueError("a weight must be above 0%")
            except ValueError as exc:
                issues.append(f"{name}: component {component}'s weight: {exc}")
                micros = 0
            weights.append(micros)
    else:
        issues.append(f"{name}: meta_json '{POOL_KEY}.components' must list the component "
                      "ensembles: an object of name and weight (\"e_a\": \"60%\"), or a list of "
                      "names for equal shares")
    if len(components) < 2:
        issues.append(f"{name}: a pool needs at least two components (has {len(components)})")
    if len(set(components)) != len(components):
        issues.append(f"{name}: a component is named twice")
    if name in components:
        issues.append(f"{name}: a pool can't be one of its own components")
    if components and not weights:
        weights = _equal_shares(len(components))
    elif weights and all(w > 0 for w in weights) and sum(weights) != _WHOLE:
        issues.append(f"{name}: component weights must add up to 100% "
                      f"(got {units.micros_to_percent(sum(weights))})")
    base = spec.get("base", components[0] if components else "")
    if not isinstance(base, str) or base not in components:
        issues.append(f"{name}: base {base!r} is not one of its components")
    size: int | None = None
    if "ensemble_size" in meta:
        try:
            size = units.json_ensemble_size(meta["ensemble_size"])
            if size == 0:
                raise ValueError("a pool's ensemble_size must be positive")
        except ValueError as exc:
            issues.append(f"{name}: {exc}")
    issues += _own_row_issues(scenarios_dir, name, meta)
    if issues:
        raise PoolError(issues)
    return Pool(name, tuple(components), tuple(weights), str(base), size, meta.get("reserve"))


def _equal_shares(n: int) -> list[int]:
    """``n`` shares adding up to exactly 100%, the remainder to the first."""
    share, extra = divmod(_WHOLE, n)
    return [share + (1 if i < extra else 0) for i in range(n)]


def _own_row_issues(scenarios_dir: Path, name: str, meta: Mapping[str, Any]) -> list[str]:
    """What a pool's own folder mustn't have: rows to inherit or files that
    nothing would read."""
    from analysis.sim.scenario import _scenario_row

    folder = scenario_dir(scenarios_dir, name)
    row = _scenario_row(folder)
    issues = []
    if read_parents(folder):
        issues.append(f"{name}: a pool has no parent_scenario; its members come from its components")
    for column in ("start", "end", "seed"):
        if (row.get(column) or "").strip():
            issues.append(f"{name}: a pool takes its {column} from its components; leave it blank")
    if unknown := sorted(set(meta) - {POOL_KEY, "ensemble_size", "reserve"}):
        issues.append(f"{name}: a pool's meta_json takes only {POOL_KEY}, ensemble_size and "
                      f"reserve (has {', '.join(unknown)})")
    if "reserve" in meta:
        try:
            units.json_dollars_to_cents(meta["reserve"])
        except ValueError as exc:
            issues.append(f"{name}: reserve: {exc}")
    extra = sorted(p.name for p in folder.iterdir()
                   if not p.name.startswith(".") and p.name not in _POOL_FILES) if folder.is_dir() else []
    if extra:
        issues.append(f"{name}: a pool holds only scenario.csv, README.md and guard_rails.csv; "
                      "nothing reads "
                      f"{', '.join(extra)}")
    return issues


def pool_issues(data_dir: Path, name: str) -> list[str]:
    """Everything wrong with pool ``name``: its definition, then each
    component (an ordinary Monte Carlo ensemble, not a pool or a mix-in),
    their spans (one start and one end month for all, so their months line
    up), and the pool's size against theirs."""
    from analysis.sim.scenario import ensemble_settings, scenario_span

    scenarios = Path(data_dir) / "scenarios"
    try:
        pool = read_pool(scenarios, name)
    except PoolError as exc:
        return exc.issues
    issues: list[str] = []
    spans: dict[str, tuple[str | None, str | None]] = {}
    sizes: dict[str, int] = {}
    for component in pool.components:
        if not scenario_dir(scenarios, component).is_dir():
            issues.append(f"{name}: component {component} not found")
            continue
        if is_pool(scenarios, component):
            issues.append(f"{name}: component {component} is a pool; a pool's components are "
                          "ordinary ensembles (list its components instead)")
            continue
        if is_mixin_only(scenarios, component):
            issues.append(f"{name}: component {component} is mix-in only, so it doesn't run")
            continue
        try:
            lineage_order(scenarios, component, strict=True)
        except ValueError as exc:
            issues.append(f"{name}: component {component}: {exc}")
            continue
        seed, size = ensemble_settings(data_dir, component)
        if size is None:
            issues.append(f"{name}: component {component} is not an ensemble (its ensemble_size is 0)")
            continue
        if seed is None:
            issues.append(f"{name}: component {component} has no seed, so its members can't be drawn")
        sizes[component] = size.value
        start, end = scenario_span(data_dir, component)
        spans[component] = (start.month if start else None, end.month if end else None)
    if len(set(spans.values())) > 1:
        listed = "; ".join(f"{c} {s or '?'} to {e or '?'}" for c, (s, e) in spans.items())
        issues.append(f"{name}: its components must share one start and end month ({listed})")
    if sizes and pool.size is not None and pool.size > min(sizes.values()):
        small = [c for c, n in sizes.items() if n < pool.size]
        issues.append(
            f"{name}: ensemble_size {pool.size} is more than {', '.join(small)} "
            f"{'has' if len(small) == 1 else 'have'} ({', '.join(str(sizes[c]) for c in small)}); "
            "pool member j is member j of one component, so every component needs at least "
            "the pool's size")
    return issues + _rail_issues(data_dir, name)


def _by_name(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Lineage rows (root first) with each named row's last version only,
    as the loader merges them, so a tag an override dropped isn't known.
    Unnamed rows (unnamed calendar rows) are kept; no rail names them."""
    named: dict[str, dict[str, str]] = {}
    unnamed = []
    for row in rows:
        if key := (row.get("name") or "").strip():
            named.pop(key, None)
            named[key] = row
        else:
            unnamed.append(row)
    return [*unnamed, *named.values()]


def readme_pools_line(components: Mapping[str, str | None]) -> str:
    """The README line naming a pool's components and shares."""
    shares = ", ".join(f"`{c}` {w or 'equal share'}" for c, w in components.items())
    return f"{README_POOLS_PREFIX}{shares} (ADR-009)."


README_POOLS_PREFIX = "Pools: "


def refresh_readme_pools_line(folder: Path, components: Mapping[str, str | None]) -> bool:
    """Rewrite the ``Pools:`` line of the pool's README.md to name
    ``components``; whether it changed."""
    readme = Path(folder) / "README.md"
    if not readme.is_file():
        return False
    text = readme.read_text(encoding="utf-8")
    line = readme_pools_line(components)
    lines = [line if each.startswith(README_POOLS_PREFIX) else each for each in text.split("\n")]
    if "\n".join(lines) == text:
        return False
    readme.write_text("\n".join(lines), encoding="utf-8")
    return True


def _rail_issues(data_dir: Path, name: str) -> list[str]:
    """The pool's guard rails (its own over its base component's), parsed,
    and what they name checked against the base component's lineage: the
    one the reports judge by its member 0 (ADR-009 §6)."""
    from analysis.sim import guard_rails, names

    rows, issues = guard_rails.read_rows(Path(data_dir) / "scenarios", name)
    rails, more = guard_rails.parse_rows(rows)
    issues += more
    if rails:
        refs = guard_rails.references_from_rows(*(
            _by_name(names.lineage_rows(Path(data_dir), name, f))
            for f in ("accounts.csv", "scheduled.csv", "calendar.csv")))
        issues += guard_rails.check_references(rails, refs)
    return issues


def pool_size(data_dir: Path, pool: Pool) -> int:
    """The pool's member count: its own ``ensemble_size``, else its
    smallest component's (all components need at least the pool's size)."""
    from analysis.sim.scenario import ensemble_settings

    if pool.size is not None:
        return pool.size
    sizes = [size.value for c in pool.components
             if (size := ensemble_settings(data_dir, c)[1]) is not None]
    return min(sizes) if sizes else 0


def assign_members(weights: Sequence[int], size: int, base: int) -> list[int]:
    """Which component each of ``size`` members comes from, by index into
    ``weights``: member 0 from ``base``, each later member from the
    component furthest behind its share of the members so far, ties to the
    earlier component. Every component's count stays within one member of
    its share at every point, so a run of consecutive members is close to
    the weights too."""
    total = sum(weights)
    counts = [0] * len(weights)
    out = []
    for j in range(size):
        if j == 0:
            c = base
        else:
            # Integer deficits: (share of j+1 members) - count, scaled by total.
            c = max(range(len(weights)), key=lambda k: (weights[k] * (j + 1) - counts[k] * total, -k))
        counts[c] += 1
        out.append(c)
    return out


def _link(source: Path, target: Path) -> None:
    """Hard-link ``source`` to ``target``, copying where links can't go (another
    filesystem, or one without them). A finished member file never changes,
    so the two are the same either way, and a link costs no space and
    outlives retention pruning the component's batch."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _component_identity(batch: Path) -> tuple[int | None, str | None]:
    """A component batch's master seed and recipe ``scenario_tree_id``, from
    any member file in it."""
    for member in sorted(batch.glob("member_*.sqlite3")):
        try:
            con = sqlite3.connect(f"{member.resolve().as_uri()}?mode=ro", uri=True)
            try:
                seed = con.execute("SELECT seed FROM scenario").fetchone()
                tree = con.execute("SELECT recipe_tree_id FROM ensemble_manifest").fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        return (None if seed is None else seed[0]), (None if tree is None else tree[0])
    return None, None


def build_pool_batch(
    pool: Pool, size: int, sources: Mapping[str, Path], runs_batch: Path,
    intent_batch: Path, intent_dir: Path, *, data_tree_id: str | None = None,
) -> list[tuple[int, Path, str]]:
    """Fill the pool's reserved batch folders from its components' batches
    (``sources``, component -> its latest Monte Carlo batch under the runs
    folder): each member's run file and resolved intent, linked, and a
    batch manifest with the pool's record. Returns the failed members as
    ``(index, intent path, error)``, as a component batch reports them."""
    from analysis.sim.ensemble import read_batch_manifest, write_batch_manifest

    choice = assign_members(pool.weights, size, pool.components.index(pool.base))
    manifests = {c: read_batch_manifest(b) or {} for c, b in sources.items()}
    short = []
    for k, component in enumerate(pool.components):
        needed = max((j for j, c in enumerate(choice) if c == k), default=-1) + 1
        have = manifests[component].get("member_count") or len(list(sources[component].glob("member_*.sqlite3")))
        if have < needed:
            short.append(f"{component}'s latest batch has {have} members; the pool needs {needed}")
    if short:
        raise PoolError([f"{pool.name}: {s}" for s in short])
    failures: list[tuple[int, Path, str]] = []
    errors = {c: {f["index"]: f.get("error", "") for f in m.get("failures", [])}
              for c, m in manifests.items()}
    for j, k in enumerate(choice):
        component = pool.components[k]
        stem = f"member_{j:04d}.sqlite3"
        source_intent = intent_dir / component / sources[component].name / stem
        if source_intent.is_file():
            _link(source_intent, intent_batch / stem)
        source_run = sources[component] / stem
        if source_run.is_file():
            # A member that failed mid-run leaves its run file too (status
            # failed); it's linked like any other, but still a failure.
            _link(source_run, runs_batch / stem)
        if j in errors[component]:
            failures.append((j, intent_batch / stem, errors[component][j]))
        elif not source_run.is_file():
            failures.append((j, intent_batch / stem, f"{component} has no run file for member {j}"))
    record: dict[str, Any] = {
        **pool.definition(), "built_size": size,
        "data_tree_id": data_tree_id,
        "sources": [],
        "member_components": [pool.components[k] for k in choice],
    }
    for k, component in enumerate(pool.components):
        seed, tree_id = _component_identity(sources[component])
        record["sources"].append({
            "component": component, "weight": pool.weight_text(component),
            "batch": sources[component].name, "seed": seed, "scenario_tree_id": tree_id,
            "members": choice.count(k),
        })
    write_batch_manifest(runs_batch, member_count=size, failures=failures, pool=record)
    return failures


def pool_record(batch_dir: Path) -> dict[str, Any] | None:
    """A pool batch's record from its manifest, or ``None`` for an ordinary batch."""
    path = Path(batch_dir) / BATCH_MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text()).get(POOL_KEY)
    except (OSError, json.JSONDecodeError):
        return None


def member_components(batch_dir: Path) -> dict[int, str] | None:
    """Pool batch member index -> the component it came from; ``None`` for
    an ordinary batch."""
    record = pool_record(batch_dir)
    if record is None:
        return None
    return dict(enumerate(record.get("member_components", [])))


def shared_seed(record: Mapping[str, Any]) -> int | None:
    """The seed every component of a pool batch drew with, or ``None`` when
    they differ (then the pool pairs with nothing, ADR-006 §8)."""
    seeds = {source.get("seed") for source in record.get("sources", [])}
    return seeds.pop() if len(seeds) == 1 else None


def pools_naming(scenarios_dir: Path, name: str) -> list[str]:
    """The pools that list ``name`` as a component, sorted."""
    from analysis.sim.loader import list_scenario_names

    out = []
    for other in list_scenario_names(Path(scenarios_dir).parent, Path(scenarios_dir)):
        if other == name or not is_pool(scenarios_dir, other):
            continue
        spec = read_meta(scenario_dir(scenarios_dir, other)).get(POOL_KEY)
        components = spec.get("components") if isinstance(spec, dict) else None
        if isinstance(components, (dict, list)) and name in components:
            out.append(other)
    return sorted(out)


def latest_pool_sources(runs_dir: Path, pool: Pool) -> dict[str, Path | None]:
    """Each component's latest Monte Carlo batch, ``None`` where it has none."""
    return {c: latest_ensemble_batch(Path(runs_dir) / c, mode="monte_carlo", wait=False)
            for c in pool.components}
