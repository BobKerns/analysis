"""#640: the background builder keeps symbol research current.

A fund's asset-class weights drift (a target-date fund glides, a manager
changes the mix) and a tear sheet goes stale, but research changes only when
someone runs ``sim symbol research``. Once a workspace has the builder, it
researches one symbol a day itself (Bob, #640):

- **Which**: every ticker the data repo names (``securities.csv``,
  ``allocations.csv``, a price file, a lot), less the ones marked
  ``no_fetch``, CASH and CD. A symbol is due when its latest research is more
  than a month old, or it has none. Each day takes the due symbol whose
  research, or failed try, is oldest; with more symbols than days in a month
  the cycle simply takes longer.
- **Last researched** is the commit date of the symbol's newest bundle under
  ``research/<SYMBOL>/``, read from git (``main``, ``builder/research`` and
  ``HEAD``), not from a state file: a run by hand, which commits by default
  (:func:`commit_research`), counts exactly as a background one does.
- **When**: from :data:`START` (06:07 New York time) each day, once a day:
  late enough to miss a nightly network restart, early enough that the
  rebuild a change of weights sets off is done before the open. The hourly
  timer wakes at :07, so it starts on time.
- **What it writes**: the bundle, always (its commit date marks the check),
  and a fund's weights as a new dated set in ``allocations.csv`` (plus its
  name in ``securities.csv`` when new) only when some class is more than
  :data:`TOLERANCE_MICROS` (0.5 points) from the symbol's latest written set.
  Against the latest written set, not the previous research, so a fund
  drifting 0.2 points a month crosses the line after a few months. An
  unchanged fund writes nothing to ``allocations.csv``, so no run goes stale
  (``research/`` is outside the provenance scope).
- **A failed run** (or one that times out) keeps nothing: the failure is
  recorded and the symbol goes to the back of the line, so one that always
  fails can't block the rest.
- **On its own branch**, as the prices (#560) and the roll (#564) are: the
  research runs in a worktree of the builder's own (``state/builder/research``),
  commits on ``builder/research``, and the daemon merges that into ``main``
  between builds when ``main`` is checked out and clean, then pushes ``main``.
  If ``main`` changed meanwhile and won't merge into the branch (both added
  weights), the writes are redone from the committed bundles on the new
  ``main``: no second research run.
- Never under ``ANALYSIS_RESEARCH=off`` (the tests set it) or ``sim builder
  install --no-research``, nor without a builder config.

The state shared by the data repo's worktrees is in git's common directory:
``analysis-research.json`` and its locks, beside the prices' and the roll's.
"""

from __future__ import annotations

import contextlib
import functools
import os
import subprocess
import tempfile
import time as clock
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from analysis.sim import price_schedule

__all__ = [
    "RESEARCH_BRANCH", "START", "SWITCH_ENV", "TOLERANCE_MICROS", "CommitOutcome",
    "ResearchOutcome", "ResearchScheduleError", "Runner", "apply_weights", "candidates",
    "commit_research", "describe", "due", "due_symbols", "enabled", "last_researched",
    "merge_pending", "read_state", "research", "switched_off", "weights_changed",
]

RESEARCH_BRANCH = "builder/research"
_RESEARCH_REF = f"refs/heads/{RESEARCH_BRANCH}"
_MAIN_REF = f"refs/heads/{price_schedule.DATA_BRANCH}"
SWITCH_ENV = "ANALYSIS_RESEARCH"
# Bob: 06:07 New York, the hourly timer's :07 after a nightly network restart.
START = time(6, 7)
MARKET = price_schedule.MARKET
# Bob: within 0.5 percentage points on every class the weights are unchanged.
TOLERANCE_MICROS = 5_000
# How far back `sim builder status` totals the research's cost.
COST_DAYS = 30
STATE_FILE = "analysis-research.json"
LOCK_FILE = "analysis-research.lock"
STATE_LOCK_FILE = "analysis-research-state.lock"
# The data repo files a research may write beside its bundle.
WEIGHT_FILES = ("securities.csv", "allocations.csv")


