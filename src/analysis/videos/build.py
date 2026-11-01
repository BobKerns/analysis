"""Build a video: each scene's narration, visual and clip, then the whole.

Every product is cached under ``out/videos/.cache`` by a hash of exactly
what went into it, so a build redoes only what changed:

- narration: the scene's sentences as spoken (respellings applied) and the
  language's voice settings;
- a slide: the page its template renders (so template, asset, theme and
  field edits all count);
- a tape: its commands, the terminal theme, its data's bytes and the
  video's ``setup.sh``. The CLI's own behavior isn't hashed, so
  ``--rerecord`` re-records tapes after a CLI change;
- a notebook scene: its steps, the notebooks it opens, the theme's size,
  its data and ``setup.sh``; ``--rerecord`` re-records these too;
- a clip: its narration, its visual and the timing.

The work runs in parallel (#413): every scene's narration and visual are
tasks on a pool, a scene's clip starts once both are done, and the video is
joined once every clip is. Recordings (tapes and notebook scenes) all run at
one fixed path, so they take turns in a lane of their own; see ``Workers``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analysis.videos import captions, notebook, render, youtube
from analysis.videos import script as scripts
from analysis.videos import theme as themes
from analysis.videos.script import Scene, Script, ScriptError
from analysis.videos.theme import Theme

# Where a video's own data repo is, for a tape whose script names no demo data.
VIDEO_DATA_ENV = "ANALYSIS_VIDEO_DATA"

# A slide with no narration still stays up this long.
MIN_SLIDE_SECONDS = 2.5

# CPUs left to the recording lane by default: VHS and the notebook recorder
# capture in real time, through a browser, and fall behind when starved.
RECORDING_CPUS = 4
# A tape whose picture froze too long (render.Timing) is recorded again, up
# to this many tries in all, waiting RETRY_WAIT seconds, then twice that, ...
RECORD_ATTEMPTS = 3
RETRY_WAIT = 5.0

GIB = 2**30
# Memory a build plans for (measured at 4K): a clip encode or a slide's
# browser takes up to about 2 GB, and the recording lane (VHS's browser and
# its own 4K encode, or JupyterLab and its browser) about 3 GB. Docker
# Desktop's VM has 8 GB unless you give it more; past that, the kernel kills
# a task (ffmpeg "failed (-9)").
TASK_MEMORY = 2 * GIB
RECORDING_MEMORY = 3 * GIB


def default_jobs(cpus: int | None = None, memory: int | None = None) -> int:
    """How many tasks run at once, besides recordings.

    The CPUs less RECORDING_CPUS, at least 2 (1 on a single CPU), and no
    more than ``memory`` (default: available_memory()) holds, at least 1.
    """
    cpus = cpus or os.process_cpu_count() or 1
    by_cpu = 1 if cpus <= 1 else max(2, cpus - RECORDING_CPUS)
    memory = memory if memory is not None else available_memory()
    if memory is None:
        return by_cpu
    return max(1, min(by_cpu, (memory - RECORDING_MEMORY) // TASK_MEMORY))


def available_memory() -> int | None:
    """Bytes a build can use: what's free, within the container's limit (None if unknown)."""
    found: list[int] = []
    meminfo = _read("/proc/meminfo")
    if meminfo and (m := re.search(r"^MemAvailable:\s+(\d+) kB", meminfo, re.MULTILINE)):
        found.append(int(m[1]) * 1024)
    for limit, usage in (("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
                         ("/sys/fs/cgroup/memory/memory.limit_in_bytes",
                          "/sys/fs/cgroup/memory/memory.usage_in_bytes")):
        cap, used = _read(limit), _read(usage)
        # cgroup v2 says "max" with no limit; v1 a huge number.
        if cap and used and cap.strip().isdigit() and int(cap) < 2**60:
            found.append(int(cap) - int(used))
    if not found:
        try:
            found.append(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))  # macOS: the whole machine
        except (ValueError, OSError, AttributeError):
            return None
    return max(0, min(found))


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


@dataclass(frozen=True)
class SceneBuild:
    id: str
    length: float
    narration: str  # "made", "cached" or "none"
    visual: str  # "made" or "cached"
    clip: str  # "made" or "cached"
    path: Path


@dataclass(frozen=True)
class VideoBuild:
    video: Path
    captions: Path
    thumbnail: Path  # the title card, for YouTube's custom thumbnail
    scenes: tuple[SceneBuild, ...]
    description: Path | None = None  # the YouTube title and description, if the script has [youtube]

    @property
    def length(self) -> float:
        return sum(s.length for s in self.scenes)


def repo_root(start: Path) -> Path:
    for parent in (start.resolve(), *start.resolve().parents):
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "analysis").is_dir():
            return parent
    raise ScriptError(f"{start}: not inside the analysis repository")


class _Demos:
    """Prepared demos (render.prepare_demo), one per demo data and setup.sh.

    Shared by every builder on the same Workers, so a video's languages
    prepare their demo once between them.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._made: dict[str, Path] = {}

    def get(self, key: str, make: Callable[[Path], Path]) -> Path:
        # One lock for all: preparing takes render.DEMO_LOCK anyway.
        with self._lock:
            if key not in self._made:
                self._made[key] = make(Path(tempfile.mkdtemp(prefix="analysis-video-prepared-")))
            return self._made[key]

    def close(self) -> None:
        with self._lock:
            for path in self._made.values():
                shutil.rmtree(path, ignore_errors=True)
            self._made.clear()


