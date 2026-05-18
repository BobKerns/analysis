from __future__ import annotations

import click

from analysis.amazon.cli import amazon_group_command
from analysis.sim.cli import sim_group_command


@click.group()
def main() -> None:
    """Analysis command-line tools."""


main.add_command(amazon_group_command)
main.add_command(sim_group_command)


if __name__ == "__main__":
    main()
