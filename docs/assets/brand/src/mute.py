"""Tone the stream renders down so they sit behind text: scene.jpg, strip.jpg.

The bright renders compete with a page's text; illustration, not art.
Reads stream-scene.* and stream-strip.* (the .png from render.js if present,
else the committed .jpg).
"""

from pathlib import Path

from PIL import Image, ImageEnhance

HERE = Path(__file__).parent


def mute(src: Path, dst: Path) -> None:
    im = Image.open(src).convert("RGB")
    im = ImageEnhance.Color(im).enhance(0.5)
    im = ImageEnhance.Contrast(im).enhance(0.7)
    im = ImageEnhance.Brightness(im).enhance(0.82)
    im.save(dst, quality=90)


for stem, out in (("stream-scene", "scene.jpg"), ("stream-strip", "strip.jpg")):
    png = HERE / f"{stem}.png"
    src = png if png.exists() else HERE / f"{stem}.jpg"
    if png.exists():
        Image.open(png).convert("RGB").save(HERE / f"{stem}.jpg", quality=90)
    mute(src, HERE / out)