class _Stopped(Exception):
    """Another build on the same Workers failed, so this one gave up."""


class Workers:
    """Where a build's tasks run.

    ``jobs`` threads take narration, slides and clip encodes. Recordings
    (tapes, notebook scenes) take turns on one more thread, the recording
    lane, since they all run at render.DEMO_ROOT. With ``jobs=1`` there's
    no separate lane: everything runs one task at a time, as before #413.
    ``threads`` is how many CPU threads each Kokoro or ffmpeg task may
    use, so the pool doesn't crowd out the recording.
    """

    def __init__(self, jobs: int | None = None, *, cpus: int | None = None, memory: int | None = None):
        cpus = cpus or os.process_cpu_count() or 1
        self.jobs = max(1, jobs or default_jobs(cpus, memory))
        self.threads: int | None = None if self.jobs == 1 else max(1, max(1, cpus - RECORDING_CPUS) // self.jobs)
        self.general = ThreadPoolExecutor(self.jobs, thread_name_prefix="video")
        self.recording = (self.general if self.jobs == 1
                          else ThreadPoolExecutor(1, thread_name_prefix="video-record"))
        self.demos = _Demos()
        render.allow_running()  # a build that failed before this one stopped them
        self.failed = threading.Event()
        self.error: BaseException | None = None  # the first failure, which stopped the rest
        self._print = threading.Lock()

    def submit(self, pool: ThreadPoolExecutor, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        """``pool.submit``, for a task that doesn't start once a build has failed."""

        def task() -> Any:
            if self.failed.is_set():
                raise _Stopped()
            return fn(*args, **kwargs)

        return pool.submit(task)

    def stop(self, error: BaseException) -> None:
        """A task failed (or the build was interrupted): stop every build on these workers now.

        Running programs are killed rather than waited for: one may never
        finish (a recording whose browser the kernel killed).
        """
        with self._print:
            if self.error is None:
                self.error = error
        self.failed.set()
        render.stop_running()

    def log(self, log: Callable[[str], None]) -> Callable[[str], None]:
        """``log``, one line at a time from any thread."""

        def locked(line: str) -> None:
            with self._print:
                log(line)

        return locked

    def close(self, *, cancel: bool = False) -> None:
        for pool in {self.general, self.recording}:
            pool.shutdown(wait=True, cancel_futures=cancel)
        self.demos.close()

    def __enter__(self) -> Workers:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, _: object) -> None:
        if error is not None:
            self.stop(error)
        self.close(cancel=kind is not None)


@dataclass
class _Visual:
    """A scene's picture, and what's left to do to have it."""

    path: Path
    still: bool
    key: str
    make: Callable[[], None] | None  # None: it's in the cache
    records: bool = False  # make uses render.DEMO_ROOT, so it runs on the recording lane


class _Builder:
    def __init__(self, script: Script, theme: Theme, cache: Path, repo: Path, *, engine: str | None, rerecord: bool,
                 setup: Path | None = None, demos: _Demos | None = None, threads: int | None = None,
                 log: Callable[[str], None] = print):
        self.script = script
        self.setup = setup
        # prepare_demo's result, made when the first tape needs recording.
        self.prepared: Path | None = None
        self.demos = demos or _Demos()
        self.theme = theme
        self.cache = cache
        self.repo = repo
        self.voice = theme.narration(script.lang)
        if engine:
            self.voice["engine"] = engine
        self.rerecord = rerecord
        self.threads = threads
        self.log = log
        self.gap = theme.timing("sentence_gap")
        self.lead = theme.timing("lead")
        self.tail = theme.timing("tail")
        self._demo_parts: list[str | None] | None = None

    def narration(self, scene: Scene) -> tuple[Path | None, list[float], str, str]:
        # The voice says the respelled sentences; the captions show scene.sentences.
        voice = {k: v for k, v in self.voice.items() if k != "pronounce"}
        sentences = scene.spoken(self.voice.get("pronounce"))
        if not sentences:
            return None, [], "none", "-"
        worker = ""
        if voice.get("engine", "kokoro") == "kokoro":
            worker = render.digest((self.repo / "scripts" / "videos" / "tts_kokoro.py").read_text(encoding="utf-8"))
        # Keyed on what's said, so a lexicon entry re-voices only the scenes that use it.
        key = render.digest("narration", voice, self.gap, sentences, worker)
        wav = self.cache / f"narration-{key}.wav"
        meta = wav.with_suffix(".json")
        if wav.is_file() and meta.is_file():
            return wav, json.loads(meta.read_text())["durations"], "cached", key
        durations = render.synthesize(sentences, voice, self.gap, wav, self.repo, threads=self.threads)
        meta.write_text(json.dumps({"durations": durations, "sentences": list(sentences)}, ensure_ascii=False))
        return wav, durations, "made", key

    def tape_data(self) -> Path:
        """The data a tape starts from: front matter ``data``, else the video's own data repo.

        The video's own repo is ``$ANALYSIS_VIDEO_DATA`` (the video container
        mounts the video's data volume there). It is deliberately not
        ``$ANALYSIS_DATA_DIR``, which in a planner's shell names the real data.
        """
        data_name = self.script.meta.get("data")
        if data_name:
            data = self.repo / data_name
            if not data.is_dir():
                raise ScriptError(f"{self.script.path}: demo data {data_name!r} isn't a folder in the repo")
            return data
        env = os.environ.get(VIDEO_DATA_ENV)
        if not env:
            raise ScriptError(
                f"{self.script.path}: a tape needs data = \"<demo data dir>\" in the front matter, "
                f"or the video's data repo in ${VIDEO_DATA_ENV} (scripts/videos/docker.sh sets it)"
            )
        data = Path(env)
        # A data repo's mark (data_repo.is_data_repo), not just a folder: an
        # aborted `sim init` leaves a partial one.
        if not (data / "persons.csv").is_file():
            raise ScriptError(
                f"{self.script.path}: the video's data {data} doesn't exist yet; create it first "
                f"(scripts/videos/docker.sh shell {self.script.name})"
            )
        return data

    def visual(self, scene: Scene) -> tuple[Path, bool, str, str]:
        """The scene's picture, made here if need be: (file, is a still, made/cached, key)."""
        v = self.plan_visual(scene)
        if v.make is not None:
            v.make()
        return v.path, v.still, "cached" if v.make is None else "made", v.key

    def plan_visual(self, scene: Scene) -> _Visual:
        """The scene's picture: where it is, or will be, and how to make it if it isn't cached."""
        v = scene.visual
        if v.kind == "slide":
            fields = v.fields
            html = render.slide_html(self.theme, self.script.meta, fields, self.script.lang)
            # A demo_image is a file setup.sh makes, so the slide changes
            # whenever the demo would: key it like a tape, without
            # preparing the demo just to read a cached slide.
            demo = fields.get("demo_image")
            demo_key = [self.demo_parts()] if demo else []
            key = render.digest("slide", html, self.theme.out_width, self.theme.out_height, *demo_key)
            png = self.cache / f"slide-{key}.png"
            if png.is_file():
                return _Visual(png, True, key, None)

            def slide() -> None:
                page = html
                if demo:
                    image = self.ensure_prepared() / str(demo)
                    if not image.is_file():
                        raise ScriptError(f"scene {scene.id}: setup.sh made no {demo} (demo_image)")
                    uri = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode("ascii")
                    page = render.slide_html(self.theme, self.script.meta, {**fields, "image": uri},
                                             self.script.lang)
                render.render_slide(page, self.theme, png)

            return _Visual(png, True, key, slide)
        if v.kind == "notebook":
            plan = notebook.parse(v.source)
            notebooks = self.repo / "notebooks"
            notebook.check(plan, notebooks)
            pages = [(notebooks / name).read_text(encoding="utf-8") for name in plan.notebooks]
            key = render.digest("notebook", v.source, pages, self.theme.out_width, self.theme.out_height,
                                self.theme.fps, *self.demo_parts())
            mp4 = self.cache / f"notebook-{key}.mp4"
            if mp4.is_file() and not self.rerecord:
                return _Visual(mp4, False, key, None)
            return _Visual(mp4, False, key, lambda: notebook.record(v.source, self.theme, self.ensure_prepared(),
                                                                    notebooks, mp4), records=True)
        key = render.digest("tape", v.source, self.theme.vhs_settings(), self.theme.data["terminal"],
                            self.theme.data["palette"]["accent"], *self.demo_parts())
        mp4 = self.cache / f"tape-{key}.mp4"
        timing = render.Timing.read(_timing_path(mp4))
        # A recording that froze is redone by the next build, cache or not.
        if mp4.is_file() and not self.rerecord and not (timing and timing.lagged):
            return _Visual(mp4, False, key, None)
        return _Visual(mp4, False, key, lambda: self.record_tape(scene, v.source, mp4), records=True)

    def record_tape(self, scene: Scene, source: str, mp4: Path) -> None:
        """Record the tape, again (after a wait) while its picture freezes too long; keep the best try."""
        best: render.Timing | None = None
        kept = mp4.with_name(f"{mp4.stem}.best{mp4.suffix}")
        try:
            for attempt in range(RECORD_ATTEMPTS):
                if attempt:
                    time.sleep(RETRY_WAIT * 2 ** (attempt - 1))
                timing = render.record_tape(source, self.theme, self.ensure_prepared(), mp4)
                if timing is None or not timing.lagged:
                    if timing is not None:
                        timing.write(_timing_path(mp4))
                    return
                if best is None or timing.worst_gap < best.worst_gap:
                    best = timing
                    mp4.replace(kept)
                more = attempt + 1 < RECORD_ATTEMPTS
                self.log(f"  {scene.id}: the recording {timing.describe()}"
                         + ("; recording it again" if more else ""))
            assert best is not None
            kept.replace(mp4)
            best.write(_timing_path(mp4))
            self.log(f"  {scene.id}: kept the best of {RECORD_ATTEMPTS} tries ({best.describe()}); "
                     f"the next build records it again (a lower --jobs may help)")
        finally:
            kept.unlink(missing_ok=True)

    def demo_parts(self) -> list[str | None]:
        """What the prepared demo is made from, for cache keys: the demo data and setup.sh."""
        if self._demo_parts is None:
            self._demo_parts = [render.tree_digest(self.tape_data()),
                                self.setup.read_text(encoding="utf-8") if self.setup else None]
        return self._demo_parts

    def ensure_prepared(self) -> Path:
        """The demo state tapes and demo images come from, prepared once a build."""
        if self.prepared is None:
            data = self.tape_data()
            self.prepared = self.demos.get(
                render.digest("demo", *self.demo_parts(), str(data)),
                lambda store: render.prepare_demo(data, self.setup, self.theme, store))
        return self.prepared

    def clip(self, scene: Scene, narration: tuple[Path | None, list[float], str, str], visual: _Visual,
             vis_state: str, *, first: bool, last: bool) -> SceneBuild:
        """The scene's clip, from its narration and its (finished) visual."""
        audio, durations, audio_state, audio_key = narration
        spoken = sum(durations) + self.gap * max(0, len(durations) - 1)
        length = self.lead + spoken + self.tail if durations else 0.0
        if visual.still:
            length = max(length, float(scene.visual.fields.get("duration", 0)), MIN_SLIDE_SECONDS)
        else:
            length = max(length, render.duration(visual.path))
        length = round(length, 3)
        key = render.digest("clip", audio_key, visual.key, length, self.lead, self.theme.data["video"],
                            self.theme.timing("slide_fade"), self.theme.bg_hex(), first, last)
        clip = self.cache / f"clip-{key}.mp4"
        clip_state = "cached"
        if not clip.is_file() or vis_state == "made":
            ffmpeg = render.need("ffmpeg", "install ffmpeg")
            # Encoded beside the cache name and renamed once whole, like a
            # recording: tasks now run side by side, so one can be stopped
            # mid-encode when another fails.
            partial = clip.with_name(f"{clip.stem}.partial{clip.suffix}")
            render.run(render.compose_args(ffmpeg, self.theme, visual=visual.path, still=visual.still,
                                           audio=audio, length=length, lead=self.lead, out=partial,
                                           fade_in=not first, fade_out=not last, threads=self.threads))
            partial.replace(clip)
            clip_state = "made"
        return SceneBuild(scene.id, length, audio_state, vis_state, clip_state, clip)


def _timing_path(mp4: Path) -> Path:
    return mp4.with_name(f"{mp4.stem}.timing.json")


def build(
    folder: Path,
    *,
    lang: str = scripts.DEFAULT_LANG,
    out_dir: Path | None = None,
    engine: str | None = None,
    rerecord: bool = False,
    repo: Path | None = None,
    log: Callable[[str], None] = print,
    workers: Workers | None = None,
    jobs: int | None = None,
) -> VideoBuild:
    """Build the video in ``folder`` for ``lang``; return what was made and where.

    ``repo`` (default: the repository holding ``folder``) is where demo data,
    the narration worker and the default ``out/videos`` are found. A
    ``setup.sh`` in ``folder`` prepares the demo data before any tape records.
    The work runs on ``workers`` (shared, for building several languages at
    once), else on a pool of ``jobs`` made for this build.
    """
    if workers is None:
        with Workers(jobs) as own:
            return build(folder, lang=lang, out_dir=out_dir, engine=engine, rerecord=rerecord, repo=repo,
                         log=log, workers=own)
    script = scripts.load(folder, lang)
    theme = themes.load(themes.videos_root(folder))
    repo = repo or repo_root(folder)
    out_dir = out_dir or repo / "out" / "videos"
    cache = out_dir / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    setup = folder / "setup.sh"
    log = workers.log(log)
    builder = _Builder(script, theme, cache, repo, engine=engine, rerecord=rerecord,
                       setup=setup if setup.is_file() else None, demos=workers.demos, threads=workers.threads,
                       log=log)
    try:
        return _assemble(script, builder, workers, out_dir, log)
    except BaseException as error:
        if not isinstance(error, _Stopped):
            _stop(workers, error, log)
        raise


def build_languages(
    folder: Path,
    langs: Sequence[str],
    *,
    jobs: int | None = None,
    log: Callable[[str], None] = print,
    **options: Any,
) -> list[VideoBuild]:
    """Build ``folder`` in every language of ``langs`` at once, on one pool; results in ``langs`` order.

    Each language's lines are tagged with it. The first failure stops the
    rest and is raised.
    """
    with Workers(jobs) as workers:
        if len(langs) == 1:
            return [build(folder, lang=langs[0], log=log, workers=workers, **options)]
        with ThreadPoolExecutor(len(langs), thread_name_prefix="video-lang") as each:
            futures = [each.submit(build, folder, lang=lang, workers=workers, **options,
                                   log=lambda line, lang=lang: log(f"[{lang}] {line.lstrip()}"))
                       for lang in langs]
            try:
                wait(futures)
            except BaseException as error:
                # Stop the languages' builds before the pool waits for them (Ctrl-C).
                workers.stop(error)
                raise
        if workers.error is not None:
            # The first failure, not what killing the rest made of them.
            raise workers.error
        return [f.result() for f in futures]


def _assemble(script: Script, builder: _Builder, workers: Workers, out_dir: Path,
              log: Callable[[str], None]) -> VideoBuild:
    scenes = script.scenes
    n = len(scenes)
    plans = [builder.plan_visual(scene) for scene in scenes]
    narrations: list[Future[Any]] = []
    visuals: list[Future[Any]] = []
    # Recordings first: they're usually the longest tasks, and the lane runs one at a time.
    order = sorted(range(n), key=lambda i: not plans[i].records)
    by_scene: dict[int, Future[Any]] = {}
    for i in order:
        plan = plans[i]
        if plan.make is None:
            done: Future[Any] = Future()
            done.set_result("cached")
            by_scene[i] = done
        else:
            pool = workers.recording if plan.records else workers.general
            by_scene[i] = workers.submit(pool, _made, plan.make)
    visuals = [by_scene[i] for i in range(n)]
    narrations = [workers.submit(workers.general, builder.narration, scene) for scene in scenes]
    clips: list[Future[SceneBuild] | None] = [None] * n
    results: list[SceneBuild] = []
    pending: set[Future[Any]] = {*narrations, *visuals}
    try:
        while pending:
            done_now, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            if workers.failed.is_set():
                raise _Stopped()
            for future in done_now:
                future.result()  # raises the task's error
            for i in range(n):
                if clips[i] is None and narrations[i].done() and visuals[i].done():
                    clip = workers.submit(workers.general, builder.clip, scenes[i], narrations[i].result(), plans[i],
                                                  visuals[i].result(), first=i == 0, last=i == n - 1)
                    clips[i] = clip
                    pending.add(clip)
            # Each scene's line in scene order, as soon as it and every scene before it are done.
            while len(results) < n and (c := clips[len(results)]) is not None and c.done():
                result = c.result()
                results.append(result)
                log(f"  {result.id:<16} {result.length:6.1f}s  narration {result.narration:<6}  "
                    f"visual {result.visual:<6}  clip {result.clip}")
    except BaseException as error:
        if not isinstance(error, _Stopped):
            _stop(workers, error, log)  # before waiting: a running task may never finish on its own
        everything = [*narrations, *visuals, *(c for c in clips if c is not None)]
        for future in everything:
            future.cancel()
        wait(everything)  # let running tasks finish, so nothing writes after the build returns
        raise

    cover_is_title = False
    cover: Path | None = None  # the title card if there is one, else the first slide, else frame 0
    cues: list[captions.Cue] = []
    t = 0.0
    for scene, plan, result, narration in zip(scenes, plans, results, narrations, strict=True):
        is_title = scene.visual.fields.get("template") == "title"
        if scene.visual.kind == "slide" and (cover is None or (is_title and not cover_is_title)):
            cover, cover_is_title = plan.path, is_title
        cues += captions.scene_cues(scene.sentences, narration.result()[1], start=t + builder.lead,
                                    gap=builder.gap)
        t += result.length

    stem = f"{script.name}.{script.lang}"
    video = out_dir / f"{stem}.mp4"
    srt = out_dir / f"{stem}.srt"
    thumb = out_dir / f"{stem}.jpg"
    render.thumbnail(cover or results[0].path, thumb)
    render.concat([r.path for r in results], video, cover=thumb)
    srt.write_text(captions.srt(cues), encoding="utf-8")
    # What `publish` needs from this build: its chapter times, and the
    # title and description they make, to read before it goes up.
    lengths = [(r.id, r.length) for r in results]
    youtube.write_scene_lengths(video, lengths)
    description: Path | None = None
    if youtube.fields(script).get("summary") and not youtube.skipped(script):
        description = out_dir / f"{stem}.youtube.txt"
        description.write_text(youtube.preview(youtube.metadata(script, lengths)), encoding="utf-8")
    return VideoBuild(video=video, captions=srt, thumbnail=thumb, scenes=tuple(results), description=description)


def _stop(workers: Workers, error: BaseException, log: Callable[[str], None]) -> None:
    """Stop every build on ``workers`` for ``error``, saying so if it's the first failure."""
    if workers.error is None and not isinstance(error, KeyboardInterrupt):
        lines = str(error).splitlines()
        log(f"  stopping: {lines[0] if lines else type(error).__name__}")
    workers.stop(error)


def _made(make: Callable[[], None]) -> str:
    make()
    return "made"
