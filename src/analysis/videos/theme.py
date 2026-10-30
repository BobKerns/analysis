"""The shared look: ``docs/videos/theme/theme.toml`` plus its templates and assets."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analysis.videos.script import ScriptError


@dataclass(frozen=True)
class Theme:
    root: Path  # docs/videos
    data: dict[str, Any]

    @property
    def width(self) -> int:
        return int(self.data["video"]["width"])

    @property
    def height(self) -> int:
        return int(self.data["video"]["height"])

    @property
    def fps(self) -> int:
        return int(self.data["video"]["fps"])

    def timing(self, key: str) -> float:
        return float(self.data["timing"][key])

    @property
    def templates(self) -> Path:
        return self.root / "theme" / "templates"

    @property
    def assets(self) -> Path:
        return self.root / "assets"

    def narration(self, lang: str) -> dict[str, Any]:
        table = self.data.get("narration", {})
        if lang not in table:
            raise ScriptError(f"theme.toml has no [narration.{lang}] table for language {lang!r}")
        return dict(table[lang])

    def css(self) -> str:
        """The palette and fonts as CSS custom properties on :root."""
        props = [f"  --{k}: {v};" for k, v in self.data["palette"].items()]
        props += [f"  --font-{k}: {v};" for k, v in self.data["fonts"].items()]
        props += [f"  --width: {self.width}px;", f"  --height: {self.height}px;"]
        return ":root {\n" + "\n".join(props) + "\n}\n"

    def vhs_settings(self) -> list[str]:
        """``Set`` lines that put a tape in the theme."""
        t = self.data["terminal"]
        colors = {"name": "analysis", **t["colors"]}
        return [
            'Set Shell "bash"',
            f'Set FontFamily "{t["font_family"]}"',
            f"Set FontSize {int(t['font_size'])}",
            f"Set Width {self.width}",
            f"Set Height {self.height}",
            f"Set Padding {int(t['padding'])}",
            f"Set Framerate {self.fps}",
            f"Set TypingSpeed {t['typing_speed']}",
            f"Set WindowBar {t['window_bar']}",
            f"Set Theme {json.dumps(colors)}",
        ]

    def bg_hex(self) -> str:
        """The background color as ffmpeg wants it (0xRRGGBB)."""
        return "0x" + str(self.data["palette"]["bg"]).lstrip("#")


def load(root: Path) -> Theme:
    path = root / "theme" / "theme.toml"
    try:
        return Theme(root=root, data=tomllib.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError as e:
        raise ScriptError(f"{path}: missing") from e
    except tomllib.TOMLDecodeError as e:
        raise ScriptError(f"{path}: {e}") from e


def videos_root(folder: Path) -> Path:
    """The docs/videos folder above a video folder (the one holding theme/)."""
    for parent in (folder, *folder.resolve().parents):
        if (parent / "theme" / "theme.toml").is_file():
            return parent
    raise ScriptError(f"{folder}: no theme/theme.toml in it or above it")
