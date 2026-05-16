from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

STATE_DB_FILENAME = "state.sqlite3"

# Columns are based on data/Amazon-2026-04.csv headers, normalized to snake_case.
ITEM_TABLE_COLUMNS: list[tuple[str, str]] = [
    ("order_date", "TEXT"),
    ("order_id", "TEXT"),
    ("account_group", "TEXT"),
    ("po_number", "TEXT"),
    ("order_quantity", "TEXT"),
    ("currency", "TEXT"),
    ("order_subtotal", "TEXT"),
    ("order_shipping_and_handling", "TEXT"),
    ("order_promotion", "TEXT"),
    ("order_tax", "TEXT"),
    ("order_net_total", "TEXT"),
    ("order_status", "TEXT"),
    ("approver", "TEXT"),
    ("order_receiving_status", "TEXT"),
    ("order_received_quantity", "TEXT"),
    ("account_user", "TEXT"),
    ("account_user_email", "TEXT"),
    ("invoice_status", "TEXT"),
    ("total_amount", "TEXT"),
    ("invoice_due_amount", "TEXT"),
    ("invoice_issue_date", "TEXT"),
    ("invoice_due_date", "TEXT"),
    ("payment_reference_id", "TEXT"),
    ("payment_date", "TEXT"),
    ("payment_amount", "TEXT"),
    ("payment_instrument_type", "TEXT"),
    ("payment_identifier", "TEXT"),
    ("amazon_internal_product_category", "TEXT"),
    ("asin", "TEXT"),
    ("title", "TEXT"),
    ("unspsc", "TEXT"),
    ("segment", "TEXT"),
    ("family", "TEXT"),
    ("class", "TEXT"),
    ("commodity", "TEXT"),
    ("brand_code", "TEXT"),
    ("brand", "TEXT"),
    ("manufacturer", "TEXT"),
    ("national_stock_number", "TEXT"),
    ("item_model_number", "TEXT"),
    ("part_number", "TEXT"),
    ("product_condition", "TEXT"),
    ("company_compliance", "TEXT"),
    ("listed_ppu", "TEXT"),
    ("purchase_ppu", "TEXT"),
    ("item_quantity", "TEXT"),
    ("item_subtotal", "TEXT"),
    ("item_shipping_and_handling", "TEXT"),
    ("item_promotion", "TEXT"),
    ("item_tax", "TEXT"),
    ("item_net_total", "TEXT"),
    ("po_line_item_id", "TEXT"),
    ("tax_exemption_applied", "TEXT"),
    ("tax_exemption_type", "TEXT"),
    ("tax_exemption_opt_out", "TEXT"),
    ("pricing_savings_program", "TEXT"),
    ("pricing_discount_applied", "TEXT"),
    ("receiving_status", "TEXT"),
    ("received_quantity", "TEXT"),
    ("received_date", "TEXT"),
    ("receiver_name", "TEXT"),
    ("receiver_email", "TEXT"),
    ("gl_code", "TEXT"),
    ("department", "TEXT"),
    ("cost_center", "TEXT"),
    ("project_code", "TEXT"),
    ("location", "TEXT"),
    ("custom_field_1", "TEXT"),
    ("seller_name", "TEXT"),
    ("seller_credentials", "TEXT"),
    ("seller_city", "TEXT"),
    ("seller_state", "TEXT"),
    ("seller_zip_code", "TEXT"),
]


