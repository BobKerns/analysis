# Finance research: tear sheets with Claude

> To read a formatted `.md` file:
> - In Jupyter: right-click on it and select `Open With / Markdown Preview`
> - In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

The `finance-research` Claude Code plugin researches a security from free
public sources and writes a **tear sheet**: a one-page summary, a Price
chart, a Detail tab with the evidence, an Overview for a newcomer and a
Glossary, each optionally also in another language. It has two skills:

- **`fund-tear-sheet`**, for mutual funds, ETFs, money market, bond,
  target-date and thematic funds. It also works out the fund's asset-class
  mix in the simulator's own classes, as rows ready for `allocations.csv`
  ([user guide §4.11](../user-guide.md#411-add-a-stock-or-fund)).
- **`public-tear-sheet`**, for companies, public or private.

Each run browses the web for several minutes and uses around 150k tokens.
That's why the plugin is opt-in: nothing loads until you install it.

## Install it

```sh
.venv/bin/analysis research-plugin install
```

`scripts/setup.sh` offers this once, defaulting to no, when `claude` is on
your `PATH` (never under CI or an agent). The command reads your data repo's
`research.yaml` (below), writes a configured copy of the plugin to
`out/finance-research/` in the main checkout, and installs it for your user
from there. It says which settings it used.

Claude Code reads the installed plugin in place, so don't delete
`out/finance-research/`. After changing `research.yaml`, run the command
again (setup does, once the plugin is installed) and start a new Claude Code
session.

Then ask in Claude Code, from any folder: "tear sheet on ABCDX". To remove
it:

```sh
.venv/bin/analysis research-plugin uninstall
```

**Without the household settings.** The repo's own copy,
`plugins/finance-research/`, uses neutral settings (English only, no tax
state). The repo registers it in `.claude/settings.json` as the `analysis`
marketplace; once you trust the folder in Claude Code, `/plugin install
finance-research@analysis` installs it. This is the copy a collaborator
gets.

**In claude.ai.** `analysis research-plugin render --skill-zips` also writes
`out/finance-research/skills/<skill>.skill`, one archive per skill, to
upload in claude.ai's skill settings.

## Settings: `research.yaml`

The settings are a household's choice, so they live in the data repo, as
`research.yaml` beside `allocations.csv` (`$ANALYSIS_DATA_DIR/research.yaml`):

```yaml
languages: [en, ja]   # tab languages; en first. Each other language adds a
                      # copy of every tab in that language.
tax_state: CA         # two-letter state code for state-tax notes on money
                      # market and Treasury income; leave out for none.
tabs:
  overview: true      # false drops the overview, in every language
  price: true         # false drops the price chart, in every language
  glossary: true      # false drops the glossary, in every language
```

Every setting is optional. With no file, the plugin is neutral: English
only, and state-tax points say that the treatment depends on your state,
with no state rate bands. Only Japanese (`ja`) has a translation file
today.

**Translated tabs.** With `ja` listed, each sheet gets a Japanese copy of
every tab, written from the finished English tabs. In a Claude doc they
come after the English tabs, nested under one parent tab ("日本語: 概要",
which holds the Japanese overview, with ティアシート, 価格チャート, 詳細 and 用語集
under it). The first line of each tab links to the same tab in the other
language, so switching language keeps your place. Numbers and dates stay
exactly as on the English tabs, never converted; the Overview is rewritten
for the reader, and the other tabs are translated faithfully. Expect a run
with a translation to use roughly a third to a half more tokens, most of it
on the Detail tab.

**Price chart.** Month-end closes for ten years (or since inception), with
a few lines on how to read them. A money market fund (held at $1.00) and a
private company get none. In the Markdown files the data is
`<TICKER>-prices.csv` beside the tabs.

A setting the plugin can't follow (a language with no translation file, a
misspelled key) stops `render` and `install` with a message naming it.

## How it is put together

```text
plugins/finance-research/
  .claude-plugin/plugin.json   name and version
  skills/<skill>/              SKILL.md, references/, scripts/: the neutral plugin
  defaults/<skill>.md.j2       each SKILL.md's "Defaults for this installation" block
  overlays/<lang>/             a translation: language.yaml (its tab names),
                               then <skill>/translate-<lang>.md
  overlays/state/<CODE>/       a state's tax rules: <skill>/state-tax.md
  evals/, evals-neutral/       test cases (below)
.claude-plugin/marketplace.json  the repo's "analysis" marketplace
```

Rendering copies `skills/`, rewrites each skill's defaults block from its
template, and copies the chosen overlays into the skill's `references/`.
Neither `defaults/` nor `overlays/` ships in a rendered copy. A state with
no rules file still works: the skill finds that state's rules itself.

To change the skills' text, edit `skills/` directly. To change a defaults
block, edit its template, then run
`.venv/bin/analysis research-plugin render --neutral` to update the
committed block; a test fails until they match, and another fails if any
Japanese text appears outside `overlays/`. To add a language, add
`overlays/<code>/language.yaml` (its English name, its parent-tab label and
a name for each tab) and a `translate-<code>.md` for each skill. `public-tear-sheet` is under the Apache
License 2.0 (its `LICENSE` file); the rest is under this repo's MIT license.

## What it reads and writes

- **Reads:** public web pages (issuer documents, SEC filings, quote and news
  sites), and `research.yaml`, only when the plugin is rendered. It never
  reads your scenarios or accounts.
- **Writes:** a Claude doc, or Markdown files in the folder Claude Code is
  working in when the docs connector isn't available. A fund's
  `<TICKER>-allocations-sim.csv` holds rows in `allocations.csv`'s format.
  `sim symbol research` (below) asks before writing them to the data repo;
  from an interactive session, copying them, or running the
  `sim symbol add` they describe, is up to you.

## Evals

Each skill has test cases in the `claude plugin eval` layout. They are not
part of the gates: every case is a full research run. The cases in `evals/`
expect the settings in `evals/research.yaml` (Japanese tabs, California),
so render with those first; `evals-neutral/` checks the neutral plugin:

```sh
.venv/bin/analysis research-plugin render \
  --config plugins/finance-research/evals/research.yaml --out out/finance-research-eval
claude plugin eval out/finance-research-eval/finance-research
claude plugin eval plugins/finance-research --eval-dir evals-neutral
```

Add `--case <glob>` to run one case.

## Headless runs

`claude -p "<request>" --plugin-dir out/finance-research/finance-research`
runs a skill without an install.

`sim symbol research ABCDX` does this for a fund, or a company, by ticker
or by name, and takes it from there
([user guide §4.11](../user-guide.md#411-add-a-stock-or-fund)). It renders
the plugin afresh from `research.yaml` into a temporary folder (or uses
`--plugin-dir`), runs the skill in a temporary folder, and stops it
after `--timeout` minutes (20 by default), along with anything it started.
It prints a line per step, and the cost when Claude Code reports one.

The run reads web pages nobody vetted, so a page with instructions hidden
in it must not be able to act on your computer. The run is confined:
Claude Code's `--restricted` mode (your own settings, which may allow more,
are ignored, and file tools stay in the run's folder), no MCP servers, and
`dontAsk`, which refuses anything not on the allow list instead of
prompting. The list allows web search and fetch, reading the run's folder
and the plugin, writing only in the run's folder, and running only the
skill's own scripts, by their path in the plugin.

Nothing here holds a key or calls the API itself: every run uses Claude
Code's own login. Signed in with a claude.ai account, runs count against
the plan's usage limits; with `ANTHROPIC_API_KEY` set, Claude Code bills
that key per token instead, and a run's ~150k tokens cost real money.
