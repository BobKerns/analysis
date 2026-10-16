"""#170: drilling an account down to its lots, in the notebook widgets and
``sim ensemble summary``/``compare``; #171: the ensemble widgets' shared
controls and default recipe."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any, cast

import matplotlib
import pandas as pd
import pytest
from click.testing import CliRunner
from ipywidgets import widgets

matplotlib.use("Agg")

from analysis.cli import main
from analysis.sim.connection import RunConnection
from analysis.sim.ensemble import EnsembleError, members_balance_by_month
from analysis.sim.notebook_ui.pickers import (
    ALL_LOTS, AccountDropdown, AccountPicker, LotPicker, merged_lots_by_account,
)
from analysis.sim.types import DollarMode

BROKERAGE = "Asset_Brokerage_Joint"


# ---------- data ----------


def test_lots_by_account_lists_only_accounts_with_several_lots(two_lot_run: RunConnection) -> None:
    assert two_lot_run.lots_by_account() == {BROKERAGE: ["a", "b"]}


def test_lot_balances_add_up_to_the_account_balance(two_lot_run: RunConnection) -> None:
    lots = pd.concat([two_lot_run.lot_balance(BROKERAGE, lot) for lot in ("a", "b")], axis=1)
    account = two_lot_run.account_balance(BROKERAGE)
    assert lots.sum(axis=1).to_numpy() == pytest.approx(account.to_numpy())
    assert two_lot_run.lot_balance(BROKERAGE, "no_such_lot").empty


def test_cpi_constant_lot_balance_is_deflated(two_lot_run: RunConnection) -> None:
    nominal = two_lot_run.lot_balance(BROKERAGE, "a")
    deflated = two_lot_run.lot_balance(BROKERAGE, "a", DollarMode.CPI_CONSTANT)
    assert deflated.iloc[-1] < nominal.iloc[-1]


def test_members_balance_by_month_reads_one_lot(two_lot_run: RunConnection) -> None:
    frame = members_balance_by_month({"m": two_lot_run}, BROKERAGE, lot="b")
    assert frame["m"].to_numpy() == pytest.approx(
        two_lot_run.lot_balance(BROKERAGE, "b").to_numpy())
    with pytest.raises(EnsembleError, match="no lot 'z'"):
        members_balance_by_month({"m": two_lot_run}, BROKERAGE, lot="z")
    with pytest.raises(EnsembleError, match="needs its account"):
        members_balance_by_month({"m": two_lot_run}, None, lot="b")


# ---------- pickers ----------


def test_lot_picker_shows_only_for_an_account_with_several_lots() -> None:
    picker = LotPicker({"Brokerage": ["a", "b"], "Savings": ["only"]})
    assert picker.layout.display == "none"
    picker.follow("Brokerage")
    assert picker.layout.display is None
    assert picker.picker.options == ("a", "b")
    picker.picker.value = ("b",)
    assert picker.lots == ("b",)
    picker.follow("Savings")
    assert picker.layout.display == "none" and picker.lots == ()


def test_lot_picker_keeps_picked_lots_the_new_set_still_has() -> None:
    picker = LotPicker({"Brokerage": ["a", "b", "c"]})
    picker.follow("Brokerage")
    picker.picker.value = ("a", "c")
    picker.set_lots({"Brokerage": ["b", "c"]})
    assert picker.picker.options == ("b", "c") and picker.lots == ("c",)
    picker.set_lots({"Brokerage": ["c"]})
    assert picker.layout.display == "none" and picker.lots == ()
    picker.set_lots({"Brokerage": ["a", "b"]})
    assert picker.layout.display is None and picker.account == "Brokerage"


def test_single_lot_picker_offers_all_lots_first() -> None:
    picker = LotPicker({"Brokerage": ["a", "b"]}, multiple=False)
    picker.follow("Brokerage")
    assert [label for label, _ in picker.picker.options] == [ALL_LOTS, "a", "b"]
    picker.picker.value = ("a",)
    assert picker.lots == ("a",)
    picker.picker.value = ()
    assert picker.lots == ()


def test_account_picker_publishes_one_selection_per_change() -> None:
    picker = AccountPicker(
        kind="asset", accounts_by_kind={"asset": ["Brokerage", "Checking"]},
        lots_by_account={"Brokerage": ["a", "b"]},
    )
    seen: list[tuple] = []
    picker.observe(lambda change: seen.append(change["new"]), names="selection")
    assert picker.selection == (("Brokerage",), ())
    assert picker.lot_picker is not None and picker.lot_picker.layout.display is None
    assert picker.lot_picker.picker.options == ("a", "b")
    picker.lot_picker.picker.value = ("a",)
    picker.account_picker.value = ("Checking",)
    # The lot is dropped with its account in the same, single, change.
    assert seen == [(("Brokerage",), ("a",)), (("Checking",), ())]
    assert picker.account_column.children == (picker.account_picker, picker.lot_picker)
    assert picker.account_column.layout.overflow == "visible"  # no stray scrollbar
    assert picker.lot_picker.layout.display == "none"


def test_account_dropdown_follows_the_account_with_its_lots() -> None:
    sentinel = object()
    picker = AccountDropdown(
        [("Net worth", sentinel), ("Brokerage", "Brokerage")],
        lots_by_account={"Brokerage": ["a", "b"]},
    )
    assert picker.selection == (sentinel, ())
    assert picker.lot_picker is not None and picker.lot_picker.layout.display == "none"
    picker.account_picker.value = "Brokerage"
    picker.lot_picker.picker.value = ("b",)
    assert picker.selection == ("Brokerage", ("b",))
    # Stacked: the lot picker sits under the account it drills into.
    assert isinstance(picker, widgets.VBox)
    assert picker.children == (picker.account_picker, picker.lot_picker)


def test_merged_lots_by_account_keeps_every_runs_lots(
    lot_variant_runs: tuple[RunConnection, dict[str, RunConnection]],
) -> None:
    baseline, variants = lot_variant_runs
    assert merged_lots_by_account(baseline, *variants.values()) == {BROKERAGE: ["a", "b", "c"]}


# ---------- widgets ----------


def test_balance_history_plots_the_picked_lots(two_lot_run: RunConnection,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from matplotlib.figure import Figure

    from analysis.sim.notebook_ui import accounts_plots

    shown: list[object] = []
    monkeypatch.setattr(accounts_plots, "display", lambda *args, **kwargs: shown.extend(args))
    widget = accounts_plots.account_history_widget(two_lot_run, "asset", "Balances")
    picker = widget.children[1]
    picker.account_picker.value = (BROKERAGE,)
    picker.lot_picker.picker.value = ("a", "b")
    figure = [item for item in shown if isinstance(item, Figure)][-1]
    assert [line.get_label() for line in figure.axes[0].lines] == [
        f"{BROKERAGE}/a", f"{BROKERAGE}/b", "Total",
    ]


def test_events_view_narrows_to_a_lot(two_lot_run: RunConnection) -> None:
    from analysis.sim.notebook_ui.events_widgets import events_view

    _heading, whole = events_view(two_lot_run, accounts=(BROKERAGE,))
    heading, lot_b = events_view(two_lot_run, accounts=(BROKERAGE,), lots=("b",))
    assert 0 < len(lot_b) < len(whole)
    ends = lot_b[["from", "from_lot", "to", "to_lot"]]
    assert (((ends["from"] == BROKERAGE) & (ends["from_lot"] == "b"))
            | ((ends["to"] == BROKERAGE) & (ends["to_lot"] == "b"))).all()
    assert "balance" not in lot_b.columns and "lot(s) b" in heading
    with pytest.raises(ValueError, match="single account"):
        events_view(two_lot_run, lots=("b",))


def test_events_widget_offers_lots_for_one_account(two_lot_run: RunConnection) -> None:
    from analysis.sim.notebook_ui.events_widgets import events_widget

    widget = events_widget(two_lot_run)
    selectors, _date_picker, _out = widget.children
    account_picker = selectors.children[0]
    account_picker.account_picker.value = (BROKERAGE,)
    account_picker.lot_picker.picker.value = ("b",)  # must not raise
    assert account_picker.selection == ((BROKERAGE,), ("b",))


def test_account_diffs_compare_one_lot(
    lot_variant_runs: tuple[RunConnection, dict[str, RunConnection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui import compare_plots

    baseline, variants = lot_variant_runs
    shown: list[Any] = []
    monkeypatch.setattr(compare_plots, "display", lambda *args, **kwargs: shown.extend(args))
    box = compare_plots.show_account_balance_diffs(None, "retire_alice", baseline, variants)
    picker = next(child for child in box.children if isinstance(child, AccountPicker))
    picker.kind_picker.value = "asset"
    picker.account_picker.value = (BROKERAGE,)
    picker.lot_picker.picker.value = ("c",)  # only the variant has lot c
    table = shown[-1].data
    assert (table["baseline"] == 0).all() and (table["variant"] > 0).all()


def test_ensemble_cli_reports_one_lot(two_lot_ensemble_runs_dir: Path) -> None:
    runner = CliRunner()
    args = ["sim", "ensemble", "summary", "two_lot", "--runs-dir", str(two_lot_ensemble_runs_dir)]
    result = runner.invoke(main, [*args, "--account", BROKERAGE, "--lot", "b"])
    assert result.exit_code == 0, result.output
    assert f"account {BROKERAGE} lot b" in result.output
    missing = runner.invoke(main, [*args, "--account", BROKERAGE, "--lot", "z"])
    assert missing.exit_code != 0 and "no lot 'z'" in missing.output
    orphan = runner.invoke(main, [*args, "--lot", "b"])
    assert orphan.exit_code != 0 and "--lot needs --account" in orphan.output

    compared = runner.invoke(main, [
        "sim", "ensemble", "compare", "two_lot", "two_lot",
        "--runs-dir", str(two_lot_ensemble_runs_dir), "--account", BROKERAGE, "--lot", "a",
    ])
    assert compared.exit_code == 0, compared.output
    assert f"account {BROKERAGE} lot a" in compared.output


def _copy_recipe(runs_dir: Path, name: str, rename_lot: str | None = None) -> None:
    """Copy the two_lot recipe's batch as recipe ``name``, its brokerage lot
    ``rename_lot`` renamed so that recipe lacks it."""
    shutil.copytree(runs_dir / "two_lot", runs_dir / name)
    if rename_lot is None:
        return
    for member in (runs_dir / name).glob("*/member_*.sqlite3"):
        with sqlite3.connect(member) as con:
            con.execute("UPDATE account_lot SET lot = 'renamed' WHERE lot = ?", (rename_lot,))


@pytest.mark.slow
def test_ensemble_widgets_share_controls_and_start_on_the_default(
    two_lot_ensemble_runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui import ensemble_widgets

    _copy_recipe(two_lot_ensemble_runs_dir, "base_ensemble")
    titles: list[Any] = []
    real_fan_chart = ensemble_widgets.ensemble_fan_chart

    def _spy_fan_chart(monthly_summary, ax=None):
        result = real_fan_chart(monthly_summary, ax=ax)
        titles.append(ax)
        return result

    monkeypatch.setattr(ensemble_widgets, "ensemble_fan_chart", _spy_fan_chart)
    controls = ensemble_widgets.EnsembleControls(runs_dir=two_lot_ensemble_runs_dir)
    fan = ensemble_widgets.ensemble_widget(
        runs_dir=two_lot_ensemble_runs_dir, controls=controls, default="two_lot")
    compare = ensemble_widgets.ensemble_compare_widget(
        runs_dir=two_lot_ensemble_runs_dir, controls=controls, default="base_ensemble")
    assert fan.children[0].value == "two_lot"
    assert compare.children[0].children[0].value == "base_ensemble"
    assert fan.children[1].controls is controls and compare.children[1].controls is controls

    controls.account_picker.account_picker.value = BROKERAGE
    fan.children[1].lot_picker.picker.value = ("b",)  # the fan's own Lot dropdown
    assert titles[-1].get_title().startswith(f"{BROKERAGE} lot b fan chart")


def test_unknown_default_recipe_keeps_the_first(two_lot_ensemble_runs_dir: Path) -> None:
    from analysis.sim.notebook_ui import ensemble_widgets

    fan = ensemble_widgets.ensemble_widget(runs_dir=two_lot_ensemble_runs_dir, default="nope")
    assert fan.children[0].value == "two_lot"


@pytest.mark.slow
def test_lot_picker_offers_only_the_shown_recipes_lots(
    two_lot_ensemble_runs_dir: Path,
) -> None:
    """The fan's Lot dropdown offers its recipe's lots, not every recipe's:
    switching recipe swaps the list, and drops a picked lot the new recipe
    lacks."""
    from analysis.sim.notebook_ui import ensemble_widgets

    _copy_recipe(two_lot_ensemble_runs_dir, "base_ensemble", rename_lot="a")
    fan = ensemble_widgets.ensemble_widget(runs_dir=two_lot_ensemble_runs_dir,
                                           default="base_ensemble")
    dropdown, row, _out = fan.children
    controls, lot_picker = row.controls, row.lot_picker
    controls.account_picker.account_picker.value = BROKERAGE
    assert [label for label, _ in lot_picker.picker.options] == [ALL_LOTS, "renamed", "b"]
    lot_picker.picker.value = ("renamed",)
    dropdown.value = "two_lot"
    assert [label for label, _ in lot_picker.picker.options] == [ALL_LOTS, "a", "b"]
    assert lot_picker.lots == ()
    lot_picker.picker.value = ("b",)
    dropdown.value = "base_ensemble"
    assert lot_picker.lots == ("b",)  # kept: this recipe has it too


@pytest.mark.slow
def test_compare_lots_are_the_baseline_and_variants_own(
    two_lot_ensemble_runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comparison offers the union of its baseline's and variants' lots,
    independent of the fan's, and a lot one side lacks gets a note."""
    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.styled import StyledMarkdown

    _copy_recipe(two_lot_ensemble_runs_dir, "base_ensemble", rename_lot="a")
    shown: list[object] = []
    monkeypatch.setattr(ensemble_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    controls = ensemble_widgets.EnsembleControls(runs_dir=two_lot_ensemble_runs_dir)
    fan = ensemble_widgets.ensemble_widget(runs_dir=two_lot_ensemble_runs_dir,
                                           controls=controls, default="base_ensemble")
    compare = ensemble_widgets.ensemble_compare_widget(
        runs_dir=two_lot_ensemble_runs_dir, controls=controls, default="base_ensemble")
    recipe_pickers, row, _out = compare.children
    lot_picker = row.lot_picker
    controls.account_picker.account_picker.value = BROKERAGE

    def offered(picker) -> list[str]:
        return [label for label, _ in picker.picker.options]

    assert offered(lot_picker) == [ALL_LOTS, "renamed", "b"]  # baseline only
    recipe_pickers.children[1].value = ("two_lot",)
    assert offered(lot_picker) == [ALL_LOTS, "renamed", "b", "a"]
    assert offered(fan.children[1].lot_picker) == [ALL_LOTS, "renamed", "b"]
    assert fan.children[1].lot_picker is controls.recipe_lot_picker
    assert lot_picker is controls.compare_lot_picker
    shown.clear()
    lot_picker.picker.value = ("a",)
    assert any(isinstance(item, StyledMarkdown)
               and f"`base_ensemble` has no account {BROKERAGE} lot a" in str(item.data)
               for item in shown)


@pytest.mark.slow
def test_widgets_of_one_kind_share_a_lot_picker(two_lot_ensemble_runs_dir: Path) -> None:
    """Two single-recipe widgets on one set of controls share its Lot
    dropdown; a comparison gets the other one."""
    from analysis.sim.notebook_ui import ensemble_widgets

    controls = ensemble_widgets.EnsembleControls(runs_dir=two_lot_ensemble_runs_dir)
    first, second = (ensemble_widgets.ensemble_widget(runs_dir=two_lot_ensemble_runs_dir,
                                                      controls=controls)
                     for _ in range(2))
    compare = ensemble_widgets.ensemble_compare_widget(runs_dir=two_lot_ensemble_runs_dir,
                                                       controls=controls)
    assert first.children[1].lot_picker is second.children[1].lot_picker is controls.recipe_lot_picker
    assert compare.children[1].lot_picker is controls.compare_lot_picker
    assert controls.recipe_lot_picker is not controls.compare_lot_picker


@pytest.mark.slow
def test_compare_widget_log_scale_uses_symlog(
    seeded_ensemble_batch, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.types import LogLinearMode

    figures: list = []
    real_figure = ensemble_widgets.ensemble_compare_figure

    def _spy(*args, **kwargs):
        figure = real_figure(*args, **kwargs)
        figures.append(figure)
        return figure

    monkeypatch.setattr(ensemble_widgets, "ensemble_compare_figure", _spy)
    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    box = ensemble_widgets.ensemble_compare_widget(runs_dir=runs_dir, default="baseline")
    recipe_pickers, row, _out = box.children
    controls = row.controls
    recipe_pickers.children[1].value = ("child",)
    assert figures[-1].axes[0].get_yscale() == "linear"
    controls.log_linear_picker.value = LogLinearMode.LOG
    assert figures[-1].axes[0].get_yscale() == "symlog"


def test_each_widget_stacks_its_lot_picker_under_account(
    two_lot_ensemble_runs_dir: Path,
) -> None:
    """The shared controls appear in each widget's own row, with that
    widget's Lot dropdown in a column under Account."""
    from analysis.sim.notebook_ui import ensemble_widgets

    controls = ensemble_widgets.EnsembleControls(runs_dir=two_lot_ensemble_runs_dir)
    fan = ensemble_widgets.ensemble_widget(runs_dir=two_lot_ensemble_runs_dir, controls=controls)
    compare = ensemble_widgets.ensemble_compare_widget(runs_dir=two_lot_ensemble_runs_dir,
                                                       controls=controls)
    for box, lot_picker in ((fan, controls.recipe_lot_picker),
                            (compare, controls.compare_lot_picker)):
        _scale, _dollars, account_column, reserve = box.children[1].children
        assert account_column.children == (controls.account_picker, lot_picker)
        assert reserve is controls.reserve_field



def test_merged_lots_include_a_runs_only_lot_of_an_account_another_splits(
    lot_variant_runs: tuple[RunConnection, dict[str, RunConnection]],
) -> None:
    """A run's one lot of an account counts in the union when another run
    splits that account, so it can be picked for comparison; an account no
    run splits still has no Lot dropdown."""
    base, _variants = lot_variant_runs
    single = base.lots_by_account(single=True)
    assert "Asset_Checking" in single and "Asset_Checking" not in base.lots_by_account()

    class _Run:
        def __init__(self, lots: dict[str, list[str]]):
            self._lots = lots

        def lots_by_account(self, *, single: bool = False) -> dict[str, list[str]]:
            return {a: lots for a, lots in self._lots.items() if single or len(lots) > 1}

    unsplit = _Run({BROKERAGE: ["default"], "Asset_Checking": ["default"]})
    split = _Run({BROKERAGE: ["a", "b"], "Asset_Checking": ["default"]})
    merged = merged_lots_by_account(*cast(list[RunConnection], [unsplit, split]))
    assert merged == {BROKERAGE: ["default", "a", "b"]}
