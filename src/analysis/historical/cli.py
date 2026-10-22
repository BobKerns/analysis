"""``analysis historical``: summarize public price and return series for
calibrating an ensemble's varying rates."""

from __future__ import annotations

from pathlib import Path

import click

from analysis.historical.calibration import DEFAULT_CACHE, DEFAULT_HOLD_YEARS, DataError, Fetcher, default_contact, run


@click.command(name="historical", epilog=(
    "Series sources: bls:ID (or a bare BLS id), bea:CODE, rcpie:COLUMN, damodaran:COLUMN, "
    "fred:ID, csv:PATH; see docs/guides/historical-calibration.md."))
@click.argument("series", nargs=-1, required=True)
@click.option("--reference", help="Series to correlate with and measure the gap against (default: none).")
@click.option("--period", "periods", multiple=True, metavar="NAME=FIRST-LAST",
              help="A period to summarize, by the years the annual rates end in; repeatable "
                   "(default: the guide's episodes and the full history).")
@click.option("--window", type=click.IntRange(min=1), default=1, show_default=True,
              help="Summarize rolling N-year compound rates instead of single years.")
@click.option("--hold-years", type=click.IntRange(min=2), default=DEFAULT_HOLD_YEARS, show_default=True,
              help="Years a held row keeps its draw, for the suggested held vary_sd.")
@click.option("--basis", type=click.Choice(["dec", "average"]), default="dec", show_default=True,
              help="Annual rate of a monthly level: December over December, "
                   "or annual average over annual average.")
@click.option("--runs-above", type=float, metavar="PCT",
              help="Also list stretches of consecutive years above PCT percent.")
@click.option("--format", "fmt", type=click.Choice(["markdown", "csv"]), default="markdown",
              show_default=True)
@click.option("--cache", type=click.Path(file_okay=False, path_type=Path), default=DEFAULT_CACHE,
              show_default=True, help="Download cache; the repository's committed copies by default.")
@click.option("--refresh", is_flag=True, help="Download again even if cached.")
@click.option("--contact", default=default_contact, show_default="git config user.email",
              help="Email for the User-Agent (BLS requires one); "
                   "$HISTORICAL_CALIBRATION_CONTACT overrides git's.")
def historical_command(series: tuple[str, ...], reference: str | None, periods: tuple[str, ...],
                       window: int, hold_years: int, basis: str, runs_above: float | None, fmt: str, cache: Path,
                       refresh: bool, contact: str | None) -> None:
    """Summarize public price and return series for calibrating rates.

    SERIES are [LABEL=]SOURCE:ID[@rate|@index]. For each one and each period
    this prints the compound mean rate, median, SD, range, persistence, and the
    correlation and gap against --reference, then the values each series
    suggests for a varying set_rate row, under calendar.csv's column names,
    and a legend tying those names to the tables' headings.
    """
    if runs_above is not None and fmt == "csv":
        raise click.UsageError("--runs-above prints a Markdown table; it can't be combined with --format csv")
    try:
        run(series, reference_arg=reference, period_args=periods, window=window, hold_years=hold_years, basis=basis,
            runs_above_pct=runs_above, fmt=fmt, cache=cache, refresh=refresh, contact=contact)
    except DataError as exc:
        raise click.ClickException(str(exc)) from exc


@click.command(name="historical-charts")
@click.argument("out_dir", type=click.Path(file_okay=False, path_type=Path),
                default="docs/guides/century-of-rates")
@click.option("--cache", type=click.Path(file_okay=False, path_type=Path), default=DEFAULT_CACHE,
              show_default=True, help="Download cache; the repository's committed copies by default.")
def historical_charts_command(out_dir: Path, cache: Path) -> None:
    """Draw the charts for docs/guides/century-of-rates.md into OUT_DIR.

    They're drawn from the committed historical data, so nothing is
    downloaded unless a series is missing from --cache.
    """
    from analysis.historical.charts import load_history, write_charts

    try:
        history = load_history(Fetcher(cache, default_contact()))
    except DataError as exc:
        raise click.ClickException(str(exc)) from exc
    for path in write_charts(history, out_dir):
        click.echo(f"wrote {path}")
