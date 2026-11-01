"""Parallel video builds (#413): the task order, the recording lane, retries of a tape that froze."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
import time
from pathlib import Path
from typing import Any

import pytest

from analysis.videos import build as builds
from analysis.videos import render
from analysis.videos import script as scripts
from analysis.videos import theme as themes
from analysis.videos.build import SceneBuild, Workers, _Builder, _Visual, build, build_languages, default_jobs

SCRIPT = """\
+++
series = "Series"
title = "Parallel"
data = "prototype_data"
+++

## Scene `title`: Title

```slide
template = "title"
```

Hello.

## Scene `one`: One

```tape
Type "sim check"
```

First tape.

## Scene `two`: Two

```tape
Type "sim list"
```

Second tape.

## Scene `end`: End

```slide
template = "card"
heading = "The end"
```

Goodbye.
"""

JA = """\
+++
title = "並列"
+++

## Scene `title`: 題

こんにちは。

## Scene `one`: 一

一つ目。

## Scene `two`: 二

二つ目。

## Scene `end`: 終

さようなら。
"""


PLENTY = 2**50  # memory enough for any number of tasks


@pytest.fixture(autouse=True)
def _running_allowed() -> Iterator[None]:
    """A failed build here stops render.run() for the rest of the process; let later tests run programs."""
    render.allow_running()
    yield
    render.allow_running()


def _video(videos_root: Path, *, ja: bool = False) -> Path:
    folder = videos_root / "01-parallel"
    folder.mkdir(parents=True)
    (folder / "script.md").write_text(SCRIPT, encoding="utf-8")
    if ja:
        (folder / "script.ja.md").write_text(JA, encoding="utf-8")
    return folder


class _Steps:
    """Stand-ins for the builder's media steps, noting when each starts and ends."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.05,
                 fail: str | None = None):
        self.delay = delay
        self.fail = fail
        self.events: list[tuple[str, str, str, str]] = []  # (lang, step, scene, start|end)
        self.lock = threading.Lock()
        self.recording_now = 0
        self.overlapping_recordings = False
        # Plain functions, so the builder comes through as their first argument.
        monkeypatch.setattr(_Builder, "narration", lambda b, scene: self.narration(b, scene))
        monkeypatch.setattr(_Builder, "plan_visual", lambda b, scene: self.plan_visual(b, scene))
        monkeypatch.setattr(_Builder, "clip", lambda b, *args, **kw: self.clip(b, *args, **kw))
        monkeypatch.setattr(render, "thumbnail", lambda source, out: out.write_bytes(b"jpg"))
        monkeypatch.setattr(render, "concat", self.concat)

    def note(self, builder: _Builder, step: str, scene: str, edge: str) -> None:
        with self.lock:
            self.events.append((builder.script.lang, step, scene, edge))

    def narration(self, builder: _Builder, scene: scripts.Scene) -> tuple[Path | None, list[float], str, str]:
        self.note(builder, "narration", scene.id, "start")
        time.sleep(self.delay)
        self.note(builder, "narration", scene.id, "end")
        return None, [1.0], "made", f"n-{scene.id}"

    def plan_visual(self, builder: _Builder, scene: scripts.Scene) -> _Visual:
        records = scene.visual.kind == "tape"

        def make() -> None:
            self.note(builder, "visual", scene.id, "start")
            if records:
                with self.lock:
                    self.recording_now += 1
                    self.overlapping_recordings |= self.recording_now > 1
            try:
                time.sleep(self.delay * (4 if records else 1))
                if scene.id == self.fail:
                    raise scripts.ScriptError(f"scene {scene.id} broke")
            finally:
                if records:
                    with self.lock:
                        self.recording_now -= 1
                self.note(builder, "visual", scene.id, "end")

        return _Visual(builder.cache / f"{scene.id}.png", not records, f"v-{scene.id}", make, records)

    def clip(self, builder: _Builder, scene: scripts.Scene, narration: Any, visual: _Visual, vis_state: str,
             *, first: bool, last: bool) -> SceneBuild:
        self.note(builder, "clip", scene.id, "start")
        time.sleep(self.delay / 5)
        self.note(builder, "clip", scene.id, "end")
        return SceneBuild(scene.id, 3.0, narration[2], vis_state, "made", builder.cache / f"clip-{scene.id}.mp4")

    def concat(self, parts: list[Path], out: Path, cover: Path | None = None) -> None:
        with self.lock:
            self.events.append(("", "concat", "", "start"))
        out.write_bytes(b"mp4")

    def at(self, lang: str, step: str, scene: str, edge: str) -> int:
        return self.events.index((lang, step, scene, edge))


