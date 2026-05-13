from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonStateStore:
    """Persist categorization state as JSON files under a state directory."""

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.categories_path = self.state_dir / "categories.json"
        self.mapping_path = self.state_dir / "item_category_map.json"

    def load_categories(self) -> list[str]:
        data = self._read_json(self.categories_path, default=[])
        return [str(item) for item in data]

    def save_categories(self, categories: list[str]) -> None:
        unique = sorted({category.strip() for category in categories if category.strip()})
        self._write_json(self.categories_path, unique)

    def load_mapping(self) -> dict[str, str]:
        data = self._read_json(self.mapping_path, default={})
        return {str(key): str(value) for key, value in data.items()}

    def save_mapping(self, mapping: dict[str, str]) -> None:
        clean = {
            key.strip(): value.strip()
            for key, value in mapping.items()
            if key.strip() and value.strip()
        }
        self._write_json(self.mapping_path, clean)

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, payload: Any) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
