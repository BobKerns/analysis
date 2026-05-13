from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

import click

from analysis.amazon.categorizer import categorize_amazon_csv
from analysis.log import configure_logging

log = logging.getLogger(__name__)

try:
    import readline
except ImportError:  # pragma: no cover
    readline = None


@contextmanager
def _category_completion(categories: list[str]):
    """Temporarily enable tab completion for known categories."""
    if readline is None or not categories:
        yield
        return
    
    log.info("Enabling tab completion for categories: %s", categories)

    unique = sorted({category for category in categories if category})
    old_completer = readline.get_completer()
    old_completer_delims = readline.get_completer_delims()
    binding = _readline_binding()

    def completer(text: str, state: int) -> str | None:
        matches = [value for value in unique if value.lower().startswith(text.lower())]
        if state < len(matches):
            return matches[state]
        return None

    try:
        readline.parse_and_bind(binding)
        readline.set_completer_delims("\n")
        readline.set_completer(completer)
        yield
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_completer_delims)


def _readline_binding() -> str:
    """Return the completion binding that matches the installed readline backend."""
    doc = getattr(readline, "__doc__", "") or ""
    if "libedit" in doc.lower():
        return "bind ^I rl_complete"
    return "tab: complete"


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
    configure_logging(state_dir)

    def ask_for_category(item: str, categories: list[str]) -> str:
        click.echo("")
        click.echo(f"Unknown item: {item}")
        if categories:
            click.echo("Known categories: " + ", ".join(sorted(categories)))
            click.echo("Hint: press Tab to complete, or type a unique prefix and press Enter.")
        with _category_completion(categories):
            raw = click.prompt(
                "Category (leave blank to keep UNCATEGORIZED)",
                default="",
                show_default=False,
            )
        return _resolve_category_input(raw, categories)

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


def _resolve_category_input(raw: str, categories: list[str]) -> str:
    """Resolve user input to an existing category when possible.

    This resolves exact matches and unique prefixes to an existing category.
    """
    cleaned = raw.strip()
    if not cleaned:
        return ""

    by_casefold = {category.casefold(): category for category in categories}
    exact = by_casefold.get(cleaned.casefold())
    if exact is not None:
        return exact

    prefix_matches = [category for category in categories if category.casefold().startswith(cleaned.casefold())]
    if len(prefix_matches) == 1:
        return prefix_matches[0]

    return cleaned
