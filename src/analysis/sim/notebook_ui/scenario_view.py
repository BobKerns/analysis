"""Scenario intent-data notebook views: manifest, persons, chains, scheduled
transactions, calendar events, year-to-date rows, itemizations and tags -- a
scenario's whole setup, readable without running it.

All take a :class:`~analysis.sim.connection.ScenarioConnection` and return
plain, display-ready DataFrames -- these are intent-side facts (unlike
:mod:`analysis.sim.notebook_ui.networth_plots`, which reads run results).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from analysis.sim import chain_edit, chain_map, registries, setup_listings
from analysis.sim.connection import ScenarioConnection


def manifest_view(sc: ScenarioConnection, **_) -> pd.DataFrame:
    """The intent file's identity card, plus the scenario row it was built
    from, and what ``sim scenario show`` adds (#433): its parent, mix-ins and
    load order, seed, ensemble size and reserve, each marked ``(inherited)``
    when an ancestor sets it, and the run file the run-based notebooks open
    for it (its latest run, else member 0 of its latest ensemble batch)."""
    manifest = sc.items("intent_manifest").iloc[0]
    scenario = sc.items("scenario").iloc[0]
    shown = setup_listings.scenario_show(sc.data_dir, sc.ref.path, None)

    def mark(value: object, inherited: bool) -> str:
        return f"{value} (inherited)" if inherited else str(value)

    run_path = sc.ref.run.latest()
    rows = {
        "scenario": manifest["scenario_name"],
        "description": scenario.get("description") or "",
        "parent": shown.parent or "none (a root scenario)",
        "mix-ins": ", ".join(shown.mixins) or "none",
        "load order": " → ".join(shown.load_order),
        "start": scenario["start"],
        "end": scenario["end"],
        "seed": mark(shown.seed.value, shown.seed.inherited) if shown.seed else "(not set)",
        "ensemble size": (mark(shown.ensemble_size.value, shown.ensemble_size.inherited)
                          if shown.ensemble_size else "0 (not an ensemble)"),
        "reserve": mark(*shown.reserve) if shown.reserve else "(not set)",
        "run file": _run_file_text(run_path, sc.ref.runs_dir),
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


def _run_file_text(run_path: Path | None, runs_dir: Path) -> str:
    """A run file under ``runs_dir`` relative to it, with when it was written;
    ``none`` when the scenario has no run yet."""
    if run_path is None:
        return "none (not run yet)"
    try:
        shown = run_path.relative_to(runs_dir)
    except ValueError:
        shown = run_path
    when = datetime.fromtimestamp(run_path.stat().st_mtime)
    return f"{shown} ({when:%Y-%m-%d %H:%M})"


def persons_view(sc: ScenarioConnection, **_) -> pd.DataFrame:
    """Every person, one row each, with their lifespan if the scenario sets one.

    ``age_at_death`` and ``date_of_death`` come from ``person_lifespan``
    (ADR-003 §4.9): the authored age, or a member's drawn one (#221), and the
    final month it reaches -- the last month a person's rules still fire;
    ``-`` if the scenario has no lifespan for them. ``varies`` is how an
    unresolved recipe's age varies across members.
    """
    items = sc.items("person")
    columns = ["id", "display", "birthdate", *(["sex"] if "sex" in items.columns else []), "role"]
    persons = items[columns].fillna('')
    lifespans = sc.items("person_lifespan").set_index("person")
    persons = persons.set_index("id")
    persons["age_at_death"] = lifespans["age_at_death"].reindex(persons.index).map(
        lambda age: '-' if pd.isna(age) else f"{age:g}"
    )
    persons["date_of_death"] = lifespans["final_month"].reindex(persons.index).fillna('-')
    if "vary_distribution" in lifespans.columns:
        persons["varies"] = [
            _lifespan_varies(lifespans.loc[p]) if p in lifespans.index else ''
            for p in persons.index
        ]
    return persons


def _lifespan_varies(row: pd.Series) -> str:
    """``triangular 75..95, mode 84``, ``life_table, set-forward +6.4``, or
    '' for a fixed age."""
    if pd.isna(row["vary_distribution"]):
        return ''
    if row["vary_distribution"] == "life_table":
        # Drawn around age_at_death as its median (#222): no bounds.
        set_forward = row.get("life_table_set_forward")
        return "life_table" + ('' if pd.isna(set_forward) else f", set-forward {set_forward:+.1f}")
    text = f"{row['vary_distribution']} {row['vary_low']:g}..{row['vary_high']:g}"
    if not pd.isna(row["vary_mode"]):
        text += f", mode {row['vary_mode']:g}"
    return text


def _chain_targets(sc: ScenarioConnection) -> Callable[[object], tuple[str, str]]:
    """A function from a chain name (as stored) to ``(account or chain name, lot)``.

    ``lot`` is ``-`` when the account has just one lot, or the name is a chain or
    missing, so a lot is only spelled out where it tells lots apart; 'just one lot'
    rather than 'default' avoids confusion when one of several is named default.
    """
    by_name = sc.items("chain").set_index("name")
    lot_counts = sc.items("account_lot").groupby("account").size()

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


UNUSED = "nothing"
"""The ``used_by`` of a chain that no rule, lot or other chain uses."""


def chain_table(sc: ScenarioConnection) -> pd.DataFrame:
    """The named chains, one row per step, with each chain's ``role`` and
    what it is ``used_by`` (one use per line) on its first step only, so the
    rows of one chain read as a block; the later steps leave ``chain``,
    ``role`` and ``used_by`` empty. ``used_by`` is :data:`UNUSED` for a chain
    nothing uses. The same facts ``sim chain list`` shows, with the steps'
    numbers kept in columns of their own (issue #141). ``defined_in``, also
    on the first step only, is the scenario whose chain file is loaded, or
    ``overrides <ancestor>`` (#433)."""
    cmap = chain_map.chain_map(sc)
    steps = chains_view(sc)
    steps = steps[steps["step"].notna()].reset_index(drop=True)
    # threshold=0 means "no minimum" -- not meaningfully different from n/a.
    steps["maximum"] = steps["maximum"].mask(steps["maximum"] == 0)
    first = ~steps["chain"].duplicated()
    steps.insert(1, "role", steps["chain"].map(cmap.roles))
    steps["used_by"] = steps["chain"].map(
        lambda name: "\n".join(chain_map.use_text(cmap, u) for u in cmap.uses.get(name, ())) or UNUSED
    )
    steps["defined_in"] = steps["chain"].map(lambda name: _chain_defined_in(sc, name))
    for column in ("chain", "role", "used_by", "defined_in"):
        steps[column] = steps[column].where(first, "")
    return steps


def _chain_defined_in(sc: ScenarioConnection, name: str) -> str:
    try:
        return setup_listings.chain_defined_in(sc.ref.path, name)
    except chain_edit.AmbiguousChain as exc:
        return str(exc)


_SCHEDULED_COLS = [
    "id", "name", "from_account", "from_lot", "to_account", "to_lot", "cadence",
    "amount_rule_kind", "amount", "tax_character", "priority", "start", "end",
    "description", "defined_in",
]


def scheduled_view(sc: ScenarioConnection) -> pd.DataFrame:
    """Scheduled transactions, human-readable (dollars, account and lot in separate
    columns as in scheduled.csv), by priority.

    ``amount`` is left as a float (NaN where the rule has none, e.g.
    ``percent_of_balance`` growth rules) so numeric formatting still works --
    display it with ``.style.format({"amount": ...}, na_rep="-")`` rather than
    filling it here.

    ``defined_in`` says where each rule comes from (#433): ``this scenario``,
    the ancestor it is inherited from, ``overrides <ancestor>``, or
    ``built in`` for a lot's growth rule, which the loader adds.
    """
    scheduled = sc.items("scheduled").copy()
    origins = setup_listings.row_origins(sc.ref.path, "scheduled.csv")
    scheduled["defined_in"] = scheduled["name"].map(lambda name: origins.get(name, setup_listings.BUILT_IN))
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
    "amount", "description", "defined_in",
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

    ``defined_in`` says where each row comes from, as ``sim calendar list``
    shows it (#433); ``-`` throughout when the folders no longer match the
    loaded intent (edited since the load), rather than a wrong guess.
    """
    events = sc.items("calendar_event").sort_values("id").copy()
    events["defined_in"] = _calendar_defined_in(sc, events)
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


def _calendar_defined_in(sc: ScenarioConnection, events: pd.DataFrame) -> list[str]:
    """Each intent calendar row's ``defined_in``, lined up with the folders'
    merged rows by position: the loader writes them in its merge order, which
    the overlay follows. Checked by kind and name, so a scenario edited since
    its load shows ``-`` instead of another row's origin."""
    origins = setup_listings.calendar_origins(sc.ref.path)
    names = ["" if pd.isna(name) else str(name) for name in events["name"]]
    if [(kind, name) for kind, name, _ in origins] != list(zip(events["kind"], names, strict=True)):
        return ["-"] * len(events)
    return [where for _kind, _name, where in origins]


# ---------- year to date, itemizations, tags (#435) ----------
#
# Read with the same functions ``sim ytd list``, ``sim itemization list|show``
# and ``sim tags`` use, so the notebook and the terminal show the same rows.

YTD_COLUMNS = ["name", "source", "from", "to", "amount", "tax_character", "year",
               "description", "defined_in"]


def ytd_view(sc: ScenarioConnection) -> pd.DataFrame:
    """``sim ytd list`` as a table: year_to_date.csv's rows, then the
    scheduled rows carrying a ``ytd_amount`` or ``ytd_preroll``, merged down
    the load order with where each is defined. ``amount`` is as written:
    dollars, an ``@itemization``, or ``preroll``."""
    rows = setup_listings.ytd_listing(sc.ref.path)
    return pd.DataFrame(
        [[r.name, r.source, r.from_account, r.to_account, r.amount, r.tax_character,
          r.year, r.description, r.defined_in] for r in rows],
        columns=YTD_COLUMNS,
    )


def itemizations_view(sc: ScenarioConnection) -> pd.DataFrame:
    """``sim itemization list`` as a table: every itemization the scenario can
    reference, with its row count, its total in dollars (NaN when it can't be
    computed; ``error`` says why), how many ``@name`` cells reference it, and
    where it is defined."""
    items = setup_listings.itemizations(sc.data_dir, sc.ref.path)
    references = setup_listings.itemization_references(sc.data_dir, sc.ref.path)
    return pd.DataFrame(
        [[i.name, len(i.rows), None if i.total_cents is None else i.total_cents / 100,
          len(references.get(i.name, [])), i.defined_in, i.error] for i in items],
        columns=["name", "rows", "total", "refs", "defined_in", "error"],
    )


@dataclass(frozen=True)
class ItemizationDetail:
    """``sim itemization show NAME``'s facts: the file's rows, its total, and
    the cells referencing it."""
    name: str
    path: Path
    defined_in: str
    rows: pd.DataFrame
    total: float | None
    """Dollars, or None when it can't be computed (``error`` says why)."""
    error: str
    references: pd.DataFrame
    """``file`` (relative to the data repo), ``line`` and ``column`` of each
    ``@name`` cell referencing it."""


def itemization_detail(sc: ScenarioConnection, name: str) -> ItemizationDetail:
    """One itemization's rows, total and references. ``name`` may start with
    ``@``; an unknown name raises KeyError."""
    name = name.removeprefix("@")
    item = next((i for i in setup_listings.itemizations(sc.data_dir, sc.ref.path)
                 if i.name == name), None)
    if item is None:
        raise KeyError(f"no itemization {name!r} for {sc.ref.name}")
    found = setup_listings.itemization_references(sc.data_dir, sc.ref.path).get(name, [])

    def shown(path: Path) -> str:
        try:
            return str(path.relative_to(sc.data_dir))
        except ValueError:
            return str(path)

    return ItemizationDetail(
        name=item.name, path=item.path, defined_in=item.defined_in,
        rows=pd.DataFrame(item.rows, columns=list(item.rows[0]) if item.rows else ["Value"]),
        total=None if item.total_cents is None else item.total_cents / 100,
        error=item.error,
        references=pd.DataFrame(
            [[shown(r.path), r.line, r.column] for r in found],
            columns=["file", "line", "column"],
        ),
    )


def _calendar_label(row: pd.Series) -> str:
    """A calendar row as ``sim tags items`` names it (its date), with its
    name, or else its kind, so two rows on one date can be told apart."""
    label = row["name"] if isinstance(row.get("name"), str) and row["name"] else row["kind"]
    return f"{str(row['date'])[:7]} {label}"


def tags_view(sc: ScenarioConnection) -> pd.DataFrame:
    """``sim tags`` and ``sim tags items`` in one table: every tag, by role
    and name, with the accounts, scheduled rules and calendar rows carrying
    it, one per line in each column."""
    tags = sc.list_tags()
    carriers: dict[str, dict[str, list[str]]] = {}
    for item_type, column, label in (
        ("account", "accounts", lambda row: str(row["name"])),
        ("scheduled", "scheduled", lambda row: str(row["name"])),
        ("calendar_event", "calendar", _calendar_label),
    ):
        items = sc.items(item_type)
        for _, row in items.iterrows():
            for token in str(row["tags"]).split():
                carriers.setdefault(token, {}).setdefault(column, []).append(label(row))
    tokens = [f"{role}:{name}" for role, name in zip(tags["role"], tags["name"], strict=True)]
    return pd.DataFrame({
        "tag": tokens,
        **{column: ["\n".join(carriers.get(token, {}).get(column, [])) for token in tokens]
           for column in ("accounts", "scheduled", "calendar")},
    })