@pytest.mark.parametrize(("cpus", "jobs"), [(1, 1), (2, 2), (4, 2), (6, 2), (8, 4), (16, 12)])
def test_default_jobs_leave_cpus_to_the_recording(cpus: int, jobs: int) -> None:
    assert default_jobs(cpus, memory=PLENTY) == jobs


@pytest.mark.parametrize(("gib", "jobs"), [(4, 1), (8, 2), (12, 4), (64, 12)])
def test_default_jobs_fit_in_memory(gib: int, jobs: int) -> None:
    """Docker Desktop's VM has 8 GB unless given more: 3 GB for recording, 2 GB a task."""
    assert default_jobs(16, memory=gib * builds.GIB) == jobs


def test_available_memory_is_known_here() -> None:
    memory = builds.available_memory()
    assert memory is None or memory > 0


def test_one_job_is_one_task_at_a_time() -> None:
    with Workers(1, cpus=8) as serial:
        assert serial.recording is serial.general and serial.threads is None
    with Workers(cpus=16, memory=PLENTY) as parallel:
        assert parallel.jobs == 12 and parallel.recording is not parallel.general and parallel.threads == 1
    with Workers(2, cpus=16) as wide:
        assert wide.threads == 6  # the 12 CPUs not kept for the recording, shared by 2 tasks


