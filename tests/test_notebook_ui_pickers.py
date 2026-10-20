"""Tests for analysis.sim.notebook_ui.pickers."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pandas as pd
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import notebook_ui
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.notebook_ui.pickers import (
    Connections,
    MultiScenarioConnections,
    TimeRangePicker,
    display_warning,
    multi_scenario_picker,
    observe_while_shown,
    scenario_picker,
    warning_html,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def _copy_prototype(tmp_path: Path) -> Path:
    dest = tmp_path / "data"
    shutil.copytree(PROTOTYPE, dest)
    return dest


def _state_dirs(data_dir: Path) -> tuple[Path, Path]:
    """Match `resolve_scenario_name`'s default: state/ lives beside data_dir."""
    state = data_dir.parent / "state"
    return state / "intent", state / "runs"


def _create_scenario(data_dir: Path, name: str, parent: str) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["sim", "scenario", "create", name, parent, "--data-dir", str(data_dir)],
    )
    assert result.exit_code == 0, result.output


def test_scenario_picker_opens_scenario_and_run_connections(
    test_data_dir: Path, retire_alice_test_run: Path,
) -> None:
    widget, container, dropdown = scenario_picker(test_data_dir, "retire_alice")

    assert isinstance(container.scenario, ScenarioConnection)
    assert isinstance(container.run, RunConnection)
    from ipywidgets import Dropdown
    assert isinstance(dropdown, Dropdown)
    assert container.scenario_ref.name == "retire_alice"
    assert dropdown.value == "retire_alice"
    container.close()


def test_scenario_picker_switches_scenarios_closing_the_old_connections(
    data_dir: Path,
    scenarios_dir: Path,
    runs_dir: Path,
    retire_alice_run_file: Path,
    variant_a_run_file: Path,
) -> None:
    widget, container, dropdown = scenario_picker(data_dir, "retire_alice",
                                                  scenarios_dir=scenarios_dir,
                                                  runs_dir=runs_dir)
    old_scenario, old_run = container.scenario, container.run

    # # Keep the test independent of the layout's title/description children.
    # dropdown = next(
    #     child
    #     for child in widget.children
    #     if hasattr(child, "options") and hasattr(child, "value")
    # )
    dropdown.value = "variant_a"

    assert container.scenario_ref.name == "variant_a"
    assert container.scenario is not old_scenario
    assert container.run is not old_run
    container.close()


def test_scenario_picker_require_run_false_allows_missing_run(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    intent_dir, runs_dir = _state_dirs(data)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "load", "retire_alice", "--data-dir", str(data),
         "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
         "--allow-dirty-data", "--no-run"],
    )
    assert result.exit_code == 0, result.output

    widget, container, dropdown = scenario_picker(data, "retire_alice", require_run=False)
    assert container.run is None
    container.close()


def test_scenario_picker_require_run_true_raises_when_missing(tmp_path: Path) -> None:
    import pytest

    data = _copy_prototype(tmp_path)
    intent_dir, runs_dir = _state_dirs(data)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "load", "retire_alice", "--data-dir", str(data),
         "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
         "--allow-dirty-data", "--no-run"],
    )
    assert result.exit_code == 0, result.output

    with pytest.raises(FileNotFoundError):
        scenario_picker(data, "retire_alice", require_run=True)


def test_scenario_picker_shows_a_member_dropdown_for_an_ensemble_recipe(
    test_data_dir: Path, ensemble_recipe: str,
) -> None:
    widget, container, _dropdown = scenario_picker(test_data_dir, ensemble_recipe)
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    assert member_dropdown.layout.display is None
    assert [v for _label, v in member_dropdown.options] == [
        "member_0000", "member_0001", "member_0002",
    ]
    assert member_dropdown.value == "member_0000"
    assert container.run is not None
    assert container.run.run_path.stem == "member_0000"
    container.close()


def test_scenario_picker_hides_the_member_dropdown_for_an_ordinary_scenario(
    test_data_dir: Path, retire_alice_test_run: Path,
) -> None:
    widget, container, _dropdown = scenario_picker(test_data_dir, "retire_alice")
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    assert member_dropdown.layout.display == "none"
    assert member_dropdown.options == ()
    container.close()


def test_scenario_picker_hides_the_member_dropdown_when_require_run_is_false(
    test_data_dir: Path, ensemble_recipe: str,
) -> None:
    """An ensemble recipe's own intent (what require_run=False looks at) is
    the unresolved recipe, identical regardless of member -- member
    resolution happens per-run, not in the shared scenario-level intent."""

    widget, container, _dropdown = scenario_picker(
        test_data_dir, ensemble_recipe, require_run=False,
    )
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    assert member_dropdown.layout.display == "none"
    container.close()


