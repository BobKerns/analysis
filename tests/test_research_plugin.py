"""The finance-research plugin: neutral defaults, rendering, install (#345)."""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis import research_plugin as rp
from analysis.sim import registries

HOUSEHOLD = rp.ResearchConfig(languages=("en", "ja"), tax_state="CA")
# Japanese script (kana and CJK ideographs): it belongs only in overlays.
JAPANESE = re.compile(r"[぀-ヿ一-鿿]")


def _skill_text(plugin: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted((plugin / "skills").rglob("*.md")))


@pytest.mark.parametrize("skill", rp.skill_names())
def test_committed_defaults_are_a_neutral_render(skill):
    # Fails after a defaults template edit: run `analysis research-plugin render --neutral`.
    text = (rp.SOURCE_DIR / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert rp.current_defaults(text) == rp.render_defaults(rp.NEUTRAL, skill)


def test_neutral_plugin_has_no_household_settings():
    text = _skill_text(rp.SOURCE_DIR)
    assert not JAPANESE.search(text)
    assert "overview-ja" not in text
    assert "Tax state: none set." in text


def test_household_render_adds_overlays(tmp_path):
    plugin = rp.render(HOUSEHOLD, tmp_path)
    fund = plugin / "skills" / "fund-tear-sheet"
    company = plugin / "skills" / "public-tear-sheet"
    assert (fund / "references" / "overview-ja.md").exists()
    assert (company / "references" / "overview-ja.md").exists()
    assert (fund / "references" / "state-tax.md").exists()
    assert not (company / "references" / "state-tax.md").exists()
    skill = (fund / "SKILL.md").read_text(encoding="utf-8")
    assert "**Tabs:** Overview, 概要 (Japanese overview), Tear sheet, Detail, Glossary." in skill
    assert "**Tax state: California.**" in skill
    assert "no overview, no Japanese or no glossary" in skill
    assert not (plugin / "defaults").exists()
    assert not (plugin / "overlays").exists()
    manifest = json.loads((tmp_path / ".claude-plugin" / "marketplace.json").read_text())
    assert manifest["name"] == rp.LOCAL_MARKETPLACE
    assert manifest["plugins"][0]["source"] == f"./{rp.PLUGIN_NAME}"


def test_render_replaces_an_earlier_render(tmp_path):
    rp.render(HOUSEHOLD, tmp_path)
    plugin = rp.render(rp.NEUTRAL, tmp_path)
    assert not (plugin / "skills" / "fund-tear-sheet" / "references" / "overview-ja.md").exists()
    assert not JAPANESE.search(_skill_text(plugin))


def test_state_without_rules_file_still_names_the_state(tmp_path):
    plugin = rp.render(rp.ResearchConfig(tax_state="NY"), tmp_path)
    skill = (plugin / "skills" / "fund-tear-sheet" / "SKILL.md").read_text(encoding="utf-8")
    assert "**Tax state: New York.**" in skill
    assert "state-tax.md" not in skill


def test_tabs_off_drop_overview_and_its_translations(tmp_path):
    config = rp.ResearchConfig(languages=("en", "ja"), overview=False, glossary=False)
    plugin = rp.render(config, tmp_path)
    skill = (plugin / "skills" / "public-tear-sheet" / "SKILL.md").read_text(encoding="utf-8")
    assert "**Tabs:** Tear sheet, Detail." in skill
    assert "If the user asks for" not in skill
    assert not (plugin / "skills" / "public-tear-sheet" / "references" / "overview-ja.md").exists()


def test_skill_zips_hold_each_skill_folder(tmp_path):
    plugin = rp.render(HOUSEHOLD, tmp_path)
    zips = rp.write_skill_zips(plugin, tmp_path / "skills")
    assert [z.name for z in zips] == ["fund-tear-sheet.skill", "public-tear-sheet.skill"]
    names = zipfile.ZipFile(zips[0]).namelist()
    assert "fund-tear-sheet/SKILL.md" in names
    assert "fund-tear-sheet/references/overview-ja.md" in names


def test_parse_config_full():
    config = rp.parse_config("languages: [en, ja]\ntax_state: ca\ntabs: {glossary: false}\n")
    assert config == rp.ResearchConfig(languages=("en", "ja"), tax_state="CA", glossary=False)


def test_missing_or_empty_config_is_neutral(tmp_path):
    assert rp.read_config(tmp_path / rp.CONFIG_FILENAME) == rp.NEUTRAL
    assert rp.parse_config("") == rp.NEUTRAL


@pytest.mark.parametrize(("text", "message"), [
    ("- en\n", "mapping"),
    ("language: [en]\n", "unknown setting"),
    ("languages: [ja]\n", "start with en"),
    ("languages: [en, en]\n", "twice"),
    ("languages: [en, fr]\n", "no overview overlay for language 'fr'"),
    ("tax_state: Calif\n", "two-letter"),
    ("tabs: {detail: false}\n", "unknown tab"),
    ("tabs: {glossary: no-thanks}\n", "true or false"),
    ("languages: [en\n", "not valid YAML"),
])
def test_parse_config_rejects(text, message):
    with pytest.raises(rp.ResearchConfigError, match=re.escape(message)):
        rp.parse_config(text)


def test_eval_fixture_config_parses():
    assert rp.read_config(rp.SOURCE_DIR / "evals" / rp.CONFIG_FILENAME) == HOUSEHOLD


def test_allocation_script_classes_match_registry():
    # The script keeps its own copy so it runs outside this repo's venv.
    text = (rp.SOURCE_DIR / "skills" / "fund-tear-sheet" / "scripts" / "allocation.py").read_text()
    copy = re.search(r"^SIM_CLASSES = (\[.*\])$", text, re.M)
    assert copy is not None
    assert tuple(json.loads(copy.group(1))) == registries.ASSET_CLASSES


def _fund_calc(tmp_path: Path, rows: list[tuple[str, str, str, str]]) -> str:
    data = tmp_path / "data.csv"
    with data.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["section", "metric", "period", "value", "unit", "source_url", "as_of"])
        for section, metric, period, value in rows:
            writer.writerow([section, metric, period, value, "", "", ""])
    script = rp.SOURCE_DIR / "skills" / "fund-tear-sheet" / "scripts" / "fund_calc.py"
    done = subprocess.run([sys.executable, str(script), str(data)], capture_output=True, text=True, check=True)
    return done.stdout


