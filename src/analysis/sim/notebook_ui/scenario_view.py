"""Scenario intent-data notebook views: manifest, persons, chains, scheduled
transactions, calendar events -- a scenario's whole setup, readable without
running it.

All take a :class:`~analysis.sim.connection.ScenarioConnection` and return
plain, display-ready DataFrames -- these are intent-side facts (unlike
:mod:`analysis.sim.notebook_ui.networth_plots`, which reads run results).
"""

from __future__ import annotations

from collections.abc import Callable

import json

import pandas as pd

from analysis.sim import registries
from analysis.sim.connection import ScenarioConnection


def manifest_view(sc: ScenarioConnection) -> pd.DataFrame:
    """The intent file's identity card, plus the scenario row it was built from."""
    manifest = sc.items("intent_manifest").iloc[0]
    scenario = sc.items("scenario").iloc[0]
    rows = {
        "scenario": manifest["scenario_name"],
        "description": scenario.get("description") or "",
        "start": scenario["start"],
        "end": scenario["end"],
        "schema_version": manifest["schema_version"],
        "code_version": manifest["code_version"][:12],
        "data_version": manifest["data_version"][:12],
        "data_dirty": bool(manifest["data_dirty"]),
        "scenario_tree_id": manifest["scenario_tree_id"][:12],
        "loaded_at": manifest["loaded_at"],
        "loaded_by": manifest["loaded_by"],
        "host": manifest["host"],
    }
    return pd.DataFrame(rows.items(), columns=["field", "value"]).set_index("field")


def persons_view(sc: ScenarioConnection) -> pd.DataFrame:
    """Every person, one row each, with their date of death if the scenario sets one.

    ``date_of_death`` comes from ``person_lifespan.final_month`` (ADR-003
    §4.9) -- the last month a person's rules still fire; blank if the
    scenario has no lifespan override for them.
    """
    persons = sc.items("person")[["id", "display", "birthdate", "role"]].fillna('')
    lifespan = sc.items("person_lifespan").set_index("person")["final_month"]
    persons = persons.set_index("id")
    persons["date_of_death"] = lifespan.reindex(persons.index).fillna('-')
    return persons


def _chain_targets(sc: ScenarioConnection) -> Callable[[object], tuple[str, str]]:
    """A function from a chain name (as stored) to ``(account or chain name, lot)``.

    ``lot`` is ``-`` when the account has just one lot, or the name is a chain or
    missing, so a lot is only spelled out where it tells lots apart; 'just one lot'
    rather than 'default' avoids confusion when one of several is named default.
    """
    by_name = sc.items("chain").set_index("name")
    lot_counts = sc.items("account_model_member").groupby("account").size()

    def resolve(chain_name: object) -> tuple[str, str]:
        if pd.isna(chain_name) or chain_name not in by_name.index:
            return ("-" if pd.isna(chain_name) else str(chain_name)), "-"
        row = by_name.loc[chain_name]
        account = row["account_name"]
        if pd.isna(account):
            return str(chain_name), "-"
        return account, row["lot"] if lot_counts.get(account, 0) > 1 else "-"

    return resolve


def chains_view(sc: ScenarioConnection) -> pd.DataFrame:
    """One row per chain step in draw order; leaf (account) chains listed alone.

    Every lot gets an implicit leaf chain at load (ADR-003 §4.7, ADR-005), so this
    also serves as a map from chain name to the account and lot it resolves to.
    ``draw_from`` is an account or another chain; ``lot`` says which lot of that
    account, and is ``-`` when the account has just one lot (or ``draw_from`` is a
    chain), so a lot is only spelled out where it tells lots apart.
    """
    chains = sc.items("chain")
    steps = sc.items("chain_steps")
    resolve = _chain_targets(sc)

    leaves = chains[~chains["id"].isin(steps["chain_id"])]
    leaf_targets = [resolve(name) for name in leaves["name"]]
    leaf_rows = pd.DataFrame({
        "chain": leaves["name"], "step": pd.NA,
        "draw_from": [account for account, _lot in leaf_targets],
        "lot": [lot for _account, lot in leaf_targets],
        "maximum": pd.NA, "percent": pd.NA,
        "series": pd.NA, "cadence": pd.NA,
    })
    step_targets = [resolve(name) for name in steps["draw_from_chain_name"]]
    step_rows = pd.DataFrame({
        "chain": steps["chain_name"], "step": steps["step_order"],
        "draw_from": [account for account, _lot in step_targets],
        "lot": [lot for _account, lot in step_targets],
        "maximum": steps["maximum_cents"].apply(lambda c: None if pd.isna(c) else c / 100),
        "percent": steps["percent_micros"].apply(
            lambda m: None if pd.isna(m) else m / 10_000
        ),
        "series": steps["series"], "cadence": steps["cadence"],
    })
    return (
        pd.concat([leaf_rows, step_rows], ignore_index=True)
        .sort_values(["chain", "step"], na_position="first")
        .reset_index(drop=True)
    )


