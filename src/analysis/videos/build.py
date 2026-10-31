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
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from analysis.videos import captions, notebook, render
from analysis.videos import script as scripts
from analysis.videos import theme as themes
from analysis.videos.script import Scene, Script, ScriptError
from analysis.videos.theme import Theme

# Where a video's own data repo is, for a tape whose script names no demo data.
VIDEO_DATA_ENV = "ANALYSIS_VIDEO_DATA"

# A slide with no narration still stays up this long.
MIN_SLIDE_SECONDS = 2.5


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

    @property
    def length(self) -> float:
        return sum(s.length for s in self.scenes)


def repo_root(start: Path) -> Path:
    for parent in (start.resolve(), *start.resolve().parents):
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "analysis").is_dir():
            return parent
    raise ScriptError(f"{start}: not inside the analysis repository")


class _Builder:
    def __init__(self, script: Script, theme: Theme, cache: Path, repo: Path, *, engine: str | None, rerecord: bool,
                 setup: Path | None = None):
        self.script = script
        self.setup = setup
        # prepare_demo's result, made when the first tape needs recording.
        self.prepared: Path | None = None
        self.theme = theme
        self.cache = cache
        self.repo = repo
        self.voice = theme.narration(script.lang)
        if engine:
            self.voice["engine"] = engine
        self.rerecord = rerecord
        self.gap = theme.timing("sentence_gap")
        self.lead = theme.timing("lead")
        self.tail = theme.timing("tail")

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
        durations = render.synthesize(sentences, voice, self.gap, wav, self.repo)
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
        """The scene's picture: (file, is a still, made/cached, key)."""
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
                return png, True, "cached", key
            if demo:
                image = self.ensure_prepared() / str(demo)
                if not image.is_file():
                    raise ScriptError(f"scene {scene.id}: setup.sh made no {demo} (demo_image)")
                uri = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode("ascii")
                html = render.slide_html(self.theme, self.script.meta, {**fields, "image": uri},
                                         self.script.lang)
            render.render_slide(html, self.theme, png)
            return png, True, "made", key
        if v.kind == "notebook":
            plan = notebook.parse(v.source)
            notebooks = self.repo / "notebooks"
            notebook.check(plan, notebooks)
            pages = [(notebooks / name).read_text(encoding="utf-8") for name in plan.notebooks]
            key = render.digest("notebook", v.source, pages, self.theme.out_width, self.theme.out_height,
                                self.theme.fps, *self.demo_parts())
            mp4 = self.cache / f"notebook-{key}.mp4"
            if mp4.is_file() and not self.rerecord:
                return mp4, False, "cached", key
            notebook.record(v.source, self.theme, self.ensure_prepared(), notebooks, mp4)
            return mp4, False, "made", key
        key = render.digest("tape", v.source, self.theme.vhs_settings(), self.theme.data["terminal"],
                            self.theme.data["palette"]["accent"], *self.demo_parts())
        mp4 = self.cache / f"tape-{key}.mp4"
        if mp4.is_file() and not self.rerecord:
            return mp4, False, "cached", key
        render.record_tape(v.source, self.theme, self.ensure_prepared(), mp4)
        return mp4, False, "made", key

    def demo_parts(self) -> list[str | None]:
        """What the prepared demo is made from, for cache keys: the demo data and setup.sh."""
        return [render.tree_digest(self.tape_data()),
                self.setup.read_text(encoding="utf-8") if self.setup else None]

    def ensure_prepared(self) -> Path:
        """The demo state tapes and demo images come from, prepared once a build."""
        if self.prepared is None:
            store = Path(tempfile.mkdtemp(prefix="analysis-video-prepared-"))
            self.prepared = render.prepare_demo(self.tape_data(), self.setup, self.theme, store)
        return self.prepared

    def scene(self, scene: Scene, *, first: bool, last: bool) -> tuple[SceneBuild, list[float], Path]:
        """The scene's clip and narration timing, and its visual (for the thumbnail)."""
        audio, durations, audio_state, audio_key = self.narration(scene)
        visual, still, vis_state, vis_key = self.visual(scene)
        spoken = sum(durations) + self.gap * max(0, len(durations) - 1)
        length = self.lead + spoken + self.tail if durations else 0.0
        if still:
            length = max(length, float(scene.visual.fields.get("duration", 0)), MIN_SLIDE_SECONDS)
        else:
            length = max(length, render.duration(visual))
        length = round(length, 3)
        key = render.digest("clip", audio_key, vis_key, length, self.lead, self.theme.data["video"],
                            self.theme.timing("slide_fade"), self.theme.bg_hex(), first, last)
        clip = self.cache / f"clip-{key}.mp4"
        clip_state = "cached"
        if not clip.is_file() or vis_state == "made":
            ffmpeg = render.need("ffmpeg", "install ffmpeg")
            render.run(render.compose_args(ffmpeg, self.theme, visual=visual, still=still, audio=audio,
                                           length=length, lead=self.lead, out=clip,
                                           fade_in=not first, fade_out=not last))
            clip_state = "made"
        return SceneBuild(scene.id, length, audio_state, vis_state, clip_state, clip), durations, visual


