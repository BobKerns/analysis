"""Render and install the finance-research Claude plugin (#345).

``plugins/finance-research/`` in this repo is the neutral plugin: English
only, no tax state. A household's choices live in the data repo, in
``$ANALYSIS_DATA_DIR/research.yaml``::

    languages: [en, ja]   # tab languages; en first, each other adds a copy of every tab
    tax_state: CA         # two-letter state code; leave out for none
    tabs:
      overview: true
      price: true
      glossary: true

An installed plugin runs wherever Claude Code does, usually with no data repo
in sight, and Claude Code's own plugin options don't reach skill text. So the
choices are fixed when the plugin is rendered: ``render`` copies the neutral
plugin to ``out/finance-research/``, rewrites each skill's "Defaults for this
installation" block from ``defaults/<skill>.md.j2``, and adds the overlay
files (``overlays/<language>/<skill>/``, ``overlays/state/<code>/<skill>/``)
to the skill's ``references/``. ``install`` renders, then installs that copy
from a local marketplace; Claude Code reads it in place, so a later render
takes effect at the next session. ``render --neutral`` rewrites the committed
blocks themselves, and a test keeps them equal to a neutral render.

See docs/guides/finance-research.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "finance-research"
SOURCE_DIR = REPO_ROOT / "plugins" / PLUGIN_NAME


def _main_checkout(root: Path) -> Path:
    """The repo's main checkout, even from a worktree: the installed plugin is
    read in place, so it must outlive any worktree it was rendered from."""
    try:
        common = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return root
    return Path(common).parent


def default_out_dir() -> Path:
    return _main_checkout(REPO_ROOT) / "out" / PLUGIN_NAME
CONFIG_FILENAME = "research.yaml"
# The marketplace the rendered copy is installed from; distinct from the
# repo's own "analysis" marketplace, which lists the neutral plugin.
LOCAL_MARKETPLACE = "finance-research-local"

BLOCK_START = "<!-- defaults:start"
BLOCK_END = "<!-- defaults:end -->"
# Plugin files that drive rendering and never ship in a rendered copy.
RENDER_ONLY = ("defaults", "overlays")

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "the District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


# Each tab's key (the Markdown fallback's file suffix, and the key a
# language.yaml labels it by) and English name, in reading order.
TABS = (
    ("overview", "Overview"),
    ("tear-sheet", "Tear sheet"),
    ("price", "Price chart"),
    ("detail", "Detail"),
    ("glossary", "Glossary"),
)
# The tabs research.yaml can turn off.
OPTIONAL_TABS = ("overview", "price", "glossary")


class ResearchConfigError(ValueError):
    """``research.yaml`` can't be read as research settings."""


@dataclass(frozen=True)
class ResearchConfig:
    """A household's research settings; the defaults are the neutral plugin's."""

    languages: tuple[str, ...] = ("en",)
    tax_state: str | None = None
    overview: bool = True
    glossary: bool = True
    price: bool = True

    def tab_keys(self) -> list[str]:
        """The keys of the tabs every sheet gets, in reading order."""
        off = {name for name in OPTIONAL_TABS if not getattr(self, name)}
        return [key for key, _ in TABS if key not in off]


NEUTRAL = ResearchConfig()


def config_path(data_dir: Path) -> Path:
    return data_dir / CONFIG_FILENAME


def read_config(path: Path, source: Path = SOURCE_DIR) -> ResearchConfig:
    """The settings in ``path``; neutral when the file is missing."""
    if not path.exists():
        return NEUTRAL
    return parse_config(path.read_text(encoding="utf-8"), where=str(path), source=source)


