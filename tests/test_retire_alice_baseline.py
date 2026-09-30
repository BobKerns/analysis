"""Golden-output regression for the retire_alice run.

Pins net worth, per-account state and the full transfer ledger so structural
refactors (ADR-005 lots) can prove they change no numbers. Regenerate after an
intentional behavior change with ``UPDATE_BASELINE=1 pytest <this file>``.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from pathlib import Path

import pytest

BASELINE_DIR = Path(__file__).parent / "data" / "sim" / "retire_alice_baseline"

QUERIES = {
    "net_worth": (
        "SELECT month, net_worth FROM net_worth ORDER BY month"
    ),
    "account_state": (
        "SELECT s.month, a.name, st.balance, st.cost_basis "
        "FROM account_state st "
        "JOIN simulation_step s ON s.id = st.step_id "
        "JOIN account a ON a.id = st.account_id "
        "ORDER BY s.month, a.name"
    ),
    "transfers": (
        "SELECT t.date, fa.name, ta.name, t.amount, t.tax_character, t.reason_kind "
        "FROM transfer_event t "
        "JOIN account fa ON fa.id = t.from_account "
        "JOIN account ta ON ta.id = t.to_account "
        "ORDER BY t.id"
    ),
}


def _dump(run_file: Path, query: str) -> str:
    con = sqlite3.connect(f"file:{run_file}?mode=ro", uri=True)
    try:
        rows = con.execute(query).fetchall()
    finally:
        con.close()
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in rows:
        writer.writerow("" if v is None else v for v in row)
    return out.getvalue()


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_retire_alice_matches_baseline(name: str, retire_alice_run_file: Path) -> None:
    actual = _dump(retire_alice_run_file, QUERIES[name])
    golden = BASELINE_DIR / f"{name}.csv"
    if os.environ.get("UPDATE_BASELINE"):
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual)
    expected = golden.read_text()
    if actual != expected:
        got, want = actual.splitlines(), expected.splitlines()
        first = next(
            (i for i, (g, w) in enumerate(zip(got, want, strict=True)) if g != w),
            min(len(got), len(want)),
        )
        pytest.fail(
            f"{name}: {len(got)} rows vs baseline {len(want)}; first difference at "
            f"row {first + 1}:\n  got:  {got[first] if first < len(got) else '<none>'}"
            f"\n  want: {want[first] if first < len(want) else '<none>'}"
        )
