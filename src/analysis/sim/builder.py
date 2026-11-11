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

Its work is jobs, a scenario and a step (#454): ``run`` (``sim load``: the
scenario's run, or its Monte Carlo batch), then for an ensemble ``series``
(the batch's series store, when ``sim run`` didn't leave a current one) and
``sensitivity`` (a one-at-a-time batch, ``sim run --sensitivity``). It does
every run first, then every series store, then every sensitivity batch; but
once a reader has asked for one of a scenario's jobs, the rest of that
scenario's follow it at once.

It also fetches prices after each weekday's close, on a branch merged into
the data repo's ``main`` when that is checked out and clean (#560: see
``price_schedule``).

Everything lives in the data repo's ``state/builder/`` (untracked, and one
per data worktree): ``config.json`` (the opt-in and the pair),
``daemon.lock`` (held for the daemon's life), ``status.json``, ``requests/``
(pokes and "build this next"), ``logs/`` (one file a day, #314: see
``builder_log``).
"""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
from analysis.sim.locks import BUILDER_CHILD_ENV, BUILDER_SWITCH_ENV, is_building

__all__ = [
    "STEPS",
    "BuilderPaths",
    "Behind",
    "await_fresh",
    "behind_upstream",
    "build_pending",
    "daemon_alive",
    "job_key",
    "main_check",
    "needed_steps",
    "note_behind",
    "notify",
    "parse_job",
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
# A job's steps, in the order the builder does them (#454).
STEPS = ("run", "series", "sensitivity")
# The steps a reader of each kind of result waits for (loader.latest_*):
# a Monte Carlo batch's readers read its series store too.
READER_STEPS: dict[str | None, tuple[str, ...]] = {
    "monte_carlo": ("run", "series"), "sensitivity": ("sensitivity",), None: STEPS,
}


def job_key(scenario: str, step: str) -> str:
    """How a job is named in the queue and the status: the scenario alone
    for its run, ``<scenario>:<step>`` for the others (#454)."""
    return scenario if step == "run" else f"{scenario}:{step}"


def parse_job(job: str) -> tuple[str, str]:
    """(scenario, step) from :func:`job_key`."""
    scenario, _, step = job.partition(":")
    return scenario, step or "run"


def needed_steps(
    data_dir: Path, runs_dir: Path, scenario: str, code: Any,
) -> list[str]:
    """Which of ``scenario``'s jobs would change something (#454):

    - ``run`` when its latest run or Monte Carlo batch could differ if it ran
      again (``freshness.unchanged_run``);
    - otherwise ``series`` when that is a batch whose series store isn't
      current (``sim run`` writes one; a batch whose members' rows couldn't
      all be gathered has none);
    - ``sensitivity`` for an ensemble that isn't a pool (ADR-009 refuses
      one) when its latest sensitivity batch could differ if it ran again.
      Whatever its Monte Carlo batch's state: a reader of sensitivity
      results waits only on this job, so a stale sensitivity batch must
      always have one.
    """
    from analysis.sim import freshness
    from analysis.sim.scenario import ensemble_settings
    from analysis.sim.series_db import series_db_current

    steps: list[str] = []
    fresh = freshness.unchanged_run(data_dir, runs_dir, scenario, code)
    if fresh is None:
        steps.append("run")
    elif fresh.path.is_dir() and not series_db_current(fresh.path):
        steps.append("series")
    if not is_pool(data_dir / "scenarios", scenario) \
            and ensemble_settings(data_dir, scenario)[1] is not None \
            and freshness.unchanged_run(data_dir, runs_dir, scenario, code,
                                        mode="sensitivity") is None:
        steps.append("sensitivity")
    return steps


def step_command(data_dir: Path, scenario: str, step: str) -> list[str]:
    """The ``sim`` arguments that do one job."""
    where = ["--data-dir", str(data_dir)]
    if step == "series":
        return ["builder", "series", scenario, *where]
    if step == "sensitivity":
        return ["run", scenario, "--sensitivity", *_sensitivity_options(data_dir, scenario), *where]
    return ["load", scenario, *where]


def _sensitivity_options(data_dir: Path, scenario: str) -> list[str]:
    """A sensitivity batch made again keeps the quantiles of the latest one
    (a response-curve ladder, say); the first gets the defaults."""
    from analysis.sim.ensemble import read_batch_manifest
    from analysis.sim.loader import latest_ensemble_batch

    runs = BuilderPaths.for_data(data_dir).runs_dir
    batch = latest_ensemble_batch(runs / scenario, mode="sensitivity", wait=False)
    try:
        quantiles = (read_batch_manifest(batch) or {}).get("quantiles") if batch else None
    except (OSError, ValueError):
        quantiles = None
    if not quantiles:
        return []
    return ["--quantiles", ",".join(repr(float(q)) for q in quantiles)]


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
    def holds(self) -> Path:
        """Scenarios a command is building itself (#308), a file per process."""
        return self.dir / "holds"

    @property
    def log_dir(self) -> Path:
        return self.dir / "logs"

    @property
    def prices_worktree(self) -> Path:
        """The sparse worktree scheduled price fetches work in (#560)."""
        return self.dir / "prices"

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


@dataclass(frozen=True)
class Behind:
    """Local ``branch`` lacks code commits its upstream had at the last fetch."""

    branch: str
    upstream: str
    commits: int  # upstream commits not on the branch that touch the code paths
    fetched: float | None  # when the repo last fetched (FETCH_HEAD's mtime), if known

    def describe(self) -> str:
        from analysis.sim.freshness import CODE_PATHS

        when = "" if self.fetched is None else f", as of the last fetch {_ago(time.time() - self.fetched)}"
        plural = "" if self.commits == 1 else "s"
        return (f"local {self.branch} is {self.commits} commit{plural} behind {self.upstream} "
                f"in {', '.join(CODE_PATHS)}{when}")


def behind_upstream(branch: str = DEFAULT_BRANCH, repo_dir: Path | None = None) -> Behind | None:
    """How far local ``branch`` is behind its upstream (its tracking branch,
    else ``origin/<branch>``) as last fetched, counting only commits that
    touch the code paths, since the builder works by code tree (#306).
    None when it isn't behind or there is nothing to compare with. Never
    fetches: a background fetch already moves the upstream, and the point
    is to say what the user hasn't pulled."""
    from git import GitCommandError

    from analysis.sim.freshness import CODE_PATHS, code_tree
    from analysis.sim.provenance import _open_repo

    repo = code_tree().repo if repo_dir is None else _open_repo(repo_dir)
    if repo is None or branch not in repo.heads:
        return None
    tracking = repo.heads[branch].tracking_branch()
    upstream = tracking.name if tracking is not None else f"origin/{branch}"
    try:
        count = int(repo.git.rev_list("--count", f"refs/heads/{branch}..{upstream}", "--", *CODE_PATHS))
    except (GitCommandError, ValueError):
        return None  # no such upstream (never fetched, or no remote)
    if count == 0:
        return None
    fetched = None
    for git_dir in (repo.git_dir, repo.common_dir):
        with contextlib.suppress(OSError):
            fetched = (Path(git_dir) / "FETCH_HEAD").stat().st_mtime
            break
    return Behind(branch, upstream, count, fetched)


def _ago(seconds: float) -> str:
    """``just now``, ``5 minutes ago``, ``3 hours ago``, ``2 days ago``."""
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


# Workspaces (state dirs) this process has already noted are behind.
_noted_behind: set[Path] = set()


def note_behind(state_dir: Path, echo: Callable[[str], None] | None = None) -> None:
    """Once per process and workspace, say so on stderr when results here
    are built from a local branch behind its upstream (#306): where the
    builder is installed and on, and the code running is that branch's, so
    what is built and read is that code. Quiet otherwise; never fetches."""
    paths = BuilderPaths(Path(state_dir).resolve())
    # The builder's own builds would repeat it into its log at every build.
    if paths.state_dir in _noted_behind or switched_off() or os.environ.get(BUILDER_CHILD_ENV):
        return
    config = read_config(paths)
    if config is None:
        return
    _noted_behind.add(paths.state_dir)
    code = config.get("code_dir")
    branch = config_branch(config)
    repo_dir = Path(code) if code else None
    if main_check(branch, repo_dir)[0] is None:
        return  # the builder is off; `sim builder status` says why
    behind = behind_upstream(branch, repo_dir)
    if behind is None:
        return
    echo = echo or (lambda text: print(text, file=sys.stderr))
    echo(f"NOTE: {behind.describe()}; runs here use the older code until you pull "
         f"(git pull, on {branch})")


# -- requests ----------------------------------------------------------------


def _request(paths: BuilderPaths, kind: str, scenario: str | None = None,
             steps: Sequence[str] | None = None) -> float:
    """Leave a request for the daemon; returns when it was made. A
    ``first`` request names the scenario's ``steps`` a reader waits on."""
    paths.requests.mkdir(parents=True, exist_ok=True)
    made = time.time()
    body = {"kind": kind, "scenario": scenario, "steps": list(steps or ()), "made": made}
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


# -- holds -------------------------------------------------------------------------

# This process's hold file, once it has one, and what it holds.
_holding: dict[Path, set[str]] = {}


def hold(data_dir: Path, scenarios: Sequence[str]) -> None:
    """Keep the background builder off ``scenarios`` while this process
    lives (#308). A command that commits their data and then builds them
    would otherwise race the build that the commit's hook starts, and one
    of the two would build them again; a build of one already running is
    stopped. Does nothing where the builder isn't installed."""
    if not scenarios:
        return
    paths = BuilderPaths.for_data(data_dir)
    if read_config(paths) is None:
        return
    path = paths.holds / f"{os.getpid()}.json"
    if path not in _holding:
        _holding[path] = set()
        atexit.register(path.unlink, missing_ok=True)
    _holding[path].update(scenarios)
    _write_json(path, {"pid": os.getpid(), "scenarios": sorted(_holding[path])})


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's, but there
    return True


def held(paths: BuilderPaths) -> dict[str, int]:
    """Each held scenario and the pid of the command holding it. A hold
    whose process has gone (killed, so its exit handler never ran) is
    removed."""
    out: dict[str, int] = {}
    if not paths.holds.is_dir():
        return out
    for path in paths.holds.glob("*.json"):
        try:
            body = json.loads(path.read_text())
            pid = int(body["pid"])
            names = [str(name) for name in body["scenarios"]]
        except (OSError, ValueError, KeyError, TypeError):
            continue  # being written; the next look sees it whole
        if not _alive(pid):
            path.unlink(missing_ok=True)
            continue
        out.update(dict.fromkeys(names, pid))
    return out


# -- the daemon ----------------------------------------------------------------


@dataclass
class _Build:
    job: str
    key: list[Any]
    process: subprocess.Popen[bytes]
    started: float
    wanted: bool = False  # someone is waiting for it: never preempted
    reader: threading.Thread | None = None  # copies its output into the log

    @property
    def scenario(self) -> str:
        return parse_job(self.job)[0]

    @property
    def step(self) -> str:
        return parse_job(self.job)[1]

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
        # Jobs to do (job_key), in order.
        self.queue: list[str] = []
        # Jobs a reader is waiting on, most recently asked first.
        self.first: list[str] = []
        # Scenarios a reader asked for whose other jobs come next (#454).
        self.follow: list[str] = []
        # Builds preempted for a scenario someone is waiting on: next in line.
        self.resume: list[str] = []
        self.waiting: dict[str, str] = {}
        # Scenarios a command is building itself (hold()), and its pid.
        self.held: dict[str, int] = {}
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
        # A scheduled price fetch in progress (#560), its log copier, when
        # prices were last looked at, and when a fetch last ended: none
        # starts within the hour after one, whatever its child recorded.
        self.fetcher: subprocess.Popen[bytes] | None = None
        self.fetch_reader: threading.Thread | None = None
        self.prices_looked = float("-inf")
        self.fetch_ended = float("-inf")

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
            current = {"scenario": self.build.scenario, "step": self.build.step,
                       "job": self.build.job, "pid": self.build.process.pid,
                       "started": self.build.started}
        _write_json(self.paths.status, {
            "pid": os.getpid(), "state": self.state, "reason": self.reason,
            "current": current, "queue": self.queue, "waiting": self.waiting,
            "failed": self.failed, "cancelled": self.cancelled, "held": self.held,
            "updated": time.time(),
        })

    def scan(self) -> None:
        """Recompute the queue from the trees as they are now."""
        from analysis.sim.freshness import code_tree
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
        queue: list[str] = []
        waiting: dict[str, str] = {}
        disabled = read_disabled_scenarios(self.data_dir)
        self.held = held(self.paths)
        for name in list_scenario_names(self.data_dir):
            if name in disabled:  # only built when named
                continue
            if name in self.held:
                waiting[name] = f"being built by pid {self.held[name]}"
                continue
            data = data_stamps(self.data_dir, name)
            if data.dirty:
                waiting[name] = "data dirty"
                continue
            for step in needed_steps(self.data_dir, self.paths.runs_dir, name, code):
                job = job_key(name, step)
                failed = self.failed.get(job)
                if failed is not None and failed.get("key") == [list(trees), data.tree_id]:
                    continue
                queue.append(job)
        self.waiting = waiting

        def why_dropped(name: str) -> str:
            if name in disabled:
                return "it was disabled"
            if name in self.held:
                return f"a command (pid {self.held[name]}) is building it"
            if name in waiting:
                return "its data has uncommitted changes"
            return "its inputs changed"

        # Queued builds no longer to be done: a reader waiting on one must
        # hear that it was cancelled, not that it is done.
        for job in set(self.queue) - set(queue):
            name = parse_job(job)[0]
            if name in disabled or name in waiting:
                self.cancelled[job] = {"at": time.time(), "why": why_dropped(name)}
        if self.build is not None:
            name = self.build.scenario
            if name in disabled or name in self.held or self.build.key != self.key(name, trees):
                why = why_dropped(name)
                job = self.build.job
                self.cancel(f"{job}: {why}")
                self.cancelled[job] = {"at": time.time(), "why": why}
            if self.build is not None and self.build.job in queue:
                queue.remove(self.build.job)
        building = {self.build.scenario} if self.build is not None else set()
        self.follow = [n for n in self.follow
                       if n in building or any(parse_job(j)[0] == n for j in queue)]
        # A reader asks for every step it reads, needed yet or not (a run
        # can leave a series store to build): such a job stays wanted while
        # its scenario has anything queued or building.
        self.first = [j for j in self.first if parse_job(j)[0] in building
                      or any(parse_job(q)[0] == parse_job(j)[0] for q in queue)]
        self.queue = self.order(queue)
        self.state = "building" if self.build is not None or self.queue else "idle"

    def order(self, queue: list[str]) -> list[str]:
        """Jobs someone is waiting on, then the rest of those scenarios'
        jobs (#454), then builds they preempted; then every run before every
        series store before every sensitivity batch (#454); within a step,
        the working set's scenarios, most recently used first (#322), then
        plain runs before ensembles (quick results first), then pools, which
        reuse their components' runs (ADR-009), then by name."""
        first = [j for j in self.first if j in queue]
        resume = [j for j in self.resume if j in queue]

        def _series_pos(name: str, step: str) -> int:
            # A series store found once a waited-on run ends goes with the
            # scenario's waited-on jobs, ahead of its sensitivity batch (Bob).
            if step == "series":
                mine = [i for i, j in enumerate(first) if parse_job(j)[0] == name]
                if mine:
                    return mine[0]
            return len(first)

        used = working_set_order(self.data_dir)
        runs = self.paths.runs_dir
        scenarios = self.data_dir / "scenarios"

        def rank(job: str) -> tuple[int, int, int, int, int, int, str]:
            name, step = parse_job(job)
            pos = first.index(job) if job in first else _series_pos(name, step)
            follow = self.follow.index(name) if name in self.follow else len(self.follow)
            again = resume.index(job) if job in resume else len(resume)
            recent = used.get(name, len(used))
            ensemble = 2 if is_pool(scenarios, name) else int((runs / name).is_dir())
            return (pos, follow, again, STEPS.index(step), recent, ensemble, job)

        return sorted(queue, key=rank)

    def preempt(self) -> None:
        """Stop a build nobody is waiting on when someone is waiting on
        another job (Bob, #300): the wanted one starts at once and the
        stopped one is requeued right after it."""
        build = self.build
        if build is None:
            return
        if build.wanted or build.job in self.first or self._goes_first(build):  # asked for
            build.wanted = True
            self.first = [j for j in self.first if j != build.job]
            self._follow(build.scenario)
            return
        wanted = next((j for j in self.first if j in self.queue), None)
        if wanted is None:
            return
        self.cancel(f"{wanted} is wanted now")
        self.resume = [build.job, *[j for j in self.resume if j != build.job]]
        self.queue = self.order([*self.queue, build.job])

    def _goes_first(self, build: _Build) -> bool:
        """A series store of a scenario someone waits on: it comes before
        that scenario's sensitivity batch, so that never preempts it."""
        return build.step == "series" and any(
            parse_job(j)[0] == build.scenario for j in self.first)

    def _follow(self, scenario: str) -> None:
        """A reader asked for one of ``scenario``'s jobs: its others follow."""
        self.follow = [scenario, *[n for n in self.follow if n != scenario]]

    def cancel(self, why: str) -> None:
        build = self.build
        if build is None:
            return
        _kill(build.process)
        build.drain()  # the rest of its output stays under its `building` record
        self.log(f"cancelled {build.job}: {why}")
        self.build = None

    def start_next(self, trees: tuple[str, ...]) -> None:
        job = self.queue.pop(0)
        name, step = parse_job(job)
        wanted = job in self.first
        self.first = [j for j in self.first if j != job]
        self.resume = [j for j in self.resume if j != job]
        if wanted:
            self._follow(name)
        key = self.key(name, trees)
        env = {**os.environ, BUILDER_SWITCH_ENV: "off", BUILDER_CHILD_ENV: "1"}
        self.log(f"building {job}")
        self.paths.dir.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [*_sim_executable(), *step_command(self.data_dir, name, step)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, env=env, close_fds=True,
        )
        assert process.stdout is not None
        # A thread, so a chatty build never blocks on a full pipe while the
        # loop sleeps.
        reader = threading.Thread(target=self.writer.copy, args=(process.stdout,),
                                  name=f"log {job}", daemon=True)
        reader.start()
        self.build = _Build(job, key, process, time.time(), wanted, reader)
        self.cancelled.pop(job, None)
        self.state = "building"

    def prices_tick(self, now: bool) -> None:
        """#560: note a price fetch that ended; merge fetched prices that
        wait for main, between builds; start a fetch when one is due. Looks on a rescan, or
        once a minute, so a timer's poke or a commit is answered at once."""
        from analysis.sim import price_schedule

        if self.fetcher is not None:
            if self.fetcher.poll() is None:
                return
            if self.fetch_reader is not None:
                self.fetch_reader.join(timeout=5)
            code = self.fetcher.returncode
            self.fetcher = self.fetch_reader = None
            self.fetch_ended = time.monotonic()
            self.log("fetched prices" if code == 0 else f"price fetch failed (exit {code})")
            now = True
        if not now and time.monotonic() - self.prices_looked < 60:
            return
        self.prices_looked = time.monotonic()
        if not price_schedule.enabled(read_config(self.paths)):
            return
        # Between builds only: a merge rewrites price files a build may be
        # reading, and changes the data version it records.
        if self.build is None and not self.held:
            try:
                merged = price_schedule.merge_pending(self.data_dir, self.paths.prices_worktree)
            except (price_schedule.PriceScheduleError, OSError) as exc:
                self.log(f"prices not merged: {exc}")
                return
            if merged:
                self.log(merged)
        if time.monotonic() - self.fetch_ended < price_schedule.RETRY_SECONDS:
            return
        if not price_schedule.due(price_schedule.read_state(self.data_dir), datetime.now(UTC)):
            return
        self.log("fetching prices")
        env = {**os.environ, BUILDER_SWITCH_ENV: "off", BUILDER_CHILD_ENV: "1"}
        self.fetcher = subprocess.Popen(
            [*_sim_executable(), "builder", "prices", "--if-due", "--data-dir", str(self.data_dir)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, env=env, close_fds=True,
        )
        assert self.fetcher.stdout is not None
        self.fetch_reader = threading.Thread(target=self.writer.copy, args=(self.fetcher.stdout,),
                                             name="log prices", daemon=True)
        self.fetch_reader.start()

    def finished(self) -> bool:
        """Note a build that ended; True if one did."""
        build = self.build
        if build is None or build.process.poll() is None:
            return False
        self.build = None
        build.drain()
        from analysis.sim.freshness import code_tree

        code = build.process.returncode
        took = _took(time.time() - build.started)
        still_stale = code == 0 and build.step in needed_steps(
            self.data_dir, self.paths.runs_dir, build.scenario, code_tree())
        if code == 0 and not still_stale:
            self.failed.pop(build.job, None)
            self.log(f"built {build.job} ({took})")
        elif still_stale:
            # An ensemble with failed members: `sim load` exits 0, but the
            # batch is incomplete, so it would be queued again at once.
            self.failed[build.job] = {
                "key": build.key, "exit": 0, "at": time.time(),
                "message": "built, but not every member succeeded; see sim builder log",
            }
            self.log(f"{build.job} built incomplete (failed members, {took})")
        else:
            self.failed[build.job] = {
                "key": build.key, "exit": code, "at": time.time(),
                "message": f"exit {code}; see sim builder log",
            }
            self.log(f"{build.job} failed (exit {code}, {took})")
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
                        jobs = [job_key(name, step) for step in request.get("steps") or ("run",)
                                if step in STEPS]
                        self.first = [*jobs, *[j for j in self.first if j not in jobs]]
                if self.finished():
                    rescan = True
                if held(self.paths) != self.held:  # one taken, or its command ended
                    rescan = True
                looked_again = rescan
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
                self.prices_tick(looked_again)
                if self.build is None and not self.queue and self.fetcher is None:
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

# What a reader last found, by (state dir, scenario, steps): the signature it was
# found under (see _signature), so it is asked again only once something
# that could change the answer has.
_checked: dict[tuple[Path, str, tuple[str, ...]], tuple[int, ...]] = {}
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
    *, mode: str | None = "monte_carlo",
) -> None:
    """Before a scenario's results of ``mode`` are opened: if a background
    build of them is pending, move it to the front and wait for it (#454:
    a Monte Carlo batch's run and series store, a sensitivity batch, or with
    ``mode`` None, all of the scenario's jobs). Does nothing where the
    builder isn't installed, is off or disabled, or the results are current.
    Ctrl-C gives up the wait (the latest existing results are opened)."""
    steps = READER_STEPS[mode]
    note_behind(Path(runs_dir).resolve().parent, echo)
    found = _pending_build(runs_dir, scenario, steps)
    if found is None:
        return
    paths, data_dir, jobs = found
    echo = echo or (lambda text: print(text, file=sys.stderr))
    # Every step it reads, not only those pending now: one a pending job
    # turns up (a series store a run needs) is then waited on, so no other
    # reader's request preempts it.
    made = _request(paths, "first", scenario, list(steps))
    if not daemon_alive(paths):
        spawn_daemon(paths, data_dir)
    try:
        _wait(paths, scenario, jobs, made, echo, steps)
    except KeyboardInterrupt:
        echo(f"gave up waiting for {scenario}; opening its latest results, which may be out of date")


def build_pending(runs_dir: Path, scenario: str, mode: str | None = "monte_carlo") -> bool:
    """Whether opening ``scenario``'s results of ``mode`` would wait for a
    background build (:func:`await_fresh`), asked without waiting or moving
    it up the queue."""
    return _pending_build(runs_dir, scenario, READER_STEPS[mode]) is not None


def _pending_build(
    runs_dir: Path, scenario: str, steps: Sequence[str] = STEPS,
) -> tuple[BuilderPaths, Path, list[str]] | None:
    """The workspace's paths, its data dir and the jobs of ``scenario``'s
    ``steps`` a background build has pending, else None (remembered until
    something that could change the answer does)."""
    if switched_off() or is_building():
        return None
    paths = BuilderPaths(Path(runs_dir).resolve().parent)
    config = read_config(paths)
    if config is None or paths.runs_dir != Path(runs_dir).resolve():
        return None
    key = (paths.state_dir, scenario, tuple(steps))
    signature = _signature(paths, config)
    if _checked.get(key) == signature:
        return None
    data_dir = Path(config["data_dir"])
    jobs = _pending(paths, data_dir, scenario, steps)
    if not jobs:
        _checked[key] = signature
        return None
    return paths, data_dir, jobs


def _pending(
    paths: BuilderPaths, data_dir: Path, scenario: str, steps: Sequence[str] = STEPS,
) -> list[str]:
    """The jobs of ``scenario``'s ``steps`` the builder has (or would have)
    to do."""
    if scenario in held(paths):
        return []  # a command is building it; its build lock is what to wait on
    wanted = [job_key(scenario, step) for step in steps]
    if daemon_alive(paths):
        status = read_status(paths)
        current = (status.get("current") or {}).get("job") \
            or (status.get("current") or {}).get("scenario")
        queued = [j for j in wanted if j == current or j in (status.get("queue") or [])]
        if queued:
            return queued
        # Otherwise check here too: a commit made a moment ago may not have
        # reached the daemon yet (its hook runs in the background).
    from analysis.sim.freshness import code_tree
    from analysis.sim.provenance import data_stamps

    config = read_config(paths) or {}
    code = config.get("code_dir")
    trees, _reason = main_check(config_branch(config), Path(code) if code else None)
    if trees is None or scenario not in list_scenario_names(data_dir):
        return []
    if scenario in read_disabled_scenarios(data_dir):
        return []  # the builder never builds it, so nothing to wait for
    data = data_stamps(data_dir, scenario)
    if data.dirty:
        return []
    failed = read_status(paths).get("failed") or {}
    out = []
    for step in needed_steps(data_dir, paths.runs_dir, scenario, code_tree()):
        job = job_key(scenario, step)
        if step not in steps:
            continue
        if (failed.get(job) or {}).get("key") == [list(trees), data.tree_id]:
            continue
        out.append(job)
    return out


def _wait(paths: BuilderPaths, scenario: str, jobs: Sequence[str], made: float,
          echo: Callable[[str], None], steps: Sequence[str] = STEPS) -> None:
    """Wait until ``jobs`` and any of ``scenario``'s ``steps`` the builder
    queues meanwhile are done: a run's batch can need a series store or a
    first sensitivity batch the reader couldn't see pending when it came."""
    echo(f"waiting for the background build of {_describe(jobs)} (Ctrl-C to stop waiting)...")
    started = time.monotonic()
    last_progress = started
    left = list(jobs)
    while True:
        time.sleep(TICK_SECONDS)
        status = read_status(paths)
        alive = daemon_alive(paths)
        if status.get("updated", 0) >= made or not alive:
            if status.get("state") == "disabled":
                echo(f"background builds are off: {status.get('reason')}")
                return
            current = (status.get("current") or {}).get("job") \
                or (status.get("current") or {}).get("scenario")
            for job in [current, *(status.get("queue") or [])]:
                if job and job not in left and job not in jobs:
                    name, step = parse_job(job)
                    if name == scenario and step in steps:
                        left.append(job)
            jobs = [*jobs, *[j for j in left if j not in jobs]]
            for job in [j for j in left if j != current and j not in (status.get("queue") or [])]:
                left.remove(job)
                failed = (status.get("failed") or {}).get(job)
                cancelled = (status.get("cancelled") or {}).get(job)
                if failed is not None and failed.get("at", 0) >= made:
                    echo(f"the background build of {_describe([job])} failed: {failed.get('message')}")
                elif cancelled is not None and cancelled.get("at", 0) >= made:
                    echo(f"the background build of {_describe([job])} was cancelled: "
                         f"{cancelled.get('why')}; opening its latest results, which are out of date")
                else:
                    echo(f"the background build of {_describe([job])} is done")
            if not left:
                return
        if not alive and time.monotonic() - started > 10:
            echo("the background builder stopped; see sim builder log")
            return
        if time.monotonic() - last_progress >= 30:
            last_progress = time.monotonic()
            echo(f"  still waiting for {_describe(left)}: {_progress(paths, left[0], status)}")


def _describe(jobs: Sequence[str]) -> str:
    """``base``; ``ens (series store)``; ``ens (run, sensitivity batch)``."""
    words = {"run": "run", "series": "series store", "sensitivity": "sensitivity batch"}
    scenario = parse_job(jobs[0])[0]
    steps = [parse_job(j)[1] for j in jobs]
    if steps == ["run"]:
        return scenario
    return f"{scenario} ({', '.join(words[s] for s in steps)})"


def _progress(paths: BuilderPaths, job: str, status: dict[str, Any]) -> str:
    from analysis.sim.loader import ensemble_batches

    current = status.get("current") or {}
    if (current.get("job") or current.get("scenario")) != job:
        queue = status.get("queue") or []
        ahead = queue.index(job) if job in queue else 0
        building = current.get("job") or current.get("scenario")
        return f"queued behind {building}" + (f" and {ahead} more" if ahead else "") \
            if building else "queued"
    scenario, step = parse_job(job)
    if step == "series":
        return "building the series store"
    mode = "sensitivity" if step == "sensitivity" else "monte_carlo"
    batches = ensemble_batches(paths.runs_dir / scenario, mode)
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


def install(data_dir: Path, branch: str | None = None, prices: bool | None = None) -> list[str]:
    """Opt this workspace in: check the pair, record it, the branch its
    code must match (``branch``, else the one recorded before, else main)
    and whether it fetches prices (``prices``, else as recorded, else yes:
    #560), install the data repo's hooks (the code repo's are in
    ``.githooks/``). Returns what it did. The price timer is the CLI's
    (``price_schedule.install_timer``)."""
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
    before = read_config(paths) or {}
    branch = branch or config_branch(before)
    prices = bool(before.get("prices", True)) if prices is None else prices
    _write_json(paths.config, {
        "data_dir": str(data_dir), "code_dir": str(code), "branch": branch,
        "prices": prices, "installed": time.time(),
    })
    done = [f"recorded the pair in {paths.config}",
            f"builds only from code matching the local {branch} branch",
            "fetches prices after each weekday's close" if prices else "doesn't fetch prices"]
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
    done = [f"removed {paths.config}"]
    if paths.prices_worktree.exists():
        # The price branch stays: it holds nothing main lacks once merged.
        subprocess.run(["git", "-C", str(data_dir), "worktree", "remove", "--force",
                        str(paths.prices_worktree)], capture_output=True, check=False)
        done.append(f"removed the price worktree {paths.prices_worktree}")
    return done
