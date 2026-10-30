"""Instructional video production (#373).

A video is a folder under ``docs/videos/`` holding a ``script.md``: scenes,
each pairing narration with one visual (a VHS terminal tape or a slide built
from an HTML template). ``python -m analysis.videos build <folder>`` turns it
into an MP4 plus captions, caching every scene's narration, visual and
composite by a hash of its inputs, so editing one sentence re-renders one
scene. ``docs/videos/README.md`` documents the conventions.
"""
