"""ADR-007 Phase 6: sensitivity pickers and notebook widgets."""

from __future__ import annotations

from pathlib import Path

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from ipywidgets import widgets

from analysis.sim import sensitivity
from analysis.sim.notebook_ui import sensitivity_widgets as sw
from analysis.sim.notebook_ui.pickers import (
    AttributionPicker,
    DeltaScalePicker,
    HeatmapValuePicker,
    ParameterPicker,
    ReportYearPicker,
)
from analysis.sim.types import AttributionAxis, DeltaScale, DollarMode, HeatmapValue

# --- pickers -------------------------------------------------------------------


def test_toggle_pickers_offer_their_enums() -> None:
    assert DeltaScalePicker().value == DeltaScale.ABSOLUTE
    assert [v for _label, v in DeltaScalePicker().options] == list(DeltaScale)
    assert [v for _label, v in HeatmapValuePicker().options] == list(HeatmapValue)
    assert [v for _label, v in AttributionPicker().options] == list(AttributionAxis)
    assert AttributionPicker(AttributionAxis.HOLDING_ACCOUNT).value == AttributionAxis.HOLDING_ACCOUNT


def test_parameter_picker_selects_first_when_nothing_to_keep() -> None:
    picker = ParameterPicker()
    assert picker.value is None
    picker.set_parameters({"gift": "amount", "retire": "date"})
    assert picker.value == "gift"


def test_parameter_picker_labels_and_keeps_selection() -> None:
    picker = ParameterPicker({"gift": "amount, $12,000 to $28,000", "retire": "date, a to b"})
    assert picker.options[0] == ("gift — amount, $12,000 to $28,000", "gift")
    picker.value = "retire"
    picker.set_parameters({"retire": "date, a to b", "other": ""})
    assert picker.value == "retire"
    assert picker.options[1] == ("other", "other")


def test_report_year_picker_keeps_year_or_falls_back_to_last() -> None:
    picker = ReportYearPicker([2030, 2031, 2032], value=2031)
    assert picker.value == 2031
    picker.set_years([2031, 2035])
    assert picker.value == 2031
    picker.set_years([2040, 2041])
    assert picker.value == 2041


# --- views on synthetic frames ------------------------------------------------


def _deltas(ladder: bool = False) -> pd.DataFrame:
    quantiles = [0.1, 0.25, 0.75, 0.9] if ladder else [0.1, 0.9]
    records = []
    for year in (2030, 2031, 2032):
        for row, scale in (("a", 1.0), ("b", 0.2)):
            for q in quantiles:
                d = scale * (q - 0.5) * 100_000 * (year - 2029)
                for measure in sensitivity.MEASURES:
                    records.append({"row": row, "target": "amount", "quantile": q,
                                    "value": f"${q * 100:.0f}", "year": year, "measure": measure,
                                    "level": 1e6 + d, "base": 1e6, "delta": d, "relative": d / 1e6})
    return pd.DataFrame(records)


@pytest.mark.parametrize("value", list(HeatmapValue))
@pytest.mark.parametrize("scale", list(DeltaScale))
def test_heatmap_view(value: HeatmapValue, scale: DeltaScale) -> None:
    grid = sensitivity.rank_by_year(_deltas(), value=value.value,
                                    relative=scale == DeltaScale.RELATIVE)
    ax = sw.heatmap_view(grid, value, scale)
    assert [t.get_text() for t in ax.get_yticklabels()] == ["a", "b"]
    assert any(t.get_text() for t in ax.texts)  # cells labeled
    plt.close("all")


def test_heatmap_and_tornado_mark_latent_factors() -> None:
    """#39: a factor's row is labeled as one, not read as one more row."""
    deltas = _deltas()
    deltas.loc[deltas["row"] == "a", "target"] = sensitivity.FACTOR_TARGET
    grid = sensitivity.rank_by_year(deltas)
    ax = sw.heatmap_view(grid, HeatmapValue.SHARE, DeltaScale.ABSOLUTE)
    assert [t.get_text() for t in ax.get_yticklabels()] == ["a (factor)", "b"]
    ax = sw.tornado_view(sensitivity.oat_table(deltas, 2031), DeltaScale.ABSOLUTE)
    assert {t.get_text() for t in ax.get_yticklabels()} == {"a (factor)", "b"}
    plt.close("all")