def test_scenario_picker_selecting_a_member_switches_the_run_connection(
    test_data_dir: Path, ensemble_recipe: str,
) -> None:
    widget, container, _dropdown = scenario_picker(test_data_dir, ensemble_recipe)
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    old_run = container.run

    member_dropdown.value = "member_0001"

    assert container.run is not None
    assert container.run.run_path.stem == "member_0001"
    assert container.run is not old_run
    container.close()


def test_scenario_picker_selecting_a_member_redraws_show_outputs_with_its_run(
    test_data_dir: Path, ensemble_recipe: str,
) -> None:
    widget, container, _dropdown = scenario_picker(test_data_dir, ensemble_recipe)
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    drawn: list[str] = []
    container.show(lambda rc, **_: drawn.append(rc.run_path.stem))
    assert drawn == ["member_0000"]

    member_dropdown.value = "member_0001"

    assert drawn == ["member_0000", "member_0001"]
    assert container.run is not None
    assert container.scenario_ref.run_path == container.run.run_path
    container.close()


def test_scenario_picker_changing_scenario_redraws_show_outputs_once(
    test_data_dir: Path, ensemble_recipe: str, retire_alice_test_run: Path,
) -> None:
    _widget, container, dropdown = scenario_picker(test_data_dir, ensemble_recipe)
    drawn: list[str] = []
    container.show(lambda _rc, **_: drawn.append(container.scenario_ref.name))

    dropdown.value = "retire_alice"

    assert drawn == [ensemble_recipe, "retire_alice"]
    container.close()


def test_show_and_show_scenario_pass_the_context_keywords(
    test_data_dir: Path, ensemble_recipe: str,
) -> None:
    """The keywords show()/show_scenario() pass are a contract with every
    widget, so they're pinned here even where no widget reads them yet."""

    widget, container, _dropdown = scenario_picker(test_data_dir, ensemble_recipe)
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    shown: list[tuple] = []
    container.show(lambda rc, *args, **kwargs: shown.append((rc, args, kwargs)))
    scenario_shown: list[tuple] = []
    container.show_scenario(
        lambda sc, *args, **kwargs: scenario_shown.append((sc, args, kwargs)), "extra",
    )

    member_dropdown.value = "member_0001"

    assert container.run is not None
    assert shown[-1] == (container.run, (), {"member": 1})
    assert scenario_shown[-1] == (
        container.scenario, ("extra",), {"run": container.run, "member": 1},
    )
    first_run = shown[0][0]
    assert shown[0] == (first_run, (), {"member": 0})
    assert scenario_shown[0][2] == {"run": first_run, "member": 0}
    container.close()


def _widgets_the_notebooks_show(repo_root: Path) -> dict[str, Callable]:
    """Every function a notebook passes to ``show()``/``show_scenario()``,
    resolved from the notebook_ui modules."""
    names: set[str] = set()
    for notebook in (repo_root / "notebooks").glob("*.ipynb"):
        for cell in json.loads(notebook.read_text())["cells"]:
            if cell["cell_type"] == "code":
                names.update(re.findall(r"\.show(?:_scenario)?\(\s*(\w+)", "".join(cell["source"])))
    found: dict[str, Callable] = {}
    for module_info in pkgutil.iter_modules(notebook_ui.__path__, f"{notebook_ui.__name__}."):
        module = importlib.import_module(module_info.name)
        for name in names - found.keys():
            if callable(fn := getattr(module, name, None)):
                found[name] = fn
    assert found.keys() == names, f"not found in notebook_ui: {sorted(names - found.keys())}"
    return found


def test_every_widget_the_notebooks_show_accepts_extra_keywords(repo_root: Path) -> None:
    """show() passes context keywords (``member``, ...) to every widget, so
    each must end its signature with ``**_``."""
    shown = _widgets_the_notebooks_show(repo_root)
    assert shown
    strict = [
        name for name, fn in sorted(shown.items())
        if not any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in inspect.signature(fn).parameters.values())
    ]
    assert strict == []


def test_scenario_picker_switching_away_from_an_ensemble_hides_the_member_dropdown(
    test_data_dir: Path, ensemble_recipe: str, retire_alice_test_run: Path,
) -> None:
    widget, _container, dropdown = scenario_picker(test_data_dir, ensemble_recipe)
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    assert member_dropdown.layout.display is None

    dropdown.value = "retire_alice"

    assert member_dropdown.layout.display == "none"


