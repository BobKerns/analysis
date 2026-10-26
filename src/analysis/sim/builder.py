"""#300: building scenarios in the background.

One daemon per workspace pair (a code worktree and the data repo it links,
each naming the other) keeps every scenario's runs current. Git hooks in both
repos poke it on a commit, checkout, merge or rewrite; each poke rebuilds its
queue from what ``freshness.unchanged_run`` says a rerun could change, so it
works by tree, not by commit. Code that opens a scenario's run waits for a
pending build of it (:func:`await_fresh`), moving it to the front.

Rules (Bob, #300):

- It builds only when the code paths (``freshness.CODE_PATHS``) are committed
  and match the local ``main`` branch. Otherwise it is disabled: it builds
  nothing and nobody waits. (Local ``main``, not ``origin/main``, so a
  background fetch, or choosing not to pull, changes nothing.)
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
(pokes and "build this next"), ``builder.log``.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analysis.sim.loader import (
    DEFAULT_INTENT_DIR,
    DEFAULT_RUNS_DIR,
    DEFAULT_STATE_DIR,
    find_state_dir,
    list_scenario_names,
    read_current_scenario,
)
from analysis.sim.locks import BUILDER_CHILD_ENV, BUILDER_SWITCH_ENV

__all__ = [
    "BuilderPaths",
    "await_fresh",
    "daemon_alive",
    "main_check",
    "notify",
    "pair_problems",
    "read_status",
    "run_daemon",
    "switched_off",
]

# The branch the code must match; "main" unless set (a mainline named
# otherwise, or a test).
MAIN_BRANCH_ENV = "ANALYSIS_BUILDER_MAIN"
# How long the daemon lingers with an empty queue before it exits.
IDLE_GRACE_SECONDS = 120.0
# How often the daemon looks for requests and its build finishing.
TICK_SECONDS = 0.5
# A reader that just found a scenario current doesn't ask again this soon.
_RECHECK_SECONDS = 5.0
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
    def log(self) -> Path:
        return self.dir / "builder.log"

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


def main_check(repo_dir: Path | None = None) -> tuple[tuple[str, ...] | None, str | None]:
    """The code trees to build with, or None and why the builder is off:
    the code paths must be committed and match the local ``main``."""
    from analysis.sim.freshness import CODE_PATHS, _trees, code_tree, code_tree_of
    from analysis.sim.provenance import _open_repo

    code = code_tree() if repo_dir is None else code_tree_of(_open_repo(repo_dir))
    paths = ", ".join(CODE_PATHS)
    if code.repo is None:
        return None, "the code isn't in a git repo"
    if code.trees is None:
        return None, f"uncommitted code in {paths}"
    branch = os.environ.get(MAIN_BRANCH_ENV) or "main"
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
    with open(paths.log, "ab") as log:
        subprocess.Popen(
            [*_sim_executable(), "builder", "run", "--data-dir", str(data_dir)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
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


class _Daemon:
    def __init__(self, data_dir: Path, echo: Callable[[str], None]) -> None:
        self.data_dir = data_dir.resolve()
        self.paths = BuilderPaths.for_data(self.data_dir)
        self.echo = echo
        self.queue: list[str] = []
        self.first: list[str] = []
        self.waiting: dict[str, str] = {}
        self.status = read_status(self.paths)
        self.failed: dict[str, dict[str, Any]] = dict(self.status.get("failed") or {})
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
        self.echo(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}")

    def write_status(self) -> None:
        current = None
        if self.build is not None:
            current = {"scenario": self.build.scenario, "pid": self.build.process.pid,
                       "started": self.build.started}
        _write_json(self.paths.status, {
            "pid": os.getpid(), "state": self.state, "reason": self.reason,
            "current": current, "queue": self.queue, "waiting": self.waiting,
            "failed": self.failed, "updated": time.time(),
        })

    def scan(self) -> None:
        """Recompute the queue from the trees as they are now."""
        from analysis.sim.freshness import code_tree, unchanged_run
        from analysis.sim.provenance import data_stamps

        trees, reason = main_check()
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
        for name in list_scenario_names(self.data_dir):
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
        if self.build is not None:
            if self.build.key != self.key(self.build.scenario, trees):
                self.cancel(f"{self.build.scenario}: its inputs changed")
            if self.build is not None and self.build.scenario in queue:
                queue.remove(self.build.scenario)
        self.queue = self.order(queue)
        self.state = "building" if self.build is not None or self.queue else "idle"

    def order(self, queue: list[str]) -> list[str]:
        """Scenarios someone is waiting on, then the current scenario, then
        plain runs before ensembles (quick results first), then by name."""
        current = read_current_scenario(self.data_dir)
        first = [n for n in self.first if n in queue]
        runs = self.paths.runs_dir

        def rank(name: str) -> tuple[int, int, str]:
            pos = first.index(name) if name in first else len(first)
            ensemble = (runs / name).is_dir()
            return (pos, 0 if name == current else 1 + ensemble, name)

        return sorted(queue, key=rank)

    def cancel(self, why: str) -> None:
        build = self.build
        if build is None:
            return
        self.log(f"cancelling {build.scenario}: {why}")
        _kill(build.process)
        self.build = None

    def start_next(self, trees: tuple[str, ...]) -> None:
        name = self.queue.pop(0)
        self.first = [n for n in self.first if n != name]
        key = self.key(name, trees)
        env = {**os.environ, BUILDER_SWITCH_ENV: "off", BUILDER_CHILD_ENV: "1"}
        self.log(f"building {name}")
        self.paths.dir.mkdir(parents=True, exist_ok=True)
        with open(self.paths.log, "ab") as log:
            process = subprocess.Popen(
                [*_sim_executable(), "load", name, "--data-dir", str(self.data_dir)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, env=env, close_fds=True,
            )
        self.build = _Build(name, key, process, time.time())
        self.state = "building"

    def finished(self) -> bool:
        """Note a build that ended; True if one did."""
        build = self.build
        if build is None or build.process.poll() is None:
            return False
        self.build = None
        code = build.process.returncode
        if code == 0:
            self.failed.pop(build.scenario, None)
            self.log(f"built {build.scenario}")
        else:
            self.failed[build.scenario] = {
                "key": build.key, "exit": code, "at": time.time(),
                "message": f"exit {code}; see {self.paths.log}",
            }
            self.log(f"{build.scenario} failed (exit {code})")
        return True

    def run(self) -> int:
        self.paths.dir.mkdir(parents=True, exist_ok=True)
        lock = os.open(self.paths.daemon_lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock)
            return 0  # one is already running; it has the request
        _trim_log(self.paths.log)
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


def _trim_log(log: Path, limit: int = 1 << 20) -> None:
    """Keep the log's last quarter once it passes ``limit`` bytes. Each
    daemon trims it as it starts; builds append to it."""
    try:
        if log.stat().st_size <= limit:
            return
        with open(log, "rb") as f:
            f.seek(-limit // 4, os.SEEK_END)
            tail = f.read()
    except OSError:
        return
    log.write_bytes(tail[tail.find(b"\n") + 1:])


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

_checked: dict[tuple[Path, str], float] = {}


def await_fresh(
    runs_dir: Path, scenario: str, echo: Callable[[str], None] | None = None,
) -> None:
    """Before a scenario's run is opened: if a background build of it is
    pending, move it to the front and wait for it. Does nothing where the
    builder isn't installed, is off or disabled, or the run is current.
    Ctrl-C gives up the wait (the latest existing run is opened)."""
    if switched_off():
        return
    paths = BuilderPaths(Path(runs_dir).resolve().parent)
    config = read_config(paths)
    if config is None or paths.runs_dir != Path(runs_dir).resolve():
        return
    key = (paths.state_dir, scenario)
    if time.monotonic() - _checked.get(key, -_RECHECK_SECONDS) < _RECHECK_SECONDS:
        return
    echo = echo or (lambda text: print(text, file=sys.stderr))
    data_dir = Path(config["data_dir"])
    if not _pending(paths, data_dir, scenario):
        _checked[key] = time.monotonic()
        return
    made = _request(paths, "first", scenario)
    if not daemon_alive(paths):
        spawn_daemon(paths, data_dir)
    try:
        _wait(paths, scenario, made, echo)
    except KeyboardInterrupt:
        echo(f"gave up waiting for {scenario}; opening its latest run, which may be out of date")
    _checked[key] = time.monotonic()


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

    trees, _reason = main_check()
    if trees is None or scenario not in list_scenario_names(data_dir):
        return False
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
                if failed is not None and failed.get("at", 0) >= made:
                    echo(f"the background build of {scenario} failed: {failed.get('message')}")
                else:
                    echo(f"the background build of {scenario} is done")
                return
        if not alive and time.monotonic() - started > 10:
            echo(f"the background builder stopped; see {paths.log}")
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
    batches = sorted(p for p in (paths.runs_dir / scenario).glob("*") if p.is_dir())
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


DATA_HOOK = f"""#!/bin/sh
{HOOK_MARKER}
# Tells this data worktree's paired code worktree (named in sim/.analysis)
# to look for scenarios to rebuild. Never blocks or fails the git command.
top="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -f "$top/state/builder/config.json" ] || exit 0
code="$(cat "$top/sim/.analysis" 2>/dev/null)" || exit 0
[ -x "$code/.venv/bin/sim" ] || exit 0
"$code/.venv/bin/sim" builder notify --quiet --data-dir "$top/sim" >/dev/null 2>&1 &
exit 0
"""


def install(data_dir: Path) -> list[str]:
    """Opt this workspace in: check the pair, record it, install the data
    repo's hooks (the code repo's are in ``.githooks/``). Returns what it did."""
    data_dir = data_dir.resolve()
    code = code_dir()
    problems = pair_problems(code, data_dir)
    if problems:
        raise ValueError("; ".join(problems))
    paths = BuilderPaths.for_data(data_dir)
    _write_json(paths.config, {
        "data_dir": str(data_dir), "code_dir": str(code), "installed": time.time(),
    })
    done = [f"recorded the pair in {paths.config}"]
    hooks = _data_hooks_dir(data_dir.parent)
    hooks.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        hook = hooks / name
        text = hook.read_text(errors="replace") if hook.exists() else None
        if text is None or text.split("\n")[1:2] == [HOOK_MARKER]:
            hook.write_text(DATA_HOOK)  # ours, or none: (re)write it
            hook.chmod(0o755)
            done.append(f"installed {hook}")
        elif HOOK_MARKER not in text:
            # Someone else's hook: run ours after it, in a subshell so its
            # exits don't end theirs, rather than replace it.
            body = DATA_HOOK.split("\n", 1)[1]
            hook.write_text(f"{text.rstrip(chr(10))}\n\n(\n{body})\n")
            done.append(f"added the builder to {hook}")
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
