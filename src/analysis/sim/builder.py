"""#300: building scenarios in the background.

One daemon per workspace pair (a code worktree and the data repo it links,
each naming the other) keeps every scenario's runs current. Git hooks in both
repos poke it on a commit, checkout, merge or rewrite; each poke rebuilds its
queue from what ``freshness.unchanged_run`` says a rerun could change, so it
works by tree, not by commit. Code that opens a scenario's run waits for a
pending build of it (:func:`await_fresh`), moving it to the front.

Rules (Bob, #300):

- It builds only when the code paths (``freshness.CODE_PATHS``) are committed
  and match the workspace's branch: local ``main`` unless
  ``sim builder install BRANCH`` named another. Otherwise it is disabled: it
  builds nothing and nobody waits. (A local branch, not ``origin/main``, so a
  background fetch, or choosing not to pull, changes nothing. Recorded in
  ``config.json``, not an environment variable, so every shell, notebook and
  daemon of the workspace agrees.)
- A build whose inputs changed is cancelled and requeued; a change to the
  code restarts the daemon, so it never builds with code it imported earlier.
- A scenario that failed isn't retried until its inputs change.
- It exits once its queue has been empty for a short while; anything that
  needs it starts it.
- It is opt-in per workspace (``sim builder install``, which
  ``scripts/setup.sh`` runs except under CI or an agent), and
  ``ANALYSIS_BUILDER=off`` turns it off entirely.

Everything lives in the data repo's ``state/builder/`` (untracked, and one
per data worktree): ``config.json`` (the opt-in and the pair),
``daemon.lock`` (held for the daemon's life), ``status.json``, ``requests/``
(pokes and "build this next"), ``logs/`` (one file a day, #314: see
``builder_log``).
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.sim import builder_log
from analysis.sim.lineage import is_pool
from analysis.sim.loader import (
    DEFAULT_INTENT_DIR,
    DEFAULT_RUNS_DIR,
    DEFAULT_STATE_DIR,
    find_state_dir,
    list_scenario_names,
    read_current_scenario,
    read_disabled_scenarios,
)
from analysis.sim.locks import BUILDER_CHILD_ENV, BUILDER_SWITCH_ENV, is_building, run_name_key

__all__ = [
    "BuilderPaths",
    "await_fresh",
    "build_pending",
    "daemon_alive",
    "main_check",
    "notify",
    "pair_problems",
    "read_status",
    "run_daemon",
    "switched_off",
]

# The branch the code must match unless `sim builder install BRANCH` names another.
DEFAULT_BRANCH = "main"
# How long the daemon lingers with an empty queue before it exits.
IDLE_GRACE_SECONDS = 120.0
# How often the daemon looks for requests and its build finishing.
TICK_SECONDS = 0.5
# Hook stubs this module writes into the data repo carry this line.
HOOK_MARKER = "# analysis sim builder hook (#300)"
HOOK_NAMES = ("post-commit", "post-checkout", "post-merge", "post-rewrite")


def switched_off() -> bool:
    """``ANALYSIS_BUILDER=off`` (or 0/no/false): no daemon, no waiting."""
    return os.environ.get(BUILDER_SWITCH_ENV, "").strip().lower() in {"off", "0", "no", "false"}


@dataclass(frozen=True)
class BuilderPaths:
    """The builder's files under a data repo's ``state/``."""

    state_dir: Path

    @classmethod
    def for_data(cls, data_dir: Path) -> BuilderPaths:
        return cls(find_state_dir(data_dir, DEFAULT_STATE_DIR))

    @property
    def dir(self) -> Path:
        return self.state_dir / "builder"

    @property
    def config(self) -> Path:
        return self.dir / "config.json"

    @property
    def daemon_lock(self) -> Path:
        return self.dir / "daemon.lock"

    @property
    def status(self) -> Path:
        return self.dir / "status.json"

    @property
    def requests(self) -> Path:
        return self.dir / "requests"

    @property
    def log_dir(self) -> Path:
        return self.dir / "logs"

    @property
    def working_set(self) -> Path:
        """The scenarios most recently used (#322, sim/working_set.py)."""
        return self.dir / "working_set"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / DEFAULT_RUNS_DIR

    @property
    def intent_dir(self) -> Path:
        return self.state_dir / DEFAULT_INTENT_DIR


def read_config(paths: BuilderPaths) -> dict[str, Any] | None:
    """The workspace's opt-in record, or None when it hasn't opted in."""
    try:
        config = json.loads(paths.config.read_text())
    except (OSError, ValueError):
        return None
    return config if isinstance(config, dict) and config.get("data_dir") else None


def read_status(paths: BuilderPaths) -> dict[str, Any]:
    try:
        status = json.loads(paths.status.read_text())
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Write ``path`` whole, by rename, so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True))
    os.replace(tmp, path)