class ResearchScheduleError(Exception):
    """Scheduled research can't go ahead; the message says why."""


def switched_off() -> bool:
    """``ANALYSIS_RESEARCH=off`` (or 0/no/false): no scheduled research."""
    return os.environ.get(SWITCH_ENV, "").strip().lower() in {"off", "0", "no", "false"}


def enabled(config: dict[str, Any] | None) -> bool:
    """Whether a workspace with builder ``config`` researches symbols."""
    return config is not None and bool(config.get("research", True)) and not switched_off()


def read_state(data_dir: Path) -> dict[str, Any]:
    return price_schedule.read_state_file(data_dir, STATE_FILE)


def _update_state(top: Path, **changes: Any) -> dict[str, Any]:
    return price_schedule.update_state_file(top, STATE_FILE, STATE_LOCK_FILE, **changes)


def _locked(top: Path) -> contextlib.AbstractContextManager[bool]:
    """One research at a time per data repo: True when this one may go ahead."""
    return price_schedule.locked_file(top, LOCK_FILE)


def _git(where: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return price_schedule._git(where, *args, env=price_schedule.IDENTITY, check=check)
    except price_schedule.PriceScheduleError as exc:
        raise ResearchScheduleError(str(exc)) from None


def _prefix(rel: str) -> str:
    return f"{rel}/" if rel not in ("", ".") else ""


# -- which symbol, and when -------------------------------------------------------


def day_of(now: datetime) -> date | None:
    """The New York date when ``now`` is at or after that day's :data:`START`,
    else None (the day's research isn't due yet)."""
    local = now.astimezone(MARKET)
    return local.date() if local.time() >= START else None


def candidates(data_dir: Path) -> list[str]:
    """The tickers the schedule researches: every one the data repo names,
    less those marked ``no_fetch``; CASH, CD and ``account:lot`` keys aren't
    tickers."""
    from analysis.sim import prices

    held = set(prices.prices_in_use(data_dir / "scenarios").symbols) \
        if (data_dir / "scenarios").is_dir() else set()
    return prices.fetchable(data_dir, held | set(prices.known_symbols(data_dir)))


def last_researched(data_dir: Path) -> dict[str, float]:
    """Symbol to the commit time of its newest research bundle, on ``main``,
    ``builder/research`` or ``HEAD``; a symbol never committed is absent."""
    try:
        top = price_schedule._top(data_dir)
    except price_schedule.PriceScheduleError:
        return {}
    rel = str(data_dir.resolve().relative_to(top.resolve()))
    folder = f"{_prefix(rel)}research/"
    refs = [ref for ref in (_MAIN_REF, _RESEARCH_REF, "HEAD") if price_schedule._ref(top, ref)]
    if not refs:
        return {}
    done = _git(top, "log", "--format=%x00%ct", "--name-only", *refs, "--", folder)
    found: dict[str, float] = {}
    stamp = 0.0
    for line in done.stdout.splitlines():
        if line.startswith("\0"):
            stamp = float(line[1:])
        elif line.startswith(folder):
            symbol = line[len(folder):].split("/", 1)[0]
            if symbol and stamp > found.get(symbol, 0.0):
                found[symbol] = stamp
    return found


def a_month_after(stamp: float) -> datetime:
    """The same day and time a month after ``stamp`` (New York), the day
    clamped to the month's last: when research done then is due again."""
    when = datetime.fromtimestamp(stamp, UTC).astimezone(MARKET)
    year, month = (when.year + 1, 1) if when.month == 12 else (when.year, when.month + 1)
    for day in (when.day, 30, 29, 28):
        with contextlib.suppress(ValueError):
            return when.replace(year=year, month=month, day=day)
    raise AssertionError("unreachable")  # pragma: no cover


def due_symbols(data_dir: Path, state: dict[str, Any], now: datetime) -> list[str]:
    """The symbols due, the one to research first leading: ordered by the
    later of its last research and its last failed try, a symbol never
    tried first, then by name."""
    researched = last_researched(data_dir)
    tried = {s: float(f.get("at") or 0) for s, f in (state.get("failing") or {}).items()}
    due = [s for s in candidates(data_dir)
           if s not in researched or a_month_after(researched[s]) <= now]
    return sorted(due, key=lambda s: (max(researched.get(s, 0.0), tried.get(s, 0.0)), s))


def due(data_dir: Path, config: dict[str, Any] | None, now: datetime) -> bool:
    """Whether a day's research is due: research is on, it is past today's
    :data:`START` in New York, none was tried yet today, and some symbol is
    due."""
    if not enabled(config):
        return False
    day = day_of(now)
    if day is None:
        return False
    state = read_state(data_dir)
    if day.isoformat() in (state.get("day"), state.get("quiet")):
        return False
    if due_symbols(data_dir, state, now):
        return True
    # Nothing due today: the usual state, once every symbol has been done in
    # the past month. Said in the state, so the daemon's minute and the
    # hourly tick don't scan the scenarios and git again until tomorrow (a
    # symbol added today waits for tomorrow's look).
    with contextlib.suppress(price_schedule.PriceScheduleError, OSError):
        _update_state(price_schedule._top(data_dir), quiet=day.isoformat())
    return False


# -- writing the weights ------------------------------------------------------------


def weights_changed(proposed: dict[str, str], latest: Iterable[tuple[str, str]] | None) -> bool:
    """Whether ``proposed`` is more than :data:`TOLERANCE_MICROS` from
    ``latest`` (a symbol's latest written set) on any class; a class missing
    from one side counts as 0. True when there is no set yet."""
    from analysis.sim import units

    if latest is None:
        return True
    old = {cls: units.percent_to_micros(w, require_sign=False) for cls, w in latest}
    new = {cls: units.percent_to_micros(w, require_sign=False) for cls, w in proposed.items()}
    return any(abs(new.get(cls, 0) - old.get(cls, 0)) > TOLERANCE_MICROS for cls in {*old, *new})


def older_than(as_of: str, latest: str) -> bool:
    """Whether a set dated ``as_of`` (YYYY-MM) would sit behind one dated
    ``latest``: a blank ``latest`` holds at every date and sorts first."""
    return bool(latest) and as_of[:7] < latest[:7]


@dataclass
class Applied:
    """What :func:`apply_weights` did: lines for the log, and whether it wrote."""

    lines: list[str]
    wrote: bool = False


def apply_weights(data_dir: Path, symbol: str, bundle: Path, *, today: date | None = None) -> Applied:
    """Write the weights in ``bundle`` as an unattended run does (#640): a
    new set dated the research's as-of (else this month) when they differ from
    ``symbol``'s latest written set by more than the tolerance, replacing a
    set already written for that month. The tear sheet's title fills a
    missing or blank name in ``securities.csv``, fund or company (Bob). A
    company's bundle, or one whose weights can't be read, writes no weights."""
    from analysis.sim import prices, research, symbols

    # Bob: a name the research finds fills a blank or missing one, fund or company.
    named = []
    if (title := research.tear_sheet_title(bundle, symbol, symbol)) \
            and prices.fill_names(data_dir, {symbol: title}):
        named = [f"{symbol}: named {title!r} in securities.csv"]
    try:
        allocation = research.read_allocation(bundle, symbol)
    except research.ResearchError as exc:
        return Applied([*named, f"{symbol}: weights not written: {exc}"])
    if allocation is None:
        return Applied([*named, f"{symbol}: no allocation export (a company's tear sheet); "
                        "no weights to write"])
    latest = symbols.latest_set(data_dir, symbol)
    if not weights_changed(allocation.weights, latest[1] if latest else None):
        assert latest is not None
        return Applied([*named, f"{symbol}: weights within 0.5 points of the set as of "
                        f"{latest[0] or 'every date'}; allocations.csv unchanged"])
    as_of = allocation.as_of or (today or date.today()).strftime("%Y-%m")
    if latest is not None and older_than(as_of, latest[0]):
        # A set behind the latest would never be the one used, and the next
        # run would find the same difference and write it again.
        return Applied([*named, f"{symbol}: the research is as of {as_of}, older than the set as of "
                        f"{latest[0]}; allocations.csv unchanged"])
    try:
        symbols.add_symbol(data_dir, symbol, weights=allocation.weights, as_of=as_of,
                           replace=symbols.has_weights(data_dir, symbol, as_of))
    except ValueError as exc:
        return Applied([*named, f"{symbol}: weights not written: {exc}"])
    lines = [*named, f"{symbol}: wrote weights as of {as_of}: "
             + ", ".join(f"{c} {w}" for c, w in allocation.weights.items())]
    lines.extend(f"{symbol}: warning: {w}" for w in allocation.warnings)
    return Applied(lines, wrote=True)


# -- committing ----------------------------------------------------------------------


@dataclass
class CommitOutcome:
    """What :func:`commit_research` did."""

    commit: str | None = None
    subject: str = ""
    # Files it left uncommitted (the person's own edits were in them).
    left: list[str] = field(default_factory=list)
    # Why nothing was committed, when that was not for lack of changes.
    why: str | None = None


def dirty_weight_files(data_dir: Path) -> set[str]:
    """Which of :data:`WEIGHT_FILES` have uncommitted changes now: a
    foreground run notes them before it writes, so the person's own edits
    don't ride along in its commit."""
    try:
        top = price_schedule._top(data_dir)
        rel = str(data_dir.resolve().relative_to(top.resolve()))
        paths = [f"{_prefix(rel)}{name}" for name in WEIGHT_FILES]
        done = _git(top, "status", "--porcelain", "--", *paths)
    except (price_schedule.PriceScheduleError, OSError):  # no git, or no repository
        return set()
    return {name for name, path in zip(WEIGHT_FILES, paths, strict=True)
            if any(line[3:] == path for line in done.stdout.splitlines())}


def commit_research(
    data_dir: Path, bundle: Path, symbol: str, *, dirty_before: Iterable[str] = (),
    command: str = "sim symbol research", builder: bool = False,
) -> CommitOutcome:
    """Commit ``bundle`` and whichever of ``securities.csv`` and
    ``allocations.csv`` the research changed, and nothing else: other staged
    or unstaged changes stay as they were (``git commit --only``). A weight
    file in ``dirty_before`` (:func:`dirty_weight_files` before the run) is
    left uncommitted. ``builder`` is the background job's commit, in its own
    worktree on a detached HEAD; a foreground commit refuses mid-merge,
    mid-rebase and on a detached HEAD, as ``sim commit`` does."""
    from analysis.sim import data_commit

    try:
        top = data_commit.repo_top(data_dir)
        if not builder:
            data_commit._refuse_unless_committable(top)
    except data_commit.CommitError as exc:
        return CommitOutcome(why=str(exc))
    except OSError as exc:  # no git to run
        return CommitOutcome(why=f"git can't be run: {exc}")
    rel = str(data_dir.resolve().relative_to(top.resolve()))
    bundle_path = bundle.resolve().relative_to(top.resolve()).as_posix()
    left = sorted(set(dirty_before) & set(WEIGHT_FILES))
    weights = [f"{_prefix(rel)}{name}" for name in WEIGHT_FILES if name not in left]
    changed = {line[3:] for line in _git(top, "status", "--porcelain", "--untracked-files=all", "--",
                                         bundle_path, *weights).stdout.splitlines()}
    wrote = [p for p in weights if p in changed]
    if not any(p == bundle_path or p.startswith(bundle_path + "/") for p in changed) and not wrote:
        return CommitOutcome(left=left)
    paths = [bundle_path, *wrote]
    subject = f"Research {symbol}" + (": new weights" if any(p.endswith("allocations.csv") for p in wrote)
                                      else "")
    why = ("Researched by the background builder (#640)." if builder
           else f"Committed by `{command}`, so the schedule counts it (#640).")
    text = "\n".join([subject, "", why, "", *(f"- {p}" for p in paths)]) + "\n"
    quiet = list(price_schedule._QUIET) if builder else ["-c", "commit.gpgsign=false"]
    env = price_schedule.git_environ(price_schedule.IDENTITY if builder else None)
    if not builder:
        data_commit.ensure_identity(top, *data_commit.operator(data_dir))

    def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "--literal-pathspecs", "-C", str(top), *args], input=stdin,
                              capture_output=True, text=True, check=False, env=env)

    added = run("add", "-A", "--", *paths)
    done = added if added.returncode else run(*quiet, "commit", "-q", "--only", "-F", "-", "--",
                                              *paths, stdin=text)
    if done.returncode:
        return CommitOutcome(left=left, why=(done.stderr or done.stdout).strip() or "git commit failed")
    head = run("rev-parse", "HEAD").stdout.strip()
    return CommitOutcome(head, subject, left)


