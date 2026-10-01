"""Scenario README.md -> scenario.description extraction (analysis.sim.readme)."""

from __future__ import annotations

from pathlib import Path

from analysis.sim.readme import parse_readme_summary, readme_description, relevel_heading


def test_parse_readme_summary_title_and_one_paragraph() -> None:
    summary = parse_readme_summary(
        "# Inflation sensitivity\n\nThis recipe varies CPI to see how sensitive the plan is.\n"
    )
    assert summary.title == "Inflation sensitivity"
    assert summary.paragraph == "This recipe varies CPI to see how sensitive the plan is."
    assert summary.has_more is False


def test_parse_readme_summary_detects_more_content() -> None:
    summary = parse_readme_summary(
        "# Title\n\nFirst paragraph.\n\nSecond paragraph.\n"
    )
    assert summary.paragraph == "First paragraph."
    assert summary.has_more is True


def test_parse_readme_summary_wraps_a_multiline_paragraph_onto_one_line() -> None:
    summary = parse_readme_summary(
        "# Title\n\nThis paragraph\nwraps across\nseveral lines.\n"
    )
    assert summary.paragraph == "This paragraph wraps across several lines."


def test_parse_readme_summary_no_title() -> None:
    summary = parse_readme_summary("Just a paragraph, no heading.\n")
    assert summary.title == ""
    assert summary.paragraph == "Just a paragraph, no heading."
    assert summary.has_more is False


def test_parse_readme_summary_title_only() -> None:
    summary = parse_readme_summary("# Title only\n")
    assert summary.title == "Title only"
    assert summary.paragraph == ""
    assert summary.has_more is False


def test_parse_readme_summary_empty() -> None:
    assert parse_readme_summary("") == parse_readme_summary("   \n\n  ")


def test_relevel_heading_rewrites_the_leading_heading_only() -> None:
    text = "# Title\n\n# Not a heading to touch\nbody"
    assert relevel_heading(text, 3) == "### Title\n\n# Not a heading to touch\nbody"


def test_relevel_heading_leaves_text_without_a_heading_alone() -> None:
    assert relevel_heading("no heading here", 3) == "no heading here"


def test_relevel_heading_handles_a_bare_hash_with_no_title_text() -> None:
    assert relevel_heading("#\nbody", 2) == "##\nbody"


def test_readme_description_none_when_no_file(tmp_path: Path) -> None:
    assert readme_description(tmp_path) is None


def test_readme_description_combines_title_paragraph_and_pointer(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "inflation_1"
    scenario_dir.mkdir()
    (scenario_dir / "README.md").write_text(
        "# Inflation sensitivity\n\n"
        "This recipe varies CPI to see how sensitive the plan is.\n\n"
        "## Why\n\nBecause hyperinflation can't really be projected.\n"
    )
    description = readme_description(scenario_dir)
    assert description == (
        "# Inflation sensitivity\n\n"
        "This recipe varies CPI to see how sensitive the plan is.\n\n"
        "_See `scenarios/inflation_1/README.md` for more._"
    )


def test_readme_description_no_pointer_when_its_the_whole_document(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "base"
    scenario_dir.mkdir()
    (scenario_dir / "README.md").write_text("# Base\n\nThe primary plan.\n")
    assert readme_description(scenario_dir) == "# Base\n\nThe primary plan."


def test_readme_description_none_for_an_empty_readme(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "empty"
    scenario_dir.mkdir()
    (scenario_dir / "README.md").write_text("\n")
    assert readme_description(scenario_dir) is None