def parse_config(text: str, *, where: str = CONFIG_FILENAME, source: Path = SOURCE_DIR) -> ResearchConfig:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ResearchConfigError(f"{where}: not valid YAML: {exc}") from exc
    if doc is None:
        return NEUTRAL
    if not isinstance(doc, dict):
        raise ResearchConfigError(f"{where}: expected a mapping of settings")
    unknown = sorted(set(doc) - {"languages", "tax_state", "tabs"})
    if unknown:
        raise ResearchConfigError(f"{where}: unknown setting(s) {', '.join(unknown)}")

    languages = doc.get("languages", ["en"])
    if isinstance(languages, str):
        languages = [languages]
    if not isinstance(languages, list) or not all(isinstance(lang, str) for lang in languages):
        raise ResearchConfigError(f"{where}: languages must be a list of language codes")
    if not languages or languages[0] != "en":
        raise ResearchConfigError(f"{where}: languages must start with en (the tear sheet's language)")
    if len(set(languages)) != len(languages):
        raise ResearchConfigError(f"{where}: languages lists a language twice")
    supported = available_languages(source)
    for lang in languages[1:]:
        if lang not in supported:
            have = ", ".join(sorted(supported)) or "none"
            raise ResearchConfigError(f"{where}: no translation overlay for language {lang!r} (available: {have})")

    state = doc.get("tax_state")
    if state is not None:
        if not isinstance(state, str) or state.upper() not in STATE_NAMES:
            raise ResearchConfigError(f"{where}: tax_state must be a two-letter US state code, not {state!r}")
        state = state.upper()

    tabs = doc.get("tabs") or {}
    if not isinstance(tabs, dict):
        raise ResearchConfigError(f"{where}: tabs must map {'/'.join(OPTIONAL_TABS)} to true or false")
    unknown = sorted(set(tabs) - set(OPTIONAL_TABS))
    if unknown:
        raise ResearchConfigError(f"{where}: unknown tab(s) {', '.join(unknown)}")
    for name, value in tabs.items():
        if not isinstance(value, bool):
            raise ResearchConfigError(f"{where}: tabs.{name} must be true or false")
    return ResearchConfig(
        languages=tuple(languages), tax_state=state,
        overview=tabs.get("overview", True), glossary=tabs.get("glossary", True), price=tabs.get("price", True),
    )


def available_languages(source: Path = SOURCE_DIR) -> dict[str, dict[str, Any]]:
    """Each translation overlay's code -> its ``language.yaml``: ``name``
    (in English), ``label`` (the parent tab's name) and ``tabs`` (each tab
    key's name in that language)."""
    found = {}
    for meta in sorted((source / "overlays").glob("*/language.yaml")):
        found[meta.parent.name] = yaml.safe_load(meta.read_text(encoding="utf-8"))
    return found


def skill_names(source: Path = SOURCE_DIR) -> list[str]:
    return sorted(p.parent.name for p in (source / "skills").glob("*/SKILL.md"))


def tab_names(meta: dict[str, Any] | None = None) -> dict[str, str]:
    """Each tab key's name: English, or in the language ``meta`` describes,
    falling back to English for a tab it doesn't name."""
    names = dict(TABS)
    if meta:
        names.update({key: str(name) for key, name in (meta.get("tabs") or {}).items() if key in names})
    return names


def defaults_context(config: ResearchConfig, skill: str, source: Path = SOURCE_DIR) -> dict[str, Any]:
    languages = available_languages(source)
    english = tab_names()
    keys = config.tab_keys()
    translations: list[dict[str, Any]] = []
    for code in config.languages[1:]:
        meta = languages[code]
        names = tab_names(meta)
        translations.append({
            "code": code, "name": meta["name"], "label": meta.get("label") or meta["name"],
            "tabs": [names[k] for k in keys],
            "pairs": [f"{english[k]} → {names[k]}" for k in keys],
        })
    # What a user may leave off one sheet, in tab order; a language goes whole.
    optional = [english[k].lower() for k in ("overview", "price") if k in keys]
    optional += [str(t["name"]) for t in translations] + (["glossary"] if config.glossary else [])
    state = None
    if config.tax_state:
        rules = source / "overlays" / "state" / config.tax_state / skill
        state = {"code": config.tax_state, "name": STATE_NAMES[config.tax_state], "rules": rules.is_dir()}
    choices = [f"no {name}" for name in optional]
    optional_text = (", ".join(choices[:-1]) + " or " + choices[-1]) if len(choices) > 1 else "".join(choices)
    return {
        "tabs": [english[k] for k in keys], "translations": translations, "state": state,
        "optional_tabs": optional_text, "price": config.price, "glossary": config.glossary,
    }


