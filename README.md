# Financial Analysis and Planning

A retirement simulator for exploring your options and possible futures, at
home, at your own pace, and for free. A session with a financial planner
typically gives you one projection from one set of assumptions; here you can
compare what-if scenarios side by side, and run hundreds of simulated futures
to see the range of outcomes, not just one number.

- **Guidelines:** check a plan against the 4% rule, or let Guyton-Klinger
  guardrails adjust your spending
  ([guard rails](./docs/guides/guard-rails.md),
  [spending strategies](./docs/user-guide.md#417-spend-less-after-bad-years-spending-strategies)).
- **Taxes:** federal and state tax worked out each year
  ([how tax is handled](./docs/user-guide.md#3-how-tax-is-handled);
  [which states](#state-and-territory-income-tax)), including
  Medicare's IRMAA surcharge
  ([§4.14](./docs/user-guide.md#414-medicares-income-surcharge-irmaa)),
  required minimum distributions, Roth conversions
  ([§4.12](./docs/user-guide.md#412-make-a-new-investment-or-a-roth-conversion)),
  capital gains, and home sales
  ([§4.5](./docs/user-guide.md#45-selling-an-asset)).
- **Future changes to the rules:** a new tax table from the date a law takes
  effect, or a move to another state
  ([§4.8](./docs/user-guide.md#48-calendar-for-more-complex-changes)).
- **A range of outcomes:** ensembles vary returns, inflation, lifespans and
  more across hundreds of runs
  ([ensembles](./docs/guides/ensembles.md)).

Watch the *Retirement Simulator Guide* on
[YouTube](https://www.youtube.com/channel/UCQnUgmsanqEqAj1I-0i7vOA/), in
[English](https://www.youtube.com/playlist?list=PLMCxY-aRDiI4) and
[Japanese (日本語)](https://www.youtube.com/playlist?list=PLQhWLzBzvwNM).
Start with *Video 1: What it does, and how it works*
([English](https://www.youtube.com/watch?v=VvoRgRvgIiY),
[Japanese (日本語)](https://www.youtube.com/watch?v=kH_Uzpp4ghU)). It shows that a
simple model already answers useful questions. You add detail only when a
question needs it.

The repository also holds an Amazon expense categorizer. Both tools work from
a CLI and from Jupyter notebooks, on Python 3.14+ with uv.

To read a formatted `.md` file:
- In Jupyter: right-click on it and select `Open With / Markdown Preview`
- In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

## State and territory income tax

The simulator works out a state's income tax the same way for every state:
federal-style income, with capital gains taxed as ordinary income by
default, less the state's standard deduction, run through the state's
brackets ([how tax is handled](./docs/user-guide.md#3-how-tax-is-handled)). A
state needs two tables in `aux/`, `state_brackets__XX.csv` and
`state_standard_deduction__XX.csv`, a state tax, `taxes/state.csv`, and a
`residence.csv` saying where the household lives. `sim tax state` chooses the
state, and adds the state tax if the scenario has none; `sim tax move` records
a move to another state or county. Where a state treats capital gains differently, up to three more
tables say how: a share of long-term gain excluded, and gain rates of its own
([the `aux/` README](./templates/aux/README.md#a-states-capital-gains)).
Social Security is left out unless `state_social_security__XX.csv` says how
the state taxes it: the federally taxable amount less an exclusion that can
phase out with income. Two more are optional: `state_surcharge__XX.csv`, a
surtax on high income (California's 1% over $1 million, Massachusetts's 4%),
and `state_hsa__XX.csv`, for a state that taxes health savings accounts
(California). A local income tax on the state's taxable income, such as a
Maryland county's or New York City's, goes in a `state_local_tax` table
written for your own county or city, named for it in `residence.csv`; one with a base of its own, or one
only on wages, is left out. A state's retirement income exclusions, by age, kind of income
and household income, go in another optional table,
`state_retirement_exclusion__XX.csv`. A state's own deductions and credits (a renter's credit,
personal exemptions, a deduction for federal tax paid) are rows of
`state_adjustments__XX.csv`; California's ship. A rule that turns on the kind of
pension or plan (a public pension, an employer plan but not an IRA) is a
`state_adjustments` row on income from accounts you tag. Many states have rules
this leaves out; the table says which matter most for a retired household, and
[State income tax, state by state](./docs/guides/state-income-tax.md) lists each
state's rules and the tables that express them. It reflects state law as of
2026 (surveyed in November 2026); check the state's own revenue department
before relying on a figure.

| Status | Meaning |
| --- | --- |
| **Shipped** | The tables come with the simulator. |
| **No tax** | No income tax: leave out the state rule. |
| **Tables needed** | The model fits; write the state's tables ([which ones](./docs/guides/state-income-tax.md)). |
| **Partial** | The tables work, but the rules listed are not modeled, so the estimate is rough. |
| **Federal rule** | The territory's tax mirrors the federal one and is paid to the territory rather than the IRS; the federal tax stands in for it. |
| **Not supported** | A tax system of its own that the model can't express. |

For a shipped state, "Not modeled" lists what its shipped tables leave out:
an issue to close, or a row you write yourself.

| State | Status | Not modeled |
| --- | --- | --- |
| Alabama | Tables needed | |
| Alaska | No tax | |
| Arizona | Partial | Long-term gain subtraction limited to assets bought after 2011 |
| Arkansas | Partial | Low-income tax tables; spouses filing separately on one return ([#580](https://github.com/BobKerns/analysis/issues/580)) |
| California | Shipped | |
| Colorado | Partial | Social Security subtraction's age test ([#581](https://github.com/BobKerns/analysis/issues/581)) |
| Connecticut | Partial | Benefit recapture ([#549](https://github.com/BobKerns/analysis/issues/549)); personal tax credit; Social Security's 25% cap |
| Delaware | Partial | Spouses filing separately on one return ([#580](https://github.com/BobKerns/analysis/issues/580)) |
| District of Columbia | Shipped | Extra deduction at 65 ([#582](https://github.com/BobKerns/analysis/issues/582)); spouses filing separately on one return ([#580](https://github.com/BobKerns/analysis/issues/580)) |
| Florida | No tax | |
| Georgia | Tables needed | |
| Hawaii | Tables needed | |
| Idaho | Shipped | Public pension deduction, reduced by Social Security; refundable grocery credit ([#579](https://github.com/BobKerns/analysis/issues/579)) |
| Illinois | Tables needed | |
| Indiana | Tables needed | |
| Iowa | Partial | School district surtax (a share of state tax) |
| Kansas | Shipped | Public pension exemption; larger standard deduction from 65; personal exemption when itemizing ([#582](https://github.com/BobKerns/analysis/issues/582)) |
| Kentucky | Partial | Spouses filing separately on one return ([#580](https://github.com/BobKerns/analysis/issues/580)) |
| Louisiana | Tables needed | |
| Maine | Partial | Standard deduction phase-out's width grows with inflation (the law's divisor is fixed) |
| Maryland | Partial | Pension exclusion reduced by Social Security ([#579](https://github.com/BobKerns/analysis/issues/579)); capital gains surtax above $350,000 of income |
| Massachusetts | Tables needed | |
| Michigan | Partial | City income taxes (a base of their own) |
| Minnesota | Partial | Standard deduction phase-out's lower 3% band (approximated); 1% surtax on investment income over $1 million; Social Security subtraction's $4,000 steps (modeled as a straight line) |
| Mississippi | Tables needed | |
| Missouri | Tables needed | |
| Montana | Tables needed | |
| Nebraska | Shipped | Personal exemption credit ([#582](https://github.com/BobKerns/analysis/issues/582)) |
| Nevada | No tax | |
| New Hampshire | No tax | |
| New Jersey | Tables needed | |
| New Mexico | Tables needed | |
| New York | Partial | Benefit recapture ([#549](https://github.com/BobKerns/analysis/issues/549)); Yonkers surcharge (a share of state tax) |
| North Carolina | Shipped | Exclusion for government pensions vested before August 1989 (a row you write) |
| North Dakota | Tables needed | |
| Ohio | Tables needed | |
| Oklahoma | Tables needed | |
| Oregon | Tables needed | |
| Pennsylvania | Partial | Separate income classes; Philadelphia's tax on investment income |
| Rhode Island | Partial | Social Security exemption's full-retirement-age test ([#581](https://github.com/BobKerns/analysis/issues/581)) |
| South Carolina | Tables needed | |
| South Dakota | No tax | |
| Tennessee | No tax | |
| Texas | No tax | |
| Utah | Tables needed | |
| Vermont | Partial | Minimum tax of 3% of AGI over $150,000; capital gain exclusion's holding period |
| Virginia | Partial | Spouses filing separately on one return ([#580](https://github.com/BobKerns/analysis/issues/580)) |
| Washington | Shipped | Charitable and family-business deductions; credits for other states' taxes |
| West Virginia | Tables needed | |
| Wisconsin | Partial | Head of household's standard deduction switching to the single figure |
| Wyoming | No tax | |

| Territory | Status | Not modeled |
| --- | --- | --- |
| American Samoa | Not supported | Own tax code; local income exempt from federal tax ([#570](https://github.com/BobKerns/analysis/issues/570)) |
| Guam | Federal rule | |
| Northern Mariana Islands | Federal rule | Rebate of the tax on CNMI-source income; wage tax |
| Puerto Rico | Not supported | Own tax code; local income exempt from federal tax ([#570](https://github.com/BobKerns/analysis/issues/570)) |
| U.S. Virgin Islands | Federal rule | Economic Development Commission incentives |

## Directory Structure
- `src/analysis/` — Core package code
- `notebooks/` — Jupyter notebooks for exploratory analysis
- `data/` — User input data (ignored in git)
- `docs/` — Documentation, including `architecture/` and `guides/`
- `historical_data/` — Downloaded and captured economic data used to derive historical rates
- `out/` — Output data (ignored in git)
- `state/` — Persistent state (ignored in git)
- `tests/` — Unit tests run to verify any changes.

This repository is a single top-level Python project. There are no nested repos or subprojects.

## Setup
To install without git knowledge, paste this into a terminal:
```sh
curl -fsSL https://raw.githubusercontent.com/BobKerns/analysis/main/scripts/install.sh | sh
```
In a clone, run the setup script (idempotent — safe to re-run):
```sh
./scripts/setup.sh
```
See [docs/SETUP.md](./docs/SETUP.md) for what it does and the manual steps.

## Usage
- CLI tools: `python -m analysis ...`
- Notebooks: Open and run notebooks in `notebooks/`
- Videos: the guide videos linked above are made from
  [docs/videos/README.md](./docs/videos/README.md) (Docker needed).

Three CLI entry points are installed:

| Script | Equivalent | Purpose |
| -------- | ------------ | --------- |
| `analysis` | — | Top-level; all subcommands |
| `amazon` | `analysis amazon` | Amazon expense categoriser |
| `sim` | `analysis sim` | Scenario simulator |

Editing a scenario? See [docs/user-guide.md](./docs/user-guide.md) for a task-based walkthrough (adding accounts and lots, changing expenses, modeling a sale, chains, lifespans, calendar events).

All examples below use the short scripts (`amazon ...`, `sim ...`). You can also use `analysis amazon ...` / `analysis sim ...`, or `uv run amazon ...` if the venv is not on your `PATH`.

Notebook helper API (same logic as CLI):

```python
from analysis.amazon.notebook import load_categorization_state, run_amazon_categorization

result, annotated, summary = run_amazon_categorization("data/amazon_2026_01.csv")
categories, mapping = load_categorization_state("state")
```

### Shell Autocomplete (Click)

This CLI uses Click shell completion. Completions are available for all three scripts.

One-time for current shell session:

```sh
eval "$(_ANALYSIS_COMPLETE=zsh_source analysis)"   # zsh
eval "$(_AMAZON_COMPLETE=zsh_source amazon)"        # zsh
eval "$(_SIM_COMPLETE=zsh_source sim)"              # zsh
```

If the scripts are not on your `PATH`, use `uv run`:

```sh
eval "$(_ANALYSIS_COMPLETE=zsh_source uv run analysis)"   # zsh
eval "$(_AMAZON_COMPLETE=zsh_source uv run amazon)"        # zsh
eval "$(_SIM_COMPLETE=zsh_source uv run sim)"              # zsh
```

Persist in your shell config:

```sh
# zsh
echo 'eval "$(_ANALYSIS_COMPLETE=zsh_source analysis)"' >> ~/.zshrc
echo 'eval "$(_AMAZON_COMPLETE=zsh_source amazon)"' >> ~/.zshrc
echo 'eval "$(_SIM_COMPLETE=zsh_source sim)"' >> ~/.zshrc

# bash — replace zsh_source with bash_source above
```

Reload your shell after adding it:

```sh
exec zsh   # or: exec bash
```

In `sim`, TAB also completes the names a scenario defines: scenarios,
ensembles, accounts, lots, chains, scheduled rules, calendar rows, persons,
series, tags, symbols and itemizations. Names come from the scenario named on
the line (`--scenario`, or the command's scenario argument), else the current
scenario, and include what it inherits. A lot completes from the account
before it (`--to-lot` from TO_ACCOUNT). The data repo is `--data-dir` or
`$ANALYSIS_DATA_DIR`, which the data repo's `activate-analysis` sets.

A command run without a name it needs asks for it, listing the choices,
with the same completion at the prompt.
At a terminal, a name the scenario doesn't have (a typo) is asked for again
the same way; in a script or pipe the command fails with its usual error.

### Amazon Expense Categorizer

Run the interactive categorizer on one or more Amazon CSVs covering any date range:

```sh
amazon categorize data/amazon_2026_01.csv
```

Multiple files in one run:

```sh
amazon categorize data/Amazon-2026-03.csv data/Amazon-2026-04.csv
```

During interactive category prompts, press `Tab` to complete from known categories.

Non-interactive mode assigns unknown items to `UNCATEGORIZED`:

```sh
amazon categorize data/amazon_2026_01.csv --no-interactive
```

Review and re-categorize existing mapped items for a category:

```sh
amazon category review
```

Delete a category and remove mappings assigned to it:

```sh
amazon category delete
```

Both commands prompt you to select a category (with Tab completion).

Outputs:
- `out/Amazon-YYYY-MM_annotated.csv` in an `out/` subdirectory of the input directory by default
- `out/Amazon-YYYY-MM_summary.csv` in an `out/` subdirectory of the input directory by default
- Use `--output-dir` to choose another directory.

Why this exists:
- These monthly files are designed to align with credit-card billing cycles, not strict calendar months.
- This makes reconciliation and statement matching easier.

Monthly files use a billing cutoff on day 3:
- Dates on the 1st-3rd are grouped into that month.
- Dates on the 4th+ are grouped into the next month.

Use `--cutoff-day` to override the boundary (1-28):

```sh
amazon categorize data/amazon_2026_01.csv --cutoff-day 3
```

If a generated billing month is still in progress for today, the monthly filenames include `_pending`:
- `out/Amazon-YYYY-MM_pending_annotated.csv`
- `out/Amazon-YYYY-MM_pending_summary.csv`

Pending rule details:
- For month `YYYY-MM`, the billing interval is from `(previous cutoff + 1 day)` through `(this month cutoff)`.
- A file is marked `_pending` only if today's date falls inside that interval.
- If there are no rows in the open interval, no `_pending` files are written.

Safe reruns:
- It is safe to run `analysis amazon categorize` multiple times on the same input data.
- If the category mapping is unchanged, outputs and stored item assignments remain unchanged.
- If the category mapping changes, existing rows are updated in place to reflect the new mapping.

Persistent memory:
- `state/state.sqlite3`

### Importing the Expense Baseline

[Importing your data](docs/guides/importing-data.md) is the guide to
`sim import`, for balances and spending both; this section is the reference
for the spending files.

The baseline importer combines canonicalized Quicken expenses with Amazon
monthly summaries and replaces only rows it previously generated:

```sh
sim import transactions --from-account Asset_Checking --dry-run
sim import transactions --from-account Asset_Checking
```

The dry run lists each generated rule's amount before and after, and whether
it is added, changed, removed or unchanged. Generated rows are tagged
`generated:expense_baseline` and carry provenance in `meta_json`, so rerunning
removes stale generated categories after the private category maps change
while preserving manually authored scheduled rows.
A generated rule a parent scenario has, which a rerun in a derived scenario no
longer produces, is listed as `kept`: it stays in force until it is ended
there with `sim scheduled delete`.

The export to read is an argument (`sim import transactions export.csv`).
With none, and `ANALYSIS_DATA_DIR` pointing to the simulator data directory,
the command reads `../quicken/Transaction Export.csv` and the Amazon state in
`../state/`, both at the data repo's top level.

The household's choices live in the data directory's `imports/` folder
([ADR-003](docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.17), so
none of them live in the code:

- `imports/quicken/categories.csv`, columns `outside_category,category`
  (required): merges Quicken's categories into the baseline's. A blank or `-`
  `category` excludes one; a category with no row becomes `Other`.
- `imports/expense_accounts.csv`, columns `category,account`: the expense
  account each category's rule pays into, or `-` for no rule. If a category
  already matches a scenario expense account, no row is needed. At a terminal
  a category with no account is asked for and the answer saved; otherwise it
  is warned about and skipped. Supply the funding account explicitly with
  `--from-account`.
- `imports/quicken/splits.csv`, columns
  `category,outside_account,target_category` (optional): splits one category
  by the Quicken account that paid. A row with a blank `outside_account` is
  that category's default, taking every expense no named account claims,
  including Amazon summaries. For example, `Food,Alice Checking,Alice Food`
  and `Food,,Bill Food` divide Food between two people.
- `imports/quicken/excluded_payees.csv`, one column `payee_pattern`
  (optional): case-insensitive regular expressions for payees whose expenses
  the scenario models separately (an insurer, HOA dues). Amazon and mortgage
  payees are always excluded.

Amounts are monthly averages over the observed baseline months and use the
scenario start date by default.

Schema guide:
- `docs/sqlite-guide.md`

## Data Safety
All files in `data/`, `out/`, and `state/` are ignored by git and protected by a pre-commit hook.

## Spell Checking
Spell checking is handled by cspell (see `cspell.json` and `.cspell/project-words.txt`). Only documentation, markdown, and comments are checked.

## Coding Standards

**Tabs are forbidden in all text files.**

Use spaces for all indentation and alignment. Hard tab characters (\t) are not allowed anywhere in the repository, including code, markdown, and documentation. This prevents formatting issues and ensures consistency across all editors and platforms.
