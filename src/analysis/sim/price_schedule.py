"""#560: the background builder fetches prices on a schedule.

Prices change only when someone runs ``sim prices fetch``, so a lot held by
shares is only as current as the last fetch. Once a workspace has the
builder (``sim builder install``), it fetches them itself, after each
weekday's US market close, without touching the user's checkout (Bob):

- The fetch works in a sparse worktree of the data repo, ``prices/`` in the
  builder's state folder, with a detached HEAD, holding the price files and
  the few files the symbol list comes from, on the ``builder/prices``
  branch. Each fetch begins by merging ``main`` into it (#588), always as a
  merge commit, and where the two conflict ``main``'s side wins: a fetch only
  adds rows, and the provider returns the history, so fetching again loses
  nothing. Only a merge git can't settle that way (a price file deleted on
  one side and changed on the other) starts the branch over from ``main``.
  When the fetch leaves the branch's files the same as ``main``'s, the branch
  goes back to ``main``: nothing waits to merge.
- ``builder/prices`` is merged into ``main`` only by the daemon, between
  builds (so no build reads a price file mid-merge), always as a merge commit
  (#588), and only when the data worktree has ``main`` checked out with no
  uncommitted change to a tracked file and no merge, rebase or cherry-pick
  under way. Otherwise it waits: the daemon tries again on every poke, so it
  lands soon after the user commits or switches back. If ``main`` moved
  meanwhile, it is merged into ``builder/prices`` again first, in the sparse
  worktree, so the merge into the user's checkout never conflicts; only if
  that merge fails are the prices dropped and fetched again. The merge runs the data repo's
  ``post-merge`` hook, which pokes the builder; a merge only changes runs
  whose start the new prices reach (``provenance``: a price file counts up to
  the scenario's start), so a daily fetch reruns nothing until a roll.
- After its merge the daemon pushes ``main`` when it is strictly ahead of
  its upstream (``origin/main``); a failed push is logged and otherwise
  ignored. When ``main`` isn't strictly ahead (it lacks some of the
  upstream's commits) it logs that as why it didn't push, and suggests
  ``git pull``.
- A fetch is due when the last good one came before the latest weekday
  17:30 in New York, after the close is final. Nothing keeps the builder
  running, so ``sim builder install`` adds an hourly OS timer (a launchd agent
  on macOS, a crontab line elsewhere) that runs ``sim builder tick``: a look
  at the state, and a fetch when one is due (and a roll, #564), which pokes
  the builder when it commits. The daemon also checks on starting, and on
  each rescan.
- A symbol that fails is recorded, with when it first failed, and doesn't
  stop the others. A fetch where every symbol failed (no network, say) is
  tried again an hour later, not counted as done.
- Never under ``ANALYSIS_PRICE_FETCH=off`` (the tests set it) or
  ``sim builder install --no-prices``, nor without a builder config: so
  never for ``--no-builder`` repos, CI or agents.

The state shared by the data repo's worktrees (one fetch at a time, and its
outcome) is in git's common directory: ``analysis-prices.json`` and its lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time as clock
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

__all__ = [
    "DATA_BRANCH", "PRICES_BRANCH", "SWITCH_ENV", "FetchOutcome", "PriceScheduleError",
    "describe", "due", "enabled", "fetch", "install_timer", "last_close", "merge_pending",
    "read_state", "stale_held", "switched_off", "uninstall_timer",
]

PRICES_BRANCH = "builder/prices"
# The data repo's branch the fetched prices go into.
DATA_BRANCH = "main"
_PRICES_REF = f"refs/heads/{PRICES_BRANCH}"
_MAIN_REF = f"refs/heads/{DATA_BRANCH}"
# Commits the builder makes in its own worktree: no hooks (it isn't a
# worktree the builder watches) and no signing (nobody is there to unlock a key).
_QUIET = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false")
SWITCH_ENV = "ANALYSIS_PRICE_FETCH"
MARKET = ZoneInfo("America/New_York")
# After the 16:00 close, once the provider's close is final.
AFTER_CLOSE = time(17, 30)
# How soon a failed fetch (or one made stale by main moving) is tried again.
RETRY_SECONDS = 3600.0
STATE_FILE = "analysis-prices.json"
LOCK_FILE = "analysis-prices.lock"
STATE_LOCK_FILE = "analysis-prices-state.lock"
IDENTITY = {"GIT_AUTHOR_NAME": "sim builder", "GIT_AUTHOR_EMAIL": "builder@analysis.local",
            "GIT_COMMITTER_NAME": "sim builder", "GIT_COMMITTER_EMAIL": "builder@analysis.local"}
TIMER_MARKER = "# analysis sim builder prices (#560)"
# Minutes past the hour the crontab line runs, away from the crowded :00.
CRON_MINUTE = 7


def switched_off() -> bool:
    """``ANALYSIS_PRICE_FETCH=off`` (or 0/no/false): no fetch, no timer."""
    return os.environ.get(SWITCH_ENV, "").strip().lower() in {"off", "0", "no", "false"}


def enabled(config: dict[str, Any] | None) -> bool:
    """Whether a workspace with builder ``config`` fetches prices."""
    return config is not None and bool(config.get("prices", True)) and not switched_off()


def last_close(now: datetime) -> datetime:
    """The latest weekday :data:`AFTER_CLOSE` in New York at or before ``now``.
    A holiday counts too: its fetch finds nothing new and commits nothing."""
    local = now.astimezone(MARKET)
    day = local.date()
    while True:
        moment = datetime.combine(day, AFTER_CLOSE, MARKET)
        if day.weekday() < 5 and moment <= local:
            return moment
        day -= timedelta(days=1)


def due(state: dict[str, Any], now: datetime) -> bool:
    """Whether a fetch is due, from :func:`read_state`'s record."""
    stamp = now.timestamp()
    attempted = state.get("attempted") or 0.0
    if stamp - attempted < RETRY_SECONDS and (
        state.get("refetch") or (state.get("succeeded") or 0.0) < attempted
    ):
        return False  # tried within the hour; it failed, or main moved
    if state.get("refetch"):
        return True
    return (state.get("succeeded") or 0.0) < last_close(now).timestamp()


