"""Parse a video's ``script.md`` (and its ``script.<lang>.md`` translations).

Format, in brief (``docs/videos/README.md`` has the full description)::

    +++
    title = "..."            # TOML front matter: fields every slide can use
    data = "prototype_data"  # demo data a tape starts from (repo-relative)
    +++

    ## Scene `intro`: Opening title

    ```slide
    template = "title"
    ```

    Narration is every plain paragraph in the scene.

    > Block quotes, lists and HTML comments are production notes, not spoken.

A scene's id (the backticked word) is what a translation refers to, so it
stays put when the English heading is reworded. A translation holds the same
scenes with only narration and slide fields; tapes and templates come from
the English script, since the terminal shows the CLI in English.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_LANG = "en"

_SCENE_HEADING = re.compile(r"^##\s+Scene\s+`(?P<id>[a-z0-9][a-z0-9_-]*)`\s*(?::\s*(?P<title>.*\S))?\s*$")
_FENCE = re.compile(r"^(?P<fence>```+|~~~+)\s*(?P<info>[\w-]*)\s*$")
_LANG_FILE = re.compile(r"^script\.(?P<lang>[a-z]{2,3}(?:-[A-Za-z0-9]+)?)\.md$")
# Sentence ends: Western punctuation followed by space, or CJK full stops.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|`)")

VISUAL_KINDS = ("tape", "slide")


class ScriptError(ValueError):
    """A script that can't be built, with the file and line that says why."""


@dataclass(frozen=True)
class Visual:
    """What the scene shows: a terminal ``tape`` or a ``slide``."""

    kind: str
    # The tape's VHS commands, or the slide's TOML source.
    source: str
    # Slide fields (template, duration, and whatever the template reads).
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Scene:
    id: str
    title: str
    narration: tuple[str, ...]  # paragraphs, markdown removed
    visual: Visual
    line: int

    @property
    def sentences(self) -> tuple[str, ...]:
        """Narration split into sentences: the unit of synthesis and of captions."""
        out: list[str] = []
        for paragraph in self.narration:
            out.extend(s.strip() for s in _SENTENCE_END.split(paragraph) if s.strip())
        return tuple(out)


@dataclass(frozen=True)
class Script:
    path: Path
    lang: str
    meta: dict[str, Any]
    scenes: tuple[Scene, ...]

    @property
    def folder(self) -> Path:
        return self.path.parent

    @property
    def name(self) -> str:
        return self.folder.name


def plain_text(markdown: str) -> str:
    """What a narrator says for a markdown paragraph: links to their text, no emphasis."""
    text = _LINK.sub(r"\1", markdown)
    text = _EMPHASIS.sub("", text)
    return " ".join(text.split())


def _front_matter(lines: list[str], path: Path) -> tuple[dict[str, Any], int]:
    if not lines or lines[0].strip() != "+++":
        return {}, 0
    for end in range(1, len(lines)):
        if lines[end].strip() == "+++":
            try:
                return tomllib.loads("\n".join(lines[1:end])), end + 1
            except tomllib.TOMLDecodeError as e:
                raise ScriptError(f"{path}:1: front matter: {e}") from e
    raise ScriptError(f"{path}:1: front matter opened with +++ is never closed")


