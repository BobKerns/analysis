"""Scenario intent-data notebook views: manifest, persons, chains, scheduled
transactions, calendar events -- a scenario's whole setup, readable without
running it.

All take a :class:`~analysis.sim.connection.ScenarioConnection` and return
plain, display-ready DataFrames -- these are intent-side facts (unlike
:mod:`analysis.sim.notebook_ui.networth_plots`, which reads run results).
"""

from __future__ import annotations

import pandas as pd

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


def chains_view(sc: ScenarioConnection) -> pd.DataFrame:
    """One row per chain step in draw order; leaf (account) chains listed alone.

    Every account gets an implicit leaf chain at load (ADR-003 §4.7), so this
    also serves as a map from chain name to the account it resolves to.
    """
    chains = sc.items("chain")
    steps = sc.items("chain_steps")
    leaves = chains[~chains["id"].isin(steps["chain_id"])]

    leaf_rows = pd.DataFrame({
        "chain": leaves["name"], "step": pd.NA, "draw_from": leaves["account_name"],
        "threshold": pd.NA, "amount": pd.NA, "percent": pd.NA,
        "series": pd.NA, "cadence": pd.NA,
    }).fillna('-')
    step_rows = pd.DataFrame({
        "chain": steps["chain_name"], "step": steps["step_order"],
        "draw_from": steps["draw_from_chain_name"],
        "threshold": steps["threshold_cents"] / 100,
        "amount": steps["amount_cents"].apply(lambda c: None if pd.isna(c) else c / 100),
        "percent": steps["percent_micros"].apply(
            lambda m: None if pd.isna(m) else m / 10_000
        ).fillna('-'),
        "series": steps["series"], "cadence": steps["cadence"],
    })
    return (
        pd.concat([leaf_rows, step_rows], ignore_index=True)
        .sort_values(["chain", "step"], na_position="first")
        .reset_index(drop=True)
    )


_SCHEDULED_COLS = [
    "name", "from_name", "to_name", "cadence", "amount_rule_kind",
    "amount", "tax_character", "priority", "start", "end", "description",
]


def scheduled_view(sc: ScenarioConnection) -> pd.DataFrame:
    """Scheduled transactions, human-readable (chain names, dollars), by priority."""
    scheduled = sc.items("scheduled").copy()
    scheduled["amount"] = scheduled["amount_cents"].apply(
        lambda c: None if pd.isna(c) else c / 100
    )
    return (
        scheduled[_SCHEDULED_COLS]
        .sort_values(["priority", "name"])
        .reset_index(drop=True)
    )


_CALENDAR_COLS = [
    "date", "kind", "scheduled_name", "account_1", "account_2", "amount", "description",
]


def calendar_view(sc: ScenarioConnection) -> pd.DataFrame:
    """Calendar events, human-readable (dollars), by date."""
    events = sc.items("calendar_event").copy()
    events["amount"] = events["amount"].apply(lambda c: None if pd.isna(c) else c / 100)
    return events[_CALENDAR_COLS].sort_values("date").reset_index(drop=True).fillna('-')