# -- running -------------------------------------------------------------------------


@dataclass
class RunReport:
    """What a research run reports: whether it worked, why not, its cost."""

    ok: bool
    message: str = ""
    cost_usd: float | None = None
    seconds: float = 0.0


# (symbol, work dir, data dir, progress) -> report; the skill writes into
# the work dir. The tests give one that writes a bundle.
Runner = Callable[[str, Path, Path, Callable[[str], None]], RunReport]


def run_with_claude(symbol: str, work: Path, data: Path, progress: Callable[[str], None], *,
                    claude: str | None = None) -> RunReport:
    """Run the finance-research plugin on ``symbol``, as ``sim symbol
    research`` does, with the plugin rendered from ``data``'s research.yaml.
    ``claude`` is where ``sim builder install`` found Claude Code (#644)."""
    from analysis import research_plugin
    from analysis.sim import research

    with tempfile.TemporaryDirectory(prefix="research-plugin-") as tmp:
        try:
            config = research_plugin.read_config(research_plugin.config_path(data))
            plugin = research_plugin.render(config, Path(tmp) / "plugin")
            done = research.run_skill(symbol, symbol, work, plugin, claude=claude, progress=progress)
        except (research.ResearchError, research_plugin.ResearchConfigError) as exc:
            return RunReport(False, str(exc))
    return RunReport(done.ok, done.message, done.cost_usd, done.seconds)