def _parse(path: Path, *, translation: bool) -> tuple[dict[str, Any], list[Scene]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    meta, i = _front_matter(lines, path)
    scenes: list[Scene] = []
    seen: set[str] = set()

    current: dict[str, Any] | None = None
    paragraph: list[str] = []

    def end_paragraph() -> None:
        if current is not None and paragraph:
            current["narration"].append(plain_text(" ".join(paragraph)))
        paragraph.clear()

    def end_scene() -> None:
        end_paragraph()
        if current is None:
            return
        visual = current["visual"]
        if visual is None and not translation:
            raise ScriptError(f"{path}:{current['line']}: scene `{current['id']}` has no ```tape or ```slide block")
        scenes.append(
            Scene(
                id=current["id"],
                title=current["title"],
                narration=tuple(current["narration"]),
                visual=visual if visual is not None else Visual("slide", "", {}),
                line=current["line"],
            )
        )

    in_comment = False
    in_note = False
    while i < len(lines):
        line = lines[i]
        lineno = i + 1
        stripped = line.strip()
        i += 1

        if in_comment:
            in_comment = "-->" not in stripped
            continue
        if stripped.startswith("<!--"):
            end_paragraph()
            in_comment = "-->" not in stripped
            continue

        heading = _SCENE_HEADING.match(stripped)
        if heading:
            end_scene()
            scene_id = heading["id"]
            if scene_id in seen:
                raise ScriptError(f"{path}:{lineno}: scene `{scene_id}` appears twice")
            seen.add(scene_id)
            current = {
                "id": scene_id,
                "title": heading["title"] or scene_id,
                "narration": [],
                "visual": None,
                "line": lineno,
            }
            continue
        if stripped.startswith("#"):
            if stripped.startswith("## "):
                raise ScriptError(f"{path}:{lineno}: a level-2 heading must be ## Scene `id`: Title")
            end_paragraph()
            continue

        fence = _FENCE.match(stripped)
        if fence:
            end_paragraph()
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != fence["fence"]:
                body.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ScriptError(f"{path}:{lineno}: code block is never closed")
            i += 1
            kind = fence["info"]
            if kind not in VISUAL_KINDS:
                continue  # an illustrative code block, not a visual
            if current is None:
                raise ScriptError(f"{path}:{lineno}: ```{kind} outside a scene")
            if current["visual"] is not None:
                raise ScriptError(f"{path}:{lineno}: scene `{current['id']}` has a second visual")
            if translation and kind == "tape":
                raise ScriptError(f"{path}:{lineno}: a translation can't change a tape; it comes from script.md")
            source = "\n".join(body)
            fields: dict[str, Any] = {}
            if kind == "slide":
                try:
                    fields = tomllib.loads(source)
                except tomllib.TOMLDecodeError as e:
                    raise ScriptError(f"{path}:{lineno}: slide fields: {e}") from e
                if not translation and "template" not in fields:
                    raise ScriptError(f"{path}:{lineno}: a slide needs template = \"<name>\"")
            else:
                for n, tape_line in enumerate(body, start=lineno + 1):
                    if tape_line.strip().startswith("Output"):
                        raise ScriptError(f"{path}:{n}: a tape must not set Output; the builder does")
            current["visual"] = Visual(kind, source, fields)
            continue

        if not stripped:
            end_paragraph()
            in_note = False
        elif stripped.startswith((">", "|")) or re.match(r"^([-*+]|\d+[.)])\s", stripped):
            end_paragraph()  # notes, lists and tables aren't spoken, through to the next blank line
            in_note = True
        elif current is not None and not in_note:
            paragraph.append(stripped)

    if in_comment:
        raise ScriptError(f"{path}: an HTML comment is never closed")
    end_scene()
    return meta, scenes


def _translate(base: Script, path: Path, lang: str) -> Script:
    meta, scenes = _parse(path, translation=True)
    by_id = {s.id: s for s in scenes}
    unknown = sorted(set(by_id) - {s.id for s in base.scenes})
    if unknown:
        first = by_id[unknown[0]]
        raise ScriptError(f"{path}:{first.line}: scene `{first.id}` isn't in {base.path.name}")
    merged: list[Scene] = []
    for scene in base.scenes:
        tr = by_id.get(scene.id)
        if tr is None:
            if scene.narration:
                raise ScriptError(f"{path}: scene `{scene.id}` has no {lang} narration")
            merged.append(scene)
            continue
        if scene.narration and not tr.narration:
            raise ScriptError(f"{path}:{tr.line}: scene `{scene.id}` has no {lang} narration")
        visual = scene.visual
        if tr.visual.fields:
            if "template" in tr.visual.fields and tr.visual.fields["template"] != scene.visual.fields.get("template"):
                raise ScriptError(f"{path}:{tr.line}: a translation can't change a slide's template")
            if scene.visual.kind != "slide":
                raise ScriptError(f"{path}:{tr.line}: scene `{scene.id}` is a tape; only slides have fields")
            visual = replace(visual, fields={**scene.visual.fields, **tr.visual.fields})
        merged.append(replace(scene, title=tr.title, narration=tr.narration, visual=visual))
    return Script(path=base.path, lang=lang, meta={**base.meta, **meta}, scenes=tuple(merged))


def load(folder: Path, lang: str = DEFAULT_LANG) -> Script:
    """The video in ``folder`` in ``lang``: script.md, overlaid by script.<lang>.md."""
    path = folder / "script.md"
    if not path.is_file():
        raise ScriptError(f"{folder}: no script.md")
    meta, scenes = _parse(path, translation=False)
    if not scenes:
        raise ScriptError(f"{path}: no scenes (## Scene `id`: Title)")
    base = Script(path=path, lang=meta.get("lang", DEFAULT_LANG), meta=meta, scenes=tuple(scenes))
    if lang == base.lang:
        return base
    tr_path = folder / f"script.{lang}.md"
    if not tr_path.is_file():
        raise ScriptError(f"{folder}: no {tr_path.name} for language {lang!r}")
    return _translate(base, tr_path, lang)


def languages(folder: Path) -> list[str]:
    """Languages the video in ``folder`` has: its own, then each translation."""
    meta, _ = _front_matter((folder / "script.md").read_text(encoding="utf-8").splitlines(), folder / "script.md")
    langs = [meta.get("lang", DEFAULT_LANG)]
    for p in sorted(folder.iterdir()):
        m = _LANG_FILE.match(p.name)
        if m:
            langs.append(m["lang"])
    return langs
