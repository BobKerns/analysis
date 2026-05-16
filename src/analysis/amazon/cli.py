from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

import click

from analysis.amazon.categorizer import categorize_amazon_csv
from analysis.log import configure_logging
from analysis.storage import JsonStateStore

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


@click.command(name="categorize")
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


@click.command(name="review")
@click.option("--state-dir", default="state", show_default=True, type=click.Path(path_type=Path))
def amazon_review_mappings_command(state_dir: Path) -> None:
    """Review mapped items for a category and optionally re-categorize them."""
    configure_logging(state_dir)

    store = JsonStateStore(state_dir)
    categories = store.load_categories()
    known_mapping = store.load_mapping()

    if not known_mapping:
        click.echo("No item mappings found in state.")
        return

    source_category = _choose_source_category(categories, "Category to review")
    if source_category is None:
        return

    items = sorted(item for item, mapped in known_mapping.items() if mapped == source_category)
    if not items:
        click.echo(f"No mapped items found for category: {source_category}")
        return

    click.echo(f"Reviewing {len(items)} item(s) in category: {source_category}")
    click.echo("Enter to keep current category, '-' to remove mapping (UNCATEGORIZED), or set a new category.")

    changed = 0
    for item in items:
        click.echo("")
        click.echo(f"Item: {item}")
        prompt_text = f"New category [{source_category}]"
        with _category_completion(categories):
            raw = click.prompt(prompt_text, default="", show_default=False)

        cleaned = raw.strip()
        if not cleaned:
            continue

        if cleaned == "-":
            if item in known_mapping:
                del known_mapping[item]
                changed += 1
                store.save_mapping(known_mapping)
            continue

        chosen = _resolve_category_input(cleaned, categories)
        if chosen == source_category:
            continue

        known_mapping[item] = chosen
        if chosen not in categories:
            categories.append(chosen)
            store.save_categories(categories)
        store.save_mapping(known_mapping)
        changed += 1

    click.echo("")
    click.echo(f"Updated mappings: {changed}")
    click.echo(f"Remaining in {source_category}: {sum(1 for value in known_mapping.values() if value == source_category)}")


@click.command(name="delete")
@click.option("--state-dir", default="state", show_default=True, type=click.Path(path_type=Path))
def amazon_delete_category_command(state_dir: Path) -> None:
    """Delete a category and remove all item mappings assigned to it."""
    configure_logging(state_dir)

    store = JsonStateStore(state_dir)
    categories = store.load_categories()
    known_mapping = store.load_mapping()

    category = _choose_source_category(categories, "Category to delete")
    if category is None:
        return

    mapped_count = sum(1 for value in known_mapping.values() if value == category)
    confirmed = click.confirm(
        f"Delete category '{category}' and remove {mapped_count} mapped item(s)?",
        default=False,
    )
    if not confirmed:
        click.echo("Canceled.")
        return

    updated_categories = [value for value in categories if value != category]
    updated_mapping = {key: value for key, value in known_mapping.items() if value != category}
    store.save_categories(updated_categories)
    store.save_mapping(updated_mapping)

    click.echo(f"Deleted category: {category}")
    click.echo(f"Removed mappings: {mapped_count}")


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


def _choose_source_category(categories: list[str], prompt_label: str) -> str | None:
    if not categories:
        click.echo("No categories found in state.")
        return None

    click.echo("Known categories: " + ", ".join(sorted(categories)))
    click.echo("Hint: press Tab to complete, or type a unique prefix and press Enter.")
    with _category_completion(categories):
        raw = click.prompt(prompt_label, default="", show_default=False)

    chosen = _resolve_category_input(raw, categories)
    if chosen in categories:
        return chosen

    click.echo("No valid category selected.")
    return None


@click.group(name="amazon")
def amazon_group_command() -> None:
    """Amazon expense categorization tools."""


amazon_group_command.add_command(amazon_categorize_command)


@click.group(name="category")
def amazon_category_group_command() -> None:
    """Manage categories and existing item mappings."""


amazon_category_group_command.add_command(amazon_review_mappings_command)
amazon_category_group_command.add_command(amazon_delete_category_command)
amazon_group_command.add_command(amazon_category_group_command)