@dataclass
class ResearchOutcome:
    """What a scheduled research did, a line each for the builder's log."""

    lines: list[str]
    symbol: str | None = None
    committed: str | None = None


def _prepare(top: Path, worktree: Path, rel: str) -> Path:
    """The research worktree at ``builder/research`` (or ``main`` when that
    holds nothing ``main`` lacks), caught up with ``main``, nothing left over;
    its data dir."""
    price_schedule.ensure_worktree(top, worktree)
    data = worktree / rel if rel not in ("", ".") else worktree
    pending = price_schedule._ref(top, _RESEARCH_REF)
    main = price_schedule._ref(top, _MAIN_REF)
    if pending is None or main is None or price_schedule._is_ancestor(top, pending, main):
        _git(worktree, "reset", "--hard", "-q", _MAIN_REF, check=True)
        _git(top, "update-ref", _RESEARCH_REF, _MAIN_REF, check=True)
    else:
        _catch_up(top, worktree, data, rel)
    _git(worktree, "clean", "-fdq", check=True)
    return data


def _catch_up(top: Path, worktree: Path, data: Path, rel: str) -> str | None:
    """``main`` merged into ``builder/research`` in the worktree; when the two
    conflict (both wrote weights), the branch starts over from ``main`` and
    each of its bundles' weights is written again on it (:func:`apply_weights`),
    so no research is lost or run again. The branch's tip after."""
    pending = price_schedule._ref(top, _RESEARCH_REF)
    _git(worktree, "reset", "--hard", "-q", _RESEARCH_REF, check=True)
    _git(worktree, "clean", "-fdq", check=True)
    if pending is None or price_schedule._is_ancestor(top, _MAIN_REF, pending):
        return pending
    merged = _git(worktree, *price_schedule._QUIET, "merge", "--no-ff", "-q",
                  "-m", f"Merge {price_schedule.DATA_BRANCH} into {RESEARCH_BRANCH}", _MAIN_REF)
    if merged.returncode == 0:
        head = _git(worktree, "rev-parse", "HEAD", check=True).stdout.strip()
        _git(top, "update-ref", _RESEARCH_REF, head, check=True)
        return head
    _git(worktree, "merge", "--abort")
    return _redo(top, worktree, data, rel, pending)


