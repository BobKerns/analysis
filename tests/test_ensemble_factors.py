"""``sim ensemble factors``: a scenario's latent factors as trees, the rows
that vary under each, and the tree drawn (#369)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import factor_tree as ft
from analysis.sim.correlation import Link


@pytest.fixture
def markets(derive_template_scenario: Callable[..., Path]) -> str:
    """The templates' year-by-year inflation and market paths on base."""
    derive_template_scenario("markets", "base:t_inflation_ensemble:t_market_ensemble")
    return "markets"


def _factors(data_dir: Path, *args: str) -> Result:
    return CliRunner().invoke(main, ["sim", "ensemble", "factors", *args, "--data-dir", str(data_dir)])


def _lines(output: str) -> list[str]:
    return [" ".join(line.strip("│┃ ").split()) for line in output.splitlines()]


def test_the_table_nests_each_factor_under_its_parent_with_its_rho(
    template_data_dir: Path, markets: str,
) -> None:
    result = _factors(template_data_dir, markets)

    assert result.exit_code == 0, result.output
    assert "Factors in markets" in result.output
    lines = _lines(result.output)
    core = lines.index("core_inflation │ │ │ how hot prices run generally")
    assert lines[core + 1].startswith("services_inflation │ 0.6 │ │")
    # A factor with a varying row under it counts it.
    assert any(line.startswith("cash_factor │ 0.95 │ 1 │") for line in lines)
    assert any(line.startswith("market │ │ │") for line in lines)
    assert "Varying rows" not in result.output
    # The names are indented by depth.
    assert "│   interest_rates " in result.output


def test_varying_lists_each_row_under_its_factor_and_tree(
    template_data_dir: Path, markets: str,
) -> None:
    result = _factors(template_data_dir, markets, "--varying")

    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    # cash_path hangs from cash_factor (ρ 1), which is 0.95 × 0.9 from core_inflation.
    assert "cash_factor │ cash_path │ 1.00 │ 0.85 │ set_rate cash │ truncated_normal, ar1 path" in lines
    assert any(line.startswith("market │ stock_us_factor │ stock_us_path │ 1.00 │ 0.95 │")
               for line in lines)
    # A tree's root is named on its first row only.
    assert sum(line.startswith("core_inflation │") for line in lines) == 2  # both tables


def test_mermaid_prints_the_tree_as_a_flowchart(template_data_dir: Path, markets: str) -> None:
    result = _factors(template_data_dir, markets, "--mermaid")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "flowchart TB"
    ids = {line.split("(")[0].strip(): line.split('"')[1] for line in lines if '(["' in line}
    edges = [line.split() for line in lines if "-->" in line]
    named = {(ids[a], rho.strip("-|>"), ids[b]) for a, rho, b in edges}
    assert ("core_inflation", "0.9", "interest_rates") in named
    assert ("interest_rates", "0.95", "cash_factor") in named
    assert not any(b == "market" for _, _, b in named)


@pytest.mark.parametrize("args, refusal", [
    (("--mermaid", "--chart"), "use one of --mermaid, --chart and --chart-file"),
    (("--mermaid", "--varying"), "--mermaid prints only the tree"),
])
def test_mermaid_refuses_the_other_outputs(
    template_data_dir: Path, markets: str, args: tuple[str, ...], refusal: str,
) -> None:
    result = _factors(template_data_dir, markets, *args)

    assert result.exit_code == 2
    assert refusal in result.output


def test_a_scenario_without_factors_says_so(
    template_data_dir: Path, derive_template_scenario: Callable[..., Path],
) -> None:
    derive_template_scenario("bare", "t_empty")

    result = _factors(template_data_dir, "bare")

    assert result.exit_code == 0, result.output
    assert "no latent factors in bare" in result.output


def test_the_chart_file_holds_the_drawn_tree(
    template_data_dir: Path, markets: str, tmp_path: Path,
) -> None:
    out = tmp_path / "factors.png"

    result = _factors(template_data_dir, markets, "--chart-file", str(out))

    assert result.exit_code == 0, result.output
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "Factors in markets" not in result.output  # the chart alone, without --table


# ---------- the tree itself ----------


def _tree() -> ft.FactorTree:
    """Two trees: ``a`` (t copula) over ``b``, which a row hangs from, and a
    row correlated only with that row; ``c`` alone; and a row on its own."""
    factors = {
        "a": ft.Factor("a", None, None, "", nu=5.0),
        "b": ft.Factor("b", "a", 0.8, ""),
        "c": ft.Factor("c", None, None, ""),
    }
    links = {"b": Link("a", 0.8), "row": Link("b", 0.5), "follower": Link("row", 0.9)}
    varying = [ft.Varying(name, "set_rate x", "uniform") for name in ("row", "follower", "loner")]
    return ft.FactorTree(factors, varying, links)


def test_each_row_belongs_to_its_nearest_factor() -> None:
    tree = _tree()

    assert tree.factor_of("row") == "b"
    assert tree.factor_of("follower") == "b"  # through the row it follows
    assert tree.factor_of("loner") is None
    assert tree.root("b") == "a"
    assert tree.rho("follower", "a") == pytest.approx(0.9 * 0.5 * 0.8)


def test_the_mermaid_tree_names_a_lone_factor_and_a_t_copula() -> None:
    source = ft.mermaid(_tree())

    assert 'f0(["a<br/>t copula, ν 5"])' in source
    assert 'f2(["c"])' in source
    assert "f0 -->|0.8| f1" in source


def test_the_figure_is_16_9_unless_given_a_size() -> None:
    tree = _tree()

    assert tuple(ft.factor_tree_figure(tree).get_size_inches()) == (12, 6.75)
    assert tuple(ft.factor_tree_figure(tree, "t", size=(10, 4)).get_size_inches()) == (10, 4)


def test_the_tree_reads_factors_and_varying_rows_from_an_intent(
    template_data_dir: Path, markets: str,
) -> None:
    from analysis.sim.loader import open_intent_db

    with open_intent_db(template_data_dir, markets) as (con, _name):
        tree = ft.factor_tree(con)

    assert list(tree.factors)[0] == "core_inflation"
    assert {"cpi_path", "cash_path", "stock_path"} <= {v.name for v in tree.varying}
    assert tree.factor_of("cpi_path") == "cpi_factor"
