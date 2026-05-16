from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from analysis.amazon.categorizer import CategorizationResult, categorize_amazon_csv
from analysis.storage import STATE_DB_FILENAME, JsonStateStore


def load_categorization_state(state_dir: Path | str = "state") -> tuple[list[str], dict[str, str]]:
    """Load current categories and item mappings for notebook inspection."""
    store = JsonStateStore(state_dir)
    return store.load_categories(), store.load_mapping()


def load_events_dataframe(state_dir: Path | str = "state") -> pd.DataFrame:
    """Load structured run events from SQLite as a DataFrame.

    Returns an empty DataFrame when no state database exists yet.
    Use this to query run history, categorization decisions, and trends::

        df = load_events_dataframe("state")
        df[df["event"] == "categorize.run.complete"]
    """
    db_path = Path(state_dir) / STATE_DB_FILENAME
    if not db_path.exists():
        return pd.DataFrame()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT payload_json FROM events ORDER BY id").fetchall()

    if not rows:
        return pd.DataFrame()

    payloads = [json.loads(str(row[0])) for row in rows]
    return pd.DataFrame(payloads)


def summarize_over_all_time(state_dir: Path | str = "state") -> pd.DataFrame:
    """Load all-time category totals from SQLite monthly summaries."""
    db_path = Path(state_dir) / STATE_DB_FILENAME
    if not db_path.exists():
        return pd.DataFrame(columns=["category", "amount_total_all_time"])

    query = """
        SELECT
            category,
            SUM(amount_total) AS amount_total_all_time
        FROM monthly_summaries
        GROUP BY category
        ORDER BY amount_total_all_time DESC, category COLLATE NOCASE
    """
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql_query(query, conn)


def run_amazon_categorization(
    input_csv_path: Path | str,
    state_dir: Path | str = "state",
    output_dir: Path | str = "out",
    cutoff_day: int = 3,
    ask_for_category=None,
) -> tuple[CategorizationResult, pd.DataFrame, pd.DataFrame]:
    """Run categorization and return both metadata and resulting output DataFrames."""
    result = categorize_amazon_csv(
        input_csv_path=input_csv_path,
        state_dir=state_dir,
        output_dir=output_dir,
        cutoff_day=cutoff_day,
        ask_for_category=ask_for_category,
    )
    annotated = pd.read_csv(result.annotated_csv_path)
    summary = pd.read_csv(result.summary_csv_path)
    return result, annotated, summary