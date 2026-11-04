"""Scenario overview views: chains, scheduled rules and calendar events, with the lot
in its own column; year-to-date rows, itemizations and tags (#435)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import pandas as pd
import pytest
from ipywidgets import widgets

from analysis.sim import setup_listings
from analysis.sim.connection import open_scenario
from analysis.sim.notebook_ui.scenario_plots import (
    SCHEDULED_OPTIONAL_COLUMNS,
    scheduled_styler,
    show_calendar_view,
    show_chains,
    show_itemizations,
    show_scheduled_view,
    show_tags,
    show_ytd_view,
)
from analysis.sim.readme import description_view
from analysis.sim.notebook_ui.scenario_view import (
    calendar_view,
    UNUSED,
    chain_table,
    chains_view,
    itemization_detail,
    itemizations_view,
    scheduled_view,
    tags_view,
    ytd_view,
)


def _steps(test_data_dir: Path):
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = chains_view(scenario)
        return view, view[view["step"].notna()].set_index(["chain", "step"])


def test_description_view_relevels_the_readme_heading(test_data_dir: Path) -> None:
    (test_data_dir / "scenarios" / "retire_alice" / "README.md").write_text(
        "# Retirement baseline\n\nAlice and Bill's main plan.\n"
    )
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        md = description_view(scenario)
    assert md.data == (
        "### Retirement baseline\n\nAlice and Bill's main plan.\n\n"
        "**Parent:** `base`  \n**Load order:** `t_empty` → `t_inflation_factors` → `t_inflation_series` → "
        "`t_market_factors` → `t_market_series` → `base` → `retire_alice`"
    )


def test_description_view_regenerates_the_more_link_against_data_dir(
    test_data_dir: Path,
) -> None:
    """A description's "see more" link is only ever correct for the data_dir
    it was generated under (ADR-003 §4.1e); passing the connection's own
    data_dir back in repoints it rather than leaving it stale."""
    (test_data_dir / "scenarios" / "retire_alice" / "README.md").write_text(
        "# Retirement baseline\n\nAlice and Bill's main plan.\n\nMore detail here.\n"
    )
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        md = description_view(scenario, data_dir=test_data_dir)
    expected_readme = test_data_dir / "scenarios" / "retire_alice" / "README.md"
    assert md.data == (
        "### Retirement baseline\n\nAlice and Bill's main plan.\n\n"
        f"_See [scenarios/retire_alice/README.md]({expected_readme}) for more._\n\n"
        "**Parent:** `base`  \n**Load order:** `t_empty` → `t_inflation_factors` → `t_inflation_series` → "
        "`t_market_factors` → `t_market_series` → `base` → `retire_alice`"
    )


def test_lot_is_a_dash_when_the_account_has_a_single_lot(test_data_dir: Path) -> None:
    _, steps = _steps(test_data_dir)
    rows = steps.loc["asset_checking_liquidity"]
    assert list(rows["draw_from"]) == ["Asset_Savings", "Asset_TraditionalIRA_Alice"]
    assert list(rows["lot"]) == ["-", "-"]      # 'default' is not shown when it is the only lot
    assert not any(":" in name for name in steps["draw_from"])


def test_lots_are_spelled_out_where_an_account_has_several(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    add_account_value: Callable[..., None],
) -> None:
    add_account_value("Asset_Roth_Alice", lot="b", opening_balance="10.00")
    (retire_alice_csv("chains/source") / "roth_first.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\n"
        "Asset_Roth_Alice,default,,,,\n"    # one of two lots, and it is named default
        "Asset_Roth_Alice,b,,,,\n"
        "Asset_Savings,,,,,\n"
    )
    _, steps = _steps(test_data_dir)
    rows = steps.loc["roth_first"]
    assert list(zip(rows["draw_from"], rows["lot"], strict=True)) == [
        ("Asset_Roth_Alice", "default"), ("Asset_Roth_Alice", "b"), ("Asset_Savings", "-"),
    ]


def test_a_step_that_draws_from_another_chain_has_no_lot(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    (retire_alice_csv("chains/source") / "outer.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nasset_checking_liquidity,,,,,\n"
    )
    _, steps = _steps(test_data_dir)
    assert steps.loc[("outer", 1), "draw_from"] == "asset_checking_liquidity"
    assert steps.loc[("outer", 1), "lot"] == "-"


def test_leaf_rows_map_a_chain_name_to_its_account_and_lot(
    test_data_dir: Path, add_account_value: Callable[..., None],
) -> None:
    add_account_value("Asset_Roth_Alice", lot="b", opening_balance="10.00")
    view, _ = _steps(test_data_dir)
    leaves = view[view["step"].isna()].set_index("chain")
    assert (leaves.loc["Asset_Roth_Alice:b", "draw_from"], leaves.loc["Asset_Roth_Alice:b", "lot"]) == (
        "Asset_Roth_Alice", "b")
    assert leaves.loc["Asset_Checking", "lot"] == "-"


def test_show_chains_displays_the_lot_column(test_data_dir: Path, monkeypatch) -> None:
    shown = []
    monkeypatch.setattr("analysis.sim.notebook_ui.scenario_plots.display",
                        lambda styled, **_: shown.append(styled))
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        show_chains(scenario)
    columns = list(shown[0].data.columns)
    assert columns.index("lot") == columns.index("draw_from") + 1
    assert set(shown[0].data["step"]) == {1, 2}  # interior chains only


def _outer_chain(retire_alice_csv: Callable[[str], Path]) -> None:
    """A second chain, after the prototype's: one step inflating with cpi,
    one drawing from the prototype's chain, and nothing using it."""
    (retire_alice_csv("chains/source") / "outer.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\n"
        "Asset_Savings,,500.00,,cpi,annual\n"
        "asset_checking_liquidity,,,,,\n"
    )


