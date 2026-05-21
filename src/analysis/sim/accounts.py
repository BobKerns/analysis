"""Accounts: tabular view over the loaded intent DB.

Pure helpers used by the CLI (``analysis sim account list``) and notebooks.
The intent DB is the single source of truth: CSV files are read only by
``analysis.sim.loader``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from analysis.sim.loader import open_intent_db


@dataclass(frozen=True)
class AccountRow:
    name: str
    kind: str
    subkind: str
    owner: str
    tax_treatment: str
    opened: str
    closed: str
    opening_balance_cents: int | None
    rate_series: str | None
    annual_rate_micros: int | None
    rate_role: str | None  # "growth" | "yield" | None


def _rate_role(subkind: str) -> str:
    cash = {"checking", "savings"}
    return "yield" if subkind in cash else "growth"


def _latest_rate_micros(
    con, series: str, as_of: str | None
) -> int | None:
    """Most recent ``aux_table`` rate row for ``series`` with
    effective_date ≤ as_of (default: today)."""
    cutoff = as_of or date.today().isoformat()
    row = con.execute(
        "SELECT payload_json FROM aux_table "
        "WHERE kind = 'rate' AND series = ? AND effective_date <= ? "
        "ORDER BY effective_date DESC LIMIT 1",
        (series, cutoff),
    ).fetchone()
    if row is None:
        # Fall back to earliest if none qualify (matches prior CSV behavior).
        row = con.execute(
            "SELECT payload_json FROM aux_table "
            "WHERE kind = 'rate' AND series = ? "
            "ORDER BY effective_date ASC LIMIT 1",
            (series,),
        ).fetchone()
        if row is None:
            return None
    payload = json.loads(row["payload_json"])
    v = payload.get("annual_rate_micros")
    return int(v) if v is not None else None


def load_accounts_view(
    data_dir: Path | str,
    scenario: str | None = None,
    *,
    as_of: str | None = None,
) -> list[AccountRow]:
    """Return accounts joined with model opening balances and percent-of-balance rate rules."""
    with open_intent_db(data_dir, scenario) as (con, _name):
        accounts = con.execute(
            "SELECT a.id, a.name, a.kind, a.subkind, "
            "       COALESCE(a.owner, '') AS owner, "
            "       COALESCE(a.tax_treatment, '') AS tax_treatment, "
            "       COALESCE(a.opened, '') AS opened, "
            "       COALESCE(a.closed, '') AS closed "
            "FROM account a ORDER BY a.id"
        ).fetchall()

        # Opening balances come from the scenario's account_model.
        opening: dict[int, int | None] = {
            r["account_id"]: r["opening_balance"]
            for r in con.execute(
                "SELECT m.account_id, m.opening_balance "
                "FROM account_model_member m "
                "JOIN scenario s ON s.account_model_id = m.model_id"
            )
        }

        # Rate series come from percent_of_balance scheduled transactions
        # keyed by to_account (the balance-bearing account, per ADR-003).
        rate_by_name: dict[str, tuple[str, int | None]] = {}
        for r in con.execute(
            "SELECT a.name AS account_name, s.series AS series "
            "FROM scheduled_transaction s "
            "JOIN account a ON a.id = s.to_account "
            "WHERE s.amount_rule_kind = 'percent_of_balance' "
            "  AND s.series IS NOT NULL"
        ):
            rate_by_name[r["account_name"]] = (
                r["series"], _latest_rate_micros(con, r["series"], as_of),
            )

        out: list[AccountRow] = []
        for a in accounts:
            series, micros = rate_by_name.get(a["name"], (None, None))
            out.append(
                AccountRow(
                    name=a["name"],
                    kind=a["kind"],
                    subkind=a["subkind"],
                    owner=a["owner"],
                    tax_treatment=a["tax_treatment"],
                    opened=a["opened"],
                    closed=a["closed"],
                    opening_balance_cents=opening.get(a["id"]),
                    rate_series=series,
                    annual_rate_micros=micros,
                    rate_role=_rate_role(a["subkind"]) if series else None,
                )
            )
        return out


def accounts_dataframe(
    data_dir: Path | str,
    scenario: str | None = None,
    *,
    as_of: str | None = None,
):
    """Return ``load_accounts_view`` as a pandas DataFrame for notebook display."""
    import pandas as pd
    rows = load_accounts_view(data_dir, scenario, as_of=as_of)
    df = pd.DataFrame([r.__dict__ for r in rows])
    if df.empty:
        return df
    df["opening_balance"] = df["opening_balance_cents"].apply(
        lambda c: None if c is None else c / 100
    )
    df["annual_rate_pct"] = df["annual_rate_micros"].apply(
        lambda m: None if m is None else round(m / 10_000, 4)
    )
    return df[[
        "name", "kind", "subkind", "owner", "tax_treatment",
        "opening_balance", "rate_role", "rate_series", "annual_rate_pct",
        "opened", "closed",
    ]]


def render_accounts_table(rows: list[AccountRow], title: str | None = None):
    """Build a ``rich.table.Table`` of accounts with balances and rates."""
    from rich.table import Table
    table = Table(title=title, header_style="bold", show_lines=False)
    for col, justify in (
        ("Name", "left"), ("Kind", "left"), ("Subkind", "left"),
        ("Owner", "left"), ("Tax", "left"),
        ("Opening", "right"), ("Rate", "right"), ("Series", "left"), ("Role", "left"),
    ):
        table.add_column(col, justify=justify, overflow="fold")
    for r in rows:
        ob = (
            "" if r.opening_balance_cents is None
            else f"${r.opening_balance_cents/100:,.2f}"
        )
        rate = (
            "" if r.annual_rate_micros is None
            else f"{r.annual_rate_micros/10_000:.2f}%"
        )
        table.add_row(
            r.name, r.kind, r.subkind, r.owner, r.tax_treatment,
            ob, rate, r.rate_series or "", r.rate_role or "",
        )
    return table
