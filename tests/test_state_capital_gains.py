"""A state's own treatment of capital gains (#485): a long-term gain
exclusion (``state_ltcg_exclusion``) and gain rates of its own
(``state_ltcg_brackets``, ``state_stcg_brackets``), each keyed by state like
its brackets, so a year reads those of the state it is filed in (#509)."""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim import units
from analysis.sim.engine import TaxRule, _load_aux, _prepare_tax, run_scenario
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.taxes.common import progressive_tax

_BRACKETS_HEADER = "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
_EXCLUSION_HEADER = "effective_date,exclusion,min_exclusion,max_exclusion\n"


@pytest.fixture
def state_gains(
    add_state_tax: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> Callable[..., dict[str, int]]:
    """Run retire_alice with a state tax rule and 2026 gains, returning
    2026's tax_year_summary buckets."""
    add_state_tax()
    runs = iter(range(100))

    def _run(ltcg: str = "100000.00", stcg: str | None = None,
             dividends: str | None = None) -> dict[str, int]:
        rows = [f"gain,Income_CapitalGain,Asset_Checking,{ltcg},ltcg"]
        if stcg is not None:
            rows.append(f"short,Income_CapitalGain,Asset_Checking,{stcg},stcg")
        if dividends is not None:
            rows.append(f"dividend,Income_CapitalGain,Asset_Checking,{dividends},qualified_dividend")
        retire_alice_csv("year_to_date.csv").write_text(
            "name,from_account,to_account,amount,tax_character\n" + "\n".join(rows) + "\n")
        run = run_scenario(load_test_copy().intent_path, runs_dir / f"r{next(runs)}.sqlite3",
                           run_by="test", allow_dirty_data=True, allow_dirty_code=True)
        with sqlite3.connect(run) as con:
            return dict(con.execute(
                "SELECT bucket, amount FROM tax_year_summary WHERE year = 2026").fetchall())
    return _run


def _ca_brackets(test_data_dir: Path) -> list[tuple[int, int]]:
    with (test_data_dir / "aux" / "state_brackets__CA.csv").open() as f:
        return sorted(
            (units.dollars_to_cents(r["floor"]), units.percent_to_micros(r["marginal_rate"]))
            for r in csv.DictReader(f)
            if r["filing_status"] == "MFJ" and r["effective_date"] == "2026-01-31")


def test_an_excluded_share_of_long_term_gain_is_left_out_of_state_income(
    state_gains: Callable[..., dict[str, int]], write_aux: Callable[[str, str], Path],
) -> None:
    half_the_gain = state_gains(ltcg="50000.00")
    write_aux("state_ltcg_exclusion__CA.csv", _EXCLUSION_HEADER + "2026-01-31,50%,,\n")
    excluded = state_gains()
    assert excluded["state_ltcg_exclusion"] == 50_000_00
    assert excluded["state_ltcg_taxed"] == 50_000_00
    assert excluded["state_tax"] == half_the_gain["state_tax"]
    assert half_the_gain["state_ltcg_exclusion"] == 0


@pytest.mark.parametrize(("row", "excluded"), [
    ("40%,,10000.00", 10_000_00),         # capped (Vermont's 40% is capped)
    ("0%,5000.00,", 5_000_00),            # a flat amount
    ("40%,5000.00,", 40_000_00),          # the greater of the two
    ("0%,250000.00,", 100_000_00),        # never more than the gain
    ("100%,,", 100_000_00),               # all of it (Missouri)
])
def test_the_exclusion_is_a_percent_raised_to_its_minimum_and_capped(
    state_gains: Callable[..., dict[str, int]], write_aux: Callable[[str, str], Path],
    row: str, excluded: int,
) -> None:
    write_aux("state_ltcg_exclusion__CA.csv", _EXCLUSION_HEADER + f"2026-01-31,{row}\n")
    summary = state_gains()
    assert summary["state_ltcg_exclusion"] == excluded
    assert summary["state_ltcg_taxed"] == 100_000_00 - excluded


def test_a_state_long_term_gain_rate_taxes_the_gain_stacked_on_top(
    state_gains: Callable[..., dict[str, int]], write_aux: Callable[[str, str], Path],
    test_data_dir: Path,
) -> None:
    as_ordinary = state_gains()
    write_aux("state_ltcg_brackets__CA.csv", _BRACKETS_HEADER + "2026-01-31,MFJ,0,0.00,3%\n")
    preferential = state_gains()
    taxable = preferential["state_taxable_income"]
    assert taxable == as_ordinary["state_taxable_income"]
    gain = min(100_000_00, taxable)
    expected = progressive_tax(taxable - gain, _ca_brackets(test_data_dir)) + gain * 3 // 100
    assert preferential["state_tax"] == expected
    assert preferential["state_tax"] < as_ordinary["state_tax"]


def test_a_state_short_term_gain_rate_taxes_the_slice_below_long_term_gain(
    state_gains: Callable[..., dict[str, int]], write_aux: Callable[[str, str], Path],
    test_data_dir: Path,
) -> None:
    write_aux("state_ltcg_brackets__CA.csv", _BRACKETS_HEADER + "2026-01-31,MFJ,0,0.00,5%\n")
    write_aux("state_stcg_brackets__CA.csv", _BRACKETS_HEADER + "2026-01-31,MFJ,0,0.00,8.5%\n")
    summary = state_gains(stcg="20000.00")
    taxable = summary["state_taxable_income"]
    ordinary = taxable - 100_000_00 - 20_000_00
    assert ordinary > 0  # the test means both gains to be wholly taxed
    expected = (progressive_tax(ordinary, _ca_brackets(test_data_dir))
                + 20_000_00 * 85 // 1000 + 100_000_00 * 5 // 100)
    assert summary["state_tax"] == expected


def test_short_term_rates_alone_keep_long_term_gain_on_top_at_ordinary_rates(
    state_gains: Callable[..., dict[str, int]], write_aux: Callable[[str, str], Path],
    test_data_dir: Path,
) -> None:
    write_aux("state_stcg_brackets__CA.csv", _BRACKETS_HEADER + "2026-01-31,MFJ,0,0.00,8.5%\n")
    summary = state_gains(stcg="20000.00")
    taxable = summary["state_taxable_income"]
    ordinary = taxable - 100_000_00 - 20_000_00
    assert ordinary > 0
    brackets = _ca_brackets(test_data_dir)
    expected = (progressive_tax(ordinary, brackets) + 20_000_00 * 85 // 1000
                + progressive_tax(taxable, brackets)
                - progressive_tax(taxable - 100_000_00, brackets))
    assert summary["state_tax"] == expected


def test_the_ordinary_part_of_taxable_income_leaves_out_separately_taxed_short_term_gain(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    """What fill_bracket measures a rate target against (#252): with the
    state's own short-term rates, that gain isn't ordinary income."""
    add_state_tax()
    write_aux("state_stcg_brackets__CA.csv", _BRACKETS_HEADER + "2026-01-31,MFJ,0,0.00,8.5%\n")
    retire_alice_csv("year_to_date.csv").write_text(
        "name,from_account,to_account,amount,tax_character\n"
        "short,Income_CapitalGain,Asset_Checking,20000.00,stcg\n")
    run = runs_dir / "measure.sqlite3"
    run_scenario(load_test_copy().intent_path, run, run_by="test", allow_dirty_data=True)
    con = sqlite3.connect(run)
    con.row_factory = sqlite3.Row
    try:
        ret = _prepare_tax(con, {"scope": "state"},
                           aux=_load_aux(con), tax_year=2026, tid=0,
                           kind="state_tables",
                           jurisdictions={"federal": TaxRule("federal", {"scope": "federal"}, 1)}).compute()
    finally:
        con.close()
    assert ret.ordinary_taxable == ret.taxable_income - 20_000_00


def test_without_gain_tables_a_state_taxes_gains_as_ordinary_income(
    state_gains: Callable[..., dict[str, int]], test_data_dir: Path,
) -> None:
    summary = state_gains(stcg="20000.00")
    assert summary["state_ltcg_exclusion"] == 0
    assert summary["state_tax"] == progressive_tax(
        summary["state_taxable_income"], _ca_brackets(test_data_dir))


def test_a_move_to_another_state_takes_its_gain_rules(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    add_residence: Callable[..., Path], runs_dir: Path,
) -> None:
    """After a move a year reads the new state's gain tables, and none of
    the old state's where the new state has none."""
    add_state_tax("CA")
    write_aux("state_brackets__WA.csv", _BRACKETS_HEADER + "2026-01-31,MFJ,0,0.00,1%\n")
    write_aux("state_ltcg_exclusion__CA.csv", _EXCLUSION_HEADER + "2026-01-31,50%,,\n")
    write_aux("state_ltcg_brackets__CA.csv", _BRACKETS_HEADER + "2026-01-31,MFJ,0,0.00,3%\n")
    write_aux("state_ltcg_exclusion__WA.csv", _EXCLUSION_HEADER + "2026-01-31,0%,1000.00,\n")
    add_residence("2028-03", "WA")
    for year in (2026, 2028):
        append_calendar_row(
            date=f"{year}-06", kind="one_off_transfer", account_1="Income_CapitalGain",
            account_2="Asset_Checking", amount="10000.00", tax_character="ltcg")
    run = runs_dir / "move.sqlite3"
    run_scenario(load_test_copy().intent_path, run, run_by="test", allow_dirty_data=True)
    with sqlite3.connect(run) as con:
        exclusion = dict(con.execute(
            "SELECT year, amount FROM tax_year_summary WHERE bucket = 'state_ltcg_exclusion'"
        ).fetchall())
        taxed = dict(con.execute(
            "SELECT year, amount FROM tax_year_summary WHERE bucket = 'state_tax'").fetchall())
        income = dict(con.execute(
            "SELECT year, amount FROM tax_year_summary WHERE bucket = 'state_taxable_income'"
        ).fetchall())
    assert exclusion[2026] == 5_000_00   # California's half, before the move
    assert exclusion[2028] == 1_000_00   # Washington's flat amount
    # Washington's 1%, without California's gain rate.
    assert taxed[2028] == progressive_tax(income[2028], [(0, 10_000)])


def test_a_move_to_a_state_whose_gain_rule_starts_later_applies_it_from_then(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    add_residence: Callable[..., Path], runs_dir: Path,
) -> None:
    add_state_tax("CA")
    write_aux("state_brackets__WA.csv", _BRACKETS_HEADER + "2026-01-31,MFJ,0,0.00,1%\n")
    write_aux("state_ltcg_exclusion__WA.csv", _EXCLUSION_HEADER + "2029-01-31,0%,1000.00,\n")
    add_residence("2028-03", "WA")
    for year in (2028, 2029):
        append_calendar_row(
            date=f"{year}-06", kind="one_off_transfer", account_1="Income_CapitalGain",
            account_2="Asset_Checking", amount="10000.00", tax_character="ltcg")
    run = runs_dir / "later.sqlite3"
    run_scenario(load_test_copy().intent_path, run, run_by="test", allow_dirty_data=True)
    with sqlite3.connect(run) as con:
        exclusion = dict(con.execute(
            "SELECT year, amount FROM tax_year_summary WHERE bucket = 'state_ltcg_exclusion'"
        ).fetchall())
    assert exclusion[2028] == 0
    assert exclusion[2029] == 1_000_00


@pytest.mark.parametrize(("filename", "text", "message"), [
    ("state_ltcg_exclusion.csv", _EXCLUSION_HEADER + "2026-01-31,50%,,\n", "name the state"),
    ("state_ltcg_brackets.csv", _BRACKETS_HEADER + "2026-01-31,MFJ,0,0.00,3%\n",
     "name the state"),
    ("state_ltcg_exclusion__CA.csv", _EXCLUSION_HEADER + "2026-01-31,150%,,\n", "0% to 100%"),
    ("state_ltcg_exclusion__CA.csv", _EXCLUSION_HEADER + "2026-01-31,40%,,-1.00\n",
     "can't be negative"),
    ("state_ltcg_exclusion__CA.csv", _EXCLUSION_HEADER + "2026-01-31,40%,9000.00,5000.00\n",
     "more than max_exclusion"),
])
def test_the_loader_refuses_a_state_gain_table_it_cannot_use(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
    filename: str, text: str, message: str,
) -> None:
    write_aux(filename, text)
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_a_state_taxes_qualified_dividends_as_ordinary_income(
    state_gains: Callable[..., dict[str, int]], write_aux: Callable[[str, str], Path],
) -> None:
    """The state's gain rules are for gains: a dividend gets neither its
    exclusion nor its gain rates."""
    write_aux("state_ltcg_exclusion__CA.csv", _EXCLUSION_HEADER + "2026-01-31,50%,,\n")
    write_aux("state_ltcg_brackets__CA.csv", _BRACKETS_HEADER + "2026-01-31,MFJ,0,0.00,0%\n")
    without = state_gains(ltcg="0")
    summary = state_gains(ltcg="0", dividends="10000.00")
    assert summary["state_ltcg_taxed"] == 0
    assert summary["state_ordinary_income"] == without["state_ordinary_income"] + 10_000_00
    assert summary["state_tax"] > without["state_tax"]
