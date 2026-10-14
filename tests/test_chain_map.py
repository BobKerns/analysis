"""Reading a scenario's chains: `sim chain list|show` and the flowchart (issue #129)."""

from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from click.testing import CliRunner
from ipywidgets import widgets
from rich.console import Console

from analysis.cli import main
from analysis.sim import chain_map
from analysis.sim.connection import open_scenario
from analysis.sim.notebook_ui.scenario_plots import MERMAID_URL, MermaidWidget, show_chain_flow


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
    assert cmap.roles["bridge"] == "source" and cmap.roles["ss_split"] == "destination"
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
    assert lines[0] == "retirement_draw (source chain)"
    assert lines[1].startswith("├── 1. bridge (chain)  50% of what's left")
    assert lines[2].startswith("│   ├── 1. Asset_Savings  all that's left, up to $10,000")
    assert lines[4].startswith("└── 2. Asset_TraditionalIRA_Alice (default)  all that's left")


def test_tree_shows_each_lots_limits(cmap: chain_map.ChainMap) -> None:
    console = Console(width=200, record=True)
    console.print(chain_map.chain_tree(cmap, "ss_split"))
    assert ("keeps $2,000; refills from asset_checking_liquidity up to $3,000; "
            "holds at most $30,000, extra to Asset_Savings") in console.export_text()


def _nodes(chart: str, prefix: str = "n") -> dict[str, str]:
    """A chart's node ids by label, a lot in its account's box as
    ``account (lot)``; with ``prefix="c"``, its copies' ids (a copy closes a loop)."""
    node: dict[str, str] = {}
    box = None
    for line in chart.splitlines()[1:]:
        if line.strip() == "end":
            box = None
        elif line.lstrip().startswith("subgraph "):
            box = line.split('"')[1]
        elif "[" in line and "--" not in line and (nid := line.split("[")[0].split("(")[0].strip()).startswith(prefix):
            label = line.split('"')[1].split("<br/>")[0]
            node[label if box is None else f"{box} ({label})"] = nid
    assert len(node) == len(set(node.values()))  # one node per chain or lot, however often used
    return node


def _boxes(chart: str) -> dict[str, list[str]]:
    """Each account box's title, by box id, and the labels of the lots in it."""
    boxes: dict[str, list[str]] = {}
    box = None
    for line in chart.splitlines():
        if line.strip() == "end":
            box = None
        elif line.lstrip().startswith("subgraph "):
            box = line.split()[1].split("[")[0]
            boxes[box] = [line.split('"')[1]]
        elif box is not None:
            boxes[box].append(line.split('"')[1].split("<br/>")[0])
    return boxes


def test_mermaid_arrows_follow_the_money(cmap: chain_map.ChainMap) -> None:
    """Issue #143: every arrow points the way money moves, so a source
    chain's steps point into it and a destination chain's out of it."""
    chart = chain_map.mermaid(cmap, rules=True)
    assert chart.startswith("flowchart LR\n")
    node, copy = _nodes(chart), _nodes(chart, "c")
    assert f'{node["bridge"]} -->|"1: 50% of what\'s left"| {node["retirement_draw"]}' in chart
    # Checking overflows to savings, which refills checking: a loop, closed by a copy.
    assert f'{node["retirement_draw"]} -->|"rule monthly_draw"| {copy["Asset_Checking"]}' in chart
    assert f'{node["asset_checking_liquidity"]} -.->|"refill, before a debit"| {copy["Asset_Checking"]}' in chart
    assert f'{node["Income_SS_Alice"]} -->|"rule ss_alice"| {node["ss_split"]}' in chart
    assert f'{node["ss_split"]} -->|"1: 50% of what\'s left"| {node["Asset_Checking"]}' in chart
    assert f'{node["Asset_Checking"]} -.->|"overflow, month end"| {node["Asset_Savings"]}' in chart


