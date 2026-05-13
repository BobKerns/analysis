from __future__ import annotations

from pathlib import Path

import pandas as pd

from analysis.amazon.categorizer import CategorizationResult, categorize_amazon_csv
from analysis.storage import JsonStateStore


def load_categorization_state(state_dir: Path | str = "state") -> tuple[list[str], dict[str, str]]:
    """Load current categories and item mappings for notebook inspection."""
    store = JsonStateStore(state_dir)
    return store.load_categories(), store.load_mapping()


def load_events_dataframe(state_dir: Path | str = "state") -> pd.DataFrame:
    """Load structured run events from events.jsonl as a DataFrame.

    Returns an empty DataFrame when no log file exists yet.
    Use this to query run history, categorization decisions, and trends::

        df = load_events_dataframe("state")
        df[df["event"] == "categorize.run.complete"]
    """
    log_path = Path(state_dir) / "events.jsonl"
    if not log_path.exists():
        return pd.DataFrame()
    return pd.read_json(log_path, lines=True)


def run_amazon_categorization(
    input_csv_path: Path | str,
    state_dir: Path | str = "state",
    output_dir: Path | str = "out",
    ask_for_category=None,
) -> tuple[CategorizationResult, pd.DataFrame, pd.DataFrame]:
    """Run categorization and return both metadata and resulting output DataFrames."""
    result = categorize_amazon_csv(
        input_csv_path=input_csv_path,
        state_dir=state_dir,
        output_dir=output_dir,
        ask_for_category=ask_for_category,
    )
    annotated = pd.read_csv(result.annotated_csv_path)
    summary = pd.read_csv(result.summary_csv_path)
    return result, annotated, summary