def render_defaults(config: ResearchConfig, skill: str, source: Path = SOURCE_DIR) -> str:
    """The "Defaults for this installation" section for ``skill``."""
    import jinja2  # only when rendering: this module loads with every `analysis` command

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(source / "defaults"),
        trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined, autoescape=False,
    )
    return env.get_template(f"{skill}.md.j2").render(defaults_context(config, skill, source))


def replace_defaults(text: str, block: str, where: str) -> str:
    """``text`` with the section between the defaults markers set to ``block``."""
    start = text.find(BLOCK_START)
    end = text.find(BLOCK_END)
    if start < 0 or end < start:
        raise ResearchConfigError(f"{where}: no {BLOCK_START} ... {BLOCK_END} section")
    body = text.index("\n", start) + 1
    return text[:body] + block + text[end:]


def current_defaults(text: str) -> str:
    start = text.index("\n", text.index(BLOCK_START)) + 1
    return text[start:text.index(BLOCK_END)]


def update_neutral(source: Path = SOURCE_DIR) -> list[Path]:
    """Rewrite the committed skills' defaults blocks as a neutral render;
    returns the files changed."""
    changed = []
    for skill in skill_names(source):
        path = source / "skills" / skill / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        new = replace_defaults(text, render_defaults(NEUTRAL, skill, source), str(path))
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path)
    return changed


def render(config: ResearchConfig, out_dir: Path, source: Path = SOURCE_DIR) -> Path:
    """Write the plugin, configured by ``config``, to ``out_dir/finance-research``
    beside a marketplace listing it; returns the plugin's directory."""
    plugin = out_dir / PLUGIN_NAME
    if plugin.exists():
        shutil.rmtree(plugin)
    shutil.copytree(source, plugin, ignore=shutil.ignore_patterns("__pycache__", *RENDER_ONLY))
    for skill in skill_names(source):
        skill_dir = plugin / "skills" / skill
        overlays = [source / "overlays" / lang / skill for lang in config.languages[1:]]
        if config.tax_state:
            overlays.append(source / "overlays" / "state" / config.tax_state / skill)
        for overlay in overlays:
            if overlay.is_dir():
                shutil.copytree(overlay, skill_dir / "references", dirs_exist_ok=True)
        path = skill_dir / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(replace_defaults(text, render_defaults(config, skill, source), str(path)), encoding="utf-8")
    manifest = out_dir / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(_marketplace_json(), encoding="utf-8")
    return plugin


def _marketplace_json() -> str:
    return json.dumps({
        "name": LOCAL_MARKETPLACE,
        "description": "The finance-research plugin, rendered with this household's research.yaml.",
        "owner": {"name": "analysis"},
        "plugins": [{"name": PLUGIN_NAME, "source": f"./{PLUGIN_NAME}",
                     "description": "Tear sheets on funds, ETFs and companies from free public sources."}],
    }, indent=2) + "\n"


