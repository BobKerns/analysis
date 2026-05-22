"""Context-managed access to scenario (intent) and run SQLite DBs.

`ScenarioConnection` opens a fresh intent DB built from the CSV data dir, and
exposes read-only queries plus write operations that mutate the underlying
CSVs and then reload the intent DB to keep reads consistent.

`RunConnection` opens an existing run snapshot DB read-only and shares the
read methods.

Today's surface is intentionally narrow: tags. Other tables will be layered
on as notebook/CLI needs grow.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import pandas as pd

from . import registries
from .loader import load_scenario, resolve_scenario_name

ItemType = Literal["account", "scheduled", "calendar_event"]


def _normalize_tag_collection(
    tags: str | Iterable[str] | Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Accept str, iterable of strings, or iterable of (role, name) pairs."""
    if isinstance(tags, str):
        return registries.parse_tags(tags)
    out: list[tuple[str, str]] = []
    for item in tags:
        if isinstance(item, str):
            out.extend(registries.parse_tags(item))
        else:
            role, name = item
            out.extend(registries.parse_tags(f"{role}:{name}"))
    # de-dupe, preserve order
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str]] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


__all__ = [
    "ItemType",
    "RunConnection",
    "ScenarioConnection",
    "open_run",
    "open_scenario",
]


# ---------- read mixin (shared by Scenario + Run) ----------

_ITEM_SQL: dict[str, str] = {
    "account": (
        "SELECT a.*, "
        "  COALESCE(GROUP_CONCAT(t.role || ':' || t.name, ' '), '') AS tags "
        "FROM account a "
        "LEFT JOIN account_tag x ON x.account_id = a.id "
        "LEFT JOIN tag t ON t.id = x.tag_id "
        "GROUP BY a.id ORDER BY a.id"
    ),
    "scheduled": (
        "SELECT s.*, "
        "  COALESCE(GROUP_CONCAT(t.role || ':' || t.name, ' '), '') AS tags "
        "FROM scheduled_transaction s "
        "LEFT JOIN scheduled_transaction_tag x "
        "  ON x.scheduled_transaction_id = s.id "
        "LEFT JOIN tag t ON t.id = x.tag_id "
        "GROUP BY s.id ORDER BY s.id"
    ),
    "calendar_event": (
        "SELECT c.*, "
        "  COALESCE(GROUP_CONCAT(t.role || ':' || t.name, ' '), '') AS tags "
        "FROM calendar_event c "
        "LEFT JOIN calendar_event_tag x ON x.calendar_event_id = c.id "
        "LEFT JOIN tag t ON t.id = x.tag_id "
        "GROUP BY c.id ORDER BY c.id"
    ),
}


class _TagReader:
    """Read API against ``self._con``. Per-item-type DataFrames are cached."""

    _con: sqlite3.Connection
    _item_cache: dict[str, pd.DataFrame]

    def list_tags(self, role: str | None = None) -> pd.DataFrame:
        """Return distinct tags with per-item-type usage counts."""
        sql = (
            "SELECT t.role, t.name, "
            "  (SELECT COUNT(*) FROM account_tag WHERE tag_id=t.id) AS n_account, "
            "  (SELECT COUNT(*) FROM scheduled_transaction_tag "
            "      WHERE tag_id=t.id) AS n_scheduled, "
            "  (SELECT COUNT(*) FROM calendar_event_tag "
            "      WHERE tag_id=t.id) AS n_calendar_event "
            "FROM tag t"
        )
        params: tuple = ()
        if role is not None:
            sql += " WHERE t.role = ?"
            params = (role,)
        sql += " ORDER BY t.role, t.name"
        return pd.read_sql_query(sql, self._con, params=params)

    def items(
        self,
        item_type: ItemType,
        *,
        tags: str | Iterable[str] | Iterable[tuple[str, str]] | None = None,
        tags_match: Literal["any", "all"] = "any",
        **filters,
    ) -> pd.DataFrame:
        """Return rows of ``item_type``, optionally filtered.

        ``**filters`` keys must be columns on the underlying table; scalar
        values match by equality, iterables (list/tuple/set) by ``isin``.
        ``tags`` accepts a single ``"role:name"`` string, an iterable of
        strings, or an iterable of ``(role, name)`` pairs; ``tags_match``
        is ``"any"`` (default) or ``"all"``.
        """
        if item_type not in _ITEM_SQL:
            raise ValueError(f"unknown item_type {item_type!r}")
        df = self._items_cache(item_type)
        mask = pd.Series(True, index=df.index)

        for col, val in filters.items():
            if col not in df.columns:
                raise KeyError(
                    f"{col!r} is not a column of {item_type} "
                    f"(have: {list(df.columns)})"
                )
            if isinstance(val, str) or not hasattr(val, "__iter__"):
                mask &= df[col] == val
            else:
                mask &= df[col].isin(list(val))

        if tags is not None:
            pairs = _normalize_tag_collection(tags)
            if not pairs:
                mask &= False
            else:
                wanted = {f"{r}:{n}" for r, n in pairs}
                if tags_match == "all":
                    mask &= df["_tag_set"].apply(wanted.issubset)
                else:
                    mask &= df["_tag_set"].apply(lambda s: bool(s & wanted))

        return df.loc[mask].drop(columns=["_tag_set"]).reset_index(drop=True)

    def tags_for_item(self, item_type: ItemType, item_id: int) -> pd.DataFrame:
        """Return the tags attached to one item."""
        link = {
            "account": ("account_tag", "account_id"),
            "scheduled": ("scheduled_transaction_tag", "scheduled_transaction_id"),
            "calendar_event": ("calendar_event_tag", "calendar_event_id"),
        }[item_type]
        sql = (
            f"SELECT t.role, t.name FROM tag t "
            f"JOIN {link[0]} x ON x.tag_id=t.id "
            f"WHERE x.{link[1]}=? ORDER BY t.role, t.name"
        )
        return pd.read_sql_query(sql, self._con, params=(item_id,))

    # -- internal --

    def _items_cache(self, item_type: str) -> pd.DataFrame:
        cache = getattr(self, "_item_cache", None)
        if cache is None:
            cache = {}
            self._item_cache = cache
        if item_type not in cache:
            df = pd.read_sql_query(_ITEM_SQL[item_type], self._con)
            df["_tag_set"] = df["tags"].apply(
                lambda s: set(s.split()) if s else set()
            )
            cache[item_type] = df
        return cache[item_type]

    def _invalidate_items_cache(self) -> None:
        if getattr(self, "_item_cache", None) is not None:
            self._item_cache.clear()


