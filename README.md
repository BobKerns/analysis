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
`state_standard_deduction__XX.csv`, and a `tax_accumulator` rule with `scope`
`state` that names the state. `sim tax state` chooses the state, and adds the
rule if the scenario has none; `sim tax move` records a move to another
state. Where a state treats capital gains differently, up to three more
tables say how: a share of long-term gain excluded, and gain rates of its own
([the `aux/` README](./templates/aux/README.md#a-states-capital-gains)).
Social Security is left out unless `state_social_security__XX.csv` says how
the state taxes it: the federally taxable amount less an exclusion that can
phase out with income. Two more are optional: `state_surcharge__XX.csv`, a
surtax on high income (California's 1% over $1 million, Massachusetts's 4%),
and `state_hsa__XX.csv`, for a state that taxes health savings accounts
(California). A state's retirement income exclusions, by age, kind of income
and household income, go in another optional table,
`state_retirement_exclusion__XX.csv`; a "Partial" row below whose gap is such
an exclusion needs that table written. Many states have rules this leaves
out; the table says which matter most for a retired household. It reflects
state law as of 2026; check the
state's own revenue department before relying on a figure.

| Status | Meaning |
| --- | --- |
| **Shipped** | The tables come with the simulator. |
| **No tax** | No income tax: leave out the state rule. |
| **Tables needed** | The model fits; write the state's tables. |
| **Partial** | The tables work, but the rules listed are not modeled, so the estimate is rough. |
| **Federal rule** | The territory's tax mirrors the federal one; the federal rule stands in for it. |
| **Not supported** | A tax system of its own that the model can't express. |

| State | Status | Not modeled |
| --- | --- | --- |
| Alabama | Partial | Deduction for federal income tax paid; pension exclusion |
| Alaska | No tax | |
| Arizona | Partial | Long-term gain subtraction limited to assets bought after 2011 |
| Arkansas | Partial | Retirement income exclusion |
| California | Shipped | Personal exemption credits |
| Colorado | Partial | Pension and annuity subtraction; Social Security taxed below 65 |
| Connecticut | Partial | Social Security's 25% cap above the income limit; pension and IRA exemptions phased by income; exemption phase-outs |
| Delaware | Partial | Pension exclusion |
| District of Columbia | Shipped | Extra deduction at 65 |
| Florida | No tax | |
| Georgia | Partial | Retirement income exclusion from age 62 |
| Hawaii | Partial | Pension exemption |
| Idaho | Shipped | Public pension deduction from 65; grocery credit |
| Illinois | Partial | Retirement income and IRA distributions exempt |
| Indiana | Partial | County income tax; age exemptions |
| Iowa | Partial | Retirement income exempt from age 55 |
| Kansas | Shipped | Public pension exemption; larger standard deduction from 65 |
| Kentucky | Partial | Pension exclusion; local taxes |
| Louisiana | Partial | Retirement income exclusion from age 65 |
| Maine | Partial | Pension deduction |
| Maryland | Partial | County income tax; pension exclusion; capital gains surtax |
| Massachusetts | Partial | Personal exemptions |
| Michigan | Partial | City income tax; retirement deduction |
| Minnesota | Partial | Social Security subtraction phases out in $4,000 steps (modeled as a straight line) |
| Mississippi | Partial | Retirement income and IRA distributions exempt |
| Missouri | Partial | Short-term gain exemption; pension exemptions |
| Montana | Tables needed | |
| Nebraska | Shipped | Personal exemption credit |
| Nevada | No tax | |
| New Hampshire | No tax | |
| New Jersey | Partial | Pension and IRA exclusion with an income cliff; personal exemptions |
| New Mexico | Tables needed | |
| New York | Partial | Pension and IRA exclusion from age 59½; New York City and Yonkers taxes |
| North Carolina | Shipped | Exclusion for government pensions vested before August 1989 |
| North Dakota | Tables needed | |
| Ohio | Partial | Municipal and school district taxes; retirement credits |
| Oklahoma | Partial | Retirement income exclusion |
| Oregon | Partial | Federal tax subtraction; retirement credit; Portland-area taxes |
| Pennsylvania | Partial | Retirement income exempt; separate income classes; local taxes |
| Rhode Island | Partial | Social Security exemption's full-retirement-age test; pension modification |
| South Carolina | Partial | Retirement and age deductions |
| South Dakota | No tax | |
| Tennessee | No tax | |
| Texas | No tax | |
| Utah | Partial | Taxpayer credit in place of a deduction |
| Vermont | Tables needed | |
| Virginia | Partial | Age deduction from 65 |
| Washington | Partial | No income tax, but a capital gains tax (retirement accounts and homes exempt) |
| West Virginia | Partial | Senior income exclusion |
| Wisconsin | Partial | Retirement income exclusion |
| Wyoming | No tax | |

| Territory | Status | Not modeled |
| --- | --- | --- |
| American Samoa | Not supported | Own tax code; local income exempt from federal tax |
| Guam | Federal rule | Tax is paid to Guam rather than the IRS |
| Northern Mariana Islands | Partial | Rebates of the mirrored tax on local income |
| Puerto Rico | Not supported | Own tax code; local income exempt from federal tax |
| U.S. Virgin Islands | Partial | Local surcharges and credits on the mirrored tax |

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

The baseline importer combines canonicalized Quicken expenses with Amazon
monthly summaries and replaces only rows it previously generated:

```sh
sim scheduled import-baseline --scenario current --dry-run
sim scheduled import-baseline --scenario current
```

Generated rows are tagged `generated:expense_baseline` and carry provenance in
`meta_json`, so rerunning removes stale generated categories after the private
category maps change while preserving manually authored scheduled rows.

When `ANALYSIS_DATA_DIR` points to the simulator data directory, the command
reads Quicken and Amazon inputs from sibling top-level data-repo directories:
`../quicken/` and `../state/`. The command expects an optional
`quicken/expense_account_map.csv` with columns
`category,target_account`. If a canonical category already matches a scenario
expense account, no map row is needed. Supply the funding account explicitly
with `--from-account`. Invalid target accounts are warned about and skipped.

Two more optional files in `quicken/` hold household-specific choices, so none
of them live in the code:

- `expense_category_split.csv`, columns `category,account,target_category`:
  splits one canonical category by the Quicken account that paid. A row with a
  blank `account` is that category's default, taking every expense no named
  account claims, including Amazon summaries. For example, `Food,Alice
  Checking,Alice Food` and `Food,,Bill Food` divide Food between two people.
- `expense_excluded_payees.csv`, one column `payee_pattern`: case-insensitive
  regular expressions for payees whose expenses the scenario models separately
  (an insurer, HOA dues). Amazon and mortgage payees are always excluded.

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