def _has_loop(chart: str) -> bool:
    after: dict[str, set[str]] = {}
    for line in chart.splitlines():
        if "--" in line:
            parts = line.split()
            after.setdefault(parts[0], set()).add(parts[-1])
    done: set[str] = set()

    def loops(node: str, path: set[str]) -> bool:
        if node in path:
            return True
        if node in done:
            return False
        done.add(node)
        return any(loops(nxt, path | {node}) for nxt in after.get(node, ()))
    return any(loops(node, set()) for node in list(after))


def test_a_loop_is_closed_by_a_copy_so_the_chart_reads_left_to_right(
    guide_chains: Path, append_calendar_row,
) -> None:
    """Bob, #143: Mermaid turns an edge of a loop around, so the chart has
    none. Here a transfer pays back into savings, one of the draw's own lots."""
    append_calendar_row(date="2030-01", kind="one_off_transfer", name="top_up",
                        account_1="retirement_draw", account_2="Asset_Savings", amount="5000")
    with open_scenario(guide_chains, "retire_alice") as sc:
        cmap = chain_map.chain_map(sc)
    for names in (None, ["ss_split"], ["asset_checking_liquidity"], ["retirement_draw"], ["bridge"]):
        assert not _has_loop(chain_map.mermaid(cmap, names, rules=True)), names
    chart = chain_map.mermaid(cmap, ["retirement_draw"], rules=True)
    node, copy = _nodes(chart), _nodes(chart, "c")
    assert f'{node["Asset_Savings"]} -->|"1: all that\'s left, up to $10,000' in chart
    assert f'(annual)"| {node["bridge"]}' in chart
    assert f'{node["retirement_draw"]} -->|"calendar top_up (2030-01-31)"| {copy["Asset_Savings"]}' in chart


def test_rules_are_left_out_unless_asked_for(cmap: chain_map.ChainMap) -> None:
    """Bob, #143: rules, many of them firing once, crowd out the chains."""
    chart = chain_map.mermaid(cmap)
    assert "rule " not in chart and "Income_SS_Alice" not in chart
    assert f'{_nodes(chart)["bridge"]} -->|"1: 50% of what\'s left"|' in chart
    assert '"rule monthly_draw"' in chain_map.mermaid(cmap, rules=True)
    # A parent chain shown alongside brings its rules, but goes no further.
    assert '"rule monthly_draw"' in chain_map.mermaid(cmap, ["bridge"], rules=True)


def test_a_chain_shown_alone_shows_the_chains_it_is_a_step_of(cmap: chain_map.ChainMap) -> None:
    """Bob, #143: money drawn from bridge goes on to retirement_draw."""
    chart = chain_map.mermaid(cmap, ["bridge"])
    node = _nodes(chart)
    assert f'{node["bridge"]} -->|"1: 50% of what\'s left"| {node["retirement_draw"]}' in chart


def test_the_chains_asked_for_are_highlighted(cmap: chain_map.ChainMap) -> None:
    """Bob, #143: the picked chain is hard to find in a tangled chart."""
    chart = chain_map.mermaid(cmap, ["ss_split"])
    assert chart.endswith(f"    class {_nodes(chart)['ss_split']} picked")
    assert "classDef picked fill:" in chart
    assert "picked" not in chain_map.mermaid(cmap)  # every chain: nothing to single out


def test_a_destination_chain_follows_overflow_not_refill(cmap: chain_map.ChainMap) -> None:
    """Money paid into ss_split may overflow on from checking, but never
    passes through checking's refill chain."""
    chart = chain_map.mermaid(cmap, ["ss_split"])
    node = _nodes(chart)
    assert f'{node["Asset_Checking"]} -.->|"overflow, month end"| {node["Asset_Savings"]}' in chart
    assert "asset_checking_liquidity" not in node and "refill, before a debit" not in chart


