"""Tests for Quicken Net Worth export parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from analysis.sim.quicken import (
    format_dollars,
    load_account_map,
    parse_net_worth_export,
)

_SAMPLE = """\
Net Worth Export
Month to Date: 2026-08-01 - 2026-08-18
41 Accounts


"",,"2026-08-18"
-------------------------,-------------------------,-------------------------
"Assets",,
,"Checking",
," - Business","304.17"
," - WF CA","20.11"
," - Total Checking","324.28"
,"Investments",
," - Retirement",
," -  - Bill Fidelity","10,000.38"
," -  - Total Retirement","10,000.38"
," - Total Investments","10,000.38"
,"Total Assets","10,324.66"
"Liabilities",,
,"Credit Card",
," - Amex","-8,378.91"
," - Total Credit Card","-8,378.91"
,"Total Liabilities","-8,378.91"
-------------------------,-------------------------,-------------------------
Net Worth,,"1,945.75"
"""


def test_parse_net_worth_export_extracts_leaf_accounts_only(tmp_path: Path) -> None:
    path = tmp_path / "export.csv"
    path.write_text(_SAMPLE, encoding="utf-8")

    balances = {b.quicken_name: b.cents for b in parse_net_worth_export(path)}

    assert balances == {
        "Business": 30417,
        "WF CA": 2011,
        "Bill Fidelity": 1000038,
        "Amex": -837891,
    }


def test_format_dollars_roundtrips_sign_and_cents() -> None:
    assert format_dollars(30417) == "304.17"
    assert format_dollars(-837891) == "-8378.91"
    assert format_dollars(0) == "0.00"


def test_load_account_map_reads_map_and_skip_actions(tmp_path: Path) -> None:
    path = tmp_path / "map.csv"
    path.write_text(
        "quicken_name,account_name,action,note\n"
        "Business,Business,map,\n"
        "Home Repair Reserve,,skip,not tracked\n",
        encoding="utf-8",
    )
    entries = load_account_map(path)
    assert entries["Business"].action == "map"
    assert entries["Business"].account_name == "Business"
    assert entries["Home Repair Reserve"].action == "skip"


def test_load_account_map_rejects_bad_header(tmp_path: Path) -> None:
    path = tmp_path / "map.csv"
    path.write_text("wrong,header\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected header"):
        load_account_map(path)
