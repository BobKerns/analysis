from pathlib import Path
from unittest.mock import patch

import pandas as pd
from click.testing import CliRunner

from analysis.amazon.cli import _resolve_category_input
from analysis.cli import main


def test_cli_amazon_categorize_non_interactive(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon.csv"
    pd.DataFrame({"Description": ["Coffee Pods"], "Amount": [12.0]}).to_csv(input_csv, index=False)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "amazon-categorize",
            str(input_csv),
            "--state-dir",
            str(tmp_path / "state"),
            "--output-dir",
            str(tmp_path / "out"),
            "--no-interactive",
        ],
    )

    assert result.exit_code == 0
    assert "Annotated CSV:" in result.output
    assert (tmp_path / "out" / "amazon_annotated.csv").exists()
    assert (tmp_path / "out" / "amazon_summary.csv").exists()


def test_resolve_category_input_prefers_existing_category() -> None:
    categories = ["Electronics", "Household"]

    assert _resolve_category_input("El", categories) == "Electronics"
    assert _resolve_category_input("electronics", categories) == "Electronics"


def test_resolve_category_input_keeps_new_category_when_no_match() -> None:
    categories = ["Electronics", "Household"]

    assert _resolve_category_input("Groceries", categories) == "Groceries"


def test_cli_interactive_uses_existing_category_after_completion(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon.csv"
    pd.DataFrame({"Description": ["Unknown Device"], "Amount": [12.0]}).to_csv(input_csv, index=False)

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "categories.json").write_text('["Electronics"]\n', encoding="utf-8")

    runner = CliRunner()

    with patch("analysis.amazon.cli.readline") as mocked_readline:
        mocked_readline.get_completer.return_value = None
        mocked_readline.get_completer_delims.return_value = " \t\n-[]~^:/"
        mocked_readline.__doc__ = "GNU readline"

        result = runner.invoke(
            main,
            [
                "amazon-categorize",
                str(input_csv),
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(tmp_path / "out"),
                "--interactive",
            ],
            input="Electronics\n",
        )

    assert result.exit_code == 0
    mapping = (state_dir / "item_category_map.json").read_text(encoding="utf-8")
    categories = (state_dir / "categories.json").read_text(encoding="utf-8")
    assert "Electronics" in mapping
    assert "Electronics" in categories


def test_readline_binding_switches_for_libedit() -> None:
    with patch("analysis.amazon.cli.readline") as mocked_readline:
        mocked_readline.__doc__ = "libedit"
        from analysis.amazon.cli import _readline_binding

        assert _readline_binding() == "bind ^I rl_complete"