def test_chain_table_names_each_chain_once_with_its_role_and_uses(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    _outer_chain(retire_alice_csv)
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        table = chain_table(scenario)
    assert list(table["chain"]) == ["asset_checking_liquidity", "", "outer", ""]
    assert list(table["role"]) == ["source", "", "source", ""]
    assert table.loc[0, "used_by"].split("\n") == [
        "a step of outer (step 2)", "refills Asset_Checking",
    ]
    assert list(table["used_by"])[1:] == ["", UNUSED, ""]
    assert (table.loc[2, "maximum"], table.loc[2, "series"], table.loc[2, "cadence"]) == (
        500.0, "cpi", "annual")


def test_show_chains_rules_off_each_chain_and_flags_an_unused_one(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], monkeypatch,
) -> None:
    _outer_chain(retire_alice_csv)
    shown = []
    monkeypatch.setattr("analysis.sim.notebook_ui.scenario_plots.display",
                        lambda styled, **_: shown.append(styled))
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        show_chains(scenario)
    html = shown[0].to_html()
    # The first row of the second chain starts a new block, all the way across.
    assert _rows_with(html, "border-top: 2px solid #888") == {2}
    assert _columns_with(html, "border-top: 2px solid #888") == set(
        range(len(shown[0].data.columns)))
    assert _rows_with(html, "color: red") == {2}
    assert "a step of outer (step 2)<br>refills Asset_<wbr>Checking" in html


def test_scheduled_view_has_separate_account_and_lot_columns(
    two_lot_run_file: Path, test_data_dir: Path,
) -> None:
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = scheduled_view(scenario).set_index("name")
    columns = list(view.columns)
    assert columns[:5] == ["id", "from_account", "from_lot", "to_account", "to_lot"]
    assert "from_name" not in columns and "to_name" not in columns
    # An account with several lots names the lot; single-lot accounts and chains show '-'.
    growth = view[view["to_account"] == "Asset_Brokerage_Joint"]
    assert sorted(growth["to_lot"]) == ["a", "b"]
    draw = view.loc["lot_b_draw"]
    assert (draw["from_account"], draw["from_lot"]) == ("lotb", "-")      # a chain
    assert (draw["to_account"], draw["to_lot"]) == ("Asset_Checking", "-")  # one lot
    assert not any(":" in str(v) for v in (*view["from_account"], *view["to_account"]))
    assert view.loc["rmd_alice", "from_lot"] == "-"                       # the IRA has one lot


def test_calendar_view_has_separate_account_and_lot_columns(
    two_lot_run_file: Path, test_data_dir: Path,
) -> None:
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = calendar_view(scenario)
    assert list(view.columns)[3:7] == ["account_1", "lot_1", "account_2", "lot_2"]
    inheritance = view[view["account_2"] == "Asset_Brokerage_Joint"].iloc[0]
    assert (inheritance["account_1"], inheritance["lot_1"]) == ("Income_Inheritance", "-")
    assert inheritance["lot_2"] == "a"
    events_without_accounts = view[view["account_1"] == "-"]
    assert not events_without_accounts.empty
    assert set(events_without_accounts["lot_1"]) == {"-"}


