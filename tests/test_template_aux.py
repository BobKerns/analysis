"""The code repository's templates/aux/ as the lowest aux tier, and the
household's filing status checked at load (#317)."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.lineage import TEMPLATE_AUX_DIR
from analysis.sim.loader import LoadError, LoadResult, template_aux_copies


def _aux_kinds(intent: Path) -> set[str]:
    con = sqlite3.connect(intent)
    try:
        return {kind for (kind,) in con.execute("SELECT DISTINCT kind FROM aux_table")}
    finally:
        con.close()


def test_a_data_repo_without_aux_tables_reads_the_templates(
    load_test_copy: Callable[[], LoadResult], test_data_dir: Path,
) -> None:
    for path in (test_data_dir / "aux").glob("*.csv"):
        if path.name != "filing_status.csv":
            path.unlink()

    kinds = _aux_kinds(load_test_copy().intent_path)

    assert {"federal_brackets", "standard_deduction", "life_table", "irmaa_tiers"} <= kinds


def test_a_data_repo_table_replaces_the_templates_whole(
    load_test_copy: Callable[[], LoadResult], write_aux: Callable[[str, str], Path],
) -> None:
    write_aux("rmd_start_age.csv", "effective_date,start_age\n2020-01-31,70\n")

    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        rows = con.execute(
            "SELECT effective_date, payload_json FROM aux_table WHERE kind = 'rmd_start_age'"
        ).fetchall()
    finally:
        con.close()

    assert [(d, json.loads(p)["start_age"]) for d, p in rows] == [("2020-01-31", 70)]


def test_the_templates_leave_the_filing_status_to_the_household() -> None:
    assert (TEMPLATE_AUX_DIR / "filing_status.csv").read_text() == "effective_date,filing_status\n"


def test_a_tax_rule_without_a_filing_status_fails_the_load(
    load_test_copy: Callable[[], LoadResult], write_aux: Callable[[str, str], Path],
    add_state_tax: Callable[..., None],
) -> None:
    add_state_tax()
    write_aux("filing_status.csv", "effective_date,filing_status\n")

    with pytest.raises(LoadError, match=r"tax_state_prev_year.*aux/filing_status\.csv has no row"):
        load_test_copy()


def test_a_filing_status_dated_after_the_first_year_fails_the_load(
    load_test_copy: Callable[[], LoadResult], write_aux: Callable[[str, str], Path],
    add_state_tax: Callable[..., None],
) -> None:
    add_state_tax()
    write_aux("filing_status.csv", "effective_date,filing_status\n2030-01-31,MFJ\n")

    with pytest.raises(LoadError, match="no row in force by 2026-12-31"):
        load_test_copy()


def test_two_filing_statuses_on_one_date_fail_the_load(
    load_test_copy: Callable[[], LoadResult], write_aux: Callable[[str, str], Path],
) -> None:
    write_aux("filing_status.csv", "effective_date,filing_status\n2026-01-31,MFJ\n2026-01-31,Single\n")

    with pytest.raises(LoadError, match=r"2026-01-31 gives 2 filing statuses \(MFJ, Single\)"):
        load_test_copy()


def test_check_verbose_names_aux_files_that_copy_the_templates(test_data_dir: Path) -> None:
    copies = template_aux_copies(test_data_dir)
    assert "federal_brackets.csv" in copies
    assert "filing_status.csv" not in copies  # the household's own

    result = CliRunner().invoke(main, ["sim", "check", "retire_alice", "-v",
                                       "--data-dir", str(test_data_dir)])

    assert result.exit_code == 0, result.output
    assert "note: aux/federal_brackets.csv is the same as templates/aux/federal_brackets.csv" in result.output


# ---------- the template tables are complete (#319) ----------

_TEMPLATE_TABLES = sorted(p for p in TEMPLATE_AUX_DIR.glob("*.csv") if p.name != "filing_status.csv")


@pytest.mark.parametrize("path", _TEMPLATE_TABLES, ids=lambda p: p.name)
def test_every_template_table_has_rows(path: Path) -> None:
    """Every data repo reads these, so a stub breaks whoever lacks their own."""
    with path.open(newline="") as fh:
        assert len(list(csv.DictReader(fh))) > 0


@pytest.mark.parametrize("path", [p for p in _TEMPLATE_TABLES if "filing_status" in
                                  p.read_text().splitlines()[0].split(",")], ids=lambda p: p.name)
def test_filing_status_tables_cover_every_status(path: Path) -> None:
    """A death makes a joint filer Single, so a table with only MFJ fails mid-run."""
    with path.open(newline="") as fh:
        statuses = {row["filing_status"] for row in csv.DictReader(fh)}
    assert statuses >= {"MFJ", "Single", "MFS"}


def test_the_rmd_factors_cover_every_age_from_72() -> None:
    with (TEMPLATE_AUX_DIR / "rmd_factors.csv").open(newline="") as fh:
        ages = sorted(int(row["age"]) for row in csv.DictReader(fh))
    assert ages == list(range(72, 131))
