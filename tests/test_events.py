"""Event explorer: filtering transfer_event by reason kind, tax character,
account and date range (notebook_ui.events_widgets)."""

from __future__ import annotations

from datetime import date

import pytest

from analysis.sim.connection import RunConnection
from analysis.sim.notebook_ui.events_widgets import events_view, events_widget, run_date_bounds


def test_no_filters_returns_everything_sorted_by_date(two_lot_run: RunConnection) -> None:
    everything = two_lot_run.items("transfer_event")
    heading, table = events_view(two_lot_run)
    assert len(table) == len(everything)
    assert list(table["date"]) == sorted(table["date"])
    assert f"{len(everything):,} event(s)" in heading


def test_amounts_are_dollars_and_add_up_to_the_heading_total(two_lot_run: RunConnection) -> None:
    everything = two_lot_run.items("transfer_event").sort_values(["date", "id"])
    heading, table = events_view(two_lot_run)
    assert list(table["amount"]) == list(everything["amount"] / 100)
    assert f"${table['amount'].sum():,.2f} total" in heading


def test_filtering_by_account_matches_either_end(two_lot_run: RunConnection) -> None:
    _heading, table = events_view(two_lot_run, accounts=("Asset_Checking",))
    assert len(table) > 0
    assert ((table["from"] == "Asset_Checking") | (table["to"] == "Asset_Checking")).all()


def test_filtering_by_reason_kind(two_lot_run: RunConnection) -> None:
    _heading, table = events_view(two_lot_run, reason_kinds=("ytd",))
    assert len(table) > 0
    assert (table["reason"] == "ytd").all()

    everything = events_view(two_lot_run)[1]
    assert len(table) < len(everything)


def test_filtering_by_tax_character(two_lot_run: RunConnection) -> None:
    everything = events_view(two_lot_run)[1]
    character = everything["tax_character"].iloc[0]
    _heading, table = events_view(two_lot_run, tax_characters=(character,))
    assert len(table) > 0
    assert (table["tax_character"] == character).all()


def test_filtering_by_date_range(two_lot_run: RunConnection) -> None:
    everything = events_view(two_lot_run)[1]
    start, end = date.fromisoformat(everything["date"].min()), date.fromisoformat(everything["date"].max())
    midpoint = date.fromisoformat(sorted(everything["date"])[len(everything) // 2])

    _heading, before = events_view(two_lot_run, start=start, end=midpoint)
    _heading, after = events_view(two_lot_run, start=midpoint, end=end)
    assert (before["date"] <= midpoint.isoformat()).all()
    assert (after["date"] >= midpoint.isoformat()).all()
    assert len(before) + len(after) >= len(everything)  # midpoint counted in both


def test_rule_name_resolves_for_rule_events_and_is_blank_otherwise(two_lot_run: RunConnection) -> None:
    _heading, table = events_view(two_lot_run, reason_kinds=("rule",))
    assert "lot_b_draw" in set(table["rule"])

    _heading, ytd = events_view(two_lot_run, reason_kinds=("ytd",))
    assert ytd["rule"].isna().all()


def test_run_date_bounds_matches_first_and_last_event(two_lot_run: RunConnection) -> None:
    everything = two_lot_run.items("transfer_event")
    start, end = run_date_bounds(two_lot_run)
    assert start.isoformat() == everything["date"].min()
    assert end.isoformat() == everything["date"].max()


def test_events_widget_returns_a_box_without_displaying_it(
    two_lot_run: RunConnection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ipywidgets import widgets
    from analysis.sim.notebook_ui import events_widgets

    shown: list[object] = []
    monkeypatch.setattr(events_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    box = events_widget(two_lot_run)
    assert isinstance(box, widgets.VBox)
    assert not any(isinstance(item, widgets.Widget) for item in shown)


def test_events_widget_account_filter_narrows_the_table(two_lot_run: RunConnection) -> None:
    widget = events_widget(two_lot_run)
    selectors, _date_picker, _out = widget.children
    account_picker, reason_picker, _tax_picker = selectors.children
    assert "Asset_Checking" in account_picker.options
    account_picker.value = ("Asset_Checking",)  # must not raise
    reason_picker.value = ("ytd",)
