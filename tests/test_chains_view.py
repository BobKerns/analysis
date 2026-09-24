"""chains_view / show_chains: which lot a chain step draws from."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from analysis.sim.connection import open_scenario
from analysis.sim.notebook_ui.scenario_plots import show_chains
from analysis.sim.notebook_ui.scenario_view import chains_view


def _steps(test_data_dir: Path):
    with open_scenario(test_data_dir, "retire_alice") as scenario:
        view = chains_view(scenario)
        return view, view[view["step"].notna()].set_index(["chain", "step"])


def test_lot_is_a_dash_when_the_account_has_a_single_lot(test_data_dir: Path) -> None:
    _, steps = _steps(test_data_dir)
    rows = steps.loc["asset checking liquidity"]
    assert list(rows["draw_from"]) == ["Asset_Savings", "Asset_TraditionalIRA_Alice"]
    assert list(rows["lot"]) == ["-", "-"]      # 'default' is not shown when it is the only lot
    assert not any(":" in name for name in steps["draw_from"])


def test_lots_are_spelled_out_where_an_account_has_several(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    add_account_value: Callable[..., None],
) -> None:
    add_account_value("Asset_Roth_Alice", lot="b", opening_balance="10.00")
    (retire_alice_csv("chains") / "asset__roth_first.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\n"
        "Asset_Roth_Alice,default,,,,\n"    # one of two lots, and it is named default
        "Asset_Roth_Alice,b,,,,\n"
        "Asset_Savings,,,,,\n"
    )
    _, steps = _steps(test_data_dir)
    rows = steps.loc["roth first"]
    assert list(zip(rows["draw_from"], rows["lot"])) == [
        ("Asset_Roth_Alice", "default"), ("Asset_Roth_Alice", "b"), ("Asset_Savings", "-"),
    ]


def test_a_step_that_draws_from_another_chain_has_no_lot(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    (retire_alice_csv("chains") / "asset__outer.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nasset checking liquidity,,,,,\n"
    )
    _, steps = _steps(test_data_dir)
    assert steps.loc[("outer", 1), "draw_from"] == "asset checking liquidity"
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
    assert set(shown[0].data["step"].dropna()) == {1, 2}  # interior chains only
