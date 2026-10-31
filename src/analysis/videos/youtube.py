"""A video's YouTube title, description and settings, and publishing it (#395).

Three sources, so a description is written once and stays right:

- ``docs/videos/youtube.toml``: the channel's settings and wording, per
  language (title pattern, footer, labels, playlist);
- each script's ``[youtube]`` front matter: what only that video can say
  (its summary and the "In this video" list), overridden key by key by a
  translation's;
- ``published.toml`` beside the script, written by ``publish``: each
  language's video id and the scene lengths of the build that went up, so
  chapters match the uploaded picture even after a later rebuild.

Chapters come from the scenes, the previous/next lines from the
neighboring videos, and a link to each other language's version from its
``published.toml``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from analysis.videos import script as scripts
from analysis.videos.script import Script, ScriptError

CHANNEL_FILE = "youtube.toml"
PUBLISHED_FILE = "published.toml"
# Where the OAuth client and the signed-in token live; never in git.
SECRETS_ENV = "ANALYSIS_VIDEO_SECRETS"

# YouTube's limits (videos resource): checked before anything is sent.
MAX_TITLE = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500
# A chapter must last at least this long; a video needs at least three.
MIN_CHAPTER_SECONDS = 10.0
MIN_CHAPTERS = 3

_YOUTUBE_KEYS = {"title", "summary", "contents", "tags", "chapters", "skip", "previous", "next"}
_PRIVACY = ("private", "unlisted", "public")
_ANGLE = re.compile(r"[<>]")
_CJK = re.compile(r"[　-鿿＀-￯]")


@dataclass(frozen=True)
class Chapter:
    start: float
    title: str


@dataclass(frozen=True)
class Metadata:
    """What one language's video says on YouTube."""

    lang: str
    title: str
    description: str
    tags: tuple[str, ...]
    category_id: str
    privacy: str  # set on upload only: a later publish never changes it
    made_for_kids: bool
    license: str
    embeddable: bool
    playlist: str | None

    def snippet(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "categoryId": self.category_id,
            "defaultLanguage": self.lang,
            "defaultAudioLanguage": self.lang,
        }

    def status(self) -> dict[str, Any]:
        return {
            "privacyStatus": self.privacy,
            "selfDeclaredMadeForKids": self.made_for_kids,
            "license": self.license,
            "embeddable": self.embeddable,
        }


@dataclass
class Published:
    """``published.toml``: per language, the video id and the uploaded build's scene lengths."""

    path: Path
    langs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def video(self, lang: str) -> str | None:
        entry = self.langs.get(lang)
        return entry["video"] if entry else None

    def lengths(self, lang: str) -> list[tuple[str, float]] | None:
        entry = self.langs.get(lang)
        return [(str(i), float(s)) for i, s in entry["scenes"]] if entry else None

    def record(self, lang: str, video: str, lengths: Sequence[tuple[str, float]], when: datetime, *,
               save: bool = True) -> None:
        self.langs[lang] = {"video": video, "uploaded": when, "scenes": [list(x) for x in lengths]}
        if save:
            self.save()

    def save(self) -> None:
        out = ["# What's on YouTube, written by `python -m analysis.videos publish` (#395).",
               "# scenes: each scene's length in seconds in the build that was uploaded."]
        for lang, entry in self.langs.items():
            when = entry["uploaded"].strftime("%Y-%m-%dT%H:%M:%SZ")
            out += ["", f"[{lang}]", f'video = "{entry["video"]}"', f"uploaded = {when}", "scenes = ["]
            out += [f'  ["{sid}", {float(s):.2f}],' for sid, s in entry["scenes"]]
            out.append("]")
        self.path.write_text("\n".join(out) + "\n", encoding="utf-8")