def test_optional_scheduled_columns_default_to_name_and_description(test_data_dir: Path) -> None:
    assert SCHEDULED_OPTIONAL_COLUMNS == {
        "id": False, "name": True, "priority": False, "description": True,
    }
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        box = show_scheduled_view(scenario)
    checkboxes = box.children[0].children
    assert [(c.description, c.value) for c in checkboxes] == [
        ("id", False), ("name", True), ("priority", False), ("description", True),
    ]
    checkboxes[0].value = True       # toggling must not raise
    checkboxes[3].value = False


def test_hidden_columns_are_dropped_and_the_rest_are_formatted(test_data_dir: Path) -> None:
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = scheduled_view(scenario)
    styled = scheduled_styler(view, hidden=["id", "priority"])
    assert "id" not in styled.data.columns and "priority" not in styled.data.columns
    assert "name" in styled.data.columns and "description" in styled.data.columns
    assert list(scheduled_styler(view).data.columns) == list(view.columns)
    assert "$" in styled.to_html()                       # amounts are formatted
    assert list(styled.data.index) == list(view.index)   # hiding columns does not reorder rows


def test_row_numbers_are_hidden_but_the_rule_id_can_be_shown(test_data_dir: Path) -> None:
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = scheduled_view(scenario)
    html = scheduled_styler(view).to_html()
    assert "row_heading" not in html                     # no 0, 1, 2 ... index column
    assert ">id<" in html                                # the database id is a real column
    assert f">{view['id'].iloc[0]}<" in html


def _columns_with(html: str, declaration: str) -> set[int]:
    """Column numbers whose cells the rendered stylesheet gives ``declaration``."""
    import re

    columns: set[int] = set()
    for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", html.split("</style>")[0]):
        if declaration in body:
            columns |= {int(n) for n in re.findall(r"_col(\d+)", selectors)}
    return columns


def _rows_with(html: str, declaration: str) -> set[int]:
    """Row numbers whose cells the rendered stylesheet gives ``declaration``."""
    import re

    rows: set[int] = set()
    for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", html.split("</style>")[0]):
        if declaration in body:
            rows |= {int(n) for n in re.findall(r"_row(\d+)", selectors)}
    return rows


def test_only_numbers_and_dates_are_kept_on_one_line(test_data_dir: Path) -> None:
    """A renderer that cannot scroll clips a wide table, so text has to wrap."""
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = scheduled_view(scenario)
    columns = list(view.columns)
    html = scheduled_styler(view).to_html()
    assert _columns_with(html, "white-space: nowrap") == {
        columns.index(c) for c in ("id", "priority", "amount", "start", "end")
    }
    assert "padding: 2px 6px" in html            # tight cells


def test_long_names_can_break_after_underscores(test_data_dir: Path) -> None:
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = scheduled_view(scenario)
    html = scheduled_styler(view).to_html()
    assert "percent_<wbr>of_<wbr>balance" in html
    assert "Asset_<wbr>Brokerage_<wbr>Joint" in html
    assert "<wbr>" not in scheduled_styler(view[["id", "priority"]]).to_html()


def test_tables_scroll_sideways_instead_of_overflowing(test_data_dir: Path) -> None:
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        chains, calendar = show_chains(scenario), show_calendar_view(scenario)
        scheduled = show_scheduled_view(scenario)
    for output in (chains, calendar, scheduled.children[1]):
        assert output.layout.overflow == "auto" and output.layout.max_width == "100%"


def test_calendar_view_formats_amount_by_kind(test_data_dir: Path) -> None:
    """Issue #24: a set_rate amount is a rate in micros, not cents -- 4.5%,
    not $450.00. Money kinds stay dollars; kinds without an amount show "-"."""
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = calendar_view(scenario)
    amounts = dict(zip(view["kind"] + ":" + view["date"], view["amount"], strict=True))
    assert amounts["set_rate:2027-01-31"] == "4.5%"
    assert amounts["set_amount:2028-07-31"] == "$1,200.00"
    assert amounts["one_off_transfer:2029-03-31"] == "$50,000.00"
    assert amounts["retire_scheduled:2028-06-30"] == "-"


def test_calendar_view_target_names_what_each_row_acts_on(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
) -> None:
    """Issue #27: a set_rate row shows which series it sets -- with its aux
    kind when that isn't an ordinary rate -- and set_amount/retire_scheduled
    rows show their scheduled row, all in one `target` column."""
    append_calendar_row(
        date="2027-03-31", kind="set_rate", series="cpi", amount="2.5%",
        params_json='{"aux_kind": "inflation"}', description="CPI eases",
    )
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = calendar_view(scenario)
    assert "scheduled_name" not in view.columns
    targets = dict(zip(view["kind"] + ":" + view["date"], view["target"], strict=True))
    assert targets["set_rate:2027-01-31"] == "Asset_Checking"
    assert targets["set_rate:2027-03-31"] == "cpi (inflation)"
    assert targets["set_amount:2028-07-31"] == "healthcare"
    assert targets["retire_scheduled:2028-06-30"] == "salary_alice"
    assert targets["one_off_transfer:2029-03-31"] == "-"  # its accounts are the target