def daemon_alive(paths: BuilderPaths) -> bool:
    """Whether a daemon holds this workspace's daemon lock."""
    try:
        fd = os.open(paths.daemon_lock, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


# -- the pair and the code -------------------------------------------------


def code_dir() -> Path | None:
    """The working tree of the code repo this package runs from."""
    import analysis
    from analysis.sim.provenance import _open_repo

    repo = _open_repo(Path(analysis.__file__).parent)
    if repo is None or repo.working_tree_dir is None:
        return None
    return Path(repo.working_tree_dir).resolve()


def pair_problems(code: Path | None, data_dir: Path) -> list[str]:
    """Why ``code`` and ``data_dir`` aren't a 1:1 workspace pair: the code
    worktree's ``data/shared`` must be the data repo holding ``data_dir``,
    and ``data_dir/.analysis`` must name the code worktree."""
    if code is None:
        return ["the code isn't running from a git worktree"]
    problems = []
    data_dir = data_dir.resolve()
    shared = code / "data" / "shared"
    if not shared.exists() or shared.resolve() != data_dir.parent:
        problems.append(f"{shared} doesn't lead to {data_dir.parent}")
    pointer = data_dir / ".analysis"
    try:
        named = Path(pointer.read_text().strip()).resolve()
    except OSError:
        named = None
    if named != code:
        problems.append(f"{pointer} doesn't name {code}")
    if not (data_dir.parent / ".git").exists():
        problems.append(f"{data_dir.parent} isn't a git repo (template data has nothing to track)")
    return problems


def config_branch(config: dict[str, Any] | None) -> str:
    """The branch a workspace's code must match (``install``'s BRANCH)."""
    branch = (config or {}).get("branch")
    return branch if isinstance(branch, str) and branch else DEFAULT_BRANCH


def main_check(
    branch: str = DEFAULT_BRANCH, repo_dir: Path | None = None,
) -> tuple[tuple[str, ...] | None, str | None]:
    """The code trees to build with, or None and why the builder is off:
    the code paths must be committed and match the local ``branch``."""
    from analysis.sim.freshness import CODE_PATHS, _trees, code_tree, code_tree_of
    from analysis.sim.provenance import _open_repo

    code = code_tree() if repo_dir is None else code_tree_of(_open_repo(repo_dir))
    paths = ", ".join(CODE_PATHS)
    if code.repo is None:
        return None, "the code isn't in a git repo"
    if code.trees is None:
        return None, f"uncommitted code in {paths}"
    main = _trees(code.repo, branch)
    if main is None:
        return None, f"no local {branch} branch"
    if main != code.trees:
        return None, f"code ({paths}) differs from {branch}"
    return code.trees, None


# -- requests ----------------------------------------------------------------


def _request(paths: BuilderPaths, kind: str, scenario: str | None = None) -> float:
    """Leave a request for the daemon; returns when it was made."""
    paths.requests.mkdir(parents=True, exist_ok=True)
    made = time.time()
    body = {"kind": kind, "scenario": scenario, "made": made}
    name = f"{time.time_ns()}-{os.getpid()}.json"
    _write_json(paths.requests / name, body)
    return made


def _drain_requests(paths: BuilderPaths) -> list[dict[str, Any]]:
    out = []
    if not paths.requests.is_dir():
        return out
    for path in sorted(paths.requests.glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass
        path.unlink(missing_ok=True)
    return out


def _sim_executable() -> list[str]:
    sim = Path(sys.executable).parent / "sim"
    return [str(sim)] if sim.exists() else [sys.executable, "-m", "analysis", "sim"]


def spawn_daemon(paths: BuilderPaths, data_dir: Path) -> None:
    """Start a daemon for this workspace, detached. A second one started at
    the same moment finds the daemon lock taken and exits."""
    paths.dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, BUILDER_SWITCH_ENV: "off"}
    # The pair's own code, whichever checkout asked (a reader, a hook in
    # another worktree linking the same data).
    code = (read_config(paths) or {}).get("code_dir")
    sim = Path(code) / ".venv" / "bin" / "sim" if code else None
    command = [str(sim)] if sim is not None and sim.exists() else _sim_executable()
    # The daemon writes its log itself; only what it can't (a traceback) goes
    # to stderr, kept in the log unindented, so shown without -v.
    log = builder_log.file_for(paths.log_dir, datetime.now())
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as stderr:
        subprocess.Popen(
            [*command, "builder", "run", "--data-dir", str(data_dir)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
            start_new_session=True, env=env, close_fds=True,
        )


def notify(data_dir: Path, *, start: bool = True) -> str:
    """What a hook calls: ask the daemon to look again, starting it if it
    isn't running. Returns a line saying what happened."""
    if switched_off():
        return f"background builds are off ({BUILDER_SWITCH_ENV}=off)"
    paths = BuilderPaths.for_data(data_dir)
    if read_config(paths) is None:
        return "background builds aren't installed here (sim builder install)"
    _request(paths, "rescan")
    if daemon_alive(paths):
        return "asked the background builder to look again"
    if not start:
        return "background builder isn't running"
    spawn_daemon(paths, data_dir)
    return "started the background builder"


# -- the daemon ----------------------------------------------------------------


@dataclass
class _Build:
    scenario: str
    key: list[Any]
    process: subprocess.Popen[bytes]
    started: float
    wanted: bool = False  # someone is waiting for it: never preempted
    reader: threading.Thread | None = None  # copies its output into the log

    def drain(self) -> None:
        """Let the reader copy the last of the output, so the outcome's
        record comes after it."""
        if self.reader is not None:
            self.reader.join(timeout=5)


class _Daemon:
    def __init__(self, data_dir: Path, echo: Callable[[str], None]) -> None:
        self.data_dir = data_dir.resolve()
        self.paths = BuilderPaths.for_data(self.data_dir)
        self.echo = echo
        self.writer = builder_log.LogWriter(self.paths.log_dir, on_new_day=self.new_day)
        self.queue: list[str] = []
        self.first: list[str] = []
        # Builds preempted for a scenario someone is waiting on: next in line.
        self.resume: list[str] = []
        self.waiting: dict[str, str] = {}
        self.status = read_status(self.paths)
        self.failed: dict[str, dict[str, Any]] = dict(self.status.get("failed") or {})
        # Builds stopped because their inputs changed (an edit, #309), so a
        # reader waiting on one says so rather than that it is done.
        self.cancelled: dict[str, dict[str, Any]] = dict(self.status.get("cancelled") or {})
        self.build: _Build | None = None
        self.state = "starting"
        self.reason: str | None = None
        self.started_trees: tuple[str, ...] | None = None
        self.stopping = False
        self.restart = False

    # The key a build is good for: the code trees and the scenario's data tree.
    def key(self, scenario: str, trees: tuple[str, ...]) -> list[Any]:
        from analysis.sim.provenance import data_stamps

        return [list(trees), data_stamps(self.data_dir, scenario).tree_id]

    def log(self, text: str) -> None:
        self.echo(self.writer.event(text))

    def new_day(self) -> None:
        self.follow_stderr()
        self.prune_logs()

    def follow_stderr(self) -> None:
        """When this daemon's stderr is a log file (as spawn_daemon starts
        it), point it at today's, so a traceback lands beside the records it
        follows, not in the file of the day the daemon started."""
        try:
            if not stat.S_ISREG(os.fstat(2).st_mode):
                return  # a terminal (`sim builder run` by hand) or nothing
            path = builder_log.file_for(self.paths.log_dir, datetime.now())
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        except OSError:
            return
        sys.stderr.flush()
        os.dup2(fd, 2)
        os.close(fd)

    def prune_logs(self) -> None:
        """Old log files, by ``.retention.yaml``'s ``logs:`` rules."""
        from analysis.sim.retention import RetentionError

        try:
            builder_log.prune_logs(self.data_dir, self.paths.dir)
        except RetentionError as exc:
            self.log(f"log files not pruned: {exc}")

    def write_status(self) -> None:
        current = None
        if self.build is not None:
            current = {"scenario": self.build.scenario, "pid": self.build.process.pid,
                       "started": self.build.started}
        _write_json(self.paths.status, {
            "pid": os.getpid(), "state": self.state, "reason": self.reason,
            "current": current, "queue": self.queue, "waiting": self.waiting,
            "failed": self.failed, "cancelled": self.cancelled, "updated": time.time(),
        })

    def scan(self) -> None:
        """Recompute the queue from the trees as they are now."""
        from analysis.sim.freshness import code_tree, unchanged_run
        from analysis.sim.provenance import data_stamps

        # Read each time: `sim builder install BRANCH` may have changed it.
        trees, reason = main_check(config_branch(read_config(self.paths)))
        if trees is not None and self.started_trees is None:
            self.started_trees = trees
        if trees is not None and trees != self.started_trees:
            self.log("code changed; restarting")
            self.restart = True
            return
        self.reason = reason
        if trees is None:
            self.state = "disabled"
            self.queue, self.waiting = [], {}
            self.cancel(f"builder disabled: {reason}")
            return
        code = code_tree()
        queue, waiting = [], {}
        disabled = read_disabled_scenarios(self.data_dir)
        for name in list_scenario_names(self.data_dir):
            if name in disabled:  # only built when named
                continue
            data = data_stamps(self.data_dir, name)
            if data.dirty:
                waiting[name] = "data dirty"
                continue
            failed = self.failed.get(name)
            if failed is not None and failed.get("key") == [list(trees), data.tree_id]:
                continue
            if unchanged_run(self.data_dir, self.paths.runs_dir, name, code) is None:
                queue.append(name)
        self.waiting = waiting

        def why_dropped(name: str) -> str:
            if name in disabled:
                return "it was disabled"
            if name in waiting:
                return "its data has uncommitted changes"
            return "its inputs changed"

        # Queued builds no longer to be done: a reader waiting on one must
        # hear that it was cancelled, not that it is done.
        for name in set(self.queue) - set(queue):
            if name in disabled or name in waiting:
                self.cancelled[name] = {"at": time.time(), "why": why_dropped(name)}
        if self.build is not None:
            name = self.build.scenario
            if name in disabled or self.build.key != self.key(name, trees):
                why = why_dropped(name)
                self.cancel(f"{name}: {why}")
                self.cancelled[name] = {"at": time.time(), "why": why}
            if self.build is not None and self.build.scenario in queue:
                queue.remove(self.build.scenario)
        self.queue = self.order(queue)
        self.state = "building" if self.build is not None or self.queue else "idle"

    def order(self, queue: list[str]) -> list[str]:
        """Scenarios someone is waiting on, then builds they preempted, then
        the working set's, most recently used first (#322), then plain runs
        before ensembles (quick results first), then pools, which reuse
        their components' runs (ADR-009), then by name."""
        first = [n for n in self.first if n in queue]
        resume = [n for n in self.resume if n in queue]
        used = working_set_order(self.data_dir)
        runs = self.paths.runs_dir
        scenarios = self.data_dir / "scenarios"

        def rank(name: str) -> tuple[int, int, int, int, str]:
            pos = first.index(name) if name in first else len(first)
            again = resume.index(name) if name in resume else len(resume)
            recent = used.get(name, len(used))
            ensemble = 2 if is_pool(scenarios, name) else int((runs / name).is_dir())
            return (pos, again, recent, ensemble, name)

        return sorted(queue, key=rank)

    def preempt(self) -> None:
        """Stop a build nobody is waiting on when someone is waiting on
        another scenario (Bob, #300): the wanted one starts at once and the
        stopped one is requeued right after it."""
        build = self.build
        wanted = next((n for n in self.first if n in self.queue), None)
        if build is None or wanted is None:
            return
        if build.wanted or build.scenario in self.first:  # asked for while building
            build.wanted = True
            self.first = [n for n in self.first if n != build.scenario]
            return
        self.cancel(f"{wanted} is wanted now")
        self.resume = [build.scenario, *[n for n in self.resume if n != build.scenario]]
        self.queue = self.order([*self.queue, build.scenario])

    def cancel(self, why: str) -> None:
        build = self.build
        if build is None:
            return
        _kill(build.process)
        build.drain()  # the rest of its output stays under its `building` record
        self.log(f"cancelled {build.scenario}: {why}")
        self.build = None

    def start_next(self, trees: tuple[str, ...]) -> None:
        name = self.queue.pop(0)
        wanted = name in self.first
        self.first = [n for n in self.first if n != name]
        self.resume = [n for n in self.resume if n != name]
        key = self.key(name, trees)
        env = {**os.environ, BUILDER_SWITCH_ENV: "off", BUILDER_CHILD_ENV: "1"}
        self.log(f"building {name}")
        self.paths.dir.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [*_sim_executable(), "load", name, "--data-dir", str(self.data_dir)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, env=env, close_fds=True,
        )
        assert process.stdout is not None
        # A thread, so a chatty build never blocks on a full pipe while the
        # loop sleeps.
        reader = threading.Thread(target=self.writer.copy, args=(process.stdout,),
                                  name=f"log {name}", daemon=True)
        reader.start()
        self.build = _Build(name, key, process, time.time(), wanted, reader)
        self.cancelled.pop(name, None)
        self.state = "building"

    def finished(self) -> bool:
        """Note a build that ended; True if one did."""
        build = self.build
        if build is None or build.process.poll() is None:
            return False
        self.build = None
        build.drain()
        from analysis.sim.freshness import code_tree, unchanged_run

        code = build.process.returncode
        took = _took(time.time() - build.started)
        still_stale = code == 0 and unchanged_run(
            self.data_dir, self.paths.runs_dir, build.scenario, code_tree()) is None
        if code == 0 and not still_stale:
            self.failed.pop(build.scenario, None)
            self.log(f"built {build.scenario} ({took})")
        elif still_stale:
            # An ensemble with failed members: `sim load` exits 0, but the
            # batch is incomplete, so it would be queued again at once.
            self.failed[build.scenario] = {
                "key": build.key, "exit": 0, "at": time.time(),
                "message": "built, but not every member succeeded; see sim builder log",
            }
            self.log(f"{build.scenario} built incomplete (failed members, {took})")
        else:
            self.failed[build.scenario] = {
                "key": build.key, "exit": code, "at": time.time(),
                "message": f"exit {code}; see sim builder log",
            }
            self.log(f"{build.scenario} failed (exit {code}, {took})")
        return True

    def run(self) -> int:
        self.paths.dir.mkdir(parents=True, exist_ok=True)
        lock = os.open(self.paths.daemon_lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock)
            return 0  # one is already running; it has the request
        self.prune_logs()
        configured = (read_config(self.paths) or {}).get("code_dir")
        if configured and code_dir() != Path(configured).resolve():
            self.log(f"not started: this is {code_dir()}, but the pair's code is {configured}")
            os.close(lock)
            return 1
        # This process and its builds must never wait on the builder itself.
        os.environ[BUILDER_SWITCH_ENV] = "off"

        def stop(_signum: int, _frame: object) -> None:
            self.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.log(f"started (pid {os.getpid()}) for {self.data_dir}")
        idle_since: float | None = None
        rescan = True
        try:
            while not self.stopping and not self.restart:
                for request in _drain_requests(self.paths):
                    rescan = True
                    if request.get("kind") == "first" and request.get("scenario"):
                        name = request["scenario"]
                        self.first = [name, *[n for n in self.first if n != name]]
                if self.finished():
                    rescan = True
                if rescan:
                    rescan = False
                    self.scan()
                    if self.restart:
                        break
                    self.preempt()
                    self.write_status()
                if self.build is None and self.queue and self.started_trees is not None:
                    self.start_next(self.started_trees)
                    self.write_status()
                if self.build is None and not self.queue:
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since > IDLE_GRACE_SECONDS:
                        break
                else:
                    idle_since = None
                time.sleep(TICK_SECONDS)
        finally:
            self.cancel("builder stopping" if not self.restart else "code changed")
            self.state = "stopped"
            self.queue = []
            self.write_status()
            self.log("restarting" if self.restart else "stopped")
            os.close(lock)  # frees the daemon lock for the next one
        if self.restart:
            os.execv(sys.executable, [
                sys.executable, "-m", "analysis", "sim", "builder", "run",
                "--data-dir", str(self.data_dir),
            ])
        return 0


def _took(seconds: float) -> str:
    """``42s``, ``3m38s``, ``1h05m``."""
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def _kill(process: subprocess.Popen[bytes]) -> None:
    """Stop a build and its workers (its own process group)."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_daemon(data_dir: Path, echo: Callable[[str], None] = print) -> int:
    return _Daemon(data_dir, echo).run()


# -- readers wait ----------------------------------------------------------------

# What a reader last found, by (state dir, scenario): the signature it was
# found under (see _signature), so it is asked again only once something
# that could change the answer has.
_checked: dict[tuple[Path, str], tuple[int, ...]] = {}
_git_dirs: dict[tuple[Path, tuple[Any, Any]], list[Path]] = {}


def working_set_order(data_dir: Path) -> dict[str, int]:
    """Each working-set scenario's rank, most recently used 0 (#322). The
    current scenario counts as used most recently when the working set
    lacks it (one current before the working set existed)."""
    from analysis.sim.working_set import read_working_set

    names = read_working_set(data_dir)
    current = read_current_scenario(data_dir)
    if current and current not in names:
        names.insert(0, current)
    return {name: i for i, name in enumerate(names)}


def _signature(paths: BuilderPaths, config: dict[str, Any]) -> tuple[int, ...]:
    """Changes whenever the answer to "is a build pending?" could: the
    daemon's status, the pair's config (`install BRANCH`), and each repo's
    reflog (every commit, checkout, merge, reset or rebase in that worktree
    appends to it) and index (staging)."""
    pair = (config.get("code_dir"), config.get("data_dir"))
    dirs = _git_dirs.get((paths.state_dir, pair))
    if dirs is None:
        dirs = []
        for top in (config.get("code_dir"), Path(config["data_dir"]).parent):
            if not top:
                continue
            out = subprocess.run(["git", "-C", str(top), "rev-parse", "--absolute-git-dir"],
                                 capture_output=True, text=True, check=False).stdout.strip()
            if out:
                dirs.append(Path(out))
        _git_dirs[(paths.state_dir, pair)] = dirs
    files = [paths.status, paths.config, *(d / "logs" / "HEAD" for d in dirs), *(d / "index" for d in dirs)]
    out = []
    for f in files:
        try:
            out.append(f.stat().st_mtime_ns)
        except OSError:
            out.append(0)
    return tuple(out)


def await_fresh(
    runs_dir: Path, scenario: str, echo: Callable[[str], None] | None = None,
) -> None:
    """Before a scenario's run is opened: if a background build of it is
    pending, move it to the front and wait for it. Does nothing where the
    builder isn't installed, is off or disabled, or the run is current.
    Ctrl-C gives up the wait (the latest existing run is opened)."""
    found = _pending_build(runs_dir, scenario)
    if found is None:
        return
    paths, data_dir = found
    echo = echo or (lambda text: print(text, file=sys.stderr))
    made = _request(paths, "first", scenario)
    if not daemon_alive(paths):
        spawn_daemon(paths, data_dir)
    try:
        _wait(paths, scenario, made, echo)
    except KeyboardInterrupt:
        echo(f"gave up waiting for {scenario}; opening its latest run, which may be out of date")


def build_pending(runs_dir: Path, scenario: str) -> bool:
    """Whether opening ``scenario``'s run would wait for a background build
    (:func:`await_fresh`), asked without waiting or moving it up the queue."""
    return _pending_build(runs_dir, scenario) is not None


def _pending_build(runs_dir: Path, scenario: str) -> tuple[BuilderPaths, Path] | None:
    """The workspace's paths and data dir when a background build of
    ``scenario`` is pending, else None (remembered until something that
    could change the answer does)."""
    if switched_off() or is_building():
        return None
    paths = BuilderPaths(Path(runs_dir).resolve().parent)
    config = read_config(paths)
    if config is None or paths.runs_dir != Path(runs_dir).resolve():
        return None
    key = (paths.state_dir, scenario)
    signature = _signature(paths, config)
    if _checked.get(key) == signature:
        return None
    data_dir = Path(config["data_dir"])
    if not _pending(paths, data_dir, scenario):
        _checked[key] = signature
        return None
    return paths, data_dir


def _pending(paths: BuilderPaths, data_dir: Path, scenario: str) -> bool:
    """Whether the builder has (or would have) a build of ``scenario`` to do."""
    if daemon_alive(paths):
        status = read_status(paths)
        current = (status.get("current") or {}).get("scenario")
        if scenario == current or scenario in (status.get("queue") or []):
            return True
        # Otherwise check here too: a commit made a moment ago may not have
        # reached the daemon yet (its hook runs in the background).
    from analysis.sim.freshness import code_tree, unchanged_run
    from analysis.sim.provenance import data_stamps

    config = read_config(paths) or {}
    code = config.get("code_dir")
    trees, _reason = main_check(config_branch(config), Path(code) if code else None)
    if trees is None or scenario not in list_scenario_names(data_dir):
        return False
    if scenario in read_disabled_scenarios(data_dir):
        return False  # the builder never builds it, so nothing to wait for
    data = data_stamps(data_dir, scenario)
    if data.dirty:
        return False
    failed = (read_status(paths).get("failed") or {}).get(scenario)
    if failed is not None and failed.get("key") == [list(trees), data.tree_id]:
        return False
    return unchanged_run(data_dir, paths.runs_dir, scenario, code_tree()) is None


def _wait(paths: BuilderPaths, scenario: str, made: float, echo: Callable[[str], None]) -> None:
    echo(f"waiting for the background build of {scenario} (Ctrl-C to stop waiting)...")
    started = time.monotonic()
    last_progress = started
    while True:
        time.sleep(TICK_SECONDS)
        status = read_status(paths)
        alive = daemon_alive(paths)
        if status.get("updated", 0) >= made or not alive:
            if status.get("state") == "disabled":
                echo(f"background builds are off: {status.get('reason')}")
                return
            current = (status.get("current") or {}).get("scenario")
            if scenario != current and scenario not in (status.get("queue") or []):
                failed = (status.get("failed") or {}).get(scenario)
                cancelled = (status.get("cancelled") or {}).get(scenario)
                if failed is not None and failed.get("at", 0) >= made:
                    echo(f"the background build of {scenario} failed: {failed.get('message')}")
                elif cancelled is not None and cancelled.get("at", 0) >= made:
                    echo(f"the background build of {scenario} was cancelled: {cancelled.get('why')}; "
                         "opening its latest run, which is out of date")
                else:
                    echo(f"the background build of {scenario} is done")
                return
        if not alive and time.monotonic() - started > 10:
            echo("the background builder stopped; see sim builder log")
            return
        if time.monotonic() - last_progress >= 30:
            last_progress = time.monotonic()
            echo(f"  still waiting for {scenario}: {_progress(paths, scenario, status)}")


def _progress(paths: BuilderPaths, scenario: str, status: dict[str, Any]) -> str:
    current = status.get("current") or {}
    if current.get("scenario") != scenario:
        queue = status.get("queue") or []
        ahead = queue.index(scenario) if scenario in queue else 0
        building = current.get("scenario")
        return f"queued behind {building}" + (f" and {ahead} more" if ahead else "") \
            if building else "queued"
    batches = sorted((p for p in (paths.runs_dir / scenario).glob("*") if p.is_dir()),
                     key=lambda p: run_name_key(p.name))
    if batches and batches[-1].stat().st_mtime >= current.get("started", 0) - 1:
        done = sum(1 for _ in batches[-1].glob("member_*.sqlite3"))
        return f"building, {done} member run(s) so far"
    return "building"


# -- install ---------------------------------------------------------------------


def _data_hooks_dir(data_repo: Path) -> Path:
    """Where the data repo's git looks for hooks."""
    out = subprocess.run(
        ["git", "-C", str(data_repo), "rev-parse", "--git-path", "hooks"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else (data_repo / path).resolve()


CHAINED_SUFFIX = ".before-builder"
DATA_HOOK = f"""#!/bin/sh
{HOOK_MARKER}
# Runs the hook this one displaced ($0{CHAINED_SUFFIX}), if any, with its
# arguments and input, and exits with its status. Then tells this data
# worktree's paired code worktree (named in sim/.analysis) to look for
# scenarios to rebuild, without blocking or failing the git command.
status=0
if [ -x "$0{CHAINED_SUFFIX}" ]; then "$0{CHAINED_SUFFIX}" "$@"; status=$?; fi
top="$(git rev-parse --show-toplevel 2>/dev/null)" || exit $status
[ -f "$top/state/builder/config.json" ] || exit $status
code="$(cat "$top/sim/.analysis" 2>/dev/null)" || exit $status
[ -x "$code/.venv/bin/sim" ] || exit $status
"$code/.venv/bin/sim" builder notify --quiet --data-dir "$top/sim" >/dev/null 2>&1 &
exit $status
"""


def _ours(hook: Path) -> bool:
    return hook.read_text(errors="replace").split("\n")[1:2] == [HOOK_MARKER]


def install(data_dir: Path, branch: str | None = None) -> list[str]:
    """Opt this workspace in: check the pair, record it and the branch its
    code must match (``branch``, else the one recorded before, else main),
    install the data repo's hooks (the code repo's are in ``.githooks/``).
    Returns what it did."""
    data_dir = data_dir.resolve()
    code = code_dir()
    problems = pair_problems(code, data_dir)
    if problems:
        raise ValueError("; ".join(problems))
    hooks = _data_hooks_dir(data_dir.parent)
    for name in HOOK_NAMES:
        hook, chained = hooks / name, hooks / (name + CHAINED_SUFFIX)
        if hook.exists() and chained.exists() and not _ours(hook):
            # Nowhere to move someone else's hook to: refuse rather than
            # leave one hook unwatched while the pair reads as installed.
            raise ValueError(f"{hook} and {chained.name} both exist and neither is the "
                             f"builder's; merge them into {name}, then install again")
    paths = BuilderPaths.for_data(data_dir)
    branch = branch or config_branch(read_config(paths))
    _write_json(paths.config, {
        "data_dir": str(data_dir), "code_dir": str(code), "branch": branch,
        "installed": time.time(),
    })
    done = [f"recorded the pair in {paths.config}",
            f"builds only from code matching the local {branch} branch"]
    hooks.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        hook = hooks / name
        if hook.exists() and not _ours(hook):
            # Someone else's hook, in any language: ours runs it first, under
            # another name, rather than editing it (an early `exit` in it
            # would skip anything appended).
            chained = hook.with_name(hook.name + CHAINED_SUFFIX)
            hook.rename(chained)
            done.append(f"moved {hook.name} to {chained.name}; the builder's hook runs it first")
        hook.write_text(DATA_HOOK)  # ours, or none: (re)write it
        hook.chmod(0o755)
        done.append(f"installed {hook}")
    return done


def uninstall(data_dir: Path) -> list[str]:
    """Opt this workspace out. The data repo's hooks stay (other data
    worktrees may be opted in); without a config they do nothing."""
    paths = BuilderPaths.for_data(data_dir.resolve())
    if daemon_alive(paths):
        status = read_status(paths)
        pid = status.get("pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    paths.config.unlink(missing_ok=True)
    return [f"removed {paths.config}"]