def test_share_heatmap_scales_to_a_factor_share_above_one() -> None:
    """PR #53 review: a factor's share can exceed 100% (it's over the
    ordinary rows' total); the scale must reach it rather than clip."""
    deltas = _deltas()
    deltas.loc[deltas["row"] == "a", "target"] = sensitivity.FACTOR_TARGET
    grid = sensitivity.rank_by_year(deltas)
    assert grid.loc["a"].max() > 1
    ax = sw.heatmap_view(grid, HeatmapValue.SHARE, DeltaScale.ABSOLUTE)
    assert ax.images[0].norm.vmax == pytest.approx(grid.to_numpy(dtype=float).max())
    plt.close("all")


def test_tornado_view_puts_largest_swing_on_top() -> None:
    ax = sw.tornado_view(sensitivity.oat_table(_deltas(), 2031), DeltaScale.ABSOLUTE)
    by_height = sorted(zip(ax.get_yticks(), (t.get_text() for t in ax.get_yticklabels()), strict=True))
    assert [label for _y, label in by_height] == ["b", "a"]  # "a" (largest swing) on top
    assert len(ax.patches) == 4
    plt.close("all")


def test_delta_over_time_view_one_line_per_quantile() -> None:
    ax = sw.delta_over_time_view(_deltas(ladder=True), "a", DeltaScale.RELATIVE)
    assert len(ax.get_lines()) == 4 + 1  # four quantiles plus the zero rule
    plt.close("all")


def test_response_curve_includes_the_base_point() -> None:
    ax = sw.response_curve_view(_deltas(ladder=True), "a", 2031, DeltaScale.ABSOLUTE)
    xs = list(ax.get_lines()[0].get_xdata())
    assert 0.5 in xs and len(xs) == 5
    plt.close("all")


def test_signed_dollars() -> None:
    assert sw._signed_dollars(60_000) == "+$60,000"
    assert sw._signed_dollars(-80_000.4) == "-$80,000"
    assert sw._signed_dollars(0.3) == "$0"


def _attribution_table(scale: float) -> pd.DataFrame:
    frame = pd.DataFrame({"salary": [-100.0 * scale, -200.0 * scale],
                          "interest": [-10.0 * scale, -30.0 * scale]}, index=[2030, 2031])
    frame[sensitivity.UNATTRIBUTED] = 0.0
    frame[sensitivity.TOTAL] = frame["salary"] + frame["interest"]
    return frame


def test_attribution_view_stacks_and_reports_numbers() -> None:
    ax, numbers = sw.attribution_view({0.1: _attribution_table(1), 0.9: _attribution_table(-1)}, 2031)
    assert list(numbers.columns) == ["q=0.1", "q=0.9"]
    assert numbers.loc[sensitivity.TOTAL, "q=0.1"] == -230
    labels = [t.get_text() for t in ax.get_legend().get_texts()]
    assert {"salary", "interest", "total"} == set(labels)
    assert "unattributed (!)" not in labels  # only drawn when non-zero
    plt.close("all")


def test_attribution_view_folds_extra_keys_into_other() -> None:
    keys = [f"k{i}" for i in range(10)]
    table = pd.DataFrame({k: [float(i + 1)] for i, k in enumerate(keys)}, index=[2030])
    table[sensitivity.UNATTRIBUTED] = 0.0
    table[sensitivity.TOTAL] = table[keys].sum(axis=1)
    _ax, numbers = sw.attribution_view({0.9: table}, 2030)
    assert "other" in numbers.index
    assert numbers.loc["other", "q=0.9"] == pytest.approx(sum(range(1, 11)) - sum(range(1, 8)))
    plt.close("all")


