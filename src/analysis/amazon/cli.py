from __future__ import annotations

from pathlib import Path

import click

from analysis.amazon.categorizer import categorize_amazon_csv


@click.command(name="amazon-categorize")
@click.argument("input_csv", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--state-dir", default="state", show_default=True, type=click.Path(path_type=Path))
@click.option("--output-dir", default="out", show_default=True, type=click.Path(path_type=Path))
@click.option(
    "--interactive/--no-interactive",
    default=True,
    show_default=True,
    help="Prompt for unknown item categories.",
)
def amazon_categorize_command(
    input_csv: Path,
    state_dir: Path,
    output_dir: Path,
    interactive: bool,
) -> None:
    """Categorize Amazon expenses from a CSV file with persistent memory."""

    def ask_for_category(item: str, categories: list[str]) -> str:
        click.echo("")
        click.echo(f"Unknown item: {item}")
        if categories:
            click.echo("Known categories: " + ", ".join(sorted(categories)))
        return click.prompt("Category (leave blank to keep UNCATEGORIZED)", default="", show_default=False)

    callback = ask_for_category if interactive else None
    result = categorize_amazon_csv(
        input_csv_path=input_csv,
        state_dir=state_dir,
        output_dir=output_dir,
        ask_for_category=callback,
    )

    click.echo(f"Annotated CSV: {result.annotated_csv_path}")
    click.echo(f"Summary CSV: {result.summary_csv_path}")
    click.echo(f"Known categories: {result.categories_count}")
    click.echo(f"Mapped items: {result.mapped_items_count}")
    click.echo(f"Uncategorized rows: {result.uncategorized_count}")
