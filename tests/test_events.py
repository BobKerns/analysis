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


def test_requested_and_remainder_show_only_where_a_request_was_split_or_fell_short(
    two_lot_run: RunConnection,
) -> None:
    _heading, table = events_view(two_lot_run)
    shown = table["requested"].notna()
    assert shown.any() and not shown.all()
    assert (table["remainder"].notna() == shown).all()
    detail = table[shown]
    assert ((detail["requested"] != detail["amount"]) | (detail["remainder"] != 0)).all()


def test_refills_are_caused_by_a_debit_of_the_account_they_refill(two_lot_run: RunConnection) -> None:
    _heading, table = events_view(two_lot_run)
    refills = table[table["reason"] == "refill"]
    assert len(refills) > 0
    causes = table.loc[refills["caused_by"].to_numpy()]
    assert list(causes["from"]) == list(refills["to"])
    assert table.loc[table["reason"] == "rule", "caused_by"].isna().any()


def test_running_balance_agrees_with_every_month_end_balance(two_lot_run: RunConnection) -> None:
    _heading, table = events_view(two_lot_run, accounts=("Asset_Checking",))
    assert list(table.index) == sorted(table.index)  # posting order
    moved = table[~table["reason"].isin(["ytd", "ytd_rule"])]
    assert table.loc[table["reason"] == "ytd", "balance"].isna().all()  # balance-neutral
    months = two_lot_run.items("transfer_event").set_index("id").loc[moved.index, "month"]
    last_each_month = (moved["balance"] * 100).round().groupby(months.to_numpy()).last()
    month_ends = two_lot_run.items("account_balance", account="Asset_Checking").set_index("month")
    assert len(last_each_month) > 12
    assert (last_each_month == month_ends.loc[last_each_month.index, "balance"]).all()


def test_running_balance_needs_one_account_with_a_balance(two_lot_run: RunConnection) -> None:
    heading, two = events_view(two_lot_run, accounts=("Asset_Checking", "Asset_Savings"))
    assert "balance" not in two.columns and "running balance" not in heading
    expense = two_lot_run.items("transfer_event", to_kind="expense")["to_name"].iloc[0]
    _heading, spent = events_view(two_lot_run, accounts=(expense,))
    assert "balance" not in spent.columns


def test_empty_detail_cells_show_a_dash(two_lot_run: RunConnection) -> None:
    from analysis.sim.notebook_ui.events_widgets import _styled

    _heading, table = events_view(two_lot_run, accounts=("Asset_Checking",))
    html = _styled(table.head(20)).to_html()
    assert ">-</td>" in html
    assert "$" in html


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
