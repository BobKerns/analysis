"""The states whose tables ship in templates/aux/ (#483), and a state tax
reading those of the state the household lives in (residence.csv, #509)."""

from __future__ import annotations

import csv
from contextlib import closing
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim import units
from analysis.sim.engine import run_scenario
from analysis.sim.taxes.common import progressive_tax
from analysis.sim.lineage import TEMPLATE_AUX_DIR
from analysis.sim.loader import LoadError, LoadResult

_STATES = ["CA", "DC", "ID", "KS", "NC", "NE", "WA", "none"]


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
    assert progressive_tax(taxable * 100, _brackets(state, "2026-01-31")) == round(tax * 100)


def test_a_state_tax_with_no_residence_fails_the_load(
    add_state_tax: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax(state=None)

    with pytest.raises(LoadError, match=r"taxes/state.csv: the scenario doesn't say where the "
                                        r"household lives; set it with `sim tax state`"):
        load_test_copy()


@pytest.mark.parametrize("state", [s for s in _STATES if s != "CA"])
def test_living_in_a_shipped_state_taxes_by_its_tables(
    state: str, add_state_tax: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    """Each shipped state's tables, and Florida, with no income tax, the
    0% none table."""
    add_state_tax(state="FL" if state == "none" else state)
    run = run_scenario(load_test_copy().intent_path, runs_dir / f"{state}.sqlite3",
                       run_by="test", allow_dirty_data=True, allow_dirty_code=True)
    with closing(sqlite3.connect(run)) as con:
        summary = dict(con.execute(
            "SELECT bucket, amount FROM tax_year_summary WHERE year = 2026").fetchall())

    taxable = summary["state_taxable_income"]
    assert taxable > 0
    if state == "ID":  # its 2025 zero band, grown to 2026 by the run's CPI
        assert 0 < summary["state_tax"] < taxable * 0.053
    else:  # before its credits (Nebraska's exemption credit)
        assert summary["state_tax"] + summary["state_credits"] == progressive_tax(
            taxable, _brackets(state, "2026-01-31"))


def test_shipped_tables_need_no_inflation_before_2026(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """Every scenario with a tax rule checks every indexed table's series,
    read or not, so a table dated before 2026 would fail one whose own CPI
    starts in January 2026."""
    write_aux("inflation__cpi.csv", "effective_date,annual_rate\n2026-01-31,2.5%\n")
    add_state_tax()

    load_test_copy()


def _filers_from(test_data_dir: Path, age: int) -> Callable[[int], int]:
    """How many of the two prototype filers have reached ``age`` by
    December 31 of a year, from the test data's own birthdates, so the
    tests below follow the data when its years move."""
    with (test_data_dir / "persons.csv").open(newline="") as fh:
        born = [int(r["birthdate"][:4]) for r in csv.DictReader(fh) if r["role"] == "primary"]
    return lambda year: sum(year - b >= age for b in born)


def _run_in(state: str, add_state_tax: Callable[..., None],
            run_test_copy: Callable[[], Path]) -> dict[str, dict[int, int]]:
    add_state_tax(state=state)
    with closing(sqlite3.connect(run_test_copy())) as con:
        lines: dict[str, dict[int, int]] = {}
        for bucket, year, amount in con.execute(
                "SELECT bucket, year, amount FROM tax_year_summary WHERE bucket LIKE 'state_%'"):
            lines.setdefault(bucket, {})[year] = amount
    return lines


@pytest.mark.slow
def test_kansas_adds_its_exemption_and_extra_deduction_at_65_to_the_deduction(
    add_state_tax: Callable[..., None], run_test_copy: Callable[[], Path], test_data_dir: Path,
) -> None:
    """Kansas's personal exemption is its own `filers` row, $9,160 each, so
    a household that itemizes keeps it; from 65 each spouse adds $700."""
    lines = _run_in("KS", add_state_tax, run_test_copy)
    over_65 = _filers_from(test_data_dir, 65)
    years = sorted(lines["state_agi"])
    assert any(over_65(y) for y in years) and any(not over_65(y) for y in years)
    for year in years:
        assert lines["state_personal_exemption"][year] == 2 * 9160_00
        assert lines["state_age_65_deduction"][year] == over_65(year) * 700_00
        assert lines["state_taxable_income"][year] == max(0, (
            lines["state_agi"][year] - lines["state_deduction"][year]
            - 2 * 9160_00 - over_65(year) * 700_00))


@pytest.mark.slow
def test_dc_adds_the_federal_extra_deduction_at_65_indexed_like_its_standard_deduction(
    add_state_tax: Callable[..., None], run_test_copy: Callable[[], Path], test_data_dir: Path,
) -> None:
    """$1,650 for each married filer from 65 (2026's federal figure), grown
    by cpi as DC's standard deduction ($32,200 joint) is."""
    lines = _run_in("DC", add_state_tax, run_test_copy)
    over_65 = _filers_from(test_data_dir, 65)
    extra = lines["state_age_65_deduction"]
    assert any(extra.values())
    for year, amount in extra.items():
        if not over_65(year):
            assert amount == 0
            continue
        per_filer = amount // over_65(year)
        assert amount == per_filer * over_65(year) and per_filer % 100 == 0
        expected = 1650_00 * lines["state_standard_deduction"][year] / 32200_00
        # The standard deduction rounds to $50, the extra to the dollar.
        assert abs(per_filer - expected) <= 5_00


@pytest.mark.slow
def test_nebraska_gives_each_filer_its_personal_exemption_credit(
    add_state_tax: Callable[..., None], run_test_copy: Callable[[], Path],
) -> None:
    """$176 each for 2026, indexed by cpi to the dollar; not refundable, so
    it is all taken only in a year still taxed after it."""
    lines = _run_in("NE", add_state_tax, run_test_copy)
    credit = lines["state_personal_exemption_credit"]
    taxed = [y for y in sorted(credit) if lines["state_tax"][y] > 0]
    amounts = [credit[y] for y in taxed]
    assert len(amounts) > 1
    assert all(a >= 2 * 176_00 and a % 200 == 0 for a in amounts)
    assert amounts == sorted(amounts) and amounts[-1] > amounts[0]
    assert all(credit[y] <= 2 * 176_00 * 2 for y in credit if y not in taxed)


@pytest.mark.slow
def test_idahos_grocery_credit_is_refundable(
    add_state_tax: Callable[..., None], run_test_copy: Callable[[], Path],
) -> None:
    """$155 each, not indexed, paid in full even in a year with no tax for
    it to come off."""
    lines = _run_in("ID", add_state_tax, run_test_copy)
    assert set(lines["state_grocery_credit"].values()) == {2 * 155_00}
    assert any(lines["state_tax"][y] < 0 for y in lines["state_tax"])
