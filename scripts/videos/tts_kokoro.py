# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["kokoro-onnx==0.6.1", "soundfile>=0.13", "misaki[ja]==0.9.4"]
# ///
# cspell:ignore sess
"""Synthesize one scene's narration with Kokoro (run by analysis.videos, not by hand).

Runs under `uv run --script`, in its own environment: kokoro-onnx doesn't
support the project's Python yet, and nobody who isn't making videos should
have to install it.

Reads a JSON request on stdin:
    {"model": ..., "voices": ..., "voice": ..., "lang": ..., "speed": ...,
     "gap": seconds, "sentences": [...], "out": "scene.wav", "threads": n}
writes `out` (mono, the model's sample rate) with `gap` seconds of silence
between sentences, and prints {"durations": [seconds per sentence]}.
`threads` (optional) caps the model's CPU threads, for builds that run
several narrations and a recording at once.

A sentence may carry IPA phonemes between \u27e6 and \u27e7 (a script's
{word|/phonemes/} respelling): the text around them is phonemized as usual
and the phonemes are spliced in as written.

Japanese goes through misaki's G2P (OpenJTalk), as Kokoro's own pipeline
does: kokoro-onnx's espeak-ng phonemizer reads kanji and kana as "Chinese
letter" and "Japanese letter". OpenJTalk's dictionary is downloaded on first
use.
"""

import json
import re
import sys

import numpy as np
import onnxruntime as rt
import soundfile as sf
from kokoro_onnx import Kokoro
from kokoro_onnx.session import resolve_providers


_IPA = re.compile("\u27e6([^\u27e7]*)\u27e7")


_ja_g2p = None


def _phonemize(kokoro: Kokoro, text: str, lang: str) -> str:
    global _ja_g2p
    if not lang.startswith("ja"):
        return kokoro.tokenizer.phonemize(text, lang)
    if _ja_g2p is None:
        from misaki import ja

        _ja_g2p = ja.JAG2P(version="pyopenjtalk")
    marked, _tokens = _ja_g2p(text)
    # misaki returns the phonemes with a pitch line of the same length appended.
    return marked[: len(marked) // 2]


def phonemes(kokoro: Kokoro, sentence: str, lang: str) -> str:
    """The sentence's phonemes, with its IPA respellings spliced in as written."""
    parts = []
    pos = 0
    for m in _IPA.finditer(sentence):
        text = sentence[pos : m.start()].strip()
        if text:
            parts.append(_phonemize(kokoro, text, lang))
        parts.append(m[1])
        pos = m.end()
    rest = sentence[pos:].strip()
    if rest:
        parts.append(_phonemize(kokoro, rest, lang))
    return " ".join(p.strip() for p in parts if p.strip())


def _kokoro(req: dict) -> Kokoro:
    threads = req.get("threads")
    if not threads:
        return Kokoro(req["model"], req["voices"])
    options = rt.SessionOptions()
    options.intra_op_num_threads = int(threads)
    options.inter_op_num_threads = 1
    session = rt.InferenceSession(req["model"], sess_options=options, providers=resolve_providers())
    return Kokoro.from_session(session, req["voices"])


def main() -> None:
    req = json.load(sys.stdin)
    kokoro = _kokoro(req)
    pieces = []
    durations = []
    rate = 24000
    for i, sentence in enumerate(req["sentences"]):
        # Phonemize here (not in kokoro-onnx) for IPA splices and for Japanese.
        own = _IPA.search(sentence) is not None or req["lang"].startswith("ja")
        text = phonemes(kokoro, sentence, req["lang"]) if own else sentence
        samples, rate = kokoro.create(
            text, voice=req["voice"], speed=float(req["speed"]), lang=req["lang"], is_phonemes=own
        )
        durations.append(len(samples) / rate)
        pieces.append(samples)
        if i < len(req["sentences"]) - 1:
            pieces.append(np.zeros(int(rate * float(req["gap"])), dtype=samples.dtype))
    audio = np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float32)
    sf.write(req["out"], audio, rate)
    json.dump({"durations": durations}, sys.stdout)


if __name__ == "__main__":
    main()
