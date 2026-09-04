"""Tests for analysis.sim.accounts_plot."""

from __future__ import annotations

import pandas as pd

from analysis.sim.accounts_plot import trim_to_last_nonzero


def test_trim_to_last_nonzero_keeps_one_month_past_last_nonzero() -> None:
    idx = pd.date_range("2026-01-31", periods=6, freq="ME")
    df = pd.DataFrame(
        {"A": [100, 200, 0, 0, 0, 0], "B": [50, 0, 30, 0, 0, 0]}, index=idx,
    )

    trimmed = trim_to_last_nonzero(df, ["A", "B"])

    assert list(trimmed.index) == list(idx[:4])  # through the April zero


def test_trim_to_last_nonzero_treats_missing_as_zero() -> None:
    idx = pd.date_range("2026-01-31", periods=4, freq="ME")
    df = pd.DataFrame({"A": [100, None, None, None]}, index=idx)

    trimmed = trim_to_last_nonzero(df, ["A"])

    assert list(trimmed.index) == list(idx[:2])


def test_trim_to_last_nonzero_all_zero_returns_unchanged() -> None:
    idx = pd.date_range("2026-01-31", periods=3, freq="ME")
    df = pd.DataFrame({"A": [0, 0, 0]}, index=idx)

    trimmed = trim_to_last_nonzero(df, ["A"])

    assert list(trimmed.index) == list(idx)


def test_trim_to_last_nonzero_only_considers_selected_columns() -> None:
    idx = pd.date_range("2026-01-31", periods=4, freq="ME")
    df = pd.DataFrame({"A": [100, 0, 0, 0], "B": [0, 0, 0, 900]}, index=idx)

    trimmed = trim_to_last_nonzero(df, ["A"])

    assert list(trimmed.index) == list(idx[:2])