def build(
    folder: Path,
    *,
    lang: str = scripts.DEFAULT_LANG,
    out_dir: Path | None = None,
    engine: str | None = None,
    rerecord: bool = False,
    repo: Path | None = None,
    log: Callable[[str], None] = print,
) -> VideoBuild:
    """Build the video in ``folder`` for ``lang``; return what was made and where.

    ``repo`` (default: the repository holding ``folder``) is where demo data,
    the narration worker and the default ``out/videos`` are found. A
    ``setup.sh`` in ``folder`` prepares the demo data before any tape records.
    """
    script = scripts.load(folder, lang)
    theme = themes.load(themes.videos_root(folder))
    repo = repo or repo_root(folder)
    out_dir = out_dir or repo / "out" / "videos"
    cache = out_dir / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    setup = folder / "setup.sh"
    builder = _Builder(script, theme, cache, repo, engine=engine, rerecord=rerecord,
                       setup=setup if setup.is_file() else None)
    try:
        return _assemble(script, builder, out_dir, log)
    finally:
        if builder.prepared is not None:
            shutil.rmtree(builder.prepared, ignore_errors=True)


def _assemble(script: Script, builder: _Builder, out_dir: Path, log: Callable[[str], None]) -> VideoBuild:

    results: list[SceneBuild] = []
    cover_is_title = False
    cues: list[captions.Cue] = []
    t = 0.0
    cover: Path | None = None  # the title card if there is one, else the first slide, else frame 0
    for i, scene in enumerate(script.scenes):
        result, durations, visual = builder.scene(scene, first=i == 0, last=i == len(script.scenes) - 1)
        is_title = scene.visual.fields.get("template") == "title"
        if scene.visual.kind == "slide" and (cover is None or (is_title and not cover_is_title)):
            cover, cover_is_title = visual, is_title
        cues += captions.scene_cues(scene.sentences, durations, start=t + builder.lead, gap=builder.gap)
        t += result.length
        results.append(result)
        log(f"  {scene.id:<16} {result.length:6.1f}s  narration {result.narration:<6}  "
            f"visual {result.visual:<6}  clip {result.clip}")

    stem = f"{script.name}.{script.lang}"
    video = out_dir / f"{stem}.mp4"
    srt = out_dir / f"{stem}.srt"
    thumb = out_dir / f"{stem}.jpg"
    render.thumbnail(cover or results[0].path, thumb)
    render.concat([r.path for r in results], video, cover=thumb)
    srt.write_text(captions.srt(cues), encoding="utf-8")
    return VideoBuild(video=video, captions=srt, thumbnail=thumb, scenes=tuple(results))
