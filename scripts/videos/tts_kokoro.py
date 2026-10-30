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
"""

import json
import sys

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro


def main() -> None:
    req = json.load(sys.stdin)
    kokoro = Kokoro(req["model"], req["voices"])
    pieces = []
    durations = []
    rate = 24000
    for i, sentence in enumerate(req["sentences"]):
        samples, rate = kokoro.create(sentence, voice=req["voice"], speed=float(req["speed"]), lang=req["lang"])
        durations.append(len(samples) / rate)
        pieces.append(samples)
        if i < len(req["sentences"]) - 1:
            pieces.append(np.zeros(int(rate * float(req["gap"])), dtype=samples.dtype))
    audio = np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float32)
    sf.write(req["out"], audio, rate)
    json.dump({"durations": durations}, sys.stdout)


if __name__ == "__main__":
    main()
