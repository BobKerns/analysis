---
name: scenario-authoring
description: Create and change finance simulator scenarios and ensembles in the user's REAL data repo with the `sim` commands — derive a what-if from an existing scenario, add or change accounts, lots, expenses and income, make changes at future dates (retire, move, sell, a windfall, an investment, a Roth conversion), mix in household mix-ins or template episodes (great inflation, financial crisis, pandemic spike), build ensembles with varying rows, and pool them. Use for "make a scenario where...", "what if we...", "add an account", "starting in 2030...", "an ensemble with...", "mix in...". For questions about results, use analysis-results.
---

# Scenario authoring

Build what the user asks for as scenarios in their **real** data repo,
through the `sim` commands. Each command checks its change by loading every
affected scenario before writing, and writes nothing if the load fails. Paths
here are relative to the repo root.

The full how-to is [the user guide](../../../docs/user-guide.md) (§1 for
scenarios, §4 for common tasks), [ensembles](../../../docs/guides/ensembles.md)
and [historical calibration](../../../docs/guides/historical-calibration.md)
for episodes, and [ADR-003](../../../docs/architecture/ADR-003-registries-and-csv-schemas.md)
for every column. Every command's `--help` is accurate. Read the parts you
need rather than guessing.

## Ground rules

- **Agree the plan before writing.** Say which scenario you'll build on and
  what you'll change, as a short list of the commands, and wait for a yes.
  Once agreed, make small follow-up tweaks to the same new scenario without
  asking again.
- **Change shared scenarios only when asked.** A change to `base` or any
  other scenario reaches every scenario built on it. Put a what-if in a new
  scenario, unless the user asks you to change the existing one.
- **Use the commands, not hand edits.** A hand-edited CSV skips the check.
  Edit a CSV by hand only where no command can make the change, only with
  the user's OK, and run `sim check --all` straight after.
- **Don't commit or push the data repo** unless the user says to. Offer a
  commit message when you're done.
- **Real names stay out of the code repo**, as always. They are fine in the
  data repo and in your answer.
- **Close stdin**: give every command `</dev/null`. A command missing a
  value asks for it, and a closed stdin makes that `Aborted!` instead of a
  hang. If you see `Aborted!`, pass the option it asked for.

## Start of every command

```sh
source .claude/skills/scenario-authoring/env.sh
```

That activates the real data through the main checkout, and sets
`ANALYSIS_KEEP_SCENARIO`, so neither your lookups nor `scenario create`
move the user's current scenario. Name the scenario on every command
(`--scenario NEW`, or as an argument). When the new scenario is done,
`sim switch NEW` makes it current, since that's what the user asked for.

Read `$ANALYSIS_DATA_DIR/AI-NOTES.md` if it exists. It holds the household's
own conventions: scenario naming, which scenario is the main model, and what
its mix-ins mean.

## 1. Look before you build

```sh
sim scenario list --verbose </dev/null          # parents, dates, ensemble size; add --all for t_ templates
sim scenario show PARENT </dev/null             # lineage (load order), dates, latest run
sim account list --scenario PARENT </dev/null
sim scheduled list --scenario PARENT </dev/null
sim calendar list --scenario PARENT </dev/null  # "Defined in" says which scenario sets each row
sim chain list --scenario PARENT </dev/null
sim guard-rail list --scenario PARENT </dev/null
```

Use the names exactly as listed. Rules, rows and accounts are matched by name.

## 2. Pick the shape

| The user wants | Build |
| --- | --- |
| A what-if on an existing scenario | `sim scenario create NEW PARENT --description "..."`, then add only what differs |
| The same what-if with a different assumption | `sim scenario create NEW PARENT MIXIN...`: each mix-in adds its own rows, later ones winning |
| An alternative beside a scenario, not on it | `sim scenario copy OLD NEW`, then `sim scenario edit NEW --parents P:M1:M2` |
| A reusable assumption ("high healthcare") | a mix-in: `sim scenario create m_NAME BASE`, its rows, then `sim scenario disable m_NAME` so it isn't built alone |
| A range of outcomes | `sim ensemble create NEW PARENT [MIXIN...] --size N`, plus varying rows (§4) |
| Market and inflation that move year by year | add the `t_inflation_ensemble t_market_ensemble` mix-ins to an ensemble. Use `t_inflation_current t_market_current` to start from today's rates |
| A historical kind of trouble | an ensemble on that one, with an episode mix-in: `t_great_inflation`, `t_financial_crisis`, `t_pandemic_spike` |
| Several kinds of trouble, weighted | `sim ensemble pool create NAME E1=40% E2=60%`; change it later with `sim ensemble pool edit` |