def _redo(top: Path, worktree: Path, data: Path, rel: str, pending: str) -> str | None:
    """``builder/research``'s bundles that ``main`` lacks, on ``main``, with
    their weights written again; None when there are none."""
    folder = f"{_prefix(rel)}research/"
    # Only the branch's own bundles, added since it left main (three dots):
    # one main has, from a run by hand, is main's to keep as it is.
    names = _git(top, "diff", "--name-only", "--diff-filter=A", f"{_MAIN_REF}...{pending}", "--",
                 folder).stdout.split()
    bundles = sorted({"/".join(n.split("/")[: folder.count("/") + 2]) for n in names})
    _git(worktree, "reset", "--hard", "-q", _MAIN_REF, check=True)
    if not bundles:
        _git(top, "update-ref", _RESEARCH_REF, _MAIN_REF, check=True)
        return None
    # The branch moves only once the redo is committed: until then, a
    # failure leaves its research where it was.
    _git(worktree, "checkout", "-q", pending, "--", *bundles, check=True)
    lines = []
    for path in bundles:
        symbol, _, folder_name = path[len(folder):].partition("/")
        try:
            researched = date.fromisoformat(folder_name[:10])  # YYYY-MM-DD, or with -2...
        except ValueError:
            researched = None
        lines.extend(apply_weights(data, symbol, worktree / path, today=researched).lines)
    _git(worktree, "add", "-A", "--", *bundles, *(f"{_prefix(rel)}{n}" for n in WEIGHT_FILES),
         check=True)
    _git(worktree, *price_schedule._QUIET, "commit", "-q", "-m",
         f"Research written again on {price_schedule.DATA_BRANCH}",
         "-m", "\n".join(lines) or "No weights changed.",
         "-m", f"{price_schedule.DATA_BRANCH} changed while the research waited to merge (#640).",
         check=True)
    head = _git(worktree, "rev-parse", "HEAD", check=True).stdout.strip()
    _git(top, "update-ref", _RESEARCH_REF, head, check=True)
    return head