# ---------- run (read-only) ----------

class RunConnection(_TagReader):
    """Read-only access to a finalized run snapshot DB."""

    def __init__(self, run_path: Path | str) -> None:
        self.run_path = Path(run_path)
        self._con = sqlite3.connect(self.run_path)
        self._con.row_factory = sqlite3.Row

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "RunConnection":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@contextmanager
def open_run(run_path: Path | str) -> Iterator[RunConnection]:
    rc = RunConnection(run_path)
    try:
        yield rc
    finally:
        rc.close()


# ---------- scenario (read + write through CSVs) ----------

_TAG_CSV_KEY: dict[ItemType, tuple[str, str]] = {
    # item_type -> (relative-csv-path-template, key column)
    # accounts.csv lives at data_dir root; scheduled.csv lives in scenarios/<name>/
    "account": ("accounts.csv", "name"),
    "scheduled": ("scenarios/{scenario}/scheduled.csv", "name"),
}


class ScenarioConnection(_TagReader):
    """Read+write access to a scenario.

    Reads run against a freshly built intent DB; writes go to the CSV source
    of truth and then trigger a reload so subsequent reads see the change.
    """

    def __init__(self, data_dir: Path | str, scenario: str | None = None) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.scenario = resolve_scenario_name(self.data_dir, scenario)
        self._tmp = tempfile.TemporaryDirectory()
        self._intent_dir = Path(self._tmp.name)
        self._con: sqlite3.Connection | None = None  # type: ignore[assignment]
        self._reload()

    # -- lifecycle --

    def _reload(self) -> None:
        if self._con is not None:
            self._con.close()
        load_scenario(
            self.data_dir, self.scenario,
            intent_dir=self._intent_dir,
            compute_stamps=False, allow_dirty_data=True,
        )
        con = sqlite3.connect(self._intent_dir / f"{self.scenario}.sqlite3")
        con.row_factory = sqlite3.Row
        self._con = con
        self._invalidate_items_cache()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None  # type: ignore[assignment]
        self._tmp.cleanup()

    def __enter__(self) -> "ScenarioConnection":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- tag writes --

    def _tag_csv(self, item_type: ItemType) -> tuple[Path, str]:
        if item_type == "calendar_event":
            raise ValueError(
                "calendar_event tag writes are unsupported via ScenarioConnection "
                "(no natural unique key in calendar.csv)"
            )
        rel, key = _TAG_CSV_KEY[item_type]
        return self.data_dir / rel.format(scenario=self.scenario), key

    def _mutate_tags(
        self,
        item_type: ItemType,
        item_key: str,
        mutate,  # (list[tuple[str,str]]) -> list[tuple[str,str]]
    ) -> str:
        csv_path, key_col = self._tag_csv(item_type)
        if not csv_path.exists():
            raise FileNotFoundError(csv_path)
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        if "tags" not in df.columns:
            raise ValueError(f"{csv_path} has no 'tags' column; run v7 migration")
        mask = df[key_col] == item_key
        if not mask.any():
            raise KeyError(f"no row with {key_col}={item_key!r} in {csv_path}")
        if mask.sum() > 1:
            raise ValueError(f"multiple rows with {key_col}={item_key!r}")
        idx = df.index[mask][0]
        existing = registries.parse_tags(df.at[idx, "tags"])
        new_val = registries.format_tags(mutate(existing))
        df.at[idx, "tags"] = new_val
        df.to_csv(csv_path, index=False)
        self._reload()
        return new_val

    def add_tags(
        self,
        item_type: ItemType,
        item_key: str,
        tokens: Iterable[str] | str,
    ) -> str:
        """Add tag tokens (idempotent). Returns the new tags string."""
        raw = tokens if isinstance(tokens, str) else " ".join(tokens)
        new_pairs = registries.parse_tags(raw)
        return self._mutate_tags(
            item_type, item_key, lambda old: old + new_pairs,
        )

    def remove_tags(
        self,
        item_type: ItemType,
        item_key: str,
        tokens: Iterable[str] | str,
    ) -> str:
        raw = tokens if isinstance(tokens, str) else " ".join(tokens)
        drop = set(registries.parse_tags(raw))
        return self._mutate_tags(
            item_type, item_key, lambda old: [p for p in old if p not in drop],
        )

    def set_tags(
        self,
        item_type: ItemType,
        item_key: str,
        tokens: Iterable[str] | str,
    ) -> str:
        raw = tokens if isinstance(tokens, str) else " ".join(tokens)
        pairs = registries.parse_tags(raw)
        return self._mutate_tags(item_type, item_key, lambda _old: pairs)


@contextmanager
def open_scenario(
    data_dir: Path | str, scenario: str | None = None,
) -> Iterator[ScenarioConnection]:
    sc = ScenarioConnection(data_dir, scenario)
    try:
        yield sc
    finally:
        sc.close()