def test_correlation_and_r2_views() -> None:
    table = pd.DataFrame([
        {"row": "a", "year": 2030, "rho": 0.8, "rho_low": 0.7, "rho_high": 0.9, "srrc": 0.8, "r2": 0.9, "n": 100},
        {"row": "b", "year": 2030, "rho": 0.05, "rho_low": -0.15, "rho_high": 0.25, "srrc": 0.0, "r2": 0.9, "n": 100},
        {"row": "a", "year": 2031, "rho": 0.7, "rho_low": 0.6, "rho_high": 0.8, "srrc": 0.7, "r2": 0.5, "n": 100},
        {"row": "b", "year": 2031, "rho": 0.1, "rho_low": -0.1, "rho_high": 0.3, "srrc": 0.1, "r2": 0.5, "n": 100},
    ])
    ax = sw.correlation_view(table, 2030, 0.05)
    assert [t.get_text() for t in ax.get_yticklabels()] == ["b", "a"]
    perfect = table.assign(rho=1.0, rho_low=0.9999978, rho_high=0.9999995)  # pre-fix interval
    sw.correlation_view(perfect, 2030, 0.05)  # must not raise on a negative whisker
    ax = sw.r2_view(table)
    assert list(ax.get_lines()[0].get_ydata()) == [0.9, 0.5]
    plt.close("all")


def test_correlation_view_plots_srrc_and_labels_factors() -> None:
    nan = float("nan")
    table = pd.DataFrame([
        {"row": "a", "year": 2030, "rho": 0.8, "rho_low": 0.7, "rho_high": 0.9, "srrc": 0.1,
         "srrc_low": 0.0, "srrc_high": 0.2, "r2": 0.9, "n": 100, "factor": False},
        {"row": "b", "year": 2030, "rho": 0.6, "rho_low": 0.5, "rho_high": 0.7, "srrc": 0.7,
         "srrc_low": 0.6, "srrc_high": 0.8, "r2": 0.9, "n": 100, "factor": False},
        {"row": "f", "year": 2030, "rho": 0.75, "rho_low": 0.65, "rho_high": 0.85, "srrc": nan,
         "srrc_low": nan, "srrc_high": nan, "r2": 0.9, "n": 100, "factor": True},
    ])
    ax = sw.correlation_view(table, 2030, 0.05, by="srrc")
    # Ranked by |SRRC|, not rho; the factor goes last, labeled.
    assert [t.get_text() for t in ax.get_yticklabels()] == ["a", "b", "f (factor)"]
    assert "others held fixed" in ax.get_xlabel()
    wide = table.assign(srrc_high=table["srrc_high"].where(table["row"] != "b", 1.4))
    ax = sw.correlation_view(wide, 2030, 0.05, by="srrc")
    assert ax.get_xlim()[1] >= 1.4  # SRRC can pass 1; its whisker isn't clipped
    plt.close("all")


# --- the widget on real batches -------------------------------------------------


