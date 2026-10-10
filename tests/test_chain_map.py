"""Reading a scenario's chains: `sim chain list|show` and the flowchart (issue #129)."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from click.testing import CliRunner
from ipywidgets import widgets
from rich.console import Console

from analysis.cli import main
from analysis.sim import chain_map
from analysis.sim.connection import ScenarioConnection, open_scenario
from analysis.sim.notebook_ui.scenario_plots import chain_flow_markdown, show_chain_flow

@pytest.fixture
def cmap(guide_chains: Path) -> Generator[chain_map.ChainMap]:
    with open_scenario(guide_chains, "retire_alice") as sc:
        yield chain_map.chain_map(sc)


def _sim(data_dir: Path, *args: str):
    return CliRunner().invoke(
        main, ["sim", "chain", *args, "--scenario", "retire_alice", "--data-dir", str(data_dir)],
        terminal_width=200,
    )


def test_steps_policies_and_uses(cmap: chain_map.ChainMap) -> None:
    assert [(s.order, s.target) for s in cmap.steps["retirement_draw"]] == [
        (1, "bridge"), (2, "Asset_TraditionalIRA_Alice:default")]
    assert cmap.kinds["bridge"] == "asset"
    assert cmap.label("Asset_TraditionalIRA_Alice:default") == "Asset_TraditionalIRA_Alice (default)"
    assert cmap.label("Asset_Savings") == "Asset_Savings"  # one lot: the lot says nothing
    checking = cmap.policies["Asset_Checking"]
    assert (checking.minimum_cents, checking.maximum_cents) == (200_000, 3_000_000)
    assert (checking.refill_chain, checking.overflow_chain) == ("asset_checking_liquidity", "Asset_Savings")
    assert checking.refill_target_cents == 300_000
    uses = {name: [(u.kind, u.by, u.detail) for u in found] for name, found in cmap.uses.items()}
    assert uses["retirement_draw"] == [("draw", "rule monthly_draw", "Asset_Checking")]
    assert uses["ss_split"] == [("sweep", "rule ss_alice", "Income_SS_Alice")]
    assert uses["bridge"] == [("step", "retirement_draw", "1")]
    assert ("refill", "Asset_Checking", "") in uses["asset_checking_liquidity"]
    assert ("overflow", "Asset_Checking", "") in uses["Asset_Savings"]
    assert cmap.descendants("retirement_draw") == [
        "retirement_draw", "bridge", "Asset_Savings", "Asset_Brokerage_Joint:default",
        "Asset_TraditionalIRA_Alice:default"]


@pytest.mark.parametrize(("maximum", "percent", "series", "expected"), [
    (None, 1_000_000, None, "all that's left"),
    (None, 500_000, None, "50% of what's left"),
    (30_000, 1_000_000, None, "all that's left, up to $300"),
    (30_050, 333_333, None, "33.3333% of what's left, up to $300.50"),
    (1_000_000, 1_000_000, "cpi", "all that's left, up to $10,000 growing with cpi (monthly)"),
])
def test_share_text(maximum: int | None, percent: int, series: str | None, expected: str) -> None:
    step = chain_map.Step(1, "x", maximum, percent, series, None)
    assert chain_map.share_text(step) == expected


def test_tree_nests_a_chain_inside_a_chain(cmap: chain_map.ChainMap) -> None:
    console = Console(width=200, record=True)
    console.print(chain_map.chain_tree(cmap, "retirement_draw"))
    lines = console.export_text().splitlines()
    assert lines[0] == "retirement_draw (asset chain)"
    assert lines[1].startswith("├── 1. bridge (chain)  50% of what's left")
    assert lines[2].startswith("│   ├── 1. Asset_Savings  all that's left, up to $10,000")
    assert lines[4].startswith("└── 2. Asset_TraditionalIRA_Alice (default)  all that's left")


def test_tree_shows_each_lots_limits(cmap: chain_map.ChainMap) -> None:
    console = Console(width=200, record=True)
    console.print(chain_map.chain_tree(cmap, "ss_split"))
    assert ("keeps $2,000; refills from asset_checking_liquidity up to $3,000; "
            "holds at most $30,000, extra to Asset_Savings") in console.export_text()


def test_mermaid_draws_steps_policies_and_rules(cmap: chain_map.ChainMap) -> None:
    chart = chain_map.mermaid(cmap)
    assert chart.startswith("flowchart LR\n")
    ids = {line.split("[")[0].split("(")[0].strip(): line for line in chart.splitlines()[1:]
           if "[" in line and "--" not in line}
    node = {label: nid for nid, line in ids.items() for label in [line.split('"')[1].split("<br/>")[0]]}
    assert f'{node["retirement_draw"]} ---|"1: 50% of what\'s left"| {node["bridge"]}' in chart
    assert f'{node["asset_checking_liquidity"]} -.->|"refill, before a debit"| {node["Asset_Checking"]}' in chart
    assert f'{node["Asset_Checking"]} ==>|"overflow, month end"| {node["Asset_Savings"]}' in chart
    assert f'{node["retirement_draw"]} -->|"rule monthly_draw"| {node["Asset_Checking"]}' in chart
    assert f'{node["Income_SS_Alice"]} -->|"rule ss_alice"| {node["ss_split"]}' in chart
    assert len(node) == len(set(node.values()))  # one node per chain or lot, however often used


def test_list_command(guide_chains: Path) -> None:
    result = _sim(guide_chains, "list")
    assert result.exit_code == 0, result.output
    assert "retirement_draw" in result.output
    assert "drawn from by rule monthly_draw (to Asset_Checking)" in result.output
    assert "this scenario" in result.output


def test_show_command_finds_the_chains_reaching_an_account(guide_chains: Path) -> None:
    result = _sim(guide_chains, "show", "Asset_Brokerage_Joint")
    assert result.exit_code == 0, result.output
    assert "bridge (asset chain)" in result.output and "retirement_draw (asset chain)" in result.output
    assert "ss_split" not in result.output
    refill = _sim(guide_chains, "show", "Asset_Checking")  # through its own refill and overflow
    assert "asset_checking_liquidity (asset chain)" in refill.output
    mermaid = _sim(guide_chains, "show", "ss_split", "--mermaid")
    assert mermaid.output.startswith("flowchart LR")
    missing = _sim(guide_chains, "show", "nowhere")
    assert missing.exit_code != 0 and "neither a chain nor an account" in missing.output


def test_flowchart_in_the_notebook(test_retire_alice_scenario: ScenarioConnection) -> None:
    assert isinstance(show_chain_flow(test_retire_alice_scenario), widgets.Output)
    markdown = chain_flow_markdown(test_retire_alice_scenario)
    assert markdown.startswith("```mermaid\nflowchart LR\n") and markdown.endswith("\n```")