# -- git ------------------------------------------------------------------------


def _git(where: Path, *args: str, env: dict[str, str] | None = None,
         check: bool = False) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        ["git", "-C", str(where), *args], capture_output=True, text=True, check=False,
        env={**os.environ, **env} if env else None,
    )
    if check and done.returncode:
        raise PriceScheduleError((done.stderr or done.stdout).strip() or f"git {args[0]} failed")
    return done


class PriceScheduleError(Exception):
    """A scheduled fetch can't go ahead; the message says why."""


def _top(data_dir: Path) -> Path:
    done = _git(data_dir, "rev-parse", "--show-toplevel")
    if done.returncode:
        raise PriceScheduleError(f"{data_dir} isn't in a git repository")
    return Path(done.stdout.strip())


def _common_dir(top: Path) -> Path:
    path = Path(_git(top, "rev-parse", "--git-common-dir", check=True).stdout.strip())
    return path if path.is_absolute() else (top / path).resolve()


def _ref(top: Path, ref: str) -> str | None:
    done = _git(top, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return done.stdout.strip() if done.returncode == 0 else None


def _is_ancestor(top: Path, older: str, newer: str) -> bool:
    return _git(top, "merge-base", "--is-ancestor", older, newer).returncode == 0


# -- shared state ----------------------------------------------------------------------


def read_state(data_dir: Path) -> dict[str, Any]:
    """The last fetch and merge, as every worktree of the data repo sees
    them; {} before the first, or outside a git repository."""
    return read_state_file(data_dir, STATE_FILE)


def read_state_file(data_dir: Path, name: str) -> dict[str, Any]:
    """The state file ``name`` in git's common directory (this module's,
    or the nightly roll's); {} before the first write, or outside a git
    repository."""
    try:
        return json.loads((_common_dir(_top(data_dir)) / name).read_text())
    except (OSError, ValueError, PriceScheduleError):
        return {}


def _update_state(top: Path, **changes: Any) -> dict[str, Any]:
    """Change some of the state's keys (:func:`update_state_file`)."""
    return update_state_file(top, STATE_FILE, STATE_LOCK_FILE, **changes)


def update_state_file(top: Path, name: str, lock_name: str, **changes: Any) -> dict[str, Any]:
    """Change some of state file ``name``'s keys, under its own lock
    ``lock_name``: the daemon and a fetch (or roll) both write it, and
    neither may undo the other's keys. Each writer replaces the file from a
    temp file of its own, so a reader never sees half a file."""
    path = _common_dir(top) / name
    fd = os.open(path.with_name(lock_name), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        state.update(changes)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f"{name}.",
                                         suffix=".tmp", delete=False) as tmp:
            tmp.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(tmp.name, path)
        return state
    finally:
        os.close(fd)


def _locked(top: Path) -> contextlib.AbstractContextManager[bool]:
    """One fetch at a time per data repo: True when this one may go ahead."""
    return locked_file(top, LOCK_FILE)


@contextlib.contextmanager
def locked_file(top: Path, name: str) -> Generator[bool]:
    """Lock ``name`` in git's common directory without waiting: True when
    this process holds it, False when another one does."""
    fd = os.open(_common_dir(top) / name, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


# -- fetching ----------------------------------------------------------------------------


def _sparse_patterns(rel: str) -> list[str]:
    """The price files and what the symbol list and the fetch's start date
    are read from (``prices.prices_in_use``, ``prices.known_symbols``)."""
    base = f"/{rel}/" if rel not in ("", ".") else "/"
    return [f"{base}prices/", f"{base}securities.csv", f"{base}allocations.csv",
            f"{base}scenarios/*/account_lots.csv", f"{base}scenarios/*/scenario.csv"]


def _prepare(top: Path, worktree: Path, rel: str) -> str | None:
    """The sparse worktree at ``builder/prices`` with ``main`` merged in
    (:func:`_merge_main`), nothing left over; a line for the log when the
    branch had to start over from ``main``."""
    if not (worktree / ".git").exists():
        _git(top, "worktree", "prune")
        if worktree.exists():
            shutil.rmtree(worktree)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(top, "worktree", "add", "--no-checkout", "--detach", str(worktree), DATA_BRANCH,
             check=True)
    _git(worktree, "sparse-checkout", "set", "--no-cone", *_sparse_patterns(rel), check=True)
    base = _PRICES_REF if _ref(top, _PRICES_REF) else _MAIN_REF
    _git(worktree, "reset", "--hard", "-q", base, check=True)
    _git(worktree, "clean", "-fdq", check=True)
    if _merge_main(top, worktree) is None:
        return f"{PRICES_BRANCH} didn't merge with {DATA_BRANCH}; starting it over from {DATA_BRANCH}"
    return None


def _merge_main(top: Path, worktree: Path) -> str | None:
    """Merge ``main`` into ``builder/prices`` in the sparse worktree (which
    has it checked out), always as a merge commit, ``main``'s side winning
    wherever the two conflict (#588); the branch's tip after. When git
    can't merge them that way (a file deleted on one side and changed on
    the other), the branch starts over at ``main`` and this returns None."""
    merged = _git(worktree, *_QUIET, "merge", "--no-ff", "-X", "theirs", "-q",
                  "-m", f"Merge {DATA_BRANCH} into {PRICES_BRANCH}", _MAIN_REF, env=IDENTITY)
    if merged.returncode:
        _git(worktree, "merge", "--abort")
        _git(worktree, "reset", "--hard", "-q", _MAIN_REF)
        _git(top, "update-ref", _PRICES_REF, _MAIN_REF)
        return None
    head = _git(worktree, "rev-parse", "HEAD", check=True).stdout.strip()
    _git(top, "update-ref", _PRICES_REF, head, check=True)
    return head


@dataclass
class FetchOutcome:
    """What a scheduled fetch did, a line each for the builder's log."""

    lines: list[str]
    committed: str | None = None
    # builder/prices's tip when it holds prices main lacks, from this fetch
    # or an earlier one still waiting to merge.
    pending: str | None = None


def fetch(
    data_dir: Path, worktree: Path, *, provider: Any = None, now: datetime | None = None,
) -> FetchOutcome:
    """Fetch every symbol's prices on ``builder/prices`` (in ``worktree``,
    the builder's sparse worktree). Merging it is the daemon's, between
    builds (:func:`merge_pending`), so a build never reads a price file
    while a merge rewrites it. The symbols are ``sim prices fetch``'s with none named: those held with a
    quantity, and those securities.csv, allocations.csv or a price file
    names; one no lot holds is noted, not counted as a failure."""
    from analysis.sim import prices

    now = now or datetime.now(UTC)
    top = _top(data_dir)
    rel = str(data_dir.resolve().relative_to(top.resolve()))
    with _locked(top) as mine:
        if not mine:
            return FetchOutcome(["another fetch of this data repo is running"])
        if _ref(top, DATA_BRANCH) is None:
            raise PriceScheduleError(f"the data repo has no {DATA_BRANCH} branch")
        state = _update_state(top, attempted=now.timestamp(), refetch=False)
        started_over = _prepare(top, worktree, rel)
        lines: list[str] = [started_over] if started_over else []
        data = worktree / rel
        use = prices.prices_in_use(data / "scenarios") if (data / "scenarios").is_dir() \
            else prices.PriceUse()
        held = set(use.symbols)
        chosen = sorted(held | set(prices.known_symbols(data)))
        failing: dict[str, Any] = dict(state.get("failing") or {})
        if not chosen:
            lines.append("no symbols to fetch")
            results = []
        else:
            today = now.date()
            provider = provider or prices.PRICE_PROVIDERS["yahoo"]()
            results = prices.fetch_prices(
                data / "prices", chosen, provider,
                since=prices.default_since(use.earliest_start, today), today=today,
            )
        for result in results:
            if result.error:
                first = (failing.get(result.symbol) or {}).get("since", now.timestamp())
                failing[result.symbol] = {"error": result.error, "since": first,
                                          "held": result.symbol in held}
                lines.append(f"{result.symbol}: {result.error}"
                             + ("" if result.symbol in held else " (no lot holds it)"))
            else:
                failing.pop(result.symbol, None)
                if result.added or result.changed:
                    assert result.latest is not None
                    lines.append(f"{result.symbol}: {result.added} added"
                                 + (f", {result.changed} changed" if result.changed else "")
                                 + f" (latest {prices.format_price(result.latest[1])} "
                                 f"on {result.latest[0]})")
        failing = {s: f for s, f in failing.items() if s in chosen}
        worked = not results or any(r.error is None for r in results)
        committed = _commit(top, worktree, rel, results)
        pending = _settle(top, worktree)
        _update_state(top, failing=failing, pending=pending, waiting=None,
                      **({"succeeded": now.timestamp()} if worked else {}))
        if committed:
            lines.append(f"committed new prices on {PRICES_BRANCH} ({committed[:10]})")
        elif not worked:
            lines.append("every symbol failed; trying again in an hour")
        elif pending:
            lines.append(f"no new prices; earlier ones still wait to merge into {DATA_BRANCH}")
        else:
            lines.append("no new prices")
    return FetchOutcome(lines, committed, pending)


def _commit(top: Path, worktree: Path, rel: str, results: list[Any]) -> str | None:
    """Commit the fetched files on ``builder/prices``; the new commit, or
    None when nothing changed."""
    path = f"{rel}/prices" if rel not in ("", ".") else "prices"
    if (worktree / path).exists():  # none when no symbol was fetched into a repo without one
        _git(worktree, "add", "-A", "--", path, check=True)
    if _git(worktree, "diff", "--cached", "--quiet").returncode == 0:
        return None
    changed = [r for r in results if r.error is None and (r.added or r.changed)]
    symbols = ", ".join(r.symbol for r in changed[:6]) + (", ..." if len(changed) > 6 else "")
    latest = max((r.latest[0] for r in changed if r.latest), default=None)
    subject = f"Prices: {symbols}" + (f" to {latest}" if latest else "") if changed else "Prices"
    _git(worktree, *_QUIET, "commit", "-q", "-m", subject,
         "-m", "Fetched by the background builder (#560).", env=IDENTITY, check=True)
    head = _git(worktree, "rev-parse", "HEAD", check=True).stdout.strip()
    _git(top, "update-ref", _PRICES_REF, head, check=True)
    return head


def _settle(top: Path, worktree: Path) -> str | None:
    """What waits to merge into ``main``: ``builder/prices``'s tip, or None
    when its files are ``main``'s (the branch then goes back to ``main``, so
    the merge commits a fetch with nothing new made don't pile up on it)."""
    head = _git(worktree, "rev-parse", "HEAD", check=True).stdout.strip()
    if _same_files(top, _MAIN_REF, head):
        _git(top, "update-ref", _PRICES_REF, _MAIN_REF, check=True)
        return None
    return head


def _same_files(top: Path, one: str, other: str) -> bool:
    return _git(top, "diff", "--quiet", one, other).returncode == 0


# -- merging ------------------------------------------------------------------------------

_IN_PROGRESS = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD")


def _git_path(top: Path, name: str) -> Path:
    path = Path(_git(top, "rev-parse", "--git-path", name, check=True).stdout.strip())
    return path if path.is_absolute() else top / path


def _why_not(top: Path) -> str | None:
    """Why ``main`` can't take the merge in this worktree now, or None."""
    branch = _git(top, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if branch != DATA_BRANCH:
        return f"the data repo has {branch or 'a detached HEAD'} checked out, not {DATA_BRANCH}"
    if any(_git_path(top, name).exists() for name in _IN_PROGRESS):
        return "a merge, rebase or cherry-pick is under way in the data repo"
    if _git(top, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return f"{DATA_BRANCH} has uncommitted changes"
    return None


def merge_pending(data_dir: Path, worktree: Path) -> list[str]:
    """Merge ``builder/prices`` into ``main`` if it holds prices ``main``
    lacks and this worktree allows it, always as a merge commit (#588), then
    push ``main`` (:func:`_push`); ``worktree`` is the builder's sparse one,
    where ``main`` is merged into the branch first if it moved. Returns lines
    for the log when it merged; why it waits goes in the state
    (:func:`read_state`'s ``waiting``). Only the daemon calls this, between
    builds."""
    try:
        top = _top(data_dir)
    except PriceScheduleError:
        return []
    pending, main = _ref(top, _PRICES_REF), _ref(top, _MAIN_REF)
    if pending is None or main is None or _is_ancestor(top, pending, main) \
            or _same_files(top, main, pending):
        # Nothing waiting, perhaps since someone merged it by hand.
        if read_state(data_dir).get("pending"):
            _update_state(top, pending=None, waiting=None)
        return []
    if not _is_ancestor(top, main, pending):
        # main moved since the fetch (a commit while the prices waited):
        # merge it in here first, so the merge into the user's checkout
        # can't conflict.
        with _locked(top) as mine:
            if not mine:
                return []  # a fetch is running, and it merges main first
            pending = _merge_main_again(top, worktree)
        if pending is False:
            _update_state(top, pending=None, refetch=True,
                          waiting=f"{PRICES_BRANCH} didn't merge with {DATA_BRANCH}; "
                                  "fetching again from it")
            return []
        if pending is None:
            return []  # main's side won every change: nothing left to merge
    why = _why_not(top)
    if why is None:
        # Signing off for the same reason as the builder's own commits; the
        # data repo's hooks run, so post-merge pokes the builder.
        done = _git(top, "-c", "commit.gpgsign=false", "merge", "--no-ff", "-q",
                    "-m", f"Merge new prices from {PRICES_BRANCH}", _PRICES_REF, env=IDENTITY)
        if done.returncode == 0:
            _update_state(top, pending=None, waiting=None, merged=clock.time())
            return [f"merged new prices into {DATA_BRANCH}", _push(top)]
        why = (done.stderr or done.stdout).strip().splitlines()[0] if (done.stderr or done.stdout) \
            else "git merge failed"
    _update_state(top, waiting=why)
    return []


def _merge_main_again(top: Path, worktree: Path) -> str | None | Literal[False]:
    """``main`` merged into ``builder/prices`` again, in the sparse worktree:
    the branch's new tip, None when that leaves nothing for ``main``
    (:func:`_settle`), or False when it fails or the worktree is gone (the
    next fetch starts over)."""
    if not (worktree / ".git").exists():
        return False
    if _git(worktree, "reset", "--hard", "-q", _PRICES_REF).returncode:
        return False
    if _merge_main(top, worktree) is None:
        return False
    head = _settle(top, worktree)
    _update_state(top, pending=head)
    return head


# How long a push may take before it counts as failed: the daemon waits on it.
PUSH_SECONDS = 120


def _push(top: Path) -> str:
    """Push ``main`` when it is strictly ahead of its upstream (#588); a
    line for the log either way. A failed push is only logged: the next
    merge pushes again."""
    upstream = _git(top, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                    f"{DATA_BRANCH}@{{upstream}}")
    remote = _git(top, "config", f"branch.{DATA_BRANCH}.remote").stdout.strip()
    target = _git(top, "config", f"branch.{DATA_BRANCH}.merge").stdout.strip()
    if upstream.returncode or remote in ("", ".") or not target:
        return f"not pushing: {DATA_BRANCH} has no upstream branch on a remote"
    name = upstream.stdout.strip()
    ours, theirs = _ref(top, _MAIN_REF), _ref(top, name)
    if ours is None or theirs is None:
        return f"not pushing: {name} isn't known here; try git fetch"
    if ours == theirs:
        return f"not pushing: {DATA_BRANCH} is already at {name}"
    if not _is_ancestor(top, theirs, ours):
        return (f"not pushing: {DATA_BRANCH} isn't strictly ahead of {name} "
                f"(it lacks commits {name} has); try git pull on {DATA_BRANCH}")
    try:
        # No terminal to ask on: a push that wants a password fails, not hangs.
        done = subprocess.run(
            ["git", "-C", str(top), "push", "-q", remote, f"{_MAIN_REF}:{target}"],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
            timeout=PUSH_SECONDS, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        return f"push of {DATA_BRANCH} to {remote} failed: no answer in {PUSH_SECONDS}s"
    except OSError as exc:
        return f"push of {DATA_BRANCH} to {remote} failed: {exc}"
    if done.returncode:
        said = [line for line in (done.stderr or done.stdout).splitlines() if line.strip()]
        return f"push of {DATA_BRANCH} to {remote} failed: {said[0] if said else 'git push failed'}"
    return f"pushed {DATA_BRANCH} to {name}"


# -- the timer -----------------------------------------------------------------------------

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


def timer_label(data_dir: Path) -> str:
    digest = hashlib.sha1(str(data_dir.resolve()).encode()).hexdigest()[:10]
    return f"local.analysis.builder.{digest}"


def _plist_path(data_dir: Path) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{timer_label(data_dir)}.plist"


def _timer_command(sim: Path, data_dir: Path) -> list[str]:
    """What the timer runs: only a look at the state unless a fetch or a
    roll (#564) is due, so the hourly wake costs no scan; one that commits
    pokes the builder, which merges between builds."""
    return [str(sim), "builder", "tick", "--data-dir", str(data_dir.resolve())]


def _cron_marker(data_dir: Path) -> str:
    return f"{TIMER_MARKER} {data_dir.resolve()}"


def _crontab(run: Runner) -> list[str] | None:
    """The user's crontab lines; None when there's no crontab command."""
    if shutil.which("crontab") is None:
        return None
    done = run(["crontab", "-l"])
    return done.stdout.splitlines() if done.returncode == 0 else []


def install_timer(data_dir: Path, sim: Path, *, run: Runner = _run,
                  platform: str = sys.platform) -> str:
    """Run ``sim builder tick`` hourly, so a due fetch (or roll, #564)
    happens whether or not anything starts the builder. Replaces the timer of
    this data dir if there is one. Returns a line saying what it did."""
    if switched_off():
        return f"price fetches are off here ({SWITCH_ENV}=off): the timer is left as it is"
    command = _timer_command(sim, data_dir)
    if platform == "darwin":
        plist = _plist_path(data_dir)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({
            "Label": timer_label(data_dir), "ProgramArguments": command,
            "StartInterval": 3600, "RunAtLoad": True,
            "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
        }))
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{timer_label(data_dir)}"])
        done = run(["launchctl", "bootstrap", domain, str(plist)])
        if done.returncode:
            return f"wrote {plist}, but launchctl couldn't load it: {done.stderr.strip()}"
        return f"installed {plist}: the builder wakes hourly to fetch prices and roll scenarios when due"
    lines = _crontab(run)
    if lines is None:
        return "no crontab here: prices are fetched only when the builder starts for other work"
    marker = _cron_marker(data_dir)
    kept = [line for line in lines if not line.endswith(marker)]
    entry = (f"{CRON_MINUTE} * * * * {shlex.join(command)} >/dev/null 2>&1 {marker}")
    done = run(["crontab", "-"], input="\n".join([*kept, entry]) + "\n")
    if done.returncode:
        return f"couldn't install the crontab line: {done.stderr.strip()}"
    return "added an hourly crontab line: the builder wakes to fetch prices and roll scenarios when due"


def uninstall_timer(data_dir: Path, *, run: Runner = _run, platform: str = sys.platform) -> str | None:
    """Remove this data dir's timer; a line saying so, or None if it had none."""
    if platform == "darwin":
        plist = _plist_path(data_dir)
        if not plist.exists():
            return None
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{timer_label(data_dir)}"])
        plist.unlink()
        return f"removed {plist}"
    lines = _crontab(run)
    marker = _cron_marker(data_dir)
    if not lines or not any(line.endswith(marker) for line in lines):
        return None
    kept = [line for line in lines if not line.endswith(marker)]
    run(["crontab", "-"], input="\n".join(kept) + "\n" if kept else "")
    return "removed the hourly crontab line"


def describe(state: dict[str, Any], now: datetime) -> list[str]:
    """``sim builder status``'s lines about prices."""
    def when(stamp: float | None) -> str:
        if not stamp:
            return "never"
        return datetime.fromtimestamp(stamp, UTC).astimezone().strftime("%Y-%m-%d %H:%M")

    lines = [f"prices: last fetched {when(state.get('succeeded'))}"
             + (f", last tried {when(state.get('attempted'))}"
                if (state.get("attempted") or 0) > (state.get("succeeded") or 0) else "")]
    if state.get("pending"):
        lines.append(f"prices waiting to merge into {DATA_BRANCH}: {state.get('waiting') or 'soon'}")
    for symbol, failed in sorted((state.get("failing") or {}).items()):
        days = int((now.timestamp() - failed.get("since", now.timestamp())) // 86400)
        since = f" for {days} days" if days >= 1 else ""
        flag = "" if failed.get("held") else " (no lot holds it)"
        lines.append(f"price failing{since}: {symbol}: {failed.get('error')}{flag}")
    return lines


def stale_held(state: dict[str, Any], now: datetime, days: int = 3) -> list[str]:
    """Held symbols whose scheduled fetch has failed for ``days`` or more."""
    return sorted(s for s, f in (state.get("failing") or {}).items()
                  if f.get("held") and now.timestamp() - f.get("since", now.timestamp())
                  >= days * 86400)