def test_fund_calc_state_bands_follow_given_rates(tmp_path):
    out = _fund_calc(tmp_path, [
        ("fund", "yield_7d", "", "4"), ("tax", "state_exempt_pct", "", "100"),
        ("tax", "state_rate", "a", "5"), ("tax", "state_rate", "b", "10"),
    ])
    line = next(x for x in out.splitlines() if x.startswith("state_tax_equivalent_yield "))
    assert "0%: 4.00%; 5%: 4.21%; 10%: 4.44%" in line


def test_fund_calc_without_state_rates_shows_no_bands(tmp_path):
    out = _fund_calc(tmp_path, [("fund", "yield_7d", "", "4"), ("tax", "state_exempt_pct", "", "100")])
    assert "state_tax_equivalent_yield" not in out
    assert "no tax,state_rate rows" in out


def test_install_renders_then_installs_from_local_marketplace(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rp.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(rp.subprocess, "run", lambda args, check: calls.append(args))
    data = tmp_path / "data"
    data.mkdir()
    (data / rp.CONFIG_FILENAME).write_text("languages: [en, ja]\n")
    out = tmp_path / "out"
    result = CliRunner().invoke(rp.research_plugin_command,
                                ["install", "--data-dir", str(data), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "overview languages English, Japanese; tax state none" in result.output
    assert (out / rp.PLUGIN_NAME / "skills" / "fund-tear-sheet" / "references" / "overview-ja.md").exists()
    assert calls == [
        ["claude", "plugin", "marketplace", "add", str(out.resolve())],
        ["claude", "plugin", "install", f"{rp.PLUGIN_NAME}@{rp.LOCAL_MARKETPLACE}"],
    ]


def test_install_without_claude_fails(monkeypatch):
    monkeypatch.setattr(rp.shutil, "which", lambda name: None)
    result = CliRunner().invoke(rp.research_plugin_command, ["install"])
    assert result.exit_code != 0
    assert "not on PATH" in result.output


def test_render_reports_a_bad_config(tmp_path):
    bad = tmp_path / rp.CONFIG_FILENAME
    bad.write_text("tax_state: XX\n")
    result = CliRunner().invoke(rp.research_plugin_command,
                                ["render", "--config", str(bad), "--out", str(tmp_path / "out")])
    assert result.exit_code != 0
    assert "two-letter" in result.output


def test_repo_marketplace_lists_the_neutral_plugin():
    manifest = json.loads((rp.REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert manifest["name"] == "analysis"
    assert manifest["plugins"][0]["source"] == f"./plugins/{rp.PLUGIN_NAME}"
    settings = json.loads((rp.REPO_ROOT / ".claude" / "settings.json").read_text())
    # Registered for discovery only: never switched on for everyone.
    assert "analysis" in settings["extraKnownMarketplaces"]
    assert "enabledPlugins" not in settings
