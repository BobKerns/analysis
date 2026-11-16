"""``sim lifespan tables|check`` and the life-table comparison behind them."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import lifespan_tables
from analysis.sim.life_table import LifeTable

MONTH = 1 / 12


@pytest.fixture
def sim(test_data_dir: Path) -> Callable[..., Result]:
    """Run a ``sim`` command against the private data copy; ``input`` answers
    its prompts."""
    def run(*args: str, input: str | None = None) -> Result:  # noqa: A002 -- CliRunner's name
        return CliRunner().invoke(
            main, ["sim", *args, "--data-dir", str(test_data_dir)], input=input,
        )
    return run


def _write_table(path: Path, rows: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("effective_date,age,q_female,q_male\n" + rows)
    return path


def test_find_tables_finds_the_templates_tables(data_dir: Path) -> None:
    found = {t.label: t.location for t in lifespan_tables.find_tables(data_dir)}
    assert found["default"] == "templates/aux"
    assert found["jp"] == "templates/aux"


def test_a_name_found_again_is_labelled_with_its_location(test_scenarios_dir: Path) -> None:
    _write_table(test_scenarios_dir / "retire_alice" / "aux" / "life_table__jp.csv",
                 "2023-12-31,0,1,1\n")
    labels = [t.label for t in lifespan_tables.find_tables(test_scenarios_dir.parent)]
    assert labels.count("jp") == 1
    assert "scenarios/retire_alice/aux/jp" in labels


def test_read_table_takes_the_latest_vintage(tmp_path: Path) -> None:
    path = _write_table(tmp_path / "life_table__x.csv",
                        "2020-12-31,0,0.5,0.6\n2023-12-31,0,0.1,0.2\n")
    effective, by_sex = lifespan_tables.read_table(path)
    assert effective == "2023-12-31"
    assert by_sex["f"].q == (0.1,)
    assert by_sex["m"].q == (0.2,)


def test_expected_age_counts_the_years_survived_and_half_the_month_of_death() -> None:
    # Nobody dies in the first year, and everybody in the month after it.
    expected = lifespan_tables.expected_ages(LifeTable((0.0, 1.0)))
    assert expected == pytest.approx([1 + MONTH / 2, 1 + MONTH / 2])


def test_expected_age_is_past_the_age_and_rises_with_it(data_dir: Path) -> None:
    default = next(t for t in lifespan_tables.find_tables(data_dir) if t.label == "default")
    _, by_sex = lifespan_tables.read_table(default.path)
    for table in by_sex.values():
        expected = lifespan_tables.expected_ages(table)
        assert all(e > age for age, e in enumerate(expected))
        assert expected[90] > expected[65] > expected[30]
        assert 70 < expected[0] < 90


def test_at_age_interpolates_between_whole_ages() -> None:
    table = LifeTable((0.1, 0.3, 1.0))
    expected = [70.0, 72.0, 2 + MONTH / 2]
    q, at = lifespan_tables.at_age(table, expected, 0.25)
    assert q == pytest.approx(0.15)
    assert at == pytest.approx(70.5)


def test_at_age_past_the_table_dies_within_the_month() -> None:
    assert lifespan_tables.at_age(LifeTable((0.5,)), [1.0], 3.0) == (1.0, 3.0 + MONTH / 2)


def test_comparison_has_each_table_sex_and_measure(data_dir: Path) -> None:
    frame = lifespan_tables.comparison(data_dir)
    assert {("default", "female", "q"), ("default", "male", "expected"),
            ("jp", "male", "q")} <= set(frame.columns)
    # jp ends before the default table does; past its end it is blank.
    assert frame[("jp", "male", "q")].isna().iloc[-1]
    assert frame.index[-1] == 119


def test_lifespan_check_shows_each_table_at_a_fractional_age(sim: Callable[..., Result]) -> None:
    result = sim("lifespan", "check", "67.5")
    assert result.exit_code == 0, result.output
    assert "life tables at age 67.5" in result.output
    assert "default" in result.output and "jp" in result.output
    assert "years left" in result.output


def test_lifespan_check_asks_for_a_missing_age(sim: Callable[..., Result]) -> None:
    result = sim("lifespan", "check", input="70\n")
    assert result.exit_code == 0, result.output
    assert "life tables at age 70" in result.output


def test_lifespan_check_refuses_an_age_past_the_last(sim: Callable[..., Result]) -> None:
    result = sim("lifespan", "check", "200")
    assert result.exit_code != 0
    assert "must be from 0 to 130" in result.output


def test_lifespan_tables_lists_the_tables_and_every_age(sim: Callable[..., Result]) -> None:
    result = sim("lifespan", "tables")
    assert result.exit_code == 0, result.output
    assert "templates/aux" in result.output
    assert "0-119" in result.output and "0-109" in result.output
    assert "│ 119 │" in result.output


def test_at_age_frame_has_a_row_per_table_and_years_left(data_dir: Path) -> None:
    frame = lifespan_tables.at_age_frame(data_dir, 67.5)
    assert ("default", "templates/aux") in frame.index
    row = frame.loc[("default", "templates/aux")]
    assert row[("female", "years left")] == pytest.approx(row[("female", "expected")] - 67.5)
    assert 0 < row[("male", "q")] < 1


def test_at_age_widget_redraws_for_a_new_age(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui import life_table_widgets

    asked: list[float] = []

    def at_age_frame(data: Path, age: float) -> pd.DataFrame:
        asked.append(age)
        return lifespan_tables.at_age_frame(data, age)

    monkeypatch.setattr(life_table_widgets, "at_age_frame", at_age_frame)
    box = life_table_widgets.at_age_widget(data_dir).children[0]
    assert box.value == 65
    box.value = 70.5
    assert asked == [65, 70.5]