class JsonStateStore:
    """Persist categorization state in SQLite."""

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / STATE_DB_FILENAME

        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

        self._ensure_schema()

    def load_categories(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM categories ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def save_categories(self, categories: list[str]) -> None:
        unique = sorted(
            {
                cleaned
                for category in categories
                if (cleaned := normalize_category_name(category)) is not None
            }
        )
        category_placeholders = ", ".join("?" for _ in unique)
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO categories(name) VALUES(?)",
                ((category,) for category in unique),
            )
            if unique:
                self._conn.execute(
                    f"""
                    DELETE FROM item_category_map
                    WHERE category_id IN (
                        SELECT id FROM categories WHERE name NOT IN ({category_placeholders})
                    )
                    """,
                    unique,
                )
                self._conn.execute(
                    f"DELETE FROM categories WHERE name NOT IN ({category_placeholders})",
                    unique,
                )
            else:
                self._conn.execute("DELETE FROM item_category_map")
                self._conn.execute("DELETE FROM categories")

    def load_mapping(self) -> dict[str, str]:
        rows = self._conn.execute(
            """
            SELECT m.item, c.name AS category
            FROM item_category_map AS m
            JOIN categories AS c ON c.id = m.category_id
            ORDER BY m.item COLLATE NOCASE
            """
        ).fetchall()
        return {str(row["item"]): str(row["category"]) for row in rows}

    def save_mapping(self, mapping: dict[str, str]) -> None:
        clean = {
            key.strip(): normalized
            for key, value in mapping.items()
            if key.strip()
            and (normalized := normalize_category_name(value)) is not None
        }
        categories = sorted({*self.load_categories(), *clean.values()})

        with self._conn:
            for category in categories:
                self._conn.execute(
                    "INSERT OR IGNORE INTO categories(name) VALUES(?)",
                    (category,),
                )
            self._conn.execute("DELETE FROM item_category_map")
            self._conn.executemany(
                """
                INSERT INTO item_category_map(item, category_id)
                VALUES(?, (SELECT id FROM categories WHERE name = ?))
                """,
                ((item, category) for item, category in clean.items()),
            )

    def insert_event(self, payload: dict[str, Any]) -> None:
        user = str(payload.get("user") or os.getenv("USER", ""))
        event_payload = dict(payload)
        event_payload.setdefault("user", user)

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO events(ts, level, logger, message, event, user, payload_json)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event_payload.get("ts", "")),
                    str(event_payload.get("level", "")),
                    str(event_payload.get("logger", "")),
                    str(event_payload.get("message", "")),
                    str(event_payload.get("event", "")),
                    user,
                    json.dumps(event_payload, default=str),
                ),
            )

    def close(self) -> None:
        self._conn.close()

    def __del__(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _ensure_schema(self) -> None:
        item_columns_sql = ",\n                ".join(
            f"{name} {data_type}" for name, data_type in ITEM_TABLE_COLUMNS
        )

        with self._conn:
            self._conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE
                );

                CREATE TABLE IF NOT EXISTS item_category_map (
                    item TEXT PRIMARY KEY,
                    category_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (category_id) REFERENCES categories(id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    level TEXT,
                    logger TEXT,
                    message TEXT,
                    event TEXT,
                    user TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY,
                    source_file TEXT,
                    normalized_item TEXT,
                    assigned_category TEXT,
                    {item_columns_sql}
                );

                CREATE TABLE IF NOT EXISTS monthly_summaries (
                    id INTEGER PRIMARY KEY,
                    summary_month_start TEXT NOT NULL,
                    category TEXT NOT NULL,
                    amount_total REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(summary_month_start, category)
                );

                CREATE INDEX IF NOT EXISTS idx_categories_name ON categories(name);
                CREATE INDEX IF NOT EXISTS idx_item_map_category_id
                    ON item_category_map(category_id);
                CREATE INDEX IF NOT EXISTS idx_events_event_ts ON events(event, ts);
                CREATE INDEX IF NOT EXISTS idx_items_assigned_category ON items(assigned_category);
                CREATE INDEX IF NOT EXISTS idx_items_title ON items(title);
                CREATE INDEX IF NOT EXISTS idx_monthly_summaries_month
                    ON monthly_summaries(summary_month_start);

                DROP VIEW IF EXISTS item_explore;

                CREATE VIEW item_explore AS
                SELECT
                    id,
                    order_date,
                    title,
                    order_status,
                    item_quantity,
                    item_net_total,
                    total_amount,
                    assigned_category
                FROM items;
                """
            )

            event_columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(events)").fetchall()
            }
            if "user" not in event_columns:
                self._conn.execute(
                    "ALTER TABLE events ADD COLUMN user TEXT NOT NULL DEFAULT ''"
                )

            user_default = self._conn.execute(
                "SELECT dflt_value FROM pragma_table_info('events') WHERE name = 'user'"
            ).fetchone()
            if user_default and user_default[0] is not None:
                self._conn.executescript(
                    """
                    CREATE TABLE events_new (
                        id INTEGER PRIMARY KEY,
                        ts TEXT NOT NULL,
                        level TEXT,
                        logger TEXT,
                        message TEXT,
                        event TEXT,
                        user TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );

                    INSERT INTO events_new(
                        id, ts, level, logger, message, event, user, payload_json
                    )
                    SELECT
                        id,
                        ts,
                        level,
                        logger,
                        message,
                        event,
                        CASE
                            WHEN user IS NULL OR TRIM(user) = '' THEN 'rwk'
                            ELSE user
                        END,
                        payload_json
                    FROM events;

                    DROP TABLE events;
                    ALTER TABLE events_new RENAME TO events;
                    """
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_event_ts ON events(event, ts)"
                )

            self._conn.execute(
                "UPDATE events SET user = 'rwk' WHERE user IS NULL OR TRIM(user) = ''"
            )


def normalize_category_name(raw: str) -> str | None:
    """Normalize category input and reject control/non-printing characters."""
    cleaned = str(raw).strip()
    if not cleaned:
        return None

    for char in cleaned:
        if (not char.isprintable()) or unicodedata.category(char).startswith("C"):
            return None

    return cleaned