def test_a_clip_waits_for_its_scene_and_the_video_for_every_clip(
    videos_root: Path, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = _Steps(monkeypatch)
    lines: list[str] = []
    result = build(_video(videos_root), out_dir=tmp_path / "out", repo=repo_root, log=lines.append, jobs=4)
    assert [s.id for s in result.scenes] == ["title", "one", "two", "end"]
    for scene in ("title", "one", "two", "end"):
        clip = steps.at("en", "clip", scene, "start")
        assert clip > steps.at("en", "narration", scene, "end")
        assert clip > steps.at("en", "visual", scene, "end")
    assert steps.events[-1][1] == "concat"
    assert not steps.overlapping_recordings
    # Work ran beside the recordings: something started while the first one was going.
    first_recording = min(steps.at("en", "visual", s, "start") for s in ("one", "two"))
    assert any(e[3] == "start" and i > first_recording for i, e in enumerate(steps.events)
               if i < steps.at("en", "visual", "one", "end") and e[2] != "one")
    # Each scene's line in scene order, whatever order the work finished in.
    assert [line.split()[0] for line in lines] == ["title", "one", "two", "end"]


def test_one_job_builds_the_same_video_in_the_same_order(
    videos_root: Path, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = _Steps(monkeypatch, delay=0.01)
    folder = _video(videos_root)
    serial = build(folder, out_dir=tmp_path / "a", repo=repo_root, log=lambda _: None, jobs=1)
    # Never two tasks at once.
    depth = 0
    for event in steps.events:
        depth += {"start": 1, "end": -1}[event[3]] if event[1] != "concat" else 0
        assert depth <= 1
    assert depth == 0
    parallel = build(folder, out_dir=tmp_path / "b", repo=repo_root, log=lambda _: None, jobs=4)
    assert [(s.id, s.length) for s in serial.scenes] == [(s.id, s.length) for s in parallel.scenes]
    assert serial.captions.read_text() == parallel.captions.read_text()


def test_a_failing_task_stops_the_build_with_its_error(
    videos_root: Path, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = _Steps(monkeypatch, fail="one")
    with pytest.raises(scripts.ScriptError, match="scene one broke"):
        build(_video(videos_root), out_dir=tmp_path / "out", repo=repo_root, log=lambda _: None, jobs=2)
    assert ("", "concat", "", "start") not in steps.events
    # Nothing still running once the build has returned.
    assert sum(e[3] == "start" for e in steps.events) == sum(e[3] == "end" for e in steps.events)


def test_languages_build_together_and_tag_their_lines(
    videos_root: Path, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = _Steps(monkeypatch)
    lines: list[str] = []
    results = build_languages(_video(videos_root, ja=True), ["en", "ja"], jobs=4, out_dir=tmp_path / "out",
                              repo=repo_root, log=lines.append)
    assert [r.video.name for r in results] == ["01-parallel.en.mp4", "01-parallel.ja.mp4"]
    assert sorted(line.split()[0] for line in lines) == ["[en]"] * 4 + ["[ja]"] * 4
    # One recording lane for both languages.
    assert not steps.overlapping_recordings
    # ...and the languages overlapped.
    assert steps.at("ja", "narration", "title", "start") < steps.at("en", "clip", "end", "end")


def test_one_languages_failure_stops_the_others(
    videos_root: Path, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = _Steps(monkeypatch, fail="one")
    with pytest.raises(scripts.ScriptError, match="scene one broke"):
        build_languages(_video(videos_root, ja=True), ["en", "ja"], jobs=4, out_dir=tmp_path / "out",
                        repo=repo_root, log=lambda _: None)
    assert ("", "concat", "", "start") not in steps.events


# --- recordings that freeze ----------------------------------------------------


@pytest.mark.parametrize(("worst_gap", "lagged"), [(0.1, False), (0.5, False), (0.6, True)])
def test_a_recording_lags_when_its_picture_froze_past_half_a_second(worst_gap: float, lagged: bool) -> None:
    assert render.Timing(100, 20, worst_gap).lagged is lagged


def test_a_timing_report_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text('{"framerate": 30, "frames": 90, "held": 12, "seconds": 3.0, "behind": 0, "worst_gap": 0.2}')
    timing = render.Timing.read(path)
    assert timing == render.Timing(90, 12, 0.2)
    timing.write(tmp_path / "again.json")
    assert render.Timing.read(tmp_path / "again.json") == timing
    assert render.Timing.read(tmp_path / "missing.json") is None


def _tape_builder(videos_root: Path, repo_root: Path, tmp_path: Path, lines: list[str]) -> tuple[_Builder, Any]:
    folder = _video(videos_root)
    script = scripts.load(folder)
    cache = tmp_path / "cache"
    cache.mkdir()
    builder = _Builder(script, themes.load(videos_root), cache, repo_root, engine="silent", rerecord=False,
                       log=lines.append)
    builder.prepared = tmp_path / "prepared"  # never prepared: record_tape is a stand-in
    return builder, script.scenes[1]


def _recorder(monkeypatch: pytest.MonkeyPatch, freezes: list[float]) -> list[float]:
    """record_tape stand-in: each try writes its longest freeze into the file and reports it."""
    waits: list[float] = []
    tries = iter(freezes)

    def record(tape: str, theme: themes.Theme, prepared: Path, out: Path) -> render.Timing:
        freeze = next(tries)
        out.write_text(str(freeze))
        return render.Timing(300, 10, freeze)

    monkeypatch.setattr(render, "record_tape", record)
    monkeypatch.setattr(builds.time, "sleep", waits.append)
    return waits


def test_a_tape_that_froze_is_recorded_again_after_a_growing_wait(
    videos_root: Path, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits = _recorder(monkeypatch, [2.0, 1.0, 0.1])
    lines: list[str] = []
    builder, scene = _tape_builder(videos_root, repo_root, tmp_path, lines)
    path, still, state, _ = builder.visual(scene)
    assert (state, still, path.read_text()) == ("made", False, "0.1")
    assert waits == [builds.RETRY_WAIT, 2 * builds.RETRY_WAIT]
    assert len(lines) == 2 and all("recording it again" in line for line in lines)
    assert builder.plan_visual(scene).make is None  # kept up: the cache keeps it


def test_a_tape_that_keeps_freezing_keeps_its_best_try_and_is_redone_next_build(
    videos_root: Path, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _recorder(monkeypatch, [2.0, 0.7, 3.0])
    lines: list[str] = []
    builder, scene = _tape_builder(videos_root, repo_root, tmp_path, lines)
    path, _, state, _ = builder.visual(scene)
    assert (state, path.read_text()) == ("made", "0.7")
    assert "kept the best of 3 tries" in lines[-1]
    assert not list(builder.cache.glob("*.best.mp4"))
    assert builder.plan_visual(scene).make is not None  # froze: not trusted from the cache


# --- stopping, memory and first narrations ------------------------------------------


def test_stop_running_kills_a_program_and_its_children() -> None:
    errors: list[BaseException] = []

    def go() -> None:
        try:
            render.run(["bash", "-c", "sleep 60 & wait"])
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=go)
    worker.start()
    time.sleep(0.3)
    started = time.monotonic()
    render.stop_running()
    worker.join(10)
    assert not worker.is_alive() and time.monotonic() - started < 5
    assert errors and "bash failed (-15)" in str(errors[0])
    with pytest.raises(RuntimeError, match="not started, the build is stopping"):
        render.run(["true"])
    render.allow_running()
    render.run(["true"])


def test_a_program_the_kernel_killed_says_so() -> None:
    with pytest.raises(RuntimeError, match=r"failed \(-9: killed, most likely out of memory\)"):
        render.run(["bash", "-c", "kill -9 $$"])


def test_stop_running_kills_children_that_ignore_terminate(tmp_path: Path) -> None:
    """The leader exits on TERM; a child that ignores it must still go."""
    pid_file = tmp_path / "child.pid"
    errors: list[BaseException] = []

    def go() -> None:
        try:
            render.run(["bash", "-c", f"bash -c 'trap \"\" TERM; echo $$ > {pid_file}; sleep 60' & wait"])
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=go)
    worker.start()
    deadline = time.monotonic() + 5
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    child = int(pid_file.read_text())
    render.stop_running()
    worker.join(10)
    assert errors and not worker.is_alive()
    deadline = time.monotonic() + 5
    while _alive(child):
        assert time.monotonic() < deadline, "the child that ignored TERM is still running"
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is dead; only its parent (gone) or init would reap it.
    stat = Path(f"/proc/{pid}/stat")
    return not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"


def test_the_memory_limit_comes_from_this_process_cgroup_and_those_above(tmp_path: Path) -> None:
    def group(path: Path, limit: str, used: str, v1: bool = False) -> None:
        path.mkdir(parents=True, exist_ok=True)
        names = ("memory.limit_in_bytes", "memory.usage_in_bytes") if v1 else ("memory.max", "memory.current")
        (path / names[0]).write_text(limit)
        (path / names[1]).write_text(used)

    group(tmp_path / "outer", str(8 * builds.GIB), str(2 * builds.GIB))
    group(tmp_path / "outer" / "inner", "max", str(builds.GIB))
    # v1, with a mount that starts at the container's own group: only the root has files.
    group(tmp_path / "memory", str(4 * builds.GIB), str(builds.GIB), v1=True)
    files = builds._cgroup_memory_files("4:memory:/docker/abc\n2:cpu:/x\n0::/outer/inner\n", tmp_path)
    found = [(builds._read(limit), builds._read(usage)) for limit, usage in files]
    assert files[0] == (tmp_path / "memory/docker/abc/memory.limit_in_bytes",
                        tmp_path / "memory/docker/abc/memory.usage_in_bytes")
    assert (str(4 * builds.GIB), str(builds.GIB)) in found  # the v1 mount root
    assert ("max", str(builds.GIB)) in found and (str(8 * builds.GIB), str(2 * builds.GIB)) in found
    assert len(files) == 3 + 3  # docker/abc, docker, root; outer/inner, outer, root


def test_a_notebook_recording_stops_between_steps_when_the_build_stops() -> None:
    from analysis.videos import notebook

    render.stop_running()
    with pytest.raises(RuntimeError, match="notebook line 1: stopped"):
        notebook._play(None, notebook.parse("Open net_worth.ipynb\n"), None, None)  # ty: ignore[invalid-argument-type]


def test_a_failure_stops_a_recording_that_would_never_finish(
    videos_root: Path, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recording whose browser died waits forever: the failure elsewhere mustn't wait for it."""
    steps = _Steps(monkeypatch)

    def plan_visual(builder: _Builder, scene: scripts.Scene) -> _Visual:
        def make() -> None:
            if scene.visual.kind == "tape":
                render.run(["sleep", "60"])

        return _Visual(builder.cache / f"{scene.id}.png", scene.visual.kind != "tape", scene.id, make,
                       scene.visual.kind == "tape")

    def narration(builder: _Builder, scene: scripts.Scene) -> Any:
        time.sleep(0.3)
        raise RuntimeError("ffmpeg failed (-9)")

    monkeypatch.setattr(_Builder, "plan_visual", plan_visual)
    monkeypatch.setattr(_Builder, "narration", narration)
    lines: list[str] = []
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"ffmpeg failed \(-9\)"):
        build(_video(videos_root), out_dir=tmp_path / "out", repo=repo_root, log=lines.append, jobs=2)
    assert time.monotonic() - started < 15
    assert lines[0] == "  stopping: ffmpeg failed (-9)"
    assert not steps.events


def test_a_languages_first_narration_runs_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Japanese's first run downloads OpenJTalk's dictionary; two at once break it."""
    for name in render.KOKORO_FILES:
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setattr(render, "cache_home", lambda: tmp_path)
    monkeypatch.setattr(render, "need", lambda tool, hint: tool)
    monkeypatch.setattr(render, "_VOICED", set())
    lock = threading.Lock()
    running: list[int] = [0]
    seen: list[int] = []  # how many ran at once, as each started

    def run(argv: list[str], *, input: str, **kwargs: Any) -> Any:
        with lock:
            running[0] += 1
            seen.append(running[0])
        time.sleep(0.1)
        with lock:
            running[0] -= 1
        return render.subprocess.CompletedProcess(argv, 0, '{"durations": [1.0]}', "")

    monkeypatch.setattr(render, "run", run)
    voice = {"engine": "kokoro", "voice": "jf_alpha", "lang": "ja"}
    threads = [threading.Thread(target=render.synthesize, args=(["文。"], voice, 0.2, tmp_path / f"{i}.wav", tmp_path))
               for i in range(4)]
    for t in threads:
        t.start()
        time.sleep(0.02)
    for t in threads:
        t.join()
    assert seen[0] == 1 and seen[1] == 1  # the second waited for the first
    assert max(seen) > 1  # then the rest ran side by side