def load_published(folder: Path) -> Published:
    path = folder / PUBLISHED_FILE
    if not path.is_file():
        return Published(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ScriptError(f"{path}: {e}") from e
    return Published(path, {k: v for k, v in data.items() if isinstance(v, dict)})


def load_channel(videos_root: Path) -> dict[str, Any]:
    path = videos_root / CHANNEL_FILE
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ScriptError(f"{path}: missing") from e
    except tomllib.TOMLDecodeError as e:
        raise ScriptError(f"{path}: {e}") from e


def _channel_lang(channel: dict[str, Any], lang: str, path: Path) -> dict[str, Any]:
    words = channel.get(lang)
    if not isinstance(words, dict):
        raise ScriptError(f"{path}: no [{lang}] table for {lang} videos")
    return words


def fields(s: Script) -> dict[str, Any]:
    """The script's ``[youtube]`` table, checked."""
    yt = s.meta.get("youtube", {})
    where = f"{s.folder / ('script.md' if s.lang == scripts.DEFAULT_LANG else f'script.{s.lang}.md')}"
    if not isinstance(yt, dict):
        raise ScriptError(f"{where}: youtube must be a table ([youtube])")
    unknown = sorted(set(yt) - _YOUTUBE_KEYS)
    if unknown:
        raise ScriptError(f"{where}: [youtube] has unknown field {unknown[0]!r}")
    for key in ("title", "summary", "previous", "next"):
        if key in yt and not isinstance(yt[key], str):
            raise ScriptError(f"{where}: [youtube] {key} must be text")
    for key in ("contents", "tags"):
        if key in yt and not (isinstance(yt[key], list) and all(isinstance(x, str) for x in yt[key])):
            raise ScriptError(f"{where}: [youtube] {key} must be a list of text")
    chapters = yt.get("chapters", {})
    if not isinstance(chapters, dict):
        raise ScriptError(f"{where}: [youtube] chapters must be a table of scene id = title (or false)")
    ids = {scene.id for scene in s.scenes}
    for sid, value in chapters.items():
        if sid not in ids:
            raise ScriptError(f"{where}: [youtube] chapters names scene `{sid}`, which isn't in the script")
        if not (isinstance(value, str) or value is False):
            raise ScriptError(f"{where}: [youtube] chapters.{sid} must be a title or false")
    return yt


def skipped(s: Script) -> bool:
    return bool(fields(s).get("skip", False))


def number(folder: Path) -> str:
    """The video's number: its folder's prefix (``03``, ``02a``)."""
    return folder.name.split("-", 1)[0]


def one_line(text: str) -> str:
    """A slide title on one line: a break becomes a space, or nothing between CJK characters."""
    def join(m: re.Match[str]) -> str:
        gap = "" if _CJK.match(m[1]) and _CJK.match(m[2]) else " "
        return f"{m[1]}{gap}{m[2]}"
    return re.sub(r"(\S)\s*\n\s*(\S)", join, text).strip()


def chapters(s: Script, lengths: Sequence[tuple[str, float]], first_title: str) -> list[Chapter]:
    """Chapters for a build whose scenes lasted ``lengths``, by YouTube's rules.

    A scene shorter than ten seconds, or one the script sets to false, joins
    the chapter before it; the first chapter is at 0:00 and named
    ``first_title`` unless the script renames it. Fewer than three chapters
    is none at all, which is what YouTube would show.
    """
    renames = fields(s).get("chapters", {})
    titles = {scene.id: scene.title for scene in s.scenes}
    out: list[list[Any]] = []  # [start, title, length]
    t = 0.0
    for i, (sid, length) in enumerate(lengths):
        name = renames.get(sid, first_title if i == 0 else titles.get(sid, sid))
        if out and (name is False or length < MIN_CHAPTER_SECONDS or sid not in titles):
            out[-1][2] += length
        else:
            out.append([t, name or first_title, length])
        t += length
    while len(out) > 1 and out[0][2] < MIN_CHAPTER_SECONDS:
        out[0][2] += out.pop(1)[2]
    if len(out) > 1 and out[-1][2] < MIN_CHAPTER_SECONDS:
        out[-2][2] += out.pop()[2]
    if len(out) < MIN_CHAPTERS:
        return []
    return [Chapter(start, title) for start, title, _ in out]


def timestamp(seconds: float) -> str:
    whole = int(seconds)
    h, rest = divmod(whole, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def video_url(video: str) -> str:
    return f"https://youtu.be/{video}"


def video_folders(videos_root: Path) -> list[Path]:
    return sorted(p for p in videos_root.iterdir() if (p / "script.md").is_file())


def _load_or_none(folder: Path, lang: str) -> Script | None:
    try:
        return scripts.load(folder, lang)
    except ScriptError:
        return None


def neighbors(s: Script) -> tuple[Path | None, Path | None]:
    """The previous and next published videos: named in ``[youtube]``, else the folders around this one."""
    root = s.folder.parent
    yt = fields(s)
    order = [f for f in video_folders(root) if not skipped(scripts.load(f))]
    picked: list[Path | None] = []
    for key, step in (("previous", -1), ("next", 1)):
        if key in yt:
            name = yt[key]
            if not name:
                picked.append(None)
                continue
            if not (root / name / "script.md").is_file():
                raise ScriptError(f"{s.path}: [youtube] {key} = {name!r}: no such video in {root}")
            picked.append(root / name)
            continue
        here = next((i for i, f in enumerate(order) if f.name == s.folder.name), None)
        j = None if here is None else here + step
        picked.append(order[j] if j is not None and 0 <= j < len(order) else None)
    return picked[0], picked[1]


Lookup = Callable[[Path], Published]


def _neighbor_line(template: str, folder: Path | None, lang: str, published: Lookup) -> str | None:
    if folder is None:
        return None
    other = _load_or_none(folder, lang) or scripts.load(folder)
    yt = fields(other)
    line = template.format(episode=other.meta.get("episode", ""),
                           title=one_line(yt.get("title") or other.meta.get("title", folder.name)))
    video = published(folder).video(lang)
    return f"{line} {video_url(video)}" if video else line


def metadata(s: Script, lengths: Sequence[tuple[str, float]], *, channel: dict[str, Any] | None = None,
             published: Lookup = load_published) -> Metadata:
    """Title, description and settings for ``s`` built with scenes of ``lengths``.

    ``published`` gives a video folder's ``published.toml``, for the links to
    this video's other languages and to its neighbors.
    """
    root = s.folder.parent
    channel = channel if channel is not None else load_channel(root)
    path = root / CHANNEL_FILE
    words = _channel_lang(channel, s.lang, path)
    yt = fields(s)
    where = s.folder / ("script.md" if s.lang == scripts.DEFAULT_LANG else f"script.{s.lang}.md")
    if not yt.get("summary"):
        raise ScriptError(f"{where}: [youtube] needs a summary, the description's opening paragraph")

    title = words["title"].format(number=number(s.folder),
                                  title=one_line(yt.get("title") or s.meta.get("title", s.name)))
    parts = [yt["summary"].strip()]
    if yt.get("contents"):
        parts.append("\n".join([words["in_this_video"], *(f"• {item}" for item in yt["contents"])]))
    marks = chapters(s, lengths, words["first_chapter"])
    if marks:
        parts.append("\n".join([words["chapters"], *(f"{timestamp(c.start)} {c.title}" for c in marks)]))
    if words.get("footer"):
        parts.append(words["footer"].strip())
    others = []
    for lang in scripts.languages(s.folder):
        video = published(s.folder).video(lang)
        if lang != s.lang and video:
            label = _channel_lang(channel, lang, path)["version_label"]
            others.append(f"{label}: {video_url(video)}")
    if others:
        parts.append("\n".join(others))
    before, after = neighbors(s)
    links = [line for line in (_neighbor_line(words["previous"], before, s.lang, published),
                               _neighbor_line(words["next"], after, s.lang, published)) if line]
    if links:
        parts.append("\n".join(links))
    description = "\n\n".join(parts)

    tags = tuple(dict.fromkeys([*channel.get("tags", []), *words.get("tags", []), *yt.get("tags", [])]))
    privacy = channel.get("privacy", "private")
    if privacy not in _PRIVACY:
        raise ScriptError(f"{path}: privacy must be one of {', '.join(_PRIVACY)}")
    meta = Metadata(
        lang=s.lang,
        title=title,
        description=description,
        tags=tags,
        category_id=str(channel.get("category_id", 27)),
        privacy=privacy,
        made_for_kids=bool(channel.get("made_for_kids", False)),
        license=channel.get("license", "youtube"),
        embeddable=bool(channel.get("embeddable", True)),
        playlist=words.get("playlist") or None,
    )
    _check_limits(meta, where)
    return meta


def _check_limits(meta: Metadata, where: Path) -> None:
    if len(meta.title) > MAX_TITLE:
        raise ScriptError(f"{where}: YouTube title is {len(meta.title)} characters; the limit is {MAX_TITLE}")
    size = len(meta.description.encode("utf-8"))
    if size > MAX_DESCRIPTION_BYTES:
        raise ScriptError(f"{where}: description is {size} bytes; YouTube's limit is {MAX_DESCRIPTION_BYTES}")
    for what, text in (("title", meta.title), ("description", meta.description), ("tags", "".join(meta.tags))):
        if _ANGLE.search(text):
            raise ScriptError(f"{where}: YouTube refuses < and > in a video's {what}")
    # YouTube counts a tag with a space as if it were quoted (+2).
    tag_chars = sum(len(t) + (2 if " " in t else 0) for t in meta.tags) + max(len(meta.tags) - 1, 0)
    if tag_chars > MAX_TAGS_CHARS:
        raise ScriptError(f"{where}: tags come to {tag_chars} characters; YouTube's limit is {MAX_TAGS_CHARS}")


def preview(meta: Metadata) -> str:
    """The title and description as they'd go up, for a person to read (``<video>.<lang>.youtube.txt``)."""
    return f"{meta.title}\n\n{meta.description}\n"


def check(s: Script) -> None:
    """Check a script's ``[youtube]`` fields without a build: every scene a chapter's length."""
    if "youtube" in s.meta and not skipped(s) and fields(s).get("summary"):
        metadata(s, [(scene.id, 60.0) for scene in s.scenes])
    else:
        fields(s)


# Publishing ---------------------------------------------------------------

def scene_lengths_path(video: Path) -> Path:
    """Where ``build`` leaves the scene lengths of ``video`` (an MP4 in out/videos)."""
    return video.with_suffix(".scenes.json")


def write_scene_lengths(video: Path, lengths: Sequence[tuple[str, float]]) -> Path:
    path = scene_lengths_path(video)
    path.write_text(json.dumps([[sid, round(length, 3)] for sid, length in lengths]) + "\n", encoding="utf-8")
    return path


def secrets_dir() -> Path:
    if os.environ.get(SECRETS_ENV):
        return Path(os.environ[SECRETS_ENV])
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "analysis-videos"


def run_worker(request: dict[str, Any], repo: Path) -> dict[str, Any]:
    """Send one request to the YouTube worker (its own ``uv run --script`` environment)."""
    worker = repo / "scripts" / "videos" / "youtube_api.py"
    # stderr passes through as it comes: the sign-in URL on first use, and
    # the upload's progress.
    result = subprocess.run(["uv", "run", "--quiet", "--script", str(worker)], input=json.dumps(request),
                            stdout=subprocess.PIPE, text=True, check=False)
    return worker_answer(result.stdout, result.returncode)


def worker_answer(stdout: str, returncode: int) -> dict[str, Any]:
    """The worker's answer: the last line it printed, which is JSON."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        answer = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        answer = {}
    if not isinstance(answer, dict) or "video" not in answer:
        answer = {"video": None, "error": stdout.strip() or "the YouTube worker failed (see above)"}
    if returncode and not answer.get("error"):
        answer["error"] = "the YouTube worker failed (see above)"
    return answer


@dataclass(frozen=True)
class Built:
    video: Path
    captions: Path
    thumbnail: Path
    lengths: list[tuple[str, float]]


def built(s: Script, out_dir: Path) -> Built:
    """The build of ``s`` in ``out_dir``, checked to match the script's scenes."""
    stem = f"{s.name}.{s.lang}"
    video, srt, thumb = (out_dir / f"{stem}{suffix}" for suffix in (".mp4", ".srt", ".jpg"))
    lengths_file = scene_lengths_path(video)
    missing = [p.name for p in (video, srt, thumb, lengths_file) if not p.is_file()]
    if missing:
        raise ScriptError(f"{s.name} [{s.lang}]: not built ({', '.join(missing)} missing in {out_dir}); build it first")
    lengths = [(str(i), float(x)) for i, x in json.loads(lengths_file.read_text(encoding="utf-8"))]
    if [sid for sid, _ in lengths] != [scene.id for scene in s.scenes]:
        raise ScriptError(f"{s.name} [{s.lang}]: the build in {out_dir} has other scenes than the script; rebuild it")
    newest = max(p.stat().st_mtime for p in s.folder.glob("script*.md"))
    if newest > video.stat().st_mtime:
        print(f"warning: {s.name}: a script changed after {video.name} was built", file=sys.stderr)
    return Built(video, srt, thumb, lengths)


def publish(
    folders: Sequence[Path],
    *,
    langs: Sequence[str] | None = None,
    out_dir: Path,
    repo: Path,
    dry_run: bool = False,
    new_upload: bool = False,
    refresh_only: bool = False,
    worker: Callable[[dict[str, Any], Path], dict[str, Any]] = run_worker,
    log: Callable[[str], None] = print,
) -> None:
    """Upload what isn't on YouTube yet, then bring every affected description up to date.

    An upload sends the picture, its thumbnail, its captions and its
    settings, and adds it to the language's playlist. A video already up
    only has its title, description and tags updated: never its privacy,
    which is yours to change in Studio, and never its picture, which
    YouTube can't replace (``new_upload`` makes a new video instead).
    ``refresh_only`` uploads nothing.
    Descriptions link to each other, so the neighbors' are refreshed too.
    """
    secrets = str(secrets_dir())
    cache: dict[Path, Published] = {}

    def lookup(folder: Path) -> Published:
        key = folder.resolve()
        if key not in cache:
            cache[key] = load_published(folder)
        return cache[key]

    # Each (folder, language) whose description to bring up to date, in order.
    refresh: dict[tuple[Path, str], None] = {}
    for folder in folders:
        published = lookup(folder)
        for lang in langs or scripts.languages(folder):
            s = scripts.load(folder, lang)
            if skipped(s):
                log(f"{folder.name} [{lang}]: skip = true in [youtube]; not published")
                continue
            refresh[(folder, lang)] = None
            for other in neighbors(s):
                if other is not None:
                    refresh[(other, lang)] = None
            if refresh_only or (published.video(lang) and not new_upload):
                continue
            b = built(s, out_dir)
            meta = metadata(s, b.lengths, published=lookup)
            if dry_run:
                log(f"{folder.name} [{lang}]: would upload {b.video.name} as {meta.privacy}, with its "
                    f"thumbnail and captions{f', to playlist {meta.playlist!r}' if meta.playlist else ''}")
                published.record(lang, "NEW-VIDEO", b.lengths, datetime.now(UTC), save=False)
                continue
            log(f"{folder.name} [{lang}]: uploading {b.video.name}...")
            request = {
                "secrets": secrets,
                "upload": {"file": str(b.video), "status": meta.status()},
                "snippet": meta.snippet(),
                "thumbnail": str(b.thumbnail),
                "captions": {"file": str(b.captions), "language": lang},
                "playlist": meta.playlist,
            }
            answer = worker(request, repo)
            video = answer.get("video")
            if video:  # even after a later step failed: it's up, so a retry mustn't upload it again
                published.record(lang, video, b.lengths, datetime.now(UTC))
            if answer.get("error") or not video:
                where = f" ({video_url(video)} is up; fix it in Studio)" if video else ""
                raise ScriptError(f"{folder.name} [{lang}]: YouTube: {answer.get('error')}{where}")
            log(f"  -> {video_url(video)} ({meta.privacy})")

    for folder, lang in refresh:
        published = lookup(folder)
        video, lengths = published.video(lang), published.lengths(lang)
        if not video or lengths is None:
            continue
        s = _load_or_none(folder, lang)
        if s is None:
            continue
        meta = metadata(s, lengths, published=lookup)
        if dry_run:
            log(f"{folder.name} [{lang}]: would set the title and description of {video}:")
            log("    " + preview(meta).rstrip().replace("\n", "\n    "))
            continue
        answer = worker({"secrets": secrets, "video": video, "snippet": meta.snippet()}, repo)
        if answer.get("error"):
            hint = ""
            if str(answer["error"]).startswith("404"):
                hint = (f" (deleted in Studio? remove [{lang}] from {published.path}, "
                        "and publish uploads it again)")
            raise ScriptError(f"{folder.name} [{lang}]: YouTube: {answer['error']}{hint}")
        log(f"{folder.name} [{lang}]: title and description updated ({video_url(video)})")


def published_folders(videos_root: Path) -> list[Path]:
    return [f for f in video_folders(videos_root) if load_published(f).langs]