**A mix-in must build on a scenario that has what it names.** Base a
household mix-in on the household's main scenario (often `base`), not
`--no-parent`. A no-parent scenario is checked on top of the empty `t_empty`
template, so a row naming an account fails there. When a mix-in is applied,
only its own rows come in: the walk stops at the first ancestor already
loaded.

## 3. Make the changes

| Change | Command (add `--scenario NEW </dev/null`) |
| --- | --- |
| An expense's amount, from the start | `sim scheduled edit RULE --amount 5000` (in a child, this writes an override) |
| An expense's amount, from a date | `sim calendar add set-amount 2032-07 RULE 2400 --description "..."` |
| A new recurring flow | `sim scheduled add 2031-01 FROM TO 400 --name NAME --end 2036-12 --series cpi` (`--kind` for SS, RMD, mortgage, `fill_bracket`, ...) |
| Stop a rule on a date | `sim calendar add retire-scheduled 2030-12 RULE`, or `sim scheduled edit RULE --end 2030-12` |
| Drop an inherited rule | `sim scheduled delete RULE --yes` (in a child, ends it before the start) |
| A rate from a date | `sim calendar add set-rate 2027-01 SERIES 4.5`; a negative one as `sim calendar add set-rate --scenario NEW -- 2033-01 SERIES -1` (options before the `--`) |
| A new account | `sim account add NAME asset cash --owner joint --opening-balance 10000` (always give `--owner`) |
| A holding at the start | `sim account lot add ACCOUNT LOT --opening-balance 5000` (or `--symbol --quantity`) |
| A one-time transfer: windfall, gift, sale | `sim transfer add 2032-06 FROM TO 25000 --tax-character none` |
| Move a whole balance (rollover) | `sim calendar add transfer-balance ...` |
| An investment later | `sim invest SOURCE ACCOUNT LOT --symbol SYM --amount 10000 --date 2027-03` |
| A Roth conversion, once or each year | `sim invest TRAD ROTH LOT --subkind roth_ira ...`, or `sim scheduled add 2029-01 TRAD ROTH --from-lot default --to-lot default --kind fill_bracket --rate 22% --end 2037-12` (needs a federal `tax_accumulator` rule in the lineage) |
| Moving state | `sim calendar add swap-aux-table ...` (user guide §4.8) |
| Lifespan | `sim person lifespan set PERSON 92` |
| Guard rails | `sim guard-rail add NAME --measure ... --side ... --basis ... --threshold ... --sense ...`, `sim guard-rail edit NAME --threshold ...` |
| The scenario's own dates, parents, size | `sim scenario edit NEW --start 2027-01 --parents P:M --size 200` |

Then replace the "Purpose" paragraph in `scenarios/NEW/README.md` with a sentence
or two on what the scenario is for. Listings and notebooks show its title
and first paragraph.

## 4. Ensembles

- **Varying row**: any `calendar add` kind, with
  `--name NAME --vary-target amount|date --vary-distribution uniform|triangular|truncated_normal|discrete`
  and that distribution's `--vary-low/--vary-high/--vary-mode/--vary-median/--vary-sd/--vary-outcomes`.
  Member 0 always takes the central value.
- **When an event happens**: a `date-marker` row that varies, with the
  event's rows dated `MARKER`, `MARKER+1y`, ... (ensembles guide, "Events
  with an uncertain start").
- **Placing an episode**: each episode template's start marker spans
  2031–2064. A scenario ending sooner sees no episode in many members, and
  even member 0 may miss it. Override the marker in the episode's ensemble
  (§5 shows how).
