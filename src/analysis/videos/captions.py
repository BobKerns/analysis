"""Captions: one cue per narrated sentence, timed from the synthesized audio."""

from __future__ import annotations

import math
import re
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass

# Two lines of about this many characters is what players show comfortably.
LINE_WIDTH = 42
# CJK has no spaces to break at: a part is about two lines of 16 characters,
# broken after a comma (、).
CJK_MAX = 32


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def scene_cues(sentences: Sequence[str], durations: Sequence[float], *, start: float, gap: float) -> list[Cue]:
    """Cues for one scene's narration, which starts ``start`` seconds into the video."""
    if len(sentences) != len(durations):
        raise ValueError(f"{len(sentences)} sentences but {len(durations)} durations")
    cues = []
    t = start
    for sentence, d in zip(sentences, durations, strict=True):
        pieces = _pieces(sentence)
        total = sum(len(p) for p in pieces)
        at = t
        for piece in pieces:
            span = d * len(piece) / total
            cues.append(Cue(at, at + span, piece))
            at += span
        t += d + gap
    return cues


def _pieces(sentence: str) -> list[str]:
    """A sentence in parts of at most two caption lines, timed by their length."""
    if " " not in sentence:
        return _cjk_pieces(sentence)
    count = math.ceil(len(textwrap.wrap(sentence, LINE_WIDTH)) / 2)
    if count <= 1:
        return [sentence]
    # Even parts, so no part is a stranded word or two.
    target = len(sentence) / count
    pieces: list[str] = []
    current: list[str] = []
    for word in sentence.split():
        current.append(word)
        if len(pieces) < count - 1 and len(" ".join(current)) >= target:
            pieces.append(" ".join(current))
            current = []
    if current:
        pieces.append(" ".join(current))
    return pieces


def _cjk_pieces(sentence: str) -> list[str]:
    """A CJK sentence (no spaces) in parts of about CJK_MAX characters, broken after commas."""
    if len(sentence) <= CJK_MAX:
        return [sentence]
    clauses = [c for c in re.split(r"(?<=[、，,])", sentence) if c]
    count = math.ceil(len(sentence) / CJK_MAX)
    target = len(sentence) / count
    pieces: list[str] = []
    current = ""
    for clause in clauses:
        if current and len(current) + len(clause) > CJK_MAX and len(current) >= target / 2:
            pieces.append(current)
            current = ""
        current += clause
    if current:
        pieces.append(current)
    return pieces


def _stamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap(text: str) -> str:
    if " " not in text:  # CJK: no spaces to break at; players wrap it themselves
        return text
    return "\n".join(textwrap.wrap(text, LINE_WIDTH)) or text


def srt(cues: Sequence[Cue]) -> str:
    """The cues as SubRip, the caption format YouTube and most players accept."""
    blocks = [f"{i}\n{_stamp(c.start)} --> {_stamp(c.end)}\n{_wrap(c.text)}\n" for i, c in enumerate(cues, start=1)]
    return "\n".join(blocks)
