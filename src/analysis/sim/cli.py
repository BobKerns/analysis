"""``analysis sim`` subcommand group."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import click

from analysis.sim.loader import LoadError, load_scenario

DEFAULT_INTENT_DIR = Path("state/intent")


@click.group(name="sim")
def sim_group_command() -> None:
    """Scenario-based finance simulator."""


@sim_group_command.command()
@click.argument("scenario_name")
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Data repo (default: $ANALYSIS_DATA_DIR)",
)
@click.option(
    "--intent-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_INTENT_DIR,
    show_default=True,
)
def load(scenario_name: str, data_dir: Path | None, intent_dir: Path) -> None:
    """Build state/intent/<scenario>.sqlite3 from the data repo."""
    resolved = _resolve_data_dir(data_dir)
    try:
        result = load_scenario(resolved, scenario_name, intent_dir=intent_dir)
    except LoadError as exc:
        for issue in exc.issues:
            click.echo(f"  error: {issue}", err=True)
        raise click.ClickException(f"{len(exc.issues)} issue(s)") from None
    click.echo(f"loaded {result.intent_path}")
    for table, n in result.counts.items():
        click.echo(f"  {table}: {n}")


@sim_group_command.command()
@click.argument("scenario_name")
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
)
def check(scenario_name: str, data_dir: Path | None) -> None:
    """Validate the scenario's CSVs without producing a persistent intent file."""
    resolved = _resolve_data_dir(data_dir)
    with tempfile.TemporaryDirectory() as td:
        try:
            load_scenario(resolved, scenario_name, intent_dir=Path(td))
        except LoadError as exc:
            for issue in exc.issues:
                click.echo(f"  error: {issue}", err=True)
            raise click.ClickException(f"{len(exc.issues)} issue(s)") from None
    click.echo("ok")


def _resolve_data_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir.resolve()
    env = os.environ.get("ANALYSIS_DATA_DIR")
    if not env:
        raise click.ClickException(
            "ANALYSIS_DATA_DIR not set; pass --data-dir or source the data repo's activate"
        )
    return Path(env).resolve()