def test_multi_scenario_picker_requires_at_least_one_variant(
    test_data_dir: Path, retire_alice_test_run: Path,
) -> None:
    widget, container = multi_scenario_picker(test_data_dir, "retire_alice", [])

    assert isinstance(container, MultiScenarioConnections)
    assert not container.ok
    assert "at least one variant" in container.warnings[0]


def test_warning_html_converts_markdown_lite_and_escapes_brackets() -> None:
    html = warning_html("run `analysis sim load <name>` -- **bold**")
    assert "<code>analysis sim load &lt;name&gt;</code>" in html
    assert "<strong>bold</strong>" in html
    assert "background-color:#fff8c5" in html


def test_display_warning_does_not_raise() -> None:
    display_warning("just a message")


def test_time_range_takes_its_first_span_whole() -> None:
    time_range = TimeRangePicker()

    time_range.cover([2031, 2026, 2040])

    assert (time_range.min, time_range.max) == (2026, 2040)
    assert time_range.value == (2026, 2040)


def test_a_full_time_range_follows_a_new_span() -> None:
    time_range = TimeRangePicker()
    time_range.cover(range(2026, 2041))

    time_range.cover(range(2030, 2061))

    assert time_range.value == (2030, 2060)


def test_a_narrowed_time_range_keeps_its_years_clamped_to_a_new_span() -> None:
    time_range = TimeRangePicker()
    time_range.cover(range(2026, 2041))
    time_range.value = (2026, 2031)

    time_range.cover(range(2028, 2061))
    assert time_range.value == (2028, 2031)

    # None of its years left in the span: back to the whole of it.
    time_range.cover(range(2040, 2051))
    assert time_range.value == (2040, 2050)


def test_time_range_clips_by_month_or_by_year() -> None:
    time_range = TimeRangePicker()
    time_range.cover([2026, 2027, 2028])
    monthly = pd.Series(range(36), index=pd.date_range("2026-01-31", periods=36, freq="ME"))
    yearly = pd.DataFrame({"total": [1, 2, 3]}, index=[2026, 2027, 2028])
    labelled = pd.Series([1, 2, 3], index=["2026", "2027", "2028"])

    # Full: nothing is cut, not even a row dated outside the span.
    assert time_range.clip(monthly) is monthly

    time_range.value = (2027, 2027)
    assert time_range.clip(monthly).index.year.unique().tolist() == [2027]
    assert time_range.clip(monthly.to_period("M")).index.year.unique().tolist() == [2027]
    assert time_range.clip(yearly).index.tolist() == [2027]
    assert time_range.clip(labelled).index.tolist() == ["2027"]


class _Shown(Connections):
    """Connections whose show() calls the widget function as is."""

    def _invoke(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def test_a_rebuilt_widget_stops_observing_shared_controls() -> None:
    time_range = TimeRangePicker()
    time_range.cover(range(2026, 2031))
    builds: list[int] = []
    redraws: list[int] = []

    def widget(*, time_range: TimeRangePicker, **_) -> None:
        build = len(builds)
        builds.append(build)
        observe_while_shown(time_range, lambda *_: redraws.append(build))

    shown = _Shown()
    shown.show(widget, time_range=time_range)
    shown.notify_changed()  # a scenario change rebuilds the widget
    shown.notify_changed()

    time_range.value = (2026, 2027)

    assert builds == [0, 1, 2]
    assert redraws == [2]  # only the widget now shown redraws


def test_a_rebuild_moving_a_shared_control_redraws_no_old_widget() -> None:
    """The first widget rebuilt after a scenario change re-spans the Years
    slider; the other output's widget, not yet rebuilt, must not redraw."""
    time_range = TimeRangePicker()
    time_range.cover(range(2026, 2031))
    shown = _Shown()
    spans = iter([range(2026, 2031), range(2030, 2041)])
    stale: list[str] = []
    current = {"scenario": "old"}

    def respanning(*, time_range: TimeRangePicker, **_) -> None:
        time_range.cover(next(spans))

    def watching(*, time_range: TimeRangePicker, **_) -> None:
        built_for = current["scenario"]
        observe_while_shown(
            time_range,
            lambda *_: stale.append(built_for) if built_for != current["scenario"] else None,
        )

    shown.show(respanning, time_range=time_range)
    shown.show(watching, time_range=time_range)
    current["scenario"] = "new"
    shown.notify_changed()

    assert (time_range.min, time_range.max) == (2030, 2040)
    assert stale == []
