import sqlite3
from pathlib import Path

from analysis.storage import JsonStateStore


def test_json_state_store_roundtrip(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "state")

    store.save_categories(["Groceries", "Household", "Groceries"])
    store.save_mapping({"coffee pods": "Groceries", "paper towels": "Household"})

    assert store.load_categories() == ["Groceries", "Household"]
    assert store.load_mapping() == {
        "coffee pods": "Groceries",
        "paper towels": "Household",
    }


def test_state_store_creates_sqlite_schema(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = JsonStateStore(state_dir)

    db_path = state_dir / "state.sqlite3"
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    views = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()
    }
    conn.close()

    assert "categories" in tables
    assert "item_category_map" in tables
    assert "events" in tables
    assert "items" in tables
    assert "item_explore" in views

    store.close()


def test_state_store_does_not_create_json_state_files(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = JsonStateStore(state_dir)
    store.save_categories(["Groceries"])
    store.save_mapping({"coffee pods": "Groceries"})
    store.close()

    assert not (state_dir / "categories.json").exists()
    assert not (state_dir / "item_category_map.json").exists()
    assert not (state_dir / "events.jsonl").exists()