def write_skill_zips(plugin: Path, out_dir: Path) -> list[Path]:
    """One ``<skill>.skill`` archive per skill, for uploading to claude.ai."""
    zips = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for skill_dir in sorted((plugin / "skills").iterdir()):
        if not (skill_dir / "SKILL.md").exists():
            continue
        target = out_dir / f"{skill_dir.name}.skill"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(skill_dir.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    archive.write(path, Path(skill_dir.name) / path.relative_to(skill_dir))
        zips.append(target)
    return zips


def describe(config: ResearchConfig) -> str:
    languages = available_languages()
    names = ["English"] + [languages[code]["name"] for code in config.languages[1:]]
    state = STATE_NAMES[config.tax_state] if config.tax_state else "none"
    english = tab_names()
    tabs = [english[t].lower() for t in OPTIONAL_TABS if not getattr(config, t)]
    off = f"; no {' or '.join(tabs)} tab" if tabs else ""
    return f"tab languages {', '.join(names)}; tax state {state}{off}"


def _claude(*args: str) -> None:
    subprocess.run(["claude", "plugin", *args], check=True)


def _resolve_config(data_dir: Path | None, config_file: Path | None) -> tuple[ResearchConfig, str]:
    if config_file is not None:
        path = config_file
    else:
        if data_dir is None:
            env = os.environ.get("ANALYSIS_DATA_DIR")
            data_dir = Path(env) if env else None
        if data_dir is None:
            return NEUTRAL, "no data repo (neutral settings)"
        path = config_path(data_dir)
    try:
        config = read_config(path)
    except ResearchConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    return config, (str(path) if path.exists() else f"no {path} (neutral settings)")


_data_dir_option = click.option(
    "--data-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
    help=f"Data repo whose {CONFIG_FILENAME} to use (default: $ANALYSIS_DATA_DIR).",
)
_config_option = click.option(
    "--config", "config_file", type=click.Path(dir_okay=False, exists=True, path_type=Path), default=None,
    help=f"A {CONFIG_FILENAME} to use instead of the data repo's.",
)
_out_option = click.option(
    "--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
    help="Where to write the rendered plugin (default: out/finance-research in the main checkout).",
)


@click.group(name="research-plugin")
def research_plugin_command() -> None:
    """The finance-research Claude plugin (tear sheets on funds and companies).

    Each run of its skills browses the web for several minutes and uses
    around 150k tokens. See docs/guides/finance-research.md.
    """


@research_plugin_command.command(name="render")
@_data_dir_option
@_config_option
@_out_option
@click.option("--skill-zips", is_flag=True, help="Also write <skill>.skill archives for claude.ai uploads.")
@click.option("--neutral", is_flag=True,
              help="Rewrite the repo's own plugin with neutral settings instead (after editing a defaults template).")
def render_command(data_dir: Path | None, config_file: Path | None, out_dir: Path | None,
                   skill_zips: bool, neutral: bool) -> None:
    """Write the plugin configured by the data repo's research.yaml."""
    if neutral:
        changed = update_neutral()
        click.echo("Updated " + ", ".join(str(p.relative_to(REPO_ROOT)) for p in changed) if changed
                   else "The repo's plugin is already a neutral render.")
        return
    config, where = _resolve_config(data_dir, config_file)
    out_dir = out_dir or default_out_dir()
    plugin = render(config, out_dir)
    click.echo(f"Rendered {plugin} from {where}: {describe(config)}.")
    if skill_zips:
        for path in write_skill_zips(plugin, out_dir / "skills"):
            click.echo(f"Wrote {path}")


@research_plugin_command.command(name="install")
@_data_dir_option
@_config_option
@_out_option
def install_command(data_dir: Path | None, config_file: Path | None, out_dir: Path | None) -> None:
    """Render the plugin and install it for your user with Claude Code.

    Safe to re-run: after changing research.yaml, run it again and start a new
    Claude Code session.
    """
    if shutil.which("claude") is None:
        raise click.ClickException("claude (Claude Code) is not on PATH")
    config, where = _resolve_config(data_dir, config_file)
    out_dir = out_dir or default_out_dir()
    plugin = render(config, out_dir)
    click.echo(f"Rendered {plugin} from {where}: {describe(config)}.")
    _claude("marketplace", "add", str(out_dir.resolve()))
    _claude("install", f"{PLUGIN_NAME}@{LOCAL_MARKETPLACE}")


@research_plugin_command.command(name="uninstall")
def uninstall_command() -> None:
    """Remove the installed plugin and its local marketplace."""
    if shutil.which("claude") is None:
        raise click.ClickException("claude (Claude Code) is not on PATH")
    _claude("uninstall", f"{PLUGIN_NAME}@{LOCAL_MARKETPLACE}")
    _claude("marketplace", "remove", LOCAL_MARKETPLACE)


if __name__ == "__main__":
    research_plugin_command()
