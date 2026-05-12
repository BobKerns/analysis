from __future__ import annotations

import click

from analysis.amazon.cli import amazon_categorize_command


@click.group()
def main() -> None:
    """Analysis command-line tools."""


main.add_command(amazon_categorize_command)


if __name__ == "__main__":
    main()
