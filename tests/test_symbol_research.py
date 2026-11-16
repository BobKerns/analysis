"""`sim symbol research`: a fund's or company's tear sheet bundle, its
weights, its page (#344, #372).

Nothing here runs the real research: the CLI tests use ``--from-bundle`` or
the ``fake_claude`` stand-in."""

from __future__ import annotations

import re

import csv
import shutil
from datetime import date
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import provenance, research, symbols


def _sim(data_dir: Path, *args: str, input: str | None = None):
    return CliRunner().invoke(main, ["sim", "symbol", "research", *args, "--data-dir", str(data_dir)],
                              input=input, terminal_width=200)


def _allocations(data_dir: Path) -> list[dict[str, str]]:
    with (data_dir / "allocations.csv").open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _bundles(data_dir: Path) -> list[Path]:
    return sorted((data_dir / "research" / "ABCDX").iterdir())


# --- reading a bundle -------------------------------------------------------


def test_read_allocation_takes_the_sim_rows_and_the_json_notes(research_bundles: Path) -> None:
    found = research.read_allocation(research_bundles / "abcdx", "ABCDX")
    assert found is not None
    assert found.as_of == "2026-09"
    assert found.weights == {"stock_us": "58.1234%", "stock_intl": "1.8766%", "bond": "39.5%", "cash": "0.5%"}
    assert found.method == "reported"
    assert found.sources == ["https://example.com/abcdx-fact-sheet"]
    assert found.warnings and "as-of" in found.warnings[0]


def test_read_allocation_refuses_an_unclassified_fund_with_its_warnings(research_bundles: Path) -> None:
    with pytest.raises(research.ResearchError, match=r"left 20.0% of ABCDX unclassified") as caught:
        research.read_allocation(research_bundles / "unclassified", "ABCDX")
    assert "20% of holdings have no asset type" in str(caught.value)


def test_read_allocation_gives_the_skills_reason_when_it_wrote_no_rows(tmp_path: Path) -> None:
    (tmp_path / "ABCDX-allocation.json").write_text(
        '{"allocation_pct": {"stock_us": 101.0, "unclassified": 0},'
        ' "sim_export": "not written: weights exceed 100%",'
        ' "warnings": ["inputs exceed 100% by 1.0000 points"]}', encoding="utf-8")
    match = r"no allocations rows for ABCDX \(not written: weights exceed"
    with pytest.raises(research.ResearchError, match=match) as caught:
        research.read_allocation(tmp_path, "ABCDX")
    assert "unclassified" not in str(caught.value)
    assert "exceed 100% by 1.0000 points" in str(caught.value)


def test_read_allocation_is_none_without_an_export(tmp_path: Path, research_bundles: Path) -> None:
    shutil.copy(research_bundles / "abcdx" / "ABCDX-tear-sheet.md", tmp_path)
    assert research.read_allocation(tmp_path, "ABCDX") is None


