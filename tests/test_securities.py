"""securities.csv: names for the symbols lots hold (the ``security`` table)."""
# cspell:ignore nqqq

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.provenance import _scoped_paths


def _securities(loaded: LoadResult) -> list[tuple[str, str]]:
    con = sqlite3.connect(loaded.intent_path)
    try:
        return con.execute("SELECT symbol, name FROM security ORDER BY symbol").fetchall()
    finally:
        con.close()


def test_rows_are_loaded_into_the_security_table(
    test_data_dir: Path, load_test_copy: Callable[[], LoadResult]
) -> None:
    (test_data_dir / "securities.csv").write_text(
        "symbol,name\nVTI,Total Stock Market ETF\nAAPL,Apple Inc.\n"
    )
    assert _securities(load_test_copy()) == [
        ("AAPL", "Apple Inc."), ("VTI", "Total Stock Market ETF"),
    ]


def test_the_file_is_optional(test_data_dir: Path, load_test_copy: Callable[[], LoadResult]) -> None:
    (test_data_dir / "securities.csv").unlink()
    assert _securities(load_test_copy()) == []


@pytest.mark.parametrize(("text", "message"), [
    ("symbol,name\nVTI,A\nVTI,B\n", r"line 3 \(VTI\): duplicate symbol"),
    ("symbol,name\nVTI,\n", r"line 2 \(VTI\): missing name"),
    ("symbol,name\n,Apple Inc.\n", r"line 2: missing symbol"),
])
def test_bad_rows_are_rejected(
    text: str, message: str, test_data_dir: Path, load_test_copy: Callable[[], LoadResult],
) -> None:
    (test_data_dir / "securities.csv").write_text(text)
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_a_symbol_without_a_row_is_allowed(
    test_data_dir: Path, load_test_copy: Callable[[], LoadResult],
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
) -> None:
    set_brokerage_lots([{"lot": "default", "symbol": "NOPE", "quantity": "1",
                         "opening_balance": "1.00"}])
    assert _securities(load_test_copy()) == []


def test_securities_csv_is_inside_the_provenance_scope(test_data_dir: Path) -> None:
    assert "securities.csv" in _scoped_paths(test_data_dir, "retire_alice")


def test_the_run_snapshot_carries_the_names(two_lot_run) -> None:
    assert list(two_lot_run.items("security")["symbol"]) == ["AAA", "BBB"]


@pytest.mark.parametrize(("path", "text", "message"), [
    ("securities.csv", "symbol,name\nqqq,Q\n", r"securities.csv line 2: symbol 'qqq' must be upper case \('QQQ'\)"),
    ("allocations.csv", "symbol,as_of,asset_class,weight\nQqq,,bond,100%\n",
     r"symbol 'Qqq' must be upper case \('QQQ'\)"),
    ("prices/security_price__qqq.csv", "effective_date,price\n2026-05-29,1.00\n",
     r"security_price__qqq.csv: symbol 'qqq' must be upper case \('QQQ'\)"),
])
def test_a_lower_case_symbol_is_rejected(
    path: str, text: str, message: str, test_data_dir: Path, load_test_copy: Callable[[], LoadResult],
) -> None:
    (test_data_dir / path).parent.mkdir(exist_ok=True)
    (test_data_dir / path).write_text(text)
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_a_lower_case_symbol_on_a_lot_is_rejected(
    load_test_copy: Callable[[], LoadResult],
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
) -> None:
    set_brokerage_lots([{"lot": "default", "symbol": "vti", "quantity": "1", "opening_balance": "1.00"}])
    with pytest.raises(LoadError, match=r"symbol 'vti' must be upper case \('VTI'\)"):
        load_test_copy()