def _quiet(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    shown: list[object] = []
    monkeypatch.setattr(sw, "display", lambda *args, **kwargs: shown.extend(args))
    monkeypatch.setattr(plt, "show", lambda *args, **kwargs: plt.close("all"))
    return shown


def _body(box: widgets.VBox) -> widgets.VBox:
    return box.children[2]


def _titles(body: widgets.VBox) -> list[str]:
    return [section.children[0].value for section in body.children]


@pytest.mark.slow
def test_sensitivity_widget_oat_batch(oat_batch: Path, runs_dir: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    shown = _quiet(monkeypatch)
    noted: list[tuple[object, ...]] = []
    monkeypatch.setattr(sw, "note_picks", lambda _data, *pickers: noted.append(pickers))
    box = sw.sensitivity_widget(runs_dir=runs_dir)
    assert isinstance(box, widgets.VBox)
    assert not any(isinstance(item, widgets.Widget) for item in shown)
    recipe, batch = box.children[0].children
    assert noted == [(recipe,)]  # picking a recipe is use of it (#322)
    assert recipe.value == "retire_alice"
    assert batch.value == oat_batch
    # No response curve for a two-quantile batch.
    assert _titles(_body(box)) == ["<h4>Parameters</h4>", "<h4>Overview</h4>",
                                   "<h4>At one year</h4>", "<h4>Over time</h4>", "<h4>Why</h4>"]
    assert any(isinstance(item, pd.DataFrame) and "distribution" in item.columns for item in shown)
    # Every picker change redraws without error.
    dollar_picker, scale_picker = box.children[1].children
    dollar_picker.value = DollarMode.NOMINAL
    scale_picker.value = DeltaScale.RELATIVE
    why = _body(box).children[4]
    parameter_picker, year_picker, attribution_picker = why.children[1].children
    parameter_picker.value = "gift"
    year_picker.value = 2030
    attribution_picker.value = AttributionAxis.HOLDING_ACCOUNT
    assert not [s for s in shown if isinstance(s, str) and "Error" in s]


@pytest.mark.slow
def test_sensitivity_widget_ladder_adds_response_curve(ladder_batch: Path, runs_dir: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet(monkeypatch)
    box = sw.sensitivity_widget(runs_dir=runs_dir)
    assert "<h4>Across the range</h4>" in _titles(_body(box))


def test_sensitivity_widget_monte_carlo_batch(mc_batch: Path, runs_dir: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet(monkeypatch)
    box = sw.sensitivity_widget(runs_dir=runs_dir)
    _recipe, batch = box.children[0].children
    assert "Monte Carlo" in batch.label
    assert _titles(_body(box))[0] == "<h4>Parameters</h4>"
    assert len(_body(box).children) == 3
    year_picker = _body(box).children[2].children[1].children[0]
    year_picker.value = year_picker.options[0]


def test_list_batches_labels_mode(oat_batch: Path, runs_dir: Path) -> None:
    batches = sw.list_batches(runs_dir / "retire_alice")
    assert batches == [(f"{oat_batch.name} (one-at-a-time, 5 members)", oat_batch)]
    assert sw.list_sensitivity_recipes(runs_dir) == ["retire_alice"]


# --- PR #25 review fixes ---------------------------------------------------------


def test_tornado_leaves_undefined_deltas_unplotted() -> None:
    frame = _deltas()
    frame.loc[(frame["row"] == "b") & (frame["quantile"] == 0.9), "relative"] = float("nan")
    table = sensitivity.oat_table(frame, 2031, relative=True)
    ax = sw.tornado_view(table, DeltaScale.RELATIVE)
    assert len(ax.patches) == 3  # b's high-quantile bar is omitted, not drawn as zero
    plt.close("all")


def test_attribution_view_relative_divides_by_base() -> None:
    tables = {0.1: _attribution_table(1), 0.9: _attribution_table(-1)}
    _ax, numbers = sw.attribution_view(tables, 2031, scale=DeltaScale.RELATIVE, base=-1_000_000)
    assert numbers.loc[sensitivity.TOTAL, "q=0.1"] == pytest.approx(-230 / 1_000_000)
    with pytest.raises(sw.EnsembleError, match="too close to zero"):
        sw.attribution_view(tables, 2031, scale=DeltaScale.RELATIVE, base=500)
    plt.close("all")


def test_sensitivity_widget_attribution_follows_delta_scale(
    oat_batch: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = _quiet(monkeypatch)
    box = sw.sensitivity_widget(runs_dir=runs_dir)
    _dollar_picker, scale_picker = box.children[1].children
    shown.clear()
    scale_picker.value = DeltaScale.RELATIVE
    tables = [data for s in shown
              if isinstance(data := getattr(s, "data", None), pd.DataFrame) and "q=0.1" in data.columns]
    assert tables, "attribution table redrawn"
    assert tables[-1].abs().max().max() < 1  # fractions of base, not dollars


def test_swing_heatmap_survives_all_undefined_values() -> None:
    grid = pd.DataFrame({2030: [float("nan")], 2031: [float("nan")]}, index=["r"])
    sw.heatmap_view(grid, HeatmapValue.SWING, DeltaScale.RELATIVE)
    plt.close("all")


def test_parameters_note_mentions_mode_specific_columns() -> None:
    assert "value@q" in sw.parameters_note("sensitivity")
    assert "drawn" in sw.parameters_note("monte_carlo")
    # "Hold the others at their center" describes only one-at-a-time members
    # (PR #26 review): Monte Carlo members draw every row.
    assert "holds the rest at their centers" in sw.parameters_note("sensitivity")
    assert "holds the rest at their centers" not in sw.parameters_note("monte_carlo")
    # A factor member moves its family too (#39).
    assert "latent factor" in sw.parameters_note("sensitivity")