def test_a_source_chain_follows_refill_not_overflow(
    guide_chains: Path, update_account_value,
) -> None:
    """Money drawn from bridge may first refill brokerage from checking's
    chain, but never passes through brokerage's overflow."""
    update_account_value("Asset_Brokerage_Joint", "default", minimum_balance="1000.00",
                         refill_chain="asset_checking_liquidity", maximum_balance="900000.00",
                         overflow_chain="Asset_Checking")
    with open_scenario(guide_chains, "retire_alice") as sc:
        chart = chain_map.mermaid(chain_map.chain_map(sc), ["bridge"])
    node = _nodes(chart)
    assert f'{node["Asset_Brokerage_Joint (default)"]} -->|"2: all that\'s left"| {node["bridge"]}' in chart
    liquidity = node["asset_checking_liquidity"]
    assert f'{liquidity} -.->|"refill, before a debit"| {node["Asset_Brokerage_Joint (default)"]}' in chart
    assert f'{node["Asset_Savings"]} -->|"1: all that\'s left"| {liquidity}' in chart
    assert "overflow, month end" not in chart


def test_an_accounts_lots_share_a_box_named_for_it(
    guide_chains: Path, set_brokerage_lots, retire_alice_csv: Callable[[str], Path],
) -> None:
    """Bob, #144: an account whose lots are named is a box titled with the
    account, holding a box per lot titled with the lot alone. A one-lot
    account stays a box of its own."""
    set_brokerage_lots([{"lot": "default", "opening_balance": "1000.00"},
                        {"lot": "growth", "opening_balance": "2000.00"}])
    bridge = retire_alice_csv("chains/source/bridge.csv")
    bridge.write_text(bridge.read_text() + "Asset_Brokerage_Joint,growth,,,,\n")
    with open_scenario(guide_chains, "retire_alice") as sc:
        chart = chain_map.mermaid(chain_map.chain_map(sc), ["retirement_draw"])
    boxes = sorted(_boxes(chart).values())
    assert boxes == [["Asset_Brokerage_Joint", "default", "growth"], ["Asset_TraditionalIRA_Alice", "default"]]
    node = _nodes(chart)
    assert f'{node["Asset_Brokerage_Joint (growth)"]} -->|"3: all that\'s left"| {node["bridge"]}' in chart
    assert f'{node["Asset_Savings"]}["Asset_Savings"]' in chart  # one lot: no box, named for the account


def test_a_copy_of_a_lot_gets_a_box_of_its_own() -> None:
    """Bob, #144: a copy in its original's box would be drawn back beside it,
    the closing edge turned around."""
    cmap = chain_map.ChainMap(
        roles={"draw": "source"},
        steps={"draw": [chain_map.Step(1, "lot", None, 1_000_000, None, None)]},
        leaves={"lot": ("Acct", "a")},
        uses={"draw": [chain_map.Use("draw", "rule back", "lot")]},
        multi_lot=frozenset({"Acct"}),
    )
    chart = chain_map.mermaid(cmap, rules=True)
    node, copy = _nodes(chart), _nodes(chart, "c")
    assert f'{node["Acct (a)"]} -->|"1: all that\'s left"| {node["draw"]}' in chart
    assert f'{node["draw"]} -->|"rule back"| {copy["Acct (a)"]}' in chart
    assert _boxes(chart) == {"a0": ["Acct", "a"], "ac0": ["Acct", "a"]}
    assert not _has_loop(chart)


def _cut(chart: str) -> set[str]:
    """The ids of the nodes a chart stops at short of what they lead to."""
    lines = [line.split()[1] for line in chart.splitlines() if line.strip().startswith("class ") and
             line.endswith(" more")]
    return set(lines[0].split(",")) if lines else set()


def test_depth_one_draws_one_step_out(cmap: chain_map.ChainMap) -> None:
    """Issue #149: bridge is a step of retirement_draw, drawn but not opened."""
    chart = chain_map.mermaid(cmap, ["retirement_draw"], depth="1")
    node = _nodes(chart)
    assert set(node) == {"retirement_draw", "bridge", "Asset_TraditionalIRA_Alice (default)"}
    assert _cut(chart) == {node["bridge"]}
    assert "classDef more stroke-dasharray" in chart
    # retirement_draw's rules are two steps from bridge, so depth 1 leaves them out.
    assert "monthly_draw" not in chain_map.mermaid(cmap, ["bridge"], rules=True, depth="1")
    assert "monthly_draw" in chain_map.mermaid(cmap, ["retirement_draw"], rules=True, depth="1")


