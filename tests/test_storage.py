import json
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
    event_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(events)"
        ).fetchall()
    }
    item_pk = conn.execute(
        "SELECT name FROM pragma_table_info('items') WHERE pk = 1"
    ).fetchone()
    user_default = conn.execute(
        "SELECT dflt_value FROM pragma_table_info('events') WHERE name = 'user'"
    ).fetchone()
    conn.close()

    assert "categories" in tables
    assert "item_category_map" in tables
    assert "events" in tables
    assert "items" in tables
    assert "monthly_summaries" in tables
    assert "item_explore" in views
    assert "user" in event_columns
    assert item_pk is not None
    assert item_pk[0] == "order_id"
    assert user_default is not None
    assert user_default[0] is None

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


def test_insert_event_uses_user_env_var(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("USER", "test-user")

    state_dir = tmp_path / "state"
    store = JsonStateStore(state_dir)
    store.insert_event({"event": "test.event", "message": "hello"})
    store.close()

    conn = sqlite3.connect(state_dir / "state.sqlite3")
    row = conn.execute(
        "SELECT user, payload_json FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "test-user"
    payload = json.loads(str(row[1]))
    assert payload["user"] == "test-user"


def test_insert_items_upserts_by_order_id_and_populates_columns(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = JsonStateStore(state_dir)
    store.insert_items(
        [
            {
                "Order ID": "A-100",
                "Order Date": "2026-04-01",
                "Title": "Coffee Pods",
                "Item Net Total": "12.34",
                "normalized_item": "coffee pods",
                "category": "Food",
            }
        ],
        source_file="data/Amazon-2026-04.csv",
    )
    store.insert_items(
        [
            {
                "Order ID": "A-100",
                "Order Date": "2026-04-01",
                "Title": "Coffee Pods Updated",
                "Item Net Total": "12.34",
                "normalized_item": "coffee pods",
                "category": "Groceries",
            }
        ],
        source_file="data/Amazon-2026-04.csv",
    )
    store.close()

    conn = sqlite3.connect(state_dir / "state.sqlite3")
    rows = conn.execute(
        "SELECT order_id, title, assigned_category, order_date, item_net_total FROM items"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "A-100"
    assert row[1] == "Coffee Pods Updated"
    assert row[2] == "Groceries"
    assert row[3] == "2026-04-01"
    assert row[4] == "12.34"


def test_insert_items_defaults_shipment_date_to_payment_date(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = JsonStateStore(state_dir)
    store.insert_items(
        [
            {
                "Order ID": "A-200",
                "Payment Date": "08/18/2026",
                "Title": "Paper Towels",
                "normalized_item": "paper towels",
                "category": "Household",
            }
        ],
        source_file="data/Amazon-2026-08.csv",
    )
    store.insert_items(
        [
            {
                "Order ID": "A-201",
                "Shipment Date": "03/02/2026",
                "Payment Date": "03/05/2026",
                "Title": "Coffee Pods",
                "normalized_item": "coffee pods",
                "category": "Groceries",
            }
        ],
        source_file="data/Amazon-2026-03.csv",
    )
    store.close()

    conn = sqlite3.connect(state_dir / "state.sqlite3")
    rows = dict(
        conn.execute("SELECT order_id, shipment_date FROM items ORDER BY order_id").fetchall()
    )
    conn.close()

    assert rows["A-200"] == "2026-08-18"
    assert rows["A-201"] == "2026-03-02"


def test_query_items_by_shipment_date(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = JsonStateStore(state_dir)
    store.insert_items(
        [
            {
                "Order ID": "A-300",
                "Shipment Date": "03/15/2026",
                "Title": "In Range",
                "Item Quantity": "1",
                "Item Net Total": "10.00",
                "normalized_item": "in range",
                "category": "Household",
            },
            {
                "Order ID": "A-301",
                "Shipment Date": "04/01/2026",
                "Title": "Out Of Range",
                "Item Quantity": "1",
                "Item Net Total": "20.00",
                "normalized_item": "out of range",
                "category": "Household",
            },
        ],
        source_file="data/Amazon-2026-03.csv",
    )

    rows = store.query_items_by_shipment_date("2026-03-01", "2026-04-01")
    store.close()

    assert [row["title"] for row in rows] == ["In Range"]


def test_delete_generated_items_removes_only_synthesized_order_ids(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = JsonStateStore(state_dir)
    store.insert_items(
        [
            {
                "Order ID": "A-400",
                "Title": "Real Order",
                "normalized_item": "real order",
                "category": "Household",
            },
            {
                # No Order ID: gets a synthesized "__generated__:..." key.
                "Title": "No Order Id",
                "normalized_item": "no order id",
                "category": "Household",
            },
        ],
        source_file="data/Amazon-2026-03.csv",
    )

    deleted = store.delete_generated_items()

    conn = sqlite3.connect(state_dir / "state.sqlite3")
    remaining = [row[0] for row in conn.execute("SELECT order_id FROM items").fetchall()]
    conn.close()
    store.close()

    assert deleted == 1
    assert remaining == ["A-400"]

