"""Accounts: tabular view across accounts.csv, account_models/, scheduled.csv, aux/.

Pure helpers used by the CLI (``analysis sim accounts``) and notebooks.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path


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


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [
            {k: (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(fh)
        ]


def _resolve_scenario_dir(data_dir: Path, scenario: str | None) -> Path | None:
    sdir = data_dir / "scenarios"
    if not sdir.is_dir():
        return None
    if scenario:
        d = sdir / scenario
        return d if d.is_dir() else None
    subs = sorted(p for p in sdir.iterdir() if p.is_dir())
    return subs[0] if len(subs) == 1 else None


def _account_model_name(scenario_dir: Path) -> str | None:
    rows = _read_csv(scenario_dir / "scenario.csv")
    return rows[0].get("account_model") if rows else None


def _dollars_to_cents(s: str) -> int | None:
    if not s:
        return None
    try:
        from decimal import Decimal
        cents = Decimal(s) * 100
        return int(cents)
    except Exception:
        return None


def _latest_rate_micros(
    aux_dir: Path, series: str, as_of: str | None = None
) -> int | None:
    """Resolve the most recent rate row in ``aux/rate__<series>.csv`` whose
    effective_date is ``<= as_of`` (default: today)."""
    stem = "rate__" + series.replace(" ", "_")
    path = aux_dir / f"{stem}.csv"
    rows = _read_csv(path)
    if not rows:
        return None
    cutoff = as_of or date.today().isoformat()
    eligible = [r for r in rows if r.get("effective_date", "") <= cutoff]
    chosen = max(eligible, key=lambda r: r["effective_date"], default=None)
    if chosen is None:
        chosen = min(rows, key=lambda r: r["effective_date"])
    raw = chosen.get("annual_rate_micros", "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _rate_role(subkind: str) -> str:
    cash = {"checking", "savings"}
    return "yield" if subkind in cash else "growth"


def load_accounts_view(
    data_dir: Path | str,
    scenario: str | None = None,
    *,
    as_of: str | None = None,
) -> list[AccountRow]:
    """Join accounts.csv with the scenario's account model and rate rules."""
    data_dir = Path(data_dir)
    accounts = _read_csv(data_dir / "accounts.csv")

    opening: dict[str, int | None] = {}
    scheduled: list[dict[str, str]] = []
    aux_dir = data_dir / "aux"
    scenario_dir = _resolve_scenario_dir(data_dir, scenario)
    if scenario_dir is not None:
        model_name = _account_model_name(scenario_dir)
        if model_name:
            for m in _read_csv(data_dir / "account_models" / f"{model_name}.csv"):
                opening[m["account_name"]] = _dollars_to_cents(
                    m.get("opening_balance", "")
                )
        scheduled = _read_csv(scenario_dir / "scheduled.csv")

    rate_by_account: dict[str, tuple[str, int | None]] = {}
    for s in scheduled:
        if s.get("amount_rule_kind") != "percent_of_balance":
            continue
        acct = s.get("account_1") or ""
        series = s.get("series") or ""
        if not acct or not series:
            continue
        rate_by_account[acct] = (series, _latest_rate_micros(aux_dir, series, as_of))

    out: list[AccountRow] = []
    for a in accounts:
        name = a.get("name", "")
        if not name:
            continue
        series, micros = rate_by_account.get(name, (None, None))
        out.append(
            AccountRow(
                name=name,
                kind=a.get("kind", ""),
                subkind=a.get("subkind", ""),
                owner=a.get("owner", ""),
                tax_treatment=a.get("tax_treatment", ""),
                opened=a.get("opened", ""),
                closed=a.get("closed", ""),
                opening_balance_cents=opening.get(name),
                rate_series=series,
                annual_rate_micros=micros,
                rate_role=_rate_role(a.get("subkind", "")) if series else None,
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