def test_depth_lots_stops_at_the_first_lot(cmap: chain_map.ChainMap) -> None:
    """Issue #149: through bridge to its lots, but not on to checking's overflow."""
    draw = chain_map.mermaid(cmap, ["retirement_draw"], depth="lots")
    assert "Asset_Savings" in _nodes(draw) and "Asset_Brokerage_Joint (default)" in _nodes(draw)
    assert not _cut(draw)  # none of those lots refills
    chart = chain_map.mermaid(cmap, ["ss_split"], depth="lots")
    node = _nodes(chart)
    assert "overflow, month end" not in chart
    assert _cut(chart) == {node["Asset_Checking"]}
    assert "overflow, month end" in chain_map.mermaid(cmap, ["ss_split"])  # depth all, as before
    assert "more" not in chain_map.mermaid(cmap, ["ss_split"])


def test_a_lot_drawn_from_its_own_chart_goes_one_step(cmap: chain_map.ChainMap) -> None:
    """A lot asked for brings its own refill and overflow at any depth."""
    chart = chain_map.mermaid(cmap, ["Asset_Checking"], depth="1")
    node = _nodes(chart)
    assert f'{node["asset_checking_liquidity"]} -.->|"refill, before a debit"| {node["Asset_Checking"]}' in chart
    assert f'{node["Asset_Checking"]} -.->|"overflow, month end"| {node["Asset_Savings"]}' in chart
    assert node["asset_checking_liquidity"] in _cut(chart)
    assert not _cut(chain_map.mermaid(cmap, ["Asset_Checking"], depth="lots")) & {node["asset_checking_liquidity"]}


def test_list_command(guide_chains: Path) -> None:
    result = _sim(guide_chains, "list")
    assert result.exit_code == 0, result.output
    rows = {line.split("│")[1].strip(): line for line in result.output.splitlines() if line.startswith("│ ")}
    assert "│ source " in rows["retirement_draw"] and "│ destination " in rows["ss_split"]
    assert "drawn from by rule monthly_draw (to" in rows["retirement_draw"]
    assert "this scenario" in result.output


def test_show_command_finds_the_chains_reaching_an_account(guide_chains: Path) -> None:
    result = _sim(guide_chains, "show", "Asset_Brokerage_Joint")
    assert result.exit_code == 0, result.output
    assert "bridge (source chain)" in result.output and "retirement_draw (source chain)" in result.output
    assert "ss_split" not in result.output
    refill = _sim(guide_chains, "show", "Asset_Checking")  # through its own refill and overflow
    assert "asset_checking_liquidity (source chain)" in refill.output
    mermaid = _sim(guide_chains, "show", "ss_split", "--mermaid")
    assert mermaid.output.startswith("flowchart LR") and "rule ss_alice" not in mermaid.output
    assert "rule ss_alice" in _sim(guide_chains, "show", "ss_split", "--mermaid", "--rules").output
    shallow = _sim(guide_chains, "show", "retirement_draw", "--mermaid", "--depth", "1")
    assert shallow.exit_code == 0, shallow.output
    assert "Asset_Savings" not in shallow.output and "Asset_Savings" in _sim(
        guide_chains, "show", "retirement_draw", "--mermaid").output
    treeless = _sim(guide_chains, "show", "retirement_draw", "--depth", "lots")
    assert treeless.exit_code != 0 and "--depth needs --mermaid" in treeless.output
    missing = _sim(guide_chains, "show", "nowhere")
    assert missing.exit_code != 0 and "neither a chain nor an account" in missing.output


