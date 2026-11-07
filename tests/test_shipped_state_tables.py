"""The states whose tables ship in templates/aux/ (#483), and a state tax
rule naming the one it reads."""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim import units
from analysis.sim.engine import _progressive_tax_cents, run_scenario
from analysis.sim.lineage import TEMPLATE_AUX_DIR
from analysis.sim.loader import LoadError, LoadResult

_STATES = ["CA", "DC", "ID", "KS", "NC", "NE", "none"]


def _brackets(state: str, effective_date: str, status: str = "MFJ") -> list[tuple[int, int]]:
    with (TEMPLATE_AUX_DIR / f"state_brackets__{state}.csv").open(newline="") as fh:
        return sorted(
            (units.dollars_to_cents(r["floor"]), units.percent_to_micros(r["marginal_rate"]))
            for r in csv.DictReader(fh)
            if r["effective_date"] == effective_date and r["filing_status"] == status
        )


def test_every_shipped_state_has_brackets_and_a_standard_deduction() -> None:
    brackets = {p.name.removeprefix("state_brackets__").removesuffix(".csv")
                for p in TEMPLATE_AUX_DIR.glob("state_brackets__*.csv")}
    deductions = {p.name.removeprefix("state_standard_deduction__").removesuffix(".csv")
                  for p in TEMPLATE_AUX_DIR.glob("state_standard_deduction__*.csv")}
    assert sorted(brackets) == _STATES
    assert deductions == brackets


@pytest.mark.parametrize(("state", "taxable", "tax"), [
    ("DC", 100_000, 6_900.00),     # DC's schedule: $3,500 + 8.5% over $60,000
    ("KS", 60_000, 3_173.20),      # Kansas's: $2,392 + 5.58% over $46,000
    ("NC", 60_000, 2_394.00),      # 3.99% flat
    ("NE", 60_000, 2_128.25),      # 2.46%, 3.51% from $8,250, 4.55% from $49,530
])
def test_a_shipped_states_2026_brackets_match_its_schedule(
    state: str, taxable: int, tax: float,
) -> None:
    assert _progressive_tax_cents(taxable * 100, _brackets(state, "2026-01-31")) == round(tax * 100)


def test_a_state_rule_that_names_no_state_fails_the_load(
    add_state_tax: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax(bracket_series=None)

    with pytest.raises(LoadError, match=r"tax_state_prev_year.*must name its state.*"
                                        r"sim tax state.*one of CA, DC, ID, KS, NC, NE, none"):
        load_test_copy()


@pytest.mark.parametrize("state", [s for s in _STATES if s != "CA"])
def test_a_rule_naming_a_shipped_state_taxes_by_its_tables(
    state: str, add_state_tax: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    add_state_tax(bracket_series=state)
    run = run_scenario(load_test_copy().intent_path, runs_dir / f"{state}.sqlite3",
                       run_by="test", allow_dirty_data=True, allow_dirty_code=True)
    with sqlite3.connect(run) as con:
        summary = dict(con.execute(
            "SELECT bucket, amount FROM tax_year_summary WHERE year = 2026").fetchall())

    taxable = summary["state_taxable_income"]
    assert taxable > 0
    if state == "ID":  # its 2025 zero band, grown to 2026 by the run's CPI
        assert 0 < summary["state_tax_liability"] < taxable * 0.053
    else:
        assert summary["state_tax_liability"] == _progressive_tax_cents(
            taxable, _brackets(state, "2026-01-31"))
