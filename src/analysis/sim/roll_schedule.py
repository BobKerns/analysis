"""#564: the background builder rolls scenarios forward at night.

A run reads prices only up to its start (``provenance``: a price file counts
up to the scenario's start, the end of its first month), so once that month
is over, new prices change nothing until the start is rolled forward
(``sim scenario roll``, #502). A scenario whose ``meta_json`` says
``"roll": "monthly"`` is rolled forward by the builder, to the current month,
on the first night after its start month ends. The key is inherited like any
other, so marking ``base`` marks every what-if built on it; one opts out with
``"roll": "off"``. Bob's choices:

- **At night**, in the builder's window (``config.json`` ``roll_window``,
  default 01:00-05:00, the machine's local time), so the full rebuild a roll
  sets off stays out of working hours; and after the day's prices have
  merged, so it values the lots at the latest close.
- **Parents first**: a scenario built on another one rolling the same night
  rolls after it, with ``--own``: only its own lots and rows (and its start,
  if it sets one), so it keeps inheriting the rest, freshly rolled. When
  its parent's roll fails, one that inherits its start waits for the next.
- **Unattended**: ``sim scenario roll NAME MONTH --yes --no-keep``. Each lot
  opens with what the old run projected (shares, for a lot valued by them,
  so the new prices set its value), and the months the run no longer covers
  become projected year-to-date rows, so their income stays in its tax year.
  A roll that would need a question (a value with no default to take) is
  skipped, and said in ``sim builder status``; so is one that fails.
- **A tag, not a copy**: a scenario kept as a sibling each month would bury
  the list. When a roll moves a start into a new year, the commit before it
  is tagged ``plan-<old year>``, so last year's plan can be brought back from
  it (``git show plan-2026:...``). A scenario rolled into the same new year
  later reuses the tag when it still holds that scenario's plan as it was
  (its lineage's folders unchanged since), else gets ``plan-<year>-2`` and
  so on: a tag is never moved, so none loses a plan it held.
- **On its own branch**, as the prices are (#560): the roll works in a
  worktree of the builder's own (``state/builder/roll``, at the data repo's
  ``main``), commits on ``builder/roll``, and the daemon merges that into
  ``main`` between builds, as a merge commit, when ``main`` is checked out
  and clean, then pushes ``main`` and the tags. If ``main`` changed a rolled
  scenario's lineage meanwhile, the roll is dropped and done again from the
  new ``main`` (a scenario's CSVs can't take "``main``'s side wins" as the
  price files can). The merge pokes the builder through the data repo's
  ``post-merge`` hook, so the rolled scenarios, and what is built on them,
  are built again.
- Never under ``ANALYSIS_ROLL=off`` (the tests set it) or ``sim builder
  install --no-roll``, nor without a builder config.

The state shared by the data repo's worktrees is in git's common directory:
``analysis-roll.json`` and its locks, beside the prices' (#560).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time as clock
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from analysis.sim import price_schedule, registries
from analysis.sim.lineage import is_pool, is_template_name, load_order, read_meta, scenario_dir, scenario_start
from analysis.sim.loader import DISABLE_FILE
from analysis.sim.locks import BUILDER_SWITCH_ENV

__all__ = [
    "DEFAULT_WINDOW", "ROLL_BRANCH", "SWITCH_ENV", "RollOutcome", "RollScheduleError",
    "describe", "due", "due_scenarios", "enabled", "marked", "merge_pending", "parse_window",
    "read_state", "roll", "switched_off", "window_of",
]

ROLL_BRANCH = "builder/roll"
_ROLL_REF = f"refs/heads/{ROLL_BRANCH}"
_MAIN_REF = f"refs/heads/{price_schedule.DATA_BRANCH}"
SWITCH_ENV = "ANALYSIS_ROLL"
DEFAULT_WINDOW = "01:00-05:00"
STATE_FILE = "analysis-roll.json"
LOCK_FILE = "analysis-roll.lock"
STATE_LOCK_FILE = "analysis-roll-state.lock"
TAG_PREFIX = "plan-"
# What `sim scenario roll --yes` says when a question has no default to take.
_NEEDS_YOU = "no default to take with --yes"
# How long one scenario's roll may take: it loads the scenario and every
# scenario built on it to check the edit.
ROLL_SECONDS = 1800


class RollScheduleError(Exception):
    """A scheduled roll can't go ahead; the message says why."""


