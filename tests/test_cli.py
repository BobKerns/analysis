import shutil
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from analysis.amazon.cli import _resolve_category_input
from analysis.cli import main
from analysis.storage import JsonStateStore

DATA_DIR = Path(__file__).resolve().parent / "data" / "amazon"


def test_cli_amazon_categorize_non_interactive(tmp_path: Path) -> None:
    input_csv = shutil.copy(DATA_DIR / "personal_coffee.csv", tmp_path / "amazon.csv")

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
    assert "Monthly output files:" in result.output
    assert (tmp_path / "out" / "amazon_annotated.csv").exists()
    assert (tmp_path / "out" / "amazon_summary.csv").exists()
    monthly_annotated = list((tmp_path / "out").glob("Amazon-*_annotated.csv"))
    monthly_summary = list((tmp_path / "out").glob("Amazon-*_summary.csv"))
    assert monthly_annotated
    assert monthly_summary


def test_cli_amazon_categorize_accepts_multiple_input_files(tmp_path: Path) -> None:
    input_csv_1 = shutil.copy(DATA_DIR / "personal_coffee.csv", tmp_path / "amazon_1.csv")
    input_csv_2 = shutil.copy(DATA_DIR / "personal_paper_towels.csv", tmp_path / "amazon_2.csv")

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "amazon",
            "categorize",
            str(input_csv_1),
            str(input_csv_2),
            "--state-dir",
            str(tmp_path / "state"),
            "--output-dir",
            str(tmp_path / "out"),
            "--no-interactive",
        ],
    )

    assert result.exit_code == 0
    assert f"Input CSV: {input_csv_1}" in result.output
    assert f"Input CSV: {input_csv_2}" in result.output
    assert (tmp_path / "out" / "amazon_1_annotated.csv").exists()
    assert (tmp_path / "out" / "amazon_2_annotated.csv").exists()


def test_cli_amazon_categorize_defaults_output_to_input_out_directory(
    tmp_path: Path, monkeypatch
) -> None:
    input_csv = shutil.copy(DATA_DIR / "personal_coffee.csv", tmp_path / "amazon.csv")
    monkeypatch.chdir(tmp_path)  # default --state-dir is relative; don't pollute the repo's state

    result = CliRunner().invoke(
        main,
        ["amazon", "categorize", str(input_csv), "--no-interactive"],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "amazon_annotated.csv").exists()
    assert (tmp_path / "out" / "amazon_summary.csv").exists()
    assert list((tmp_path / "out").glob("Amazon-*_summary.csv"))


def test_cli_amazon_categorize_requires_at_least_one_input_file(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "amazon",
            "categorize",
            "--state-dir",
            str(tmp_path / "state"),
            "--output-dir",
            str(tmp_path / "out"),
            "--no-interactive",
        ],
    )

    assert result.exit_code != 0
    assert "Provide at least one input CSV file." in result.output


def test_cli_amazon_items_filters_by_shipment_date_range(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = JsonStateStore(state_dir)
    store.insert_items(
        [
            {
                "Order ID": "A-1",
                "Shipment Date": "03/15/2026",
                "Title": "In Range",
                "Item Quantity": "1",
                "Item Net Total": "10.00",
                "normalized_item": "in range",
                "category": "Household",
            },
            {
                "Order ID": "A-2",
                "Shipment Date": "04/01/2026",
                "Title": "Out Of Range",
                "Item Quantity": "1",
                "Item Net Total": "20.00",
                "normalized_item": "out of range",
                "category": "Household",
            },
        ],
        source_file="data/Amazon-2026-03.csv",
    )
    store.close()

    result = CliRunner().invoke(
        main,
        ["amazon", "items", "2026-03-01", "--state-dir", str(state_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "In Range" in result.output
    assert "Out Of Range" not in result.output
    assert "1 item(s)" in result.output


def test_cli_amazon_categorize_accepts_cutoff_day_option(tmp_path: Path) -> None:
    input_csv = shutil.copy(DATA_DIR / "personal_coffee.csv", tmp_path / "amazon.csv")

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
            "--cutoff-day",
            "3",
            "--no-interactive",
        ],
    )

    assert result.exit_code == 0


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
    input_csv = shutil.copy(DATA_DIR / "personal_unknown_device.csv", tmp_path / "amazon.csv")

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
    input_csv = shutil.copy(DATA_DIR / "personal_unknown_device.csv", tmp_path / "amazon.csv")

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
    input_csv = shutil.copy(DATA_DIR / "personal_unknown_device.csv", tmp_path / "amazon.csv")

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
