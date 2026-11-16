"""Household strategy kinds that build on a built-in (#411, ADR-010 §3):
``ctx.rule_budgets``, ``ctx.with_memory``, ``Strategy.delegate``, and the
built-in classes exported from :mod:`analysis.sim.strategies`.

The persona kinds are ``whole_items`` (a subclass of ``Guardrail``) and
``whole_items_delegated`` (the same, delegating to one), in the
``strategy_scenarios`` fixture's ``strategies/whole_items.py``. They own
``alice_spending``'s two optional rules: travel ($1,500 a month, $18,000 a
year) and gifts ($12,000 each December)."""

# cspell:ignore ordr

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from analysis.sim import strategies
from analysis.sim.strategies import Guardrail
from analysis.sim.strategy_api import Context, Strategy, StrategyError, Tag, register
from analysis.sim.strategy_code import builtin_kinds

SCENARIO = "alice_spending"
FLEX = "strategies/flex.csv"
# Cut deep and at once, so the first January above the initial rate drops a rule.
FORCED = {"portfolio": "portfolio:invest", "upper": "0%", "adjust": "50%", "final_years": 0,
          "order": ["gifts", "travel"]}


def _ctx(memory: dict, budgets: dict[str, int], month: date = date(2030, 1, 31)) -> Context:
    return Context(month=month, lots={}, weights=lambda ref: {}, targets=lambda name: {}, memory=memory,
                   budget=lambda: sum(budgets.values()), rule_budgets=lambda: dict(budgets))


def _use(strategy_csv: Callable[[str, str], Path], kind: str, params: dict) -> None:
    """Make ``alice_spending``'s ``flex`` strategy (one row) ``kind`` with ``params``."""
    path = strategy_csv(SCENARIO, FLEX)
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0].update(kind=kind, params_json=json.dumps(params))
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _decisions(run: Path) -> list[tuple[str, str, int, str]]:
    con = sqlite3.connect(run)
    try:
        return con.execute(
            "SELECT s.month, d.hook, d.chosen_cents, d.note FROM strategy_decision d "
            "JOIN simulation_step s ON s.id = d.step_id ORDER BY d.id").fetchall()
    finally:
        con.close()


# ---------- the built-ins are importable ----------

def test_every_builtin_kind_is_exported_from_the_package() -> None:
    exported = {cls.kind for cls in (getattr(strategies, name) for name in strategies.__all__)}
    assert exported == set(builtin_kinds())


def test_a_subclass_must_name_its_own_kind() -> None:
    with pytest.raises(StrategyError, match="kind must be set"):
        @register
        class Inherits(Guardrail):  # noqa: F811 - only its registration is tested
            pass


# ---------- rule_budgets and with_memory ----------

def test_rule_budgets_are_the_budget_rule_by_rule() -> None:
    ctx = _ctx({}, {"travel": 1_800_000, "gifts": 1_200_000})
    assert ctx.rule_budgets() == {"travel": 1_800_000, "gifts": 1_200_000}
    assert ctx.budget() == 3_000_000


def test_a_context_without_rules_has_no_rule_budgets() -> None:
    ctx = Context(month=date(2030, 1, 31), lots={}, weights=lambda ref: {}, targets=lambda name: {}, memory={})
    assert ctx.rule_budgets() == {}


def test_with_memory_keeps_a_delegate_s_state_under_its_key() -> None:
    memory: dict = {"mine": 1}
    sub = _ctx(memory, {"travel": 100}).with_memory("inner")
    sub.memory["factor"] = 0.9
    assert memory == {"mine": 1, "inner": {"factor": 0.9}}
    assert sub.budget() == 100  # everything else is the same context
    assert _ctx(memory, {}).with_memory("inner").memory == {"factor": 0.9}


def test_with_memory_refuses_a_key_holding_something_else() -> None:
    with pytest.raises(StrategyError, match="not a dict"):
        _ctx({"inner": 3}, {}).with_memory("inner")


# ---------- delegate ----------

@dataclass(frozen=True)
class _Params:
    portfolio: Tag
    adjust: int = 50_000
    extra: str = "x"


class _Holder(Strategy):
    kind = "holder"
    Params = _Params


def test_delegate_passes_the_fields_it_shares_and_the_overrides() -> None:
    holder = _Holder("spend", _Params(portfolio="portfolio:invest"))
    inner = holder.delegate(Guardrail, upper=0)
    assert isinstance(inner, Guardrail)
    assert inner.name == "spend"
    assert (inner.params.portfolio, inner.params.adjust, inner.params.upper) == ("portfolio:invest", 50_000, 0)
    assert inner.params.lower == Guardrail.Params(portfolio="p").lower  # its own default


def test_delegate_names_a_field_the_delegate_lacks() -> None:
    holder = _Holder("spend", _Params(portfolio="portfolio:invest"))
    with pytest.raises(StrategyError, match="can't delegate to Guardrail.*nonsense"):
        holder.delegate(Guardrail, nonsense=1)


# ---------- whole rules, by subclassing and by delegation ----------

@pytest.mark.parametrize("kind", ["whole_items", "whole_items_delegated"])
def test_whole_items_skips_what_does_not_fit_after_a_cut(
    kind: str, strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    _use(strategy_csv, kind, FORCED)
    rows = _decisions(run_strategy_scenario(SCENARIO))
    januaries = [note for _, hook, _, note in rows if hook == "year_start"]
    assert januaries[0].startswith("initial withdrawal rate")
    # At half the budget, gifts ($12,000) fit and travel ($18,000) doesn't.
    cut = next(note for note in januaries if "cut 50%" in note)
    assert cut.endswith("; skip travel")
    skipped = [(month, chosen, note) for month, hook, chosen, note in rows if hook == "scale"]
    assert skipped and all(chosen == 0 for _, chosen, _ in skipped)  # a skipped rule posts nothing
    first_year = skipped[0][0][:4]
    assert {note for month, _, note in skipped if month.startswith(first_year)} == {"skip travel this year"}


def test_subclassing_and_delegating_decide_the_same(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path], runs_dir: Path,
) -> None:
    _use(strategy_csv, "whole_items", FORCED)
    first = _decisions(run_strategy_scenario(SCENARIO))
    (runs_dir / f"{SCENARIO}.sqlite3").rename(runs_dir / "subclass.sqlite3")
    _use(strategy_csv, "whole_items_delegated", FORCED)
    assert _decisions(run_strategy_scenario(SCENARIO)) == first


def test_an_unknown_parameter_lists_the_kind_s_in_declared_order() -> None:
    from analysis.sim.strategy_guide import parse_param_specs

    with pytest.raises(ValueError, match=r"has no parameter 'ordr' \(it has portfolio, upper, lower, adjust, "
                                         r"skip_inflation_after_loss, final_years\)"):
        parse_param_specs(Guardrail, ["ordr=[]"])