def switched_off() -> bool:
    """``ANALYSIS_ROLL=off`` (or 0/no/false): no roll."""
    return os.environ.get(SWITCH_ENV, "").strip().lower() in {"off", "0", "no", "false"}


def enabled(config: dict[str, Any] | None) -> bool:
    """Whether a workspace with builder ``config`` rolls scenarios."""
    return config is not None and bool(config.get("roll", True)) and not switched_off()


# -- the window -------------------------------------------------------------------


_WINDOW = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")


def parse_window(text: str) -> tuple[time, time]:
    """``HH:MM-HH:MM`` as its two ends; the second may be past midnight
    (``23:00-05:00``). Raises ValueError for anything else."""
    found = _WINDOW.fullmatch(text.strip())
    if found is None or int(found[1]) > 23 or int(found[3]) > 23 \
            or int(found[2]) > 59 or int(found[4]) > 59:
        raise ValueError(f"not a window: {text!r} (HH:MM-HH:MM)")
    start, end = time(int(found[1]), int(found[2])), time(int(found[3]), int(found[4]))
    if start == end:
        raise ValueError(f"an empty window: {text!r}")
    return start, end


def window_of(config: dict[str, Any] | None) -> tuple[time, time]:
    """The configured window, or the default's when it's missing or bad."""
    try:
        return parse_window(str((config or {}).get("roll_window") or DEFAULT_WINDOW))
    except ValueError:
        return parse_window(DEFAULT_WINDOW)


def night_of(now: datetime, window: tuple[time, time]) -> date | None:
    """The day the window ``now`` (in local time) falls in opened, or None
    when ``now`` is outside it."""
    local = now.astimezone()
    start, end = window
    at = local.time()
    if start < end:
        return local.date() if start <= at < end else None
    # Past midnight: the evening part opened today, the morning part yesterday.
    if at >= start:
        return local.date()
    if at < end:
        return local.date() - timedelta(days=1)
    return None


# -- which scenarios ---------------------------------------------------------------


def roll_mark(scenarios: Path, name: str) -> object:
    """``name``'s ``roll`` as its load merges ``meta_json``: the last one
    set along its load order (so ``base``'s covers what is built on it)."""
    mark = None
    for one in load_order(scenarios, name):
        meta = read_meta(scenario_dir(scenarios, one))
        if registries.ROLL_KEY in meta:
            mark = meta[registries.ROLL_KEY]
    return mark


def marked(data_dir: Path) -> list[str]:
    """The scenarios whose ``meta_json``, as their load merges it, says
    ``"roll": "monthly"``, parents before what is built on them (so a
    child's roll reads its parent's new start), then by name. A pool or
    template never rolls."""
    scenarios = data_dir / "scenarios"
    if not scenarios.is_dir():
        return []
    depth: dict[str, int] = {}
    for folder in scenarios.iterdir():
        name = folder.name
        if not folder.is_dir() or is_template_name(name) or is_pool(scenarios, name):
            continue
        try:
            if roll_mark(scenarios, name) == "monthly":
                depth[name] = len(load_order(scenarios, name))
        except (OSError, ValueError):
            continue
    return sorted(depth, key=lambda name: (depth[name], name))


def due_scenarios(data_dir: Path, today: date) -> list[tuple[str, str]]:
    """``(name, start)`` of each marked scenario whose start month is over
    by ``today``, a disabled one left out (no run is kept current for it to
    project from)."""
    scenarios = data_dir / "scenarios"
    this_month = f"{today:%Y-%m}"
    found = []
    for name in marked(data_dir):
        if (scenario_dir(scenarios, name) / DISABLE_FILE).exists():
            continue
        start = scenario_start(scenarios, name)
        if start and start[:7] < this_month:
            found.append((name, start[:7]))
    return found


