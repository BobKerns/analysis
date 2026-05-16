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


def test_state_store_bootstraps_legacy_files(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "categories.json").write_text('["Groceries", "Household"]\n', encoding="utf-8")
    (state_dir / "item_category_map.json").write_text(
        '{"coffee pods": "Groceries", "paper towels": "Household"}\n',
        encoding="utf-8",
    )
    (state_dir / "events.jsonl").write_text(
        '{"ts":"2026-05-15T10:00:00","level":"INFO","logger":"analysis.test",'
        '"message":"bootstrapped","event":"test.bootstrap"}\n',
        encoding="utf-8",
    )

    store = JsonStateStore(state_dir)
    assert store.load_categories() == ["Groceries", "Household"]
    assert store.load_mapping() == {
        "coffee pods": "Groceries",
        "paper towels": "Household",
    }

    conn = sqlite3.connect(state_dir / "state.sqlite3")
    event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    assert event_count == 1

    store.close()
