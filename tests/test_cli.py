from pathlib import Path

import pandas as pd
from click.testing import CliRunner

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