# ---------- year to date, itemizations, tags (#435) ----------


def test_ytd_view_lists_the_rows_sim_ytd_list_does(test_data_dir: Path) -> None:
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = ytd_view(scenario)
        listed = setup_listings.ytd_listing(scenario.ref.path)
    assert list(view["name"]) == [row.name for row in listed]
    assert set(view["source"]) == {"year_to_date"}
    assert set(view["defined_in"]) == {setup_listings.THIS_SCENARIO}


def test_itemizations_view_totals_and_references(
    test_data_dir: Path, write_itemization: Callable[..., None],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    write_itemization("fees", "100.00", "25.50")
    write_itemization("unused", "1.00")
    ytd = retire_alice_csv("year_to_date.csv")
    rows = pd.read_csv(ytd, dtype=str, keep_default_na=False)
    rows.loc[0, "amount"] = "@fees"
    rows.to_csv(ytd, index=False)

    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = itemizations_view(scenario).set_index("name")
        detail = itemization_detail(scenario, "@fees")
        with pytest.raises(KeyError):
            itemization_detail(scenario, "nope")
        widget = show_itemizations(scenario)

    assert view.loc["fees", "total"] == 125.5
    assert view.loc["fees", "refs"] == 1
    assert view.loc["unused", "refs"] == 0
    assert view.loc["fees", "defined_in"] == setup_listings.SHARED
    assert detail.total == 125.5
    assert list(detail.rows["Value"]) == ["100.00", "25.50"]
    assert detail.references.to_dict("records") == [
        {"file": "scenarios/retire_alice/year_to_date.csv", "line": 2, "column": "amount"},
    ]
    assert isinstance(widget, widgets.VBox)


def test_tags_view_names_what_carries_each_tag(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
) -> None:
    append_calendar_row(date="2029-05-31", kind="one_off_transfer", account_1="Asset_Checking",
                        account_2="Asset_Savings", amount="100.00", name="gift",
                        tags="estimate:rough")
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        scenario.set_tags("account", "Asset_Checking", "estimate:rough notebook:net_worth")
        scenario.set_tags("scheduled", "salary_alice", "estimate:rough")
        view = tags_view(scenario).set_index("tag")
        counts = scenario.list_tags()

    assert list(view.index) == [f"{r}:{n}" for r, n in zip(counts["role"], counts["name"], strict=True)]
    assert view.loc["estimate:rough"].to_dict() == {
        "accounts": "Asset_Checking", "scheduled": "salary_alice", "calendar": "2029-05 gift",
    }
    assert view.loc["notebook:net_worth", "scheduled"] == ""


def test_overview_widgets_say_when_there_is_nothing_to_show(test_data_dir: Path) -> None:
    (test_data_dir / "scenarios" / "retire_alice" / "year_to_date.csv").unlink()
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        shown = [show_ytd_view(scenario), show_itemizations(scenario), show_tags(scenario)]
    assert [type(each).__name__ for each in shown] == ["Markdown"] * 3
    assert "No year-to-date rows" in shown[0].data


def test_overview_tables_show_authored_text_not_html(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A description or an itemization's note is text: ``<pending>`` shows as
    written rather than vanishing as a tag."""
    from analysis.sim.notebook_ui import scenario_plots

    (test_data_dir / "itemizations" / "fees.csv").write_text(
        "Item,Note <b>,Value\nclosing,<pending>,10.00\n", encoding="utf-8")
    ytd = retire_alice_csv("year_to_date.csv")
    rows = pd.read_csv(ytd, dtype=str, keep_default_na=False)
    rows.loc[0, "description"] = "Payment <pending>"
    rows.to_csv(ytd, index=False)
    shown: list[str] = []
    # Output widgets capture nothing outside a kernel, so collect what is displayed.
    monkeypatch.setattr(scenario_plots, "display", lambda obj: shown.append(
        obj.to_html() if hasattr(obj, "to_html") else ""))
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        show_ytd_view(scenario)
        show_itemizations(scenario)

    html = "".join(shown)
    assert "Payment &lt;pending&gt;" in html
    assert "&lt;pending&gt;" in html.replace("Payment &lt;pending&gt;", "")
    assert "Note &lt;b&gt;" in html