def _settle(top: Path) -> str | None:
    """What waits to merge into ``main``: ``builder/research``'s tip, or None
    when it holds nothing ``main`` lacks."""
    pending = price_schedule._ref(top, _RESEARCH_REF)
    if pending is None or price_schedule._is_ancestor(top, pending, _MAIN_REF) \
            or price_schedule._same_files(top, _MAIN_REF, pending):
        return None
    return pending


def research(
    data_dir: Path, worktree: Path, *, now: datetime | None = None, symbol: str | None = None,
    runner: Runner | None = None, progress: Callable[[str], None] = print, claude: str | None = None,
) -> ResearchOutcome:
    """Research the first due symbol (or ``symbol``) in ``worktree`` and
    commit its bundle, and any weights it changed, on ``builder/research``.
    Merging is the daemon's, between builds (:func:`merge_pending`). The
    outcome, its cost and a failure go in the state, for ``sim builder
    status``. ``claude`` is where ``sim builder install`` found Claude Code."""
    from analysis.sim import research as research_mod

    now = now or datetime.now(UTC)
    top = price_schedule._top(data_dir)
    rel = str(data_dir.resolve().relative_to(top.resolve()))
    day = (day_of(now) or now.astimezone(MARKET).date()).isoformat()
    runner = runner or functools.partial(run_with_claude, claude=claude)
    with _locked(top) as mine:
        if not mine:
            return ResearchOutcome(["another research of this data repo is running"])
        if price_schedule._ref(top, _MAIN_REF) is None:
            raise ResearchScheduleError(f"the data repo has no {price_schedule.DATA_BRANCH} branch")
        # A symbol named by hand isn't the day's pick: the schedule's still runs.
        state = _update_state(top, attempted=now.timestamp(), **({"day": day} if symbol is None else {}))
        data = _prepare(top, worktree, rel)
        chosen = symbol or next(iter(due_symbols(data, state, now)), None)
        if chosen is None:
            _update_state(top, pending=_settle(top))
            return ResearchOutcome(["no symbol is due for research"])
        lines = [f"researching {chosen}"]
        failing: dict[str, Any] = dict(state.get("failing") or {})
        with tempfile.TemporaryDirectory(prefix=f"research-{chosen}-") as tmp:
            work = Path(tmp)
            try:
                report = runner(chosen, work, data, progress)
            except Exception as exc:  # noqa: BLE001 - any failure is the symbol's, recorded below
                report = RunReport(False, f"{type(exc).__name__}: {exc}")
            kept = [p for p in work.iterdir() if p.is_file() and p.suffix.lower() in research_mod.KEPT_SUFFIXES]
            costs = [c for c in state.get("costs") or [] if now.timestamp() - c[0] < COST_DAYS * 86400]
            if report.cost_usd is not None:
                costs.append([now.timestamp(), report.cost_usd])
            last = {"symbol": chosen, "at": now.timestamp(), "seconds": report.seconds,
                    "cost": report.cost_usd}
            if not report.ok or not kept:
                why = report.message if not report.ok else "the run wrote no files"
                first = (failing.get(chosen) or {}).get("since", now.timestamp())
                failing[chosen] = {"error": why, "at": now.timestamp(), "since": first}
                _update_state(top, failing=failing, costs=costs, pending=_settle(top),
                              last={**last, "outcome": "failed", "why": why})
                return ResearchOutcome([*lines, f"{chosen}: research failed: {why}"], chosen)
            failing.pop(chosen, None)
            bundle = research_mod.bundle_dir(data, chosen, now.astimezone(MARKET).date())
            research_mod.keep_bundle(work, bundle)
        with contextlib.suppress(research_mod.ResearchError, OSError):
            research_mod.write_page(bundle, chosen)
        applied = apply_weights(data, chosen, bundle, today=now.astimezone(MARKET).date())
        lines.extend(applied.lines)
        done = commit_research(data, bundle, chosen, builder=True)
        if done.commit is not None:
            _git(top, "update-ref", _RESEARCH_REF, done.commit, check=True)
            lines.append(f"committed {done.subject} on {RESEARCH_BRANCH} ({done.commit[:10]})")
        elif done.why:
            lines.append(f"not committed: {done.why}")
        _update_state(top, failing=failing, costs=costs, pending=_settle(top), waiting=None,
                      last={**last, "outcome": "weights" if applied.wrote else "researched"})
    return ResearchOutcome(lines, chosen, done.commit)