def test_flowchart_in_the_notebook_shows_one_chain(guide_chains: Path) -> None:
    with open_scenario(guide_chains, "retire_alice") as sc:
        box = show_chain_flow(sc, chain="bridge")
        cmap = chain_map.chain_map(sc)
    (picker, depth, rules), chart = box.children[0].children, box.children[1]
    assert isinstance(picker, widgets.Dropdown) and picker.value == "bridge"
    assert isinstance(rules, widgets.Checkbox) and not rules.value
    assert list(picker.options) == ["asset_checking_liquidity", "bridge", "retirement_draw", "ss_split"]
    assert isinstance(chart, MermaidWidget) and chart.chart == chain_map.mermaid(cmap, ["bridge"])
    picker.value = "ss_split"  # picking another chain redraws
    assert chart.chart == chain_map.mermaid(cmap, ["ss_split"])
    rules.value = True  # and so does asking for the rules
    assert chart.chart == chain_map.mermaid(cmap, ["ss_split"], rules=True) != chain_map.mermaid(cmap, ["ss_split"])
    depth.value = "1"  # and so does limiting its depth
    assert chart.chart == chain_map.mermaid(cmap, ["ss_split"], rules=True, depth="1")
    # The widget draws itself, so VS Code (which runs no script in an Output) draws it too.
    assert f'import mermaid from "{MERMAID_URL}"' in MermaidWidget._esm


def test_calendar_transfers_use_chains(guide_chains: Path, append_calendar_row) -> None:
    # A calendar transfer is a rule only once the engine runs, so the map reads the calendar.
    append_calendar_row(date="2030-01", kind="one_off_transfer", name="roof",
                        account_1="retirement_draw", account_2="Asset_Checking", amount="5000")
    append_calendar_row(date="2031-01", kind="transfer_balance", name="close_savings",
                        account_1="Asset_Savings", account_2="ss_split")
    with open_scenario(guide_chains, "retire_alice") as sc:
        cmap = chain_map.chain_map(sc)
    uses = {name: [(u.kind, u.by, u.detail) for u in found] for name, found in cmap.uses.items()}
    assert ("draw", "calendar roof (2030-01-31)", "Asset_Checking") in uses["retirement_draw"]
    assert ("sweep", "calendar close_savings (2031-01-31)", "Asset_Savings") in uses["ss_split"]


def test_list_finds_a_chain_whose_name_looks_like_a_glob(
    guide_chains: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    (retire_alice_csv("chains/source") / "odd[1].csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nAsset_Savings,,,,,\n")
    result = _sim(guide_chains, "list")
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if "odd[1]" in line)
    assert "this scenario" in row


def test_flowchart_follows_policies_of_policy_chains(guide_chains: Path, update_account_value) -> None:
    # ss_split reaches checking, which overflows into savings, which
    # overflows into brokerage: two policies deep.
    update_account_value("Asset_Savings", "", maximum_balance="900000.00",
                         overflow_chain="Asset_Brokerage_Joint", overflow_lot="default")
    with open_scenario(guide_chains, "retire_alice") as sc:
        chart = chain_map.mermaid(chain_map.chain_map(sc), ["ss_split"])
    assert "Asset_Brokerage_Joint (default)" in _nodes(chart)  # its own node, not just named


def test_names_that_look_like_markup_print_as_written(
    guide_chains: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    chains = retire_alice_csv("chains/source")
    (chains / "tag[red].csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nAsset_Savings,,,,,\n")
    (chains / "out[bold]er.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\ntag[red],,,,,\n")
    listed = _sim(guide_chains, "list")
    assert listed.exit_code == 0, listed.output
    assert "tag[red]" in listed.output and "out[bold]er" in listed.output
    shown = _sim(guide_chains, "show", "tag[red]", "tag[red]")
    assert shown.exit_code == 0, shown.output
    assert "a step of out[bold]er (step 1)" in shown.output
    assert shown.output.count("tag[red] (source chain)") == 1  # a repeated name shows once
