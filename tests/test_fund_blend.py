"""A fund grows at the weighted blend of its allocation slices' rates (#78)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.engine import _amount_percent_of_balance, _blend_slices_at, run_scenario
from analysis.sim.loader import LoadResult, growth_rule_name

BROKERAGE = "Asset_Brokerage_Joint"
FUND_RULE = growth_rule_name(BROKERAGE, "fund")


def _growth_rule(intent: Path) -> tuple[str | None, dict, str]:
    con = sqlite3.connect(intent)
    try:
        series, params, description = con.execute(
            "SELECT series, params_json, description FROM scheduled_transaction "
            "WHERE name = ?", (FUND_RULE,),
        ).fetchone()
    finally:
        con.close()
    return series, json.loads(params or "{}"), description


def _first_growth(run_file: Path) -> int:
    con = sqlite3.connect(run_file)
    try:
        return con.execute(
            "SELECT t.amount FROM transfer_event t "
            "JOIN scheduled_transaction s ON s.id = t.reason_id "
            "WHERE t.reason_kind = 'rule' AND s.name = ? ORDER BY t.id LIMIT 1",
            (FUND_RULE,),
        ).fetchone()[0]
    finally:
        con.close()


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund")
def test_fund_grows_at_its_slices_blend(load_test_copy: Callable[[], LoadResult],
                                         runs_dir: Path) -> None:
    result = load_test_copy()
    series, params, description = _growth_rule(result.intent_path)
    assert series is None
    assert params["blend"] == [["", [["bond", 400_000], ["stock_us", 600_000]]]]
    assert "60% stock_us" in description and "40% bond" in description
    assert not result.warnings
    run = run_scenario(result.intent_path, runs_dir / "fund.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    # $300,000 x (0.6 x 8% + 0.4 x 4%) / 12 = $1,600.00
    assert _first_growth(run) == 160_000


@pytest.mark.usefixtures("blended_fund")
def test_a_fund_blends_the_prototypes_default_class_series(
    prototype_data: Path, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """#80: retire_alice ships rate__stock_us (7%) and rate__bond (4%), so a
    fund held in it, or in any scenario derived from it, blends with no
    series of its own. (``blended_fund`` replaced them; put them back.)"""
    for name in ("stock_us", "bond"):
        shutil.copy(prototype_data / "scenarios" / "retire_alice" / "series" / f"rate__{name}.csv",
                    retire_alice_csv("series"))
    result = load_test_copy()
    _, params, _ = _growth_rule(result.intent_path)
    assert params["blend"] == [["", [["bond", 400_000], ["stock_us", 600_000]]]]
    run = run_scenario(result.intent_path, runs_dir / "fund.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    # $300,000 x (0.6 x 7% + 0.4 x 4%) / 12 = $1,450.00
    assert _first_growth(run) == 145_000


@pytest.mark.usefixtures("blended_fund")
def test_a_fund_slice_series_overrides_its_class(
    write_series: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_series("FND.stock_us", "2026-01-31,100000")
    _, params, _ = _growth_rule(load_test_copy().intent_path)
    assert params["blend"] == [["", [["bond", 400_000], ["FND.stock_us", 600_000]]]]


@pytest.mark.usefixtures("blended_fund")
def test_a_symbols_own_series_beats_its_blend(
    write_series: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_series("FND", "2026-01-31,50000")
    series, params, _ = _growth_rule(load_test_copy().intent_path)
    assert series == "FND" and "blend" not in params


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund")
def test_a_partly_covered_fund_warns_and_keeps_its_account_series(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    (retire_alice_csv("series") / "rate__bond.csv").unlink()
    result = load_test_copy()
    series, params, _ = _growth_rule(result.intent_path)
    assert series == "brokerage_growth" and "blend" not in params
    assert any("FND" in w and "not for bond" in w for w in result.warnings)


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund")
def test_an_uncovered_fund_keeps_its_account_series_quietly(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    for name in ("stock_us", "bond"):
        (retire_alice_csv("series") / f"rate__{name}.csv").unlink()
    result = load_test_copy()
    series, _, _ = _growth_rule(result.intent_path)
    assert series == "brokerage_growth"
    assert not result.warnings


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund")
def test_a_zero_weight_class_needs_no_series(
    test_data_dir: Path, load_test_copy: Callable[[], LoadResult],
) -> None:
    with (test_data_dir / "allocations.csv").open("a") as f:
        f.write("FND,,cash,0\n")
    result = load_test_copy()
    _, params, _ = _growth_rule(result.intent_path)
    assert params["blend"] == [["", [["bond", 400_000], ["stock_us", 600_000]]]]
    assert not result.warnings


@pytest.mark.usefixtures("blended_fund")
def test_a_set_rate_on_a_slice_series_moves_the_fund(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    # Before the run starts, so the first growth already reads it. Accepted at
    # load: the blend counts as reading stock_us.
    append_calendar_row(date="2026-05-31", kind="set_rate", series="stock_us", amount="120000")
    run = run_scenario(load_test_copy().intent_path, runs_dir / "fund.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    # $300,000 x (0.6 x 12% + 0.4 x 4%) / 12 = $2,200.00
    assert _first_growth(run) == 220_000


def test_a_glide_path_uses_the_set_in_force() -> None:
    blend = [["", [["stock_us", 1_000_000]]],
             ["2030-12-31", [["stock_us", 500_000], ["bond", 500_000]]]]
    assert _blend_slices_at(blend, "2029-06-30") == [["stock_us", 1_000_000]]
    assert _blend_slices_at(blend, "2030-12-31") == blend[1][1]
    dated_only = blend[1:]
    assert _blend_slices_at(dated_only, "2026-05-31") == dated_only[0][1]


def test_blended_amount_rounds_once() -> None:
    aux = {("rate", "a"): [("2026-01-31", {"annual_rate_micros": 70_001})],
           ("rate", "b"): [("2026-01-31", {"annual_rate_micros": 30_001})]}
    params = {"blend": [["", [["a", 333_333], ["b", 666_667]]]]}
    amount = _amount_percent_of_balance(params, {1: 12_000_000_00}, {}, 1, aux, "2026-05-31", 1)
    weighted = 333_333 * 70_001 + 666_667 * 30_001
    assert amount == 12_000_000_00 * weighted // (12 * 10**12)