# -- merging ------------------------------------------------------------------------------


def merge_pending(data_dir: Path, worktree: Path) -> list[str]:
    """Merge ``builder/research`` into ``main`` when it holds research
    ``main`` lacks and this worktree allows it (``main`` checked out, clean,
    nothing under way: ``price_schedule._why_not``), as a merge commit, then
    push ``main``. If ``main`` moved, it is merged into the branch first, in
    ``worktree`` (:func:`_catch_up`). Returns lines for the log; why it waits
    goes in the state. Only the daemon calls this, between builds."""
    try:
        top = price_schedule._top(data_dir)
    except price_schedule.PriceScheduleError:
        return []
    pending = _settle(top)
    if pending is None:
        if read_state(data_dir).get("pending"):
            _update_state(top, pending=None, waiting=None)
        return []
    if not price_schedule._is_ancestor(top, _MAIN_REF, pending):
        with _locked(top) as mine:
            if not mine:
                return []  # a research is running, and it catches up first
            rel = str(data_dir.resolve().relative_to(top.resolve()))
            if not price_schedule.worktree_ok(top, worktree):
                _prepare(top, worktree, rel)
            else:
                data = worktree / rel if rel not in ("", ".") else worktree
                _catch_up(top, worktree, data, rel)
            pending = _settle(top)
            _update_state(top, pending=pending)
        if pending is None:
            return []
    why = price_schedule._why_not(top)
    if why is None:
        done = _git(top, "-c", "commit.gpgsign=false", "merge", "--no-ff", "-q",
                    "-m", f"Merge new research from {RESEARCH_BRANCH}", pending)
        if done.returncode == 0:
            _update_state(top, pending=None, waiting=None, merged=clock.time())
            return [f"merged new research into {price_schedule.DATA_BRANCH}", price_schedule._push(top)]
        _git(top, "merge", "--abort")
        said = (done.stderr or done.stdout).strip().splitlines()
        why = said[0] if said else "git merge failed"
    _update_state(top, waiting=why)
    return []