# -- shared state --------------------------------------------------------------------


def _state_path(top: Path) -> Path:
    return price_schedule._common_dir(top) / STATE_FILE


def read_state(data_dir: Path) -> dict[str, Any]:
    """The last roll and merge, as every worktree of the data repo sees
    them; {} before the first, or outside a git repository."""
    try:
        return json.loads(_state_path(price_schedule._top(data_dir)).read_text())
    except (OSError, ValueError, price_schedule.PriceScheduleError):
        return {}


def _update_state(top: Path, **changes: Any) -> dict[str, Any]:
    """Change some of the state's keys under its own lock, as the price
    state is changed (``price_schedule._update_state``)."""
    path = _state_path(top)
    fd = os.open(path.with_name(STATE_LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        state.update(changes)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f"{STATE_FILE}.",
                                         suffix=".tmp", delete=False) as tmp:
            tmp.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(tmp.name, path)
        return state
    finally:
        os.close(fd)


@contextlib.contextmanager
def _locked(top: Path) -> Generator[bool]:
    """One roll at a time per data repo: True when this one may go ahead."""
    fd = os.open(price_schedule._common_dir(top) / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def due(data_dir: Path, config: dict[str, Any] | None, now: datetime) -> bool:
    """Whether a roll is due: rolls are on, ``now`` is in the window, none
    was tried yet this night, the day's prices aren't still to come or to
    merge, and some marked scenario's start month is over."""
    if not enabled(config):
        return False
    night = night_of(now, window_of(config))
    if night is None:
        return False
    if read_state(data_dir).get("night") == night.isoformat():
        return False
    if price_schedule.enabled(config):
        prices = price_schedule.read_state(data_dir)
        if prices.get("pending") or price_schedule.due(prices, now):
            return False  # the roll should see the latest close
    return bool(due_scenarios(data_dir, now.astimezone().date()))


# -- rolling -------------------------------------------------------------------------


@dataclass
class RollOutcome:
    """What a scheduled roll did, a line each for the builder's log."""

    lines: list[str]
    committed: str | None = None
    rolled: dict[str, str] = field(default_factory=dict)  # name: "old -> new"


def _git(where: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return price_schedule._git(where, *args, env=price_schedule.IDENTITY, check=check)
    except price_schedule.PriceScheduleError as exc:
        raise RollScheduleError(str(exc)) from None


def _prepare(top: Path, worktree: Path, data_dir: Path, rel: str) -> Path:
    """The roll's worktree at ``main``, nothing left over, with the data
    repo's runs (which hold what each scenario's run projected) linked in
    where the worktree's own would be; its data dir."""
    if not (worktree / ".git").exists():
        _git(top, "worktree", "prune")
        if worktree.exists():
            shutil.rmtree(worktree)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(top, "worktree", "add", "--detach", str(worktree), price_schedule.DATA_BRANCH,
             check=True)
    _git(worktree, "reset", "--hard", "-q", _MAIN_REF, check=True)
    # Not -x: the linked runs (state/ is ignored) stay.
    _git(worktree, "clean", "-fdq", check=True)
    data = worktree / rel if rel not in ("", ".") else worktree
    from analysis.sim.loader import DEFAULT_RUNS_DIR, DEFAULT_STATE_DIR, find_state_dir

    runs = find_state_dir(data_dir, DEFAULT_STATE_DIR) / DEFAULT_RUNS_DIR
    linked = find_state_dir(data, DEFAULT_STATE_DIR) / DEFAULT_RUNS_DIR
    if not linked.is_symlink():
        if linked.is_dir():
            shutil.rmtree(linked)
        linked.parent.mkdir(parents=True, exist_ok=True)
        runs.mkdir(parents=True, exist_ok=True)
        linked.symlink_to(runs, target_is_directory=True)
    return data


def _sim() -> list[str]:
    return [sys.executable, "-m", "analysis", "sim"]


def _roll_one(data: Path, name: str, month: str, *, own: bool = False) -> tuple[int, list[str]]:
    """``sim scenario roll`` in the worktree: its exit status and output."""
    # Not waiting on the builder: the daemon may be this roll's parent.
    env = {**os.environ, BUILDER_SWITCH_ENV: "off", "ANALYSIS_DATA_DIR": str(data)}
    try:
        done = subprocess.run(
            [*_sim(), "scenario", "roll", name, month, "--yes", "--no-keep",
             *(["--own"] if own else []), "--data-dir", str(data)],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
            timeout=ROLL_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired:
        return 1, [f"Error: no answer in {ROLL_SECONDS}s"]
    lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    return done.returncode, lines


def _why(lines: Sequence[str]) -> str:
    """The line that says why a roll stopped: its ``Error:``, else its last."""
    errors = [line.removeprefix("Error:").strip() for line in lines if line.startswith("Error:")]
    found = [line.strip() for line in lines if line.strip().startswith("error:")]
    return "; ".join([*found[:3], *errors]) or (lines[-1].strip() if lines else "it failed")


def _lineage_names(data: Path, name: str) -> list[str]:
    """``name``'s load order, root first; just ``name`` when it can't be read."""
    try:
        return load_order(data / "scenarios", name)
    except (OSError, ValueError):
        return [name]


def _lineage_paths(data: Path, rel: str, name: str) -> list[str]:
    """The folders a scenario's plan is read from, as repository paths."""
    prefix = f"{rel}/" if rel not in ("", ".") else ""
    return [f"{prefix}scenarios/{one}" for one in _lineage_names(data, name)]


def _tag_for(top: Path, base: str, year: int, paths: Sequence[str]) -> tuple[str, bool]:
    """The tag that keeps the plan before a roll out of ``year``: an
    existing ``plan-<year>`` (or ``-2``...) that already holds ``paths`` as
    ``base`` has them, else the first free name. True when it is new."""
    count = 1
    while True:
        name = f"{TAG_PREFIX}{year}" + (f"-{count}" if count > 1 else "")
        if price_schedule._ref(top, f"refs/tags/{name}") is None:
            return name, True
        if _git(top, "diff", "--quiet", f"refs/tags/{name}", base, "--", *paths).returncode == 0:
            return name, False
        count += 1


def roll(data_dir: Path, worktree: Path, config: dict[str, Any] | None = None, *,
         now: datetime | None = None) -> RollOutcome:
    """Roll each due scenario forward to this month, in ``worktree``, and
    commit what rolled on ``builder/roll``, the plan before it to be tagged
    (once merged) when a start moves into a new year. Merging is the daemon's, between builds
    (:func:`merge_pending`). Each scenario's outcome goes in the state, for
    ``sim builder status``."""
    now = now or datetime.now(UTC)
    top = price_schedule._top(data_dir)
    rel = str(data_dir.resolve().relative_to(top.resolve()))
    month = f"{now.astimezone():%Y-%m}"
    # The night it counts for: the window's, or by hand outside it, today.
    night = night_of(now, window_of(config)) or now.astimezone().date()
    with _locked(top) as mine:
        if not mine:
            return RollOutcome(["another roll of this data repo is running"])
        if price_schedule._ref(top, _MAIN_REF) is None:
            raise RollScheduleError(f"the data repo has no {price_schedule.DATA_BRANCH} branch")
        state = _update_state(top, attempted=now.timestamp(), night=night.isoformat())
        data = _prepare(top, worktree, data_dir, rel)
        base = _git(worktree, "rev-parse", "HEAD", check=True).stdout.strip()
        scenarios: dict[str, Any] = dict(state.get("scenarios") or {})
        rolled: dict[str, str] = {}
        tags: dict[str, list[str]] = {}
        lines: list[str] = []
        due_now = due_scenarios(data, now.astimezone().date())
        if not due_now:
            lines.append("no scenario to roll")
        from analysis.sim.roll import PARENT_FIRST  # pandas: only when rolling
        rolling = {name for name, _ in due_now}
        for name, old in due_now:
            lines.append(f"rolling {name} forward: {old} -> {month}")
            # Built on one rolling tonight: that one's roll covers the rest.
            parents = [one for one in _lineage_names(data, name)[:-1] if one in rolling]
            entry: dict[str, Any] = {"at": now.timestamp(), "from": old, "to": month}
            # Its own rows rolled on a parent's that didn't would open a
            # month the rest of its plan doesn't.
            if stuck := [one for one in parents if one not in rolled]:
                code, said = 1, [f"Error: {', '.join(stuck)} didn't roll; {PARENT_FIRST}"]
            else:
                code, said = _roll_one(data, name, month, own=bool(parents))
            if code == 0:
                _git(worktree, "add", "-A", "--", _lineage_paths(data, rel, name)[-1], check=True)
                rolled[name] = f"{old} -> {month}"
                entry["outcome"] = "rolled"
                lines.extend(f"  {line}" for line in said if line.startswith("  "))
                if int(month[:4]) > int(old[:4]):
                    tag, _ = _tag_for(top, base, int(old[:4]), _lineage_paths(data, rel, name))
                    tags.setdefault(tag, []).append(name)
                    entry["tag"] = tag
            else:
                # Back to what the earlier scenarios' rolls staged.
                _git(worktree, "checkout", "-q", "--", ".")
                _git(worktree, "clean", "-fdq")
                why = _why(said)
                entry["outcome"] = ("skipped" if _NEEDS_YOU in why or PARENT_FIRST in why
                                    else "failed")
                entry["why"] = why
                lines.append(f"  {entry['outcome']}: {why}")
            scenarios[name] = entry
        # Forget a scenario that is no longer marked.
        marked_now = set(marked(data))
        scenarios = {name: entry for name, entry in scenarios.items() if name in marked_now}
        committed = None
        if rolled:
            committed = _commit(top, worktree, month, rolled)
            lines.append(f"committed the roll on {ROLL_BRANCH} ({committed[:10]})")
        _update_state(
            top, scenarios=scenarios,
            pending=committed, pending_paths=sorted(
                {path for name in rolled for path in _lineage_paths(data, rel, name)}),
            # Tagged once merged: a roll dropped before then leaves no tag.
            pending_tags=[{"tag": tag, "base": base, "message":
                           f"The plan before {', '.join(names)} rolled into {month[:4]} (#564)"}
                          for tag, names in sorted(tags.items())] if committed else [],
            waiting=None,
            **({"succeeded": now.timestamp()} if committed else {}),
        )
    return RollOutcome(lines, committed, rolled)


def _commit(top: Path, worktree: Path, month: str, rolled: dict[str, str]) -> str:
    """Commit what the rolls staged on ``builder/roll``; the new commit."""
    names = ", ".join(sorted(rolled))
    body = "\n".join(f"{name}: {span}" for name, span in sorted(rolled.items()))
    _git(worktree, *price_schedule._QUIET, "commit", "-q", "-m", f"Roll {names} forward to {month}",
         "-m", body, "-m", "Rolled by the background builder (#564).", check=True)
    head = _git(worktree, "rev-parse", "HEAD", check=True).stdout.strip()
    _git(top, "update-ref", _ROLL_REF, head, check=True)
    return head


# -- merging ------------------------------------------------------------------------------


def merge_pending(data_dir: Path) -> list[str]:
    """Merge ``builder/roll`` into ``main`` when it holds a roll ``main``
    lacks and this worktree allows it (``main`` checked out, clean, no
    merge under way: ``price_schedule._why_not``), as a merge commit, then
    push ``main`` and the roll's tags. A roll whose scenarios ``main``
    changed since is dropped, to be done again from the new ``main`` while
    the night lasts. Returns lines for the log; why it waits goes in the
    state. Only the daemon calls this, between builds."""
    try:
        top = price_schedule._top(data_dir)
    except price_schedule.PriceScheduleError:
        return []
    state = read_state(data_dir)
    pending, main = price_schedule._ref(top, _ROLL_REF), price_schedule._ref(top, _MAIN_REF)
    if not state.get("pending") or pending is None or main is None \
            or price_schedule._is_ancestor(top, pending, main):
        if state.get("pending"):
            _update_state(top, pending=None, waiting=None)
        return []
    base = _git(top, "rev-parse", f"{pending}^", check=True).stdout.strip()
    paths = list(state.get("pending_paths") or [])
    if main != base and paths and _git(top, "diff", "--quiet", base, main, "--", *paths).returncode:
        # Edited since the roll: it was made from the plan as it was then.
        _git(top, "update-ref", _ROLL_REF, main)
        _update_state(top, pending=None, waiting=None, night=None)
        return [f"dropped the roll: {price_schedule.DATA_BRANCH} changed a rolled scenario since; "
                "rolling again from it"]
    why = price_schedule._why_not(top)
    if why is None:
        done = _git(top, "-c", "commit.gpgsign=false", "merge", "--no-ff", "-q",
                    "-m", f"Merge the roll from {ROLL_BRANCH}", _ROLL_REF)
        if done.returncode == 0:
            _update_state(top, pending=None, waiting=None, merged=clock.time())
            lines = [f"merged the roll into {price_schedule.DATA_BRANCH}"]
            tags = []
            for one in state.get("pending_tags") or []:
                if price_schedule._ref(top, f"refs/tags/{one['tag']}") is None:
                    _git(top, "tag", "-a", one["tag"], one["base"], "-m", one["message"], check=True)
                    lines.append(f"tagged the plan before the roll {one['tag']}")
                tags.append(one["tag"])
            return [*lines, price_schedule._push(top), *_push_tags(top, tags)]
        _git(top, "merge", "--abort")
        said = (done.stderr or done.stdout).strip().splitlines()
        why = said[0] if said else "git merge failed"
    _update_state(top, waiting=why)
    return []


def _push_tags(top: Path, tags: Sequence[str]) -> list[str]:
    """Push the roll's tags where ``main`` goes; a line each that failed."""
    remote = _git(top, "config", f"branch.{price_schedule.DATA_BRANCH}.remote").stdout.strip()
    if not tags or remote in ("", "."):
        return []
    try:
        done = subprocess.run(
            ["git", "-C", str(top), "push", "-q", remote, *(f"refs/tags/{t}" for t in tags)],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
            timeout=price_schedule.PUSH_SECONDS, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [f"push of {', '.join(tags)} failed: {exc}"]
    if done.returncode:
        said = [line for line in (done.stderr or done.stdout).splitlines() if line.strip()]
        return [f"push of {', '.join(tags)} failed: {said[0] if said else 'git push failed'}"]
    return [f"pushed {', '.join(tags)} to {remote}"]


# -- reporting ------------------------------------------------------------------------------


def describe(state: dict[str, Any], config: dict[str, Any] | None, data_dir: Path) -> list[str]:
    """``sim builder status``'s lines about rolls."""
    def when(stamp: float | None) -> str:
        if not stamp:
            return "never"
        return datetime.fromtimestamp(stamp, UTC).astimezone().strftime("%Y-%m-%d %H:%M")

    start, end = window_of(config)
    names = marked(data_dir)
    lines = [f"roll: {', '.join(names) if names else 'no scenario is marked to roll'} "
             f"(nightly {start:%H:%M}-{end:%H:%M}; `sim scenario edit NAME --roll monthly`)"]
    if state.get("pending"):
        lines.append(f"roll waiting to merge into {price_schedule.DATA_BRANCH}: "
                     f"{state.get('waiting') or 'soon'}")
    for name, entry in sorted((state.get("scenarios") or {}).items()):
        span = f"{entry.get('from')} -> {entry.get('to')}"
        if entry.get("outcome") == "rolled":
            tag = f", plan before it tagged {entry['tag']}" if entry.get("tag") else ""
            lines.append(f"roll: {name}: rolled {span} on {when(entry.get('at'))}{tag}")
        else:
            lines.append(f"roll {entry.get('outcome')}: {name} ({span}, {when(entry.get('at'))}): "
                         f"{entry.get('why')}; `sim scenario roll {name}` does it by hand")
    return lines
