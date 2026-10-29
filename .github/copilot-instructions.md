# Working in this repository

Personal finance analysis and planning: an Amazon expense categorizer and a
scenario-based finance simulator, usable from both a CLI and Jupyter notebooks.
Python 3.14+, `uv`, `src/` layout, single top-level project (no nested repos).

## Two repositories, one workflow

Code lives here. **Data lives in a separate git repository**, with the
simulator's inputs under its `sim/` subtree. Commands find it via
`$ANALYSIS_DATA_DIR` (set by sourcing the data repo's `activate-analysis`) or an
explicit `--data-dir`. Nothing in `data/`, `out/`, or `state/` here is tracked.

Never edit the data repo to make code or tests pass. If scenario data looks
wrong or stale, say so and let the user decide.

**Real names stay in the data repo.** Account, chain, person and payee names
from the real data must not appear in this repo's code, tests, docs,
`prototype_data/`, commit messages, issues, or PR text. Use the prototype
personas (Alice and Bill) and the prototype scenario's account names, and put
household-specific choices in data-repo files, not in defaults or literals.
`.githooks/blocked-keywords.txt` lists words that give such names away; the
pre-commit and commit-msg hooks block commits containing them, and
`tests/test_blocked_keywords.py` checks every tracked file. The `Gates`
workflow on GitHub checks tracked files, every commit message, and the pull
request's title and body. If you find one already in the repo, say so instead
of tidying or quoting it. A deliberate use can be let through locally with
`ALLOW_BLOCKED_KEYWORDS=1`; the GitHub check has no override.

## Architecture

Read the ADRs before changing the simulator; they are the specification, and
they are kept in sync with the code.

| Doc | Covers |
| --- | --- |
| [`docs/architecture/ADR-001-overall-architecture.md`](../docs/architecture/ADR-001-overall-architecture.md) | Design principles, priority bands |
| [`docs/architecture/ADR-002-storage-schema.md`](../docs/architecture/ADR-002-storage-schema.md) | Two-layer SQLite storage, DDL, glossary |
| [`docs/architecture/ADR-003-registries-and-csv-schemas.md`](../docs/architecture/ADR-003-registries-and-csv-schemas.md) | Locked vocabularies, every CSV schema |
| [`docs/architecture/ADR-004-reproducibility-and-activation.md`](../docs/architecture/ADR-004-reproducibility-and-activation.md) | Provenance, determinism, activation |
| [`docs/architecture/ADR-005-lots-and-per-lot-state.md`](../docs/architecture/ADR-005-lots-and-per-lot-state.md) | Lots and per-lot state |
| [`docs/architecture/ADR-006-ensemble-scenarios.md`](../docs/architecture/ADR-006-ensemble-scenarios.md) | Ensembles: parameter variation, Monte Carlo runs |
| [`docs/architecture/ADR-007-sensitivity-analysis.md`](../docs/architecture/ADR-007-sensitivity-analysis.md) | Sensitivity analysis over ensemble parameters |
| [`docs/architecture/ADR-008-correlated-parameters.md`](../docs/architecture/ADR-008-correlated-parameters.md) | Correlated parameters in ensembles |
| [`docs/architecture/ADR-010-responsive-strategies.md`](../docs/architecture/ADR-010-responsive-strategies.md) | Responsive strategies: code that decides a chain's draws and placements |

ADR-005 through ADR-008 each have a companion `ADR-00N-implementation-plan.md`
beside them.

An ADR is edited in place to describe the current design, so it stays easy
to read: rewrite the affected sections and add one bullet to the ADR's
closing `## Amendments` list: the issue (or the ADR) that changed it, a short
phrase saying what changed, and the sections affected. Add it at the end of the
list; when two branches both add one, resolving the conflict is keeping both. The
header's fixed "Amended" line only points to that list, so it never changes. The
bullet marks a change to the design; an editorial edit (an example's names, a
typo, a link) needs none. Don't keep
superseded text as tombstones or ask for it to be kept; git history holds the
previous decisions and their rationale. If the code and an ADR disagree, fix the drift in the
same change — the ADRs are load-bearing, not historical.

Key modules:

- `src/analysis/sim/registries.py` — the locked vocabularies (account kinds and
  subkinds, the nine `AMOUNT_RULE_KINDS`, tax characters, `CHAIN_KINDS`,
  `DEFAULT_PRIORITIES`, CSV headers). [ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) defines these; keep them paired.
- `src/analysis/sim/schema.py` — all SQLite DDL, `SCHEMA_VERSION`. Mirrors [ADR-002](../docs/architecture/ADR-002-storage-schema.md).
- `src/analysis/sim/loader.py` — data repo CSVs to `state/intent/<scenario>.sqlite3`.
  Validation belongs here: unresolvable references must fail loudly at load.
- `src/analysis/sim/engine.py` — monthly stepping to a run file under `state/runs/`.
- `src/analysis/sim/provenance.py` — code/data versions and `scenario_tree_id`.

Invariants that are easy to break:

- **Chains, not accounts, on both sides of a transfer.** Every account gets an
  implicit leaf chain at load, so `chain.name` is one namespace shared with
  account names. `scheduled_transaction.from_account`/`to_account`,
  `chain_steps.draw_from_chain_id`, and
  `account.refill_chain_id`/`overflow_chain_id` all reference `chain(id)` —
  never `account(id)`.
- **Refill is a per-debit look-ahead; overflow is a month-end sweep.** Neither
  is a rule, and neither has a priority band.
- **Insufficient funds does not halt the run.** The debit posts, the balance
  goes negative, a `RuntimeWarning` fires once per negative transition, and the
  event is recorded under
  `simulation_run.run_args_json.$.negative_balance_warnings` — visible and
  queryable, not fatal. A scenario must stay runnable past its first shortfall.
- **Money is integer cents everywhere past the loader.** CSVs are authored in
  decimal dollars; the loader converts. Don't leak floats inward.
- **Scenario inheritance is real.** Most scenarios set `parent_scenario` and
  ship only their overrides. Anything reasoning about "the files this scenario
  read" must walk the whole lineage, not just the scenario's own folder.

## CLI entry points

Three scripts are installed via `pyproject.toml` `[project.scripts]`:

| Script | Equivalent | Purpose |
| --- | --- | --- |
| `analysis` | — | Top-level; all subcommands |
| `amazon` | `analysis amazon` | Amazon expense categorizer |
| `sim` | `analysis sim` | Scenario simulator |

Use `.venv/bin/analysis` / `.venv/bin/amazon` / `.venv/bin/sim`, or `uv run ...`,
if the venv isn't on `PATH`.

## Verifying a change

Five gates should pass before reporting any change done, plus a sixth for
changes to the simulator or its loader:

```sh
.venv/bin/python -m pytest -q -n auto -m "not slow"  # tests, less the slow ones
.venv/bin/python -m ruff check src tests          # lint
.venv/bin/ty check                                # types (ty; src and tests, per pyproject.toml)
npm run spell                                     # spelling (cspell)
npm run markdownlint                              # markdown (markdownlint)
.venv/bin/analysis sim check --all                # simulator changes: real scenarios still load
```

`cspell` and `markdownlint` are pinned in `package.json` and installed by
`scripts/setup.sh` (`npm ci`); see [`docs/SETUP.md`](../docs/SETUP.md).

[`.github/workflows/gates.yml`](workflows/gates.yml) runs the first five gates,
with every test including the slow ones, and the blocked-keyword check on every
pull request that isn't a draft and every push to `main`. It skips the tests,
lint and types when a change touches only prose or editor settings (the list is
in the workflow).

Tests marked `@pytest.mark.slow` (about a second or more each) are left out of
the everyday local run. When a change touches a slow test or the code it
exercises, run that test locally too (`-m slow`, or the test by name). Mark a
new test `slow` when it takes about a second on its own.

Copilot reviews a pull request when it leaves draft, and every push after
that; Gates skips drafts too, since Actions minutes are billed. So open pull
requests as drafts and iterate there with the local gates. Before marking one
ready, merge `main` and run the whole suite locally, slow tests included
(`.venv/bin/python -m pytest -q -n auto`). Marking it ready starts Copilot's review and one Gates
run together; fix anything either finds. When a ready pull request needs
several more pushes (a merge from `main`, a batch of review fixes), turn it
back into a draft first, push, and mark it ready again: one review and one
Gates run per return to ready, not one per push.

Run a single test with `.venv/bin/python -m pytest tests/test_engine.py::test_name -q`
(or `-k pattern` to match by substring).

`ruff check` is clean on `main`; keep it that way.

`ty check` is clean on `main` too. Fix a type error rather than silencing it.
Where a library hides types from ty, use the existing helper: `named_rows()`
for `DataFrame.itertuples()` and `index_years()`/`index_months()` for a
`DatetimeIndex`'s date fields (both in `src/analysis/frames.py`), or
`_trait()` for a traitlets class attribute (`notebook_ui/pickers.py`). A
narrow `# ty: ignore[<rule>]` with a reason is the last resort.

## Conventions

- **`edit` means guided.** A `sim <type> edit` command prompts, with
  completion, for whatever its arguments and options leave out (with no
  options, it shows the fields and asks which to change). A command that only
  takes its changes as options is named `sim <type> change` instead, so the
  name says which kind it is.
- **Names complete, and are asked for.** An argument or option naming
  something a scenario defines takes a completing type from
  `sim/completion.py` (`type=completion.ACCOUNT`), so TAB offers the names.
  A name a command needs is optional on its line and, when left out, asked
  for with `cli._ask_name` (completion over `sim/names.py`'s readers). At a
  terminal, a given name that isn't among its type's choices is re-asked
  before the command runs; pass `check=False` to a `NameType` whose value may
  be new (a tag being added, a symbol). `tests/test_completion.py` and
  `tests/test_prompt_names.py` check all three.
- `uv` manages the environment; the venv is `.venv/`. Prefer `.venv/bin/...` or
  `uv run ...` over a bare `python`.
- Notebooks are stripped with `nbstripout`. Don't commit output cells or churn
  in kernel metadata.
- Comments explain *why*, at the density of the surrounding code. A comment
  restating the line below it is noise; one recording a decision or a trap is
  worth keeping.
- When a test's name stops describing what it asserts, rename it.
- Test data on disk comes from the fixtures in `tests/conftest.py`
  (`test_data_dir`, `intent_dir`, `runs_dir`, `retire_alice_csv`,
  `load_test_copy`, `append_calendar_row`, ...), never from a local helper or
  inline `shutil.copytree`/`tmp_path` setup. Check there before writing setup;
  a new reusable need becomes a new fixture. Its module docstring explains the
  session (shared, read-only) vs. function (private, mutation-safe) families.
- **Tabs are forbidden** in all text files (code, markdown, docs) — spaces only.
- Spell checking (cspell) runs over markdown, Python, CSV, and TOML (see
  `cspell.json`). Add project-specific words to `.cspell/project-words.txt`,
  kept sorted; for strings one test asserts verbatim, use an inline
  `# cspell:ignore` in that file instead.
- `data/`, `out/`, and `state/` are gitignored and protected by a pre-commit
  hook — never try to make code or tests pass by editing files there. The same
  hook, and a commit-msg hook, run `scripts/check-blocked-keywords.sh`.