# -- reporting ------------------------------------------------------------------------------


def describe(state: dict[str, Any], data_dir: Path, now: datetime, *,
             claude: str | None = None) -> list[str]:
    """``sim builder status``'s lines about research; ``claude`` is where
    ``sim builder install`` found Claude Code."""
    from analysis.sim import research as research_mod

    def when(stamp: float | None) -> str:
        if not stamp:
            return "never"
        return datetime.fromtimestamp(stamp, UTC).astimezone().strftime("%Y-%m-%d %H:%M")

    def dollars(value: float | None) -> str:
        return f"${value:.2f}" if value is not None else "cost not reported"

    lines = []
    last = state.get("last") or {}
    if last:
        what = {"weights": "wrote new weights", "researched": "weights unchanged or none",
                "failed": f"failed: {last.get('why')}"}.get(str(last.get("outcome")), "")
        lines.append(f"research: last {last.get('symbol')} on {when(last.get('at'))} "
                     f"({what}; {dollars(last.get('cost'))})")
    else:
        lines.append("research: none yet")
    # As the hourly timer finds it, without this shell's PATH (#644).
    found = research_mod.find_claude(claude, path=False)
    lines.append(f"research runs: {found}" if found else
                 "research can't find claude outside your shell's PATH: run `sim builder install` "
                 "again from a shell where `claude` works")
    start = datetime.combine(now.astimezone(MARKET).date(), START, MARKET).astimezone()
    following = due_symbols(data_dir, state, now)
    if following:
        lines.append(f"research next: {following[0]} ({len(following)} due; daily from "
                     f"{start:%H:%M} local time)")
    else:
        lines.append("research next: nothing due")
    recent = [c for c in state.get("costs") or [] if now.timestamp() - c[0] < COST_DAYS * 86400]
    if recent:
        lines.append(f"research cost, last {COST_DAYS} days: ${sum(c[1] for c in recent):.2f} over "
                     f"{len(recent)} run(s) (what the API would charge; on a claude.ai plan it "
                     "counts against the plan's limits instead)")
    if state.get("pending"):
        lines.append(f"research waiting to merge into {price_schedule.DATA_BRANCH}: "
                     f"{state.get('waiting') or 'soon'}")
    for symbol, failed in sorted((state.get("failing") or {}).items()):
        days = int((now.timestamp() - failed.get("since", now.timestamp())) // 86400)
        since = f" for {days} days" if days >= 1 else ""
        lines.append(f"research failing{since}: {symbol}: {failed.get('error')}")
    return lines

