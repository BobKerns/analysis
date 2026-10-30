# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["kokoro-onnx==0.6.1", "soundfile>=0.13"]
# ///
"""Synthesize one scene's narration with Kokoro (run by analysis.videos, not by hand).

Runs under `uv run --script`, in its own environment: kokoro-onnx doesn't
support the project's Python yet, and nobody who isn't making videos should
have to install it.

Reads a JSON request on stdin:
    {"model": ..., "voices": ..., "voice": ..., "lang": ..., "speed": ...,
     "gap": seconds, "sentences": [...], "out": "scene.wav"}
writes `out` (mono, the model's sample rate) with `gap` seconds of silence
between sentences, and prints {"durations": [seconds per sentence]}.

A sentence may carry IPA phonemes between \u27e6 and \u27e7 (a script's
{word|/phonemes/} respelling): the text around them is phonemized as usual
and the phonemes are spliced in as written.
"""

import json
import re
import sys

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro


_IPA = re.compile("\u27e6([^\u27e7]*)\u27e7")


def phonemes(kokoro: Kokoro, sentence: str, lang: str) -> str:
    """The sentence's phonemes, with its IPA respellings spliced in as written."""
    parts = []
    pos = 0
    for m in _IPA.finditer(sentence):
        text = sentence[pos : m.start()].strip()
        if text:
            parts.append(kokoro.tokenizer.phonemize(text, lang))
        parts.append(m[1])
        pos = m.end()
    rest = sentence[pos:].strip()
    if rest:
        parts.append(kokoro.tokenizer.phonemize(rest, lang))
    return " ".join(p.strip() for p in parts if p.strip())


def main() -> None:
    req = json.load(sys.stdin)
    kokoro = Kokoro(req["model"], req["voices"])
    pieces = []
    durations = []
    rate = 24000
    for i, sentence in enumerate(req["sentences"]):
        ipa = _IPA.search(sentence) is not None
        text = phonemes(kokoro, sentence, req["lang"]) if ipa else sentence
        samples, rate = kokoro.create(
            text, voice=req["voice"], speed=float(req["speed"]), lang=req["lang"], is_phonemes=ipa
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
