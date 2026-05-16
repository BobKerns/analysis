from pathlib import Path
from unittest.mock import patch

import pandas as pd
from click.testing import CliRunner

from analysis.amazon.cli import _resolve_category_input
from analysis.cli import main
from analysis.storage import JsonStateStore


def test_cli_amazon_categorize_non_interactive(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon.csv"
    pd.DataFrame({"Description": ["Coffee Pods"], "Amount": [12.0]}).to_csv(input_csv, index=False)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "amazon",
            "categorize",
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


def test_resolve_category_input_rejects_control_chars() -> None:
    categories = ["Electronics", "Household"]

    assert _resolve_category_input("El\tctronics", categories) == ""


def test_cli_interactive_uses_existing_category_after_completion(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon.csv"
    pd.DataFrame(
        {"Description": ["Unknown Device"], "Amount": [12.0]}
    ).to_csv(input_csv, index=False)

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonStateStore(state_dir)
    store.save_categories(["Electronics"])
    store.close()

    runner = CliRunner()

    with patch("analysis.amazon.cli.readline") as mocked_readline:
        mocked_readline.get_completer.return_value = None
        mocked_readline.get_completer_delims.return_value = " \t\n-[]~^:/"
        mocked_readline.__doc__ = "GNU readline"

        result = runner.invoke(
            main,
            [
                "amazon",
                "categorize",
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
    store = JsonStateStore(state_dir)
    mapping = store.load_mapping()
    categories = store.load_categories()
    store.close()
    assert mapping.get("unknown device") == "Electronics"
    assert "Electronics" in categories


def test_cli_interactive_unknown_item_is_green_when_color_enabled(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon.csv"
    pd.DataFrame(
        {"Description": ["Unknown Device"], "Amount": [12.0]}
    ).to_csv(input_csv, index=False)

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonStateStore(state_dir)
    store.save_categories(["Electronics"])
    store.close()

    runner = CliRunner()

    with patch("analysis.amazon.cli.readline") as mocked_readline:
        mocked_readline.get_completer.return_value = None
        mocked_readline.get_completer_delims.return_value = " \t\n-[]~^:/"
        mocked_readline.__doc__ = "GNU readline"

        result = runner.invoke(
            main,
            [
                "amazon",
                "categorize",
                str(input_csv),
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(tmp_path / "out"),
                "--interactive",
            ],
            input="Electronics\n",
            color=True,
        )

    assert result.exit_code == 0
    assert "Unknown item:" in result.output
    assert "\x1b[32munknown device\x1b[0m" in result.output


def test_cli_interactive_reprompts_for_invalid_category(tmp_path: Path) -> None:
    input_csv = tmp_path / "amazon.csv"
    pd.DataFrame(
        {"Description": ["Unknown Device"], "Amount": [12.0]}
    ).to_csv(input_csv, index=False)

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonStateStore(state_dir)
    store.save_categories([])
    store.close()

    runner = CliRunner()

    with patch("analysis.amazon.cli.readline") as mocked_readline:
        mocked_readline.get_completer.return_value = None
        mocked_readline.get_completer_delims.return_value = " \t\n-[]~^:/"
        mocked_readline.__doc__ = "GNU readline"

        result = runner.invoke(
            main,
            [
                "amazon",
                "categorize",
                str(input_csv),
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(tmp_path / "out"),
                "--interactive",
            ],
            input="El\tctronics\nElectronics\n",
        )

    assert result.exit_code == 0
    assert "Invalid category: control or non-printing characters are not allowed." in result.output
    store = JsonStateStore(state_dir)
    mapping = store.load_mapping()
    store.close()
    assert mapping.get("unknown device") == "Electronics"


def test_readline_binding_switches_for_libedit() -> None:
    with patch("analysis.amazon.cli.readline") as mocked_readline:
        mocked_readline.__doc__ = "libedit"
        from analysis.amazon.cli import _readline_binding

        assert _readline_binding() == "bind ^I rl_complete"


def test_cli_review_mappings_interactive_recategorize_and_remove(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonStateStore(state_dir)
    store.save_categories(["Groceries", "Household"])
    store.save_mapping(
        {
            "coffee pods": "Groceries",
            "paper towels": "Household",
            "tea": "Groceries",
        }
    )
    store.close()

    runner = CliRunner()

    with patch("analysis.amazon.cli.readline") as mocked_readline:
        mocked_readline.get_completer.return_value = None
        mocked_readline.get_completer_delims.return_value = " \t\n-[]~^:/"
        mocked_readline.__doc__ = "GNU readline"

        result = runner.invoke(
            main,
            [
                "amazon",
                "category",
                "review",
                "--state-dir",
                str(state_dir),
            ],
            input="Groceries\nHousehold\n-\n",
        )

    assert result.exit_code == 0
    store = JsonStateStore(state_dir)
    mapping = store.load_mapping()
    store.close()
    assert mapping.get("coffee pods") == "Household"
    assert "tea" not in mapping


def test_cli_review_mappings_rejects_unknown_category(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonStateStore(state_dir)
    store.save_categories(["Groceries"])
    store.save_mapping({"coffee pods": "Groceries"})
    store.close()

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "amazon",
            "category",
            "review",
            "--state-dir",
            str(state_dir),
        ],
        input="Unknown\n",
    )

    assert result.exit_code == 0
    assert "No valid category selected." in result.output


def test_cli_category_delete_removes_category_and_mappings(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonStateStore(state_dir)
    store.save_categories(["Groceries", "Household"])
    store.save_mapping(
        {
            "coffee pods": "Groceries",
            "paper towels": "Household",
            "tea": "Groceries",
        }
    )
    store.close()

    runner = CliRunner()

    with patch("analysis.amazon.cli.readline") as mocked_readline:
        mocked_readline.get_completer.return_value = None
        mocked_readline.get_completer_delims.return_value = " \t\n-[]~^:/"
        mocked_readline.__doc__ = "GNU readline"

        result = runner.invoke(
            main,
            [
                "amazon",
                "category",
                "delete",
                "--state-dir",
                str(state_dir),
            ],
            input="Groceries\ny\n",
        )

    assert result.exit_code == 0
    store = JsonStateStore(state_dir)
    categories = store.load_categories()
    mapping = store.load_mapping()
    store.close()
    assert "Groceries" not in categories
    assert mapping.get("paper towels") == "Household"
    assert "coffee pods" not in mapping
    assert "tea" not in mapping