@pytest.mark.parametrize(("rows", "message"), [
    (["ABCDX,2026-09,stocks,100%"], "unknown asset class 'stocks'"),
    (["ABCDX,2026-09,stock_us,60%", "ABCDX,2026-09,bond,30%"], "add up to 90%, not 100%"),
    (["ABCDX,2026-09,stock_us,60%", "ABCDX,2026-08,bond,40%"], "several as_of months"),
    (["OTHER,2026-09,stock_us,100%"], "symbol 'OTHER', expected ABCDX"),
    (["ABCDX,2026-09,stock_us,100"], "weight"),
    (["ABCDX,2026-09,stock_us,110%", "ABCDX,2026-09,cash,-10%"], "110% is not between 0% and 100%"),
])
def test_read_allocation_checks_the_rows(tmp_path: Path, rows: list[str], message: str) -> None:
    (tmp_path / "ABCDX-allocations-sim.csv").write_text(
        "symbol,as_of,asset_class,weight\n" + "\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(research.ResearchError, match=message):
        research.read_allocation(tmp_path, "ABCDX")


def test_tear_sheet_title_is_the_funds_name(research_bundles: Path) -> None:
    assert research.tear_sheet_title(research_bundles / "abcdx", "ABCDX") == "A Balanced Index Fund"


def test_bundle_dir_never_reuses_a_days_folder(tmp_path: Path) -> None:
    day = date(2026, 10, 29)
    first = research.bundle_dir(tmp_path, "ABCDX", day)
    assert first == tmp_path / "research" / "ABCDX" / "2026-10-29"
    first.mkdir(parents=True)
    assert research.bundle_dir(tmp_path, "ABCDX", day).name == "2026-10-29-2"


# --- the page ---------------------------------------------------------------


JA = {"ja": {"name": "Japanese", "label": "日本語",
             "tabs": {"overview": "概要", "tear-sheet": "ティアシート", "detail": "詳細"}}}


def test_tabs_follow_the_reading_order(research_bundles: Path) -> None:
    tabs = research.bundle_tabs(research_bundles / "abcdx", "ABCDX", JA)
    assert [(t.lang, t.label) for t in tabs] == [
        ("en", "Overview"), ("en", "Tear sheet"), ("en", "Price chart"), ("en", "Detail"), ("en", "Glossary"),
        ("ja", "概要"), ("ja", "ティアシート"),
    ]
    assert [t.chart is not None for t in tabs if t.key == "price"] == [True]


def test_unknown_language_is_labelled_by_its_code(research_bundles: Path) -> None:
    tabs = research.bundle_tabs(research_bundles / "abcdx", "ABCDX")
    assert [t.label for t in tabs if t.lang == "ja"] == ["Overview (ja)", "Tear sheet (ja)"]


def test_prices_without_text_still_get_a_chart_tab(tmp_path: Path) -> None:
    (tmp_path / "ABCDX-tear-sheet.md").write_text("# Tear sheet\n", encoding="utf-8")
    (tmp_path / "ABCDX-prices.csv").write_text("date,close\n2025-01-31,10\n2025-02-28,11\n", encoding="utf-8")
    tabs = research.bundle_tabs(tmp_path, "ABCDX")
    assert [(t.label, t.path) for t in tabs][1] == ("Price chart", None)
    assert "<polyline" in research.render_page(tabs, title="ABCDX")


def test_page_is_one_self_contained_file_with_every_tab(research_bundles: Path, tmp_path: Path) -> None:
    tabs = research.bundle_tabs(research_bundles / "abcdx", "ABCDX", JA)
    page = research.render_page(tabs, title="ABCDX <research>", language_labels={"ja": "日本語"})
    assert "ABCDX &lt;research&gt;" in page
    assert page.count('name="lang"') == 2 and page.count('name="tab"') == 5
    assert '<label for="lang1">日本語</label>' in page
    assert "株式約60%" in page
    assert "<table>" in page  # Markdown tables render
    assert "<polyline" in page  # the price chart
    assert "<script" not in page and "http" not in page.split("<body>")[0]


def test_switching_language_keeps_the_tab(research_bundles: Path) -> None:
    # Language and tab are separate radio groups: each (language, tab) panel
    # shows only when both are picked.
    page = research.render_page(research.bundle_tabs(research_bundles / "abcdx", "ABCDX", JA), title="ABCDX")
    assert "#lang1:checked ~ #tab1:checked ~ #p1-1 { display: block; }" in page
    # Japanese has no Detail: its panel shows the English one, saying so.
    detail = page.split('<section id="p1-3">')[1].split("</section>")[0]
    assert "showing English" in detail and "fact sheet" in detail


def test_links_between_tabs_switch_tabs(research_bundles: Path) -> None:
    page = research.render_page(research.bundle_tabs(research_bundles / "abcdx", "ABCDX", JA), title="ABCDX")
    english = page.split('<section id="p0-1">')[1].split("</section>")[0]
    assert '<label class="tablink" for="tab4">Glossary</label>' in english
    assert '<label class="tablink" for="tab3">Detail</label>' in english
    # The same tab in the other language switches only the language.
    assert '<label class="tablink" for="lang1">日本語: ティアシート</label>' in english
    assert '<a href="https://example.com/abcdx.pdf">Fact sheet</a>' in english
    japanese = page.split('<section id="p1-1">')[1].split("</section>")[0]
    assert '<label class="tablink" for="lang0">English: Tear sheet</label>' in japanese
    assert ".md" not in english + japanese


def test_titled_and_anchored_links_switch_tabs(tmp_path: Path) -> None:
    (tmp_path / "X-tear-sheet.md").write_text(
        '[Detail](X-detail.md "the detail") and [drawdown](X-glossary.md#drawdown)\n', encoding="utf-8")
    (tmp_path / "X-detail.md").write_text("# Detail\n", encoding="utf-8")
    (tmp_path / "X-glossary.md").write_text("# Glossary\n", encoding="utf-8")
    page = research.render_page(research.bundle_tabs(tmp_path, "X"), title="X")
    assert '<label class="tablink" for="tab1">Detail</label>' in page
    assert '<label class="tablink" for="tab2">drawdown</label>' in page
    assert ".md" not in page.split("<body>")[1]


def test_a_tab_only_in_a_translation_shows_under_english_too(tmp_path: Path) -> None:
    (tmp_path / "X-tear-sheet.md").write_text("# Tear sheet\n", encoding="utf-8")
    (tmp_path / "X-glossary-ja.md").write_text("# 用語集\n", encoding="utf-8")
    tabs = research.bundle_tabs(tmp_path, "X", JA)
    page = research.render_page(tabs, title="X", language_labels={"ja": "日本語"})
    english = page.split('<section id="p0-1">')[1].split("</section>")[0]
    assert "Not in English; showing 日本語." in english and "用語集" in english
    assert '<nav class="row l0"><label for="tab0">Tear sheet</label><label for="tab1">Glossary</label></nav>' in page


def test_language_codes_may_carry_a_region(tmp_path: Path) -> None:
    (tmp_path / "X-overview.md").write_text("# Overview\n", encoding="utf-8")
    (tmp_path / "X-overview-pt-br.md").write_text("# Overview (pt-br)\n", encoding="utf-8")
    assert [(t.lang, t.key) for t in research.bundle_tabs(tmp_path, "X")] == [
        ("en", "overview"), ("pt-br", "overview")]


def test_price_file_from_stooq_with_a_bom_reads(tmp_path: Path) -> None:
    prices = tmp_path / "X-prices.csv"
    prices.write_text("\N{BYTE ORDER MARK}Date,Open,High,Low,Close,Volume\n2025-01-31,9,11,8,10,100\n"
                      "2025-02-28,10,12,9,11,100\n", encoding="utf-8")
    assert research.read_prices(prices) == [("2025-01-31", 10.0), ("2025-02-28", 11.0)]


def test_price_chart_skips_non_finite_closes(tmp_path: Path) -> None:
    prices = tmp_path / "X-prices.csv"
    prices.write_text("date,close\n2025-01-31,10\n2025-02-28,nan\n2025-03-31,inf\n2025-04-30,12\n",
                      encoding="utf-8")
    assert research.read_prices(prices) == [("2025-01-31", 10.0), ("2025-04-30", 12.0)]
    assert "<svg" in research.price_chart_svg(prices)


def test_price_chart_places_points_by_date_and_breaks_at_gaps(tmp_path: Path) -> None:
    prices = tmp_path / "X-prices.csv"
    prices.write_text("date,close\n2025-01-31,10\n2025-02-28,11\n2025-06-30,12\n2025-07-31,13\n"
                      "2025-09-30,14\n", encoding="utf-8")
    svg = research.price_chart_svg(prices)
    lines = re.findall(r'<polyline class="line" points="([^"]+)"/>', svg)
    assert len(lines) == 2 and svg.count('<circle class="dot"') == 1
    xs = [float(p.split(",")[0]) for line in lines for p in line.split()]
    # Jan, Feb, Jun, Jul over Jan..Sep: x moves by date, not by row.
    assert xs[1] - xs[0] == pytest.approx((xs[2] - xs[0]) / 5)


def test_price_chart_survives_bad_rows(tmp_path: Path) -> None:
    prices = tmp_path / "X-prices.csv"
    prices.write_text("date,close\n2025-01-31,10\n2025-02-28,n/a\n", encoding="utf-8")
    assert "too few rows" in research.price_chart_svg(prices)
    prices.write_text("date,close\n2025-01-31,10\n2025-02-28,10\n", encoding="utf-8")
    assert "<polyline" in research.price_chart_svg(prices)  # a flat line still draws


# --- the command, from a bundle ----------------------------------------------


def test_from_bundle_writes_weights_and_name_and_keeps_the_bundle(
    test_data_dir: Path, research_bundles: Path, load_test_copy,
) -> None:
    result = _sim(test_data_dir, "ABCDX", "--from-bundle", str(research_bundles / "abcdx"),
                  "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    assert "reported, as of 2026-09, 1 source(s)" in result.output
    assert "warning: inputs carry several as-of dates" in result.output
    assert symbols.security_name(test_data_dir, "ABCDX") == "A Balanced Index Fund"
    assert symbols.latest_set(test_data_dir, "ABCDX") == (
        "2026-09", [("stock_us", "58.1234%"), ("stock_intl", "1.8766%"), ("bond", "39.5%"), ("cash", "0.5%")])
    [bundle] = _bundles(test_data_dir)
    kept = sorted(p.name for p in bundle.iterdir())
    assert "fund-work" not in kept
    assert {"ABCDX-allocations-sim.csv", "ABCDX-tear-sheet.md", "report.html"} <= set(kept)
    assert "ABCDX has no prices yet" in result.output
    load_test_copy()  # what it wrote loads


def test_from_bundle_asks_for_the_name_and_before_writing(test_data_dir: Path, research_bundles: Path) -> None:
    bundle = str(research_bundles / "abcdx")
    declined = _sim(test_data_dir, "ABCDX", "--from-bundle", bundle, "--no-open", input="My Fund\nn\n")
    assert declined.exit_code == 0, declined.output
    assert "[A Balanced Index Fund]" in declined.output
    assert "Nothing written" in declined.output
    assert symbols.latest_set(test_data_dir, "ABCDX") is None

    accepted = _sim(test_data_dir, "ABCDX", "--from-bundle", bundle, "--no-open", input="My Fund\ny\n")
    assert accepted.exit_code == 0, accepted.output
    assert symbols.security_name(test_data_dir, "ABCDX") == "My Fund"
    assert len(_bundles(test_data_dir)) == 2


def test_a_second_set_for_the_same_month_needs_replace_with_yes(
    test_data_dir: Path, research_bundles: Path,
) -> None:
    symbols.add_symbol(test_data_dir, "ABCDX", name="Old", weights={"stock_us": "100%"}, as_of="2026-09")
    bundle = str(research_bundles / "abcdx")
    refused = _sim(test_data_dir, "ABCDX", "--from-bundle", bundle, "--yes", "--no-open")
    assert refused.exit_code != 0
    assert "add --replace" in refused.output
    assert symbols.latest_set(test_data_dir, "ABCDX") == ("2026-09", [("stock_us", "100%")])

    replaced = _sim(test_data_dir, "ABCDX", "--from-bundle", bundle, "--yes", "--replace", "--no-open")
    assert replaced.exit_code == 0, replaced.output
    assert "replaced in allocations.csv" in replaced.output
    assert [r["asset_class"] for r in _allocations(test_data_dir) if r["symbol"] == "ABCDX"] == [
        "stock_us", "stock_intl", "bond", "cash"]
    assert symbols.security_name(test_data_dir, "ABCDX") == "Old"  # an existing name stays


def test_name_for_a_symbol_already_named_is_reported_ignored(test_data_dir: Path, research_bundles: Path) -> None:
    symbols.add_symbol(test_data_dir, "ABCDX", name="Old")
    result = _sim(test_data_dir, "ABCDX", "--from-bundle", str(research_bundles / "abcdx"),
                  "--name", "New", "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    assert "--name ignored: ABCDX is already named 'Old'" in result.output
    assert symbols.security_name(test_data_dir, "ABCDX") == "Old"


@pytest.mark.parametrize(("args", "message"), [
    (["..", ], "no letters or digits"),
    (["A Fund", "--symbol", "A B"], "must be one word"),
    (["ABCDX", "--symbol", "../x"], "must be one word"),
])
def test_a_folder_name_that_cant_be_made_is_refused(test_data_dir: Path, research_bundles: Path,
                                                    args: list[str], message: str) -> None:
    result = _sim(test_data_dir, *args, "--from-bundle", str(research_bundles / "abcdx"), "--yes", "--no-open")
    assert result.exit_code == 2
    assert message in result.output
    assert not (test_data_dir / "research").exists()


# --- research on a name (#372) ---------------------------------------------


@pytest.mark.parametrize(("name", "key"), [
    ("Acme Robotics", "ACME-ROBOTICS"),
    ("  Acme   Robotics, Inc. ", "ACME-ROBOTICS-INC"),
    ("A/B", "A-B"),
])
def test_a_names_folder_is_its_words_joined(name: str, key: str) -> None:
    assert research.name_key(name) == key


def test_a_company_by_name_is_researched_and_kept_under_its_words(
    test_data_dir: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_BUNDLE", "acme-robotics")
    result = _sim(test_data_dir, "Acme  Robotics", "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    assert "Researching Acme Robotics:" in result.output
    args = fake_claude.read_text(encoding="utf-8").splitlines()
    prompt = args[1]
    assert "Tear sheet on Acme Robotics." in prompt
    assert "fund-tear-sheet" in prompt and "public-tear-sheet" in prompt  # the run picks
    assert "ACME-ROBOTICS-tear-sheet.md" in prompt and "--ticker ACME-ROBOTICS" in prompt
    allowed = args[args.index("--allowedTools") + 1:args.index("--output-format")]
    bash = " ".join(a for a in allowed if a.startswith("Bash"))
    assert "/fund-tear-sheet/scripts/" in bash and "/public-tear-sheet/scripts/" in bash
    [bundle] = sorted((test_data_dir / "research" / "ACME-ROBOTICS").iterdir())
    assert "<title>Acme Robotics research" in (bundle / "report.html").read_text(encoding="utf-8")
    assert "Acme Robotics: the bundle has no allocation export, so nothing was written." in result.output
    assert not (test_data_dir / "allocations.csv").exists() or not any(
        r["symbol"].startswith("ACME") for r in _allocations(test_data_dir))


@pytest.mark.parametrize(("flag", "skill", "other"), [
    ("--company", "public-tear-sheet", "fund-tear-sheet"),
    ("--fund", "fund-tear-sheet", "public-tear-sheet"),
])
def test_company_or_fund_runs_only_that_skill(test_data_dir: Path, fake_claude: Path, flag: str, skill: str,
                                              other: str) -> None:
    result = _sim(test_data_dir, "ABCDX", flag, "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    args = fake_claude.read_text(encoding="utf-8").splitlines()
    assert f"Use the {skill} skill." in args[1] and other not in args[1]
    allowed = args[args.index("--allowedTools") + 1:args.index("--output-format")]
    bash = " ".join(a for a in allowed if a.startswith("Bash"))
    assert f"/{skill}/scripts/" in bash and f"/{other}/" not in bash


def test_weights_from_research_on_a_name_ask_for_a_ticker(test_data_dir: Path, research_bundles: Path) -> None:
    result = _sim(test_data_dir, "A Balanced Index Fund", "--from-bundle", str(research_bundles / "balanced-by-name"),
                  "--no-open", input="a b\nabcdx\n\ny\n")
    assert result.exit_code == 0, result.output
    assert "must be one word" in result.output  # re-asked
    assert symbols.latest_set(test_data_dir, "ABCDX") == ("2026-09", [("stock_us", "60%"), ("bond", "40%")])
    assert symbols.security_name(test_data_dir, "ABCDX") == "A Balanced Index Fund"
    assert len(list((test_data_dir / "research" / "A-BALANCED-INDEX-FUND").iterdir())) == 1


def test_weights_from_research_on_a_name_are_not_written_with_yes(
    test_data_dir: Path, research_bundles: Path,
) -> None:
    result = _sim(test_data_dir, "A Balanced Index Fund", "--from-bundle", str(research_bundles / "balanced-by-name"),
                  "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    assert "is a name, not a ticker, so its weights were not written" in result.output
    assert symbols.latest_set(test_data_dir, "A-BALANCED-INDEX-FUND") is None


def test_symbol_files_research_on_a_name_under_that_ticker(test_data_dir: Path, research_bundles: Path) -> None:
    result = _sim(test_data_dir, "A Balanced Index Fund", "--symbol", "abcdx",
                  "--from-bundle", str(research_bundles / "abcdx"), "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    assert symbols.latest_set(test_data_dir, "ABCDX") is not None
    [bundle] = _bundles(test_data_dir)
    assert "<title>A Balanced Index Fund research" in (bundle / "report.html").read_text(encoding="utf-8")


def test_asked_replace_shows_both_sets(test_data_dir: Path, research_bundles: Path) -> None:
    symbols.add_symbol(test_data_dir, "ABCDX", name="Old", weights={"stock_us": "100%"}, as_of="2026-09")
    result = _sim(test_data_dir, "ABCDX", "--from-bundle", str(research_bundles / "abcdx"), "--no-open",
                  input="y\n")
    assert result.exit_code == 0, result.output
    assert "This replaces the set already written for 2026-09" in result.output
    line = next(ln for ln in result.output.splitlines() if ln.strip().startswith("stock_us"))
    assert "58.1234%" in line and "100%" in line


def test_as_of_dates_the_set(test_data_dir: Path, research_bundles: Path) -> None:
    result = _sim(test_data_dir, "ABCDX", "--from-bundle", str(research_bundles / "abcdx"),
                  "--as-of", "2026-10", "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    latest = symbols.latest_set(test_data_dir, "ABCDX")
    assert latest is not None and latest[0] == "2026-10"


def test_an_unclassified_fund_writes_nothing_but_keeps_the_bundle(
    test_data_dir: Path, research_bundles: Path,
) -> None:
    result = _sim(test_data_dir, "ABCDX", "--from-bundle", str(research_bundles / "unclassified"),
                  "--yes", "--no-open")
    assert result.exit_code != 0
    assert "no asset type" in result.output and "bundle is kept" in result.output
    assert symbols.latest_set(test_data_dir, "ABCDX") is None
    assert len(_bundles(test_data_dir)) == 1


def test_a_bundle_already_in_research_is_not_copied_again(
    test_data_dir: Path, research_bundles: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(click, "launch", lambda url, **_: opened.append(url))
    first = _sim(test_data_dir, "ABCDX", "--from-bundle", str(research_bundles / "abcdx"), input="\nn\n")
    assert first.exit_code == 0, first.output
    [bundle] = _bundles(test_data_dir)
    assert opened == [str(bundle / "report.html")]
    again = _sim(test_data_dir, "abcdx", "--from-bundle", str(bundle), "--yes", "--no-open")
    assert again.exit_code == 0, again.output
    assert _bundles(test_data_dir) == [bundle]


def test_research_folder_is_outside_the_provenance_scope(test_data_dir: Path, research_bundles: Path) -> None:
    research.keep_bundle(research_bundles / "abcdx", research.bundle_dir(test_data_dir, "ABCDX"))
    scenario = next(p.name for p in (test_data_dir / "scenarios").iterdir() if p.is_dir())
    scoped = provenance._scoped_paths(test_data_dir, scenario)
    assert not any(p == "research" or p.startswith("research/") for p in scoped)


# --- the command, running the skill --------------------------------------------


def test_a_run_renders_the_plugin_runs_claude_and_imports(test_data_dir: Path, fake_claude: Path) -> None:
    result = _sim(test_data_dir, "ABCDX", "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    assert "Plugin: finance-research (tab languages English" in result.output
    assert "WebSearch ABCDX fact sheet" in result.output
    assert "$1.25" in result.output
    args = fake_claude.read_text(encoding="utf-8").splitlines()
    assert args[0] == "-p" and "Tear sheet on ABCDX." in args[1]
    plugin = Path(args[args.index("--plugin-dir") + 1])
    assert plugin.name == "finance-research"
    assert not plugin.exists()  # rendered into a temporary folder
    assert "--dangerously-skip-permissions" not in args
    assert {"--restricted", "--strict-mcp-config"} <= set(args)
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    allowed = args[args.index("--allowedTools") + 1:args.index("--output-format")]
    bash = [a for a in allowed if a.startswith("Bash")]
    # Resolved, as the rules are: on macOS the temporary folder is behind a symlink.
    skills = plugin.resolve() / "skills"
    scripts = [skills / skill / "scripts" for skill in ("fund-tear-sheet", "public-tear-sheet")]
    assert bash and all(a.startswith(tuple(f"Bash(python3 {s}/" for s in scripts)) and a.endswith(".py *)")
                        for a in bash)
    edits = [a for a in allowed if a.startswith(("Edit", "Write"))]
    assert len(edits) == 1 and edits[0].startswith("Edit(//") and edits[0].endswith("/work/**)")
    assert all(str(s) in args[1] for s in scripts)  # the prompt says where the scripts are
    assert symbols.latest_set(test_data_dir, "ABCDX") is not None


def test_a_given_plugin_dir_is_passed_through(test_data_dir: Path, fake_claude: Path, tmp_path: Path) -> None:
    plugin = tmp_path / "my-plugin"
    plugin.mkdir()
    result = _sim(test_data_dir, "ABCDX", "--plugin-dir", str(plugin), "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    args = fake_claude.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--plugin-dir") + 1] == str(plugin)
    assert "Plugin:" not in result.output


def test_a_failed_run_keeps_what_it_wrote(
    test_data_dir: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "usage limit reached")
    result = _sim(test_data_dir, "ABCDX", "--yes", "--no-open")
    assert result.exit_code != 0
    assert "the research failed: usage limit reached" in result.output
    assert len(_bundles(test_data_dir)) == 1
    assert symbols.latest_set(test_data_dir, "ABCDX") is None


def test_a_run_that_writes_nothing_points_at_the_plugin(
    test_data_dir: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_BUNDLE", "")
    result = _sim(test_data_dir, "ABCDX", "--yes", "--no-open")
    assert result.exit_code != 0
    assert "--plugin-dir" in result.output
    assert not (test_data_dir / "research").exists()


def test_run_skill_stops_at_the_timeout(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    work = tmp_path / "work"
    work.mkdir()
    outcome = research.run_skill("ABCDX", "ABCDX", work, tmp_path, timeout_minutes=0.01, progress=lambda _: None)
    assert not outcome.ok
    assert "stopped after" in outcome.message
    assert outcome.seconds < 10


def test_the_timeout_stops_what_claude_started_too(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_CHILD", "1")
    work = tmp_path / "work"
    work.mkdir()
    outcome = research.run_skill("ABCDX", "ABCDX", work, tmp_path, timeout_minutes=0.01, progress=lambda _: None)
    assert "stopped after" in outcome.message
    assert outcome.seconds < 10


def test_a_bundle_folder_with_no_files_is_a_clear_error(test_data_dir: Path, tmp_path: Path) -> None:
    (tmp_path / "download" / "abcdx").mkdir(parents=True)
    result = _sim(test_data_dir, "ABCDX", "--from-bundle", str(tmp_path / "download"), "--yes", "--no-open")
    assert result.exit_code == 1, result.output
    assert "has no Markdown, JSON or CSV files" in result.output
    assert "Traceback" not in result.output


def test_without_claude_on_path_it_says_so(test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    result = _sim(test_data_dir, "ABCDX", "--yes", "--no-open")
    assert result.exit_code != 0
    assert "isn't on PATH" in result.output


# --- add_symbol(replace=True) ------------------------------------------------


def test_add_symbol_replace_swaps_only_that_months_set(test_data_dir: Path) -> None:
    symbols.add_symbol(test_data_dir, "ABCDX", weights={"stock_us": "100%"}, as_of="2026-08")
    symbols.add_symbol(test_data_dir, "ABCDX", weights={"stock_us": "100%"}, as_of="2026-09")
    with pytest.raises(ValueError, match="already has weights as of 2026-09"):
        symbols.add_symbol(test_data_dir, "ABCDX", weights={"bond": "100%"}, as_of="2026-09")
    symbols.add_symbol(test_data_dir, "ABCDX", weights={"bond": "100%"}, as_of="2026-09", replace=True)
    rows = [(r["as_of"], r["asset_class"]) for r in _allocations(test_data_dir) if r["symbol"] == "ABCDX"]
    assert rows == [("2026-08", "stock_us"), ("2026-09", "bond")]


def test_add_symbol_replace_keeps_the_set_where_it_was(test_data_dir: Path) -> None:
    symbols.add_symbol(test_data_dir, "ABCDX", weights={"stock_us": "100%"}, as_of="2026-09")
    symbols.add_symbol(test_data_dir, "OTHER", weights={"bond": "100%"})
    symbols.add_symbol(test_data_dir, "ABCDX", weights={"bond": "40%", "cash": "60%"}, as_of="2026-09",
                       replace=True)
    rows = [(r["symbol"], r["asset_class"]) for r in _allocations(test_data_dir)]
    assert rows == [("ABCDX", "bond"), ("ABCDX", "cash"), ("OTHER", "bond")]
