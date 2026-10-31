"""The shared look: ``docs/videos/theme/theme.toml`` plus its templates and assets."""

from __future__ import annotations

import json
import re
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
    def scale(self) -> int:
        """Output pixels per layout pixel: 2 renders a 1920x1080 layout at 4K."""
        return int(self.data["video"].get("scale", 1))

    @property
    def out_width(self) -> int:
        return self.width * self.scale

    @property
    def out_height(self) -> int:
        return self.height * self.scale

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
            f"Set FontSize {int(t['font_size']) * self.scale}",
            f"Set Width {self.out_width}",
            f"Set Height {self.out_height}",
            f"Set Padding {int(t['padding']) * self.scale}",
            f"Set WindowBarSize {int(t.get('window_bar_size', 30)) * self.scale}",
            # `Wait` in a tape waits for the prompt, however long the command takes.
            f"Set WaitPattern /{re.escape(str(t['prompt']))} \\$$/",
            "Set WaitTimeout 120s",
            f"Set Framerate {self.fps}",
            f"Set TypingSpeed {t['typing_speed']}",
            f"Set WindowBar {t['window_bar']}",
            f"Set Theme {json.dumps(colors)}",
            # Record the terminal's image layer too (`sim chain show --graph`);
            # needs the VHS build render.VHS_VERSION names.
            "Set Sixel true",
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
