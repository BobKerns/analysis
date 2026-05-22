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

class _TagReader:
    """Read-only tag queries against ``self._con``."""

    _con: sqlite3.Connection

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

    def items_for_tag(
        self,
        role: str,
        name: str,
        item_type: ItemType | None = None,
    ) -> pd.DataFrame:
        """Return items carrying ``role:name``.

        Columns: ``item_type``, ``item_id``, ``label``. ``label`` is the
        natural identifier per type (account name, scheduled name, or
        ``date kind [scheduled_name]`` for calendar events).
        """
        tag_row = self._con.execute(
            "SELECT id FROM tag WHERE role=? AND name=?", (role, name)
        ).fetchone()
        if tag_row is None:
            return pd.DataFrame(columns=["item_type", "item_id", "label"])
        tid = tag_row[0] if not isinstance(tag_row, sqlite3.Row) else tag_row["id"]

        queries = {
            "account": (
                "SELECT 'account' AS item_type, a.id AS item_id, a.name AS label "
                "FROM account a JOIN account_tag x ON x.account_id=a.id "
                "WHERE x.tag_id=? ORDER BY a.name"
            ),
            "scheduled": (
                "SELECT 'scheduled' AS item_type, s.id AS item_id, s.name AS label "
                "FROM scheduled_transaction s "
                "JOIN scheduled_transaction_tag x "
                "  ON x.scheduled_transaction_id=s.id "
                "WHERE x.tag_id=? ORDER BY s.name"
            ),
            "calendar_event": (
                "SELECT 'calendar_event' AS item_type, c.id AS item_id, "
                "  c.date || ' ' || c.kind || "
                "    COALESCE(' '||c.scheduled_name,'') AS label "
                "FROM calendar_event c "
                "JOIN calendar_event_tag x ON x.calendar_event_id=c.id "
                "WHERE x.tag_id=? ORDER BY c.date"
            ),
        }
        if item_type is not None:
            return pd.read_sql_query(queries[item_type], self._con, params=(tid,))
        frames = [
            pd.read_sql_query(q, self._con, params=(tid,)) for q in queries.values()
        ]
        return pd.concat(frames, ignore_index=True)

    def items_with_tags(
        self,
        tags: str | Iterable[str] | Iterable[tuple[str, str]],
        item_type: ItemType | None = None,
        match: Literal["any", "all"] = "any",
    ) -> pd.DataFrame:
        """Items carrying one or more tags.

        ``tags`` may be a single ``"role:name"`` string, an iterable of such
        strings (whitespace inside a string is also split), or an iterable
        of ``(role, name)`` pairs. ``match="any"`` (default) returns items
        carrying at least one of the tags; ``match="all"`` requires every
        tag. Columns: ``item_type``, ``item_id``, ``label``, ``n_matched``.
        """
        pairs = _normalize_tag_collection(tags)
        if not pairs:
            return pd.DataFrame(
                columns=["item_type", "item_id", "label", "n_matched"]
            )
        rows = self._con.execute(
            f"SELECT id, role, name FROM tag WHERE (role, name) IN "
            f"(VALUES {','.join(['(?, ?)'] * len(pairs))})",
            [v for p in pairs for v in p],
        ).fetchall()
        ids = [r[0] for r in rows]
        if not ids:
            return pd.DataFrame(
                columns=["item_type", "item_id", "label", "n_matched"]
            )
        threshold = len(pairs) if match == "all" else 1
        placeholders = ",".join("?" * len(ids))

        per_type = {
            "account": (
                f"SELECT 'account' AS item_type, a.id AS item_id, "
                f"  a.name AS label, COUNT(*) AS n_matched "
                f"FROM account a JOIN account_tag x ON x.account_id=a.id "
                f"WHERE x.tag_id IN ({placeholders}) "
                f"GROUP BY a.id HAVING n_matched >= ? ORDER BY a.name"
            ),
            "scheduled": (
                f"SELECT 'scheduled' AS item_type, s.id AS item_id, "
                f"  s.name AS label, COUNT(*) AS n_matched "
                f"FROM scheduled_transaction s "
                f"JOIN scheduled_transaction_tag x "
                f"  ON x.scheduled_transaction_id=s.id "
                f"WHERE x.tag_id IN ({placeholders}) "
                f"GROUP BY s.id HAVING n_matched >= ? ORDER BY s.name"
            ),
            "calendar_event": (
                f"SELECT 'calendar_event' AS item_type, c.id AS item_id, "
                f"  c.date || ' ' || c.kind || "
                f"    COALESCE(' '||c.scheduled_name,'') AS label, "
                f"  COUNT(*) AS n_matched "
                f"FROM calendar_event c "
                f"JOIN calendar_event_tag x ON x.calendar_event_id=c.id "
                f"WHERE x.tag_id IN ({placeholders}) "
                f"GROUP BY c.id HAVING n_matched >= ? ORDER BY c.date"
            ),
        }
        sqls = (
            [per_type[item_type]] if item_type is not None else list(per_type.values())
        )
        frames = [
            pd.read_sql_query(q, self._con, params=[*ids, threshold]) for q in sqls
        ]
        return pd.concat(frames, ignore_index=True)

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