- **Size**: 30 is enough for a first look, 100 or more for a stable tail.
  Every member is a full run. Real ensembles of 1000 take about 5 minutes,
  and a pool runs any out-of-date component first. Tell the user before a
  long run, and run it in the background.
- Ensembles built on one parent ensemble inherit its seed and size. Member N
  of each then draws the same future, so `sim ensemble compare` pairs them.
  `--new-seed` breaks that; use it only on purpose.

## 5. Guided-only commands: pipe the answers

`sim calendar edit` (and `sim scenario edit` or `sim account lot edit` with
no options) have no option for each field. They show the row's fields and
ask `write this row, cancel, or name a field to change it`. Pipe field
names and values, ending with `write`:

```sh
printf 'vary-high\n2040-06-30\ndate\n2035-12-31\nwrite\n' |
    sim calendar edit great_inflation_start --scenario e_gi
```

Use the field names the review shows (`vary-high`, not `vary_high`). Read
the final review the command prints, and `git -C "$ANALYSIS_DATA_DIR/.." diff`,
before going on. Run out of input and the command stops with `Aborted!`,
writing nothing.

## 6. Check, run, hand over

```sh
sim check NEW </dev/null                     # or --all after changing a shared scenario
git -C "$ANALYSIS_DATA_DIR/.." status --short
sim run NEW --allow-dirty-data </dev/null    # a trial run before the user commits
ANALYSIS_KEEP_SCENARIO=1 sim compare PARENT NEW --section net-worth </dev/null
sim switch NEW </dev/null
```

- A run refuses uncommitted data (`data dirty; refusing to load`), because
  the run records which commit it came from. Use `--allow-dirty-data` for a
  trial. Once the user commits, a plain `sim run`, or the background
  builder, makes the run to keep.
- `WARN: ... latent_factors have no dependents yet` is normal for a scenario
  without the `_ensemble` templates.
- For a closer look at the results, use the analysis-results skill.
- End with: what you built (`sim scenario show NEW`), the files changed
  (`git status`), the first numbers, and a commit message to offer.

## Gotchas

- **An added row doesn't replace an inherited one.** Adding a second
  `retire_scheduled` for a rule that already retires in 2028 does nothing,
  since the earlier one fires first. To move or vary an inherited event,
  override that row with `sim calendar edit RULE --scenario NEW`, which keeps
  its name. An **unnamed** inherited row can't have its date moved, and no
  command can name it (#356). The only fix is a hand edit adding a `name` in
  the scenario that defines it, which is a shared scenario: ask first.
- **A child can't delete what it inherits.** `calendar delete`,
  `account delete` and `person lifespan clear` refuse. `scheduled delete`
  ends the rule instead, and `account lot delete` zeroes the lot.
- **`sim invest` with a new symbol**, like `sim symbol add`, writes the
  shared `securities.csv` and `allocations.csv`, which every scenario reads.
- **Renames reach every scenario** sharing the name, not just the one given.
- A guided `write` with nothing changed still rewrites the file, adding any
  newer columns. That's harmless, but it shows in `git diff`.
- `sim account add` asks for `--owner` even though it defaults to `joint`.
  Give it.
- A varying row needs `--name`, and the name must be one no other row uses.
- Two calendar rows alike in date, kind, rule, accounts, lots, series and
  amount are one event, so an exact duplicate is refused.

## Self-test

```sh
.claude/skills/scenario-authoring/walkthrough.sh
```

This replays every kind of command above on a throwaway copy of
`prototype_data/` (Alice and Bill), in about 30 seconds, and exits non-zero
at the first failure. Run it after changing this skill or the commands. It
never touches the real data.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `owner [joint]: Aborted!` (or another `...: Aborted!`) | A value was missing: pass that option |
| `Error: unknown to account or chain: 'X'` | X isn't in this scenario's lineage. Add it first, or build on a scenario that has it |
| `guard rail 'X' already exists` | It's inherited: `sim guard-rail edit X` writes an override |
| `Not written: this row comes from scenario 'P' and has no name...` | See the first gotcha (#356) |
| `data dirty; refusing to load` | `--allow-dirty-data` for a trial, or the user commits |
| `the scenario is already broken` | The parent fails to load as it stands. Report it and don't fix the data yourself |