_SCHEDULED_COLS = [
    "id", "name", "from_account", "from_lot", "to_account", "to_lot", "cadence",
    "amount_rule_kind", "amount", "tax_character", "priority", "start", "end",
    "description",
]


def scheduled_view(sc: ScenarioConnection) -> pd.DataFrame:
    """Scheduled transactions, human-readable (dollars, account and lot in separate
    columns as in scheduled.csv), by priority.

    ``amount`` is left as a float (NaN where the rule has none, e.g.
    ``percent_of_balance`` growth rules) so numeric formatting still works --
    display it with ``.style.format({"amount": ...}, na_rep="-")`` rather than
    filling it here.
    """
    scheduled = sc.items("scheduled").copy()
    resolve = _chain_targets(sc)
    for column, side in (("from_name", "from"), ("to_name", "to")):
        targets = [resolve(name) for name in scheduled[column]]
        scheduled[f"{side}_account"] = [account for account, _lot in targets]
        scheduled[f"{side}_lot"] = [lot for _account, lot in targets]
    scheduled["amount"] = scheduled["amount_cents"].apply(
        lambda c: None if pd.isna(c) else c / 100
    )
    scheduled[["end", "description"]] = scheduled[["end", "description"]].fillna("-")
    return (
        scheduled[_SCHEDULED_COLS]
        .sort_values(["priority", "name"])
        .reset_index(drop=True)
    )


_CALENDAR_COLS = [
    "date", "kind", "target", "account_1", "lot_1", "account_2", "lot_2",
    "amount", "description",
]


def _calendar_target(kind: str, scheduled_name: str | None, series: str | None,
                     payload_json: str | None) -> str | None:
    """What one calendar row acts on: a ``set_rate``'s series (plus its aux
    kind, when that isn't ``rate``), else its scheduled row."""
    if kind == "set_rate" and isinstance(series, str):
        payload = json.loads(payload_json) if isinstance(payload_json, str) and payload_json else {}
        aux_kind = payload.get("aux_kind", "rate")
        return series if aux_kind == "rate" else f"{series} ({aux_kind})"
    return scheduled_name if isinstance(scheduled_name, str) else None


def calendar_view(sc: ScenarioConnection) -> pd.DataFrame:
    """Calendar events, human-readable, by date.

    ``amount`` is display text in the unit its ``kind`` gives it (issue #24):
    a ``set_rate`` as a percent, a ``set_amount``/``one_off_transfer`` as
    dollars, ``-`` where the event carries none. One column for both,
    rather than separate rate and dollar columns, because a calendar row
    sets exactly one of them.

    ``target`` is what the row acts on, by kind (issue #27): the rate
    series for a ``set_rate`` (with its aux kind when it isn't an ordinary
    rate, e.g. ``cpi (inflation)``), otherwise the scheduled row it changes
    or retires. Again one column, since each kind acts on exactly one.
    """
    events = sc.items("calendar_event").copy()
    events["target"] = [
        _calendar_target(kind, scheduled, series, payload)
        for kind, scheduled, series, payload in zip(
            events["kind"], events["scheduled_name"], events["series"],
            events["payload_json"], strict=True,
        )
    ]
    resolve = _chain_targets(sc)
    for column, lot_column in (("account_1", "lot_1"), ("account_2", "lot_2")):
        targets = [resolve(name) for name in events[column]]
        events[column] = [account for account, _lot in targets]
        events[lot_column] = [lot for _account, lot in targets]
    events["amount"] = [
        registries.format_calendar_amount(kind, amount)
        for kind, amount in zip(events["kind"], events["amount"], strict=True)
    ]
    events = events[_CALENDAR_COLS].sort_values("date").reset_index(drop=True)
    events[_CALENDAR_COLS] = events[_CALENDAR_COLS].fillna("-")
    return events
