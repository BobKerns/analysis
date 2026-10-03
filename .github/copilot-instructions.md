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

ADR-005 through ADR-008 each have a companion `ADR-00N-implementation-plan.md`
beside them.

An ADR is edited in place to describe the current design, so it stays easy
to read: rewrite the affected sections and add a brief note (e.g. an "Amended
by" line in the header naming the issue or PR) that it was amended. Don't keep
superseded text as tombstones or ask for it to be kept; git history holds the
previous decisions and their rationale. If the code and an ADR disagree, fix the drift in the
same change — the ADRs are load-bearing, not historical.

Key modules:

- `src/analysis/sim/registries.py` — the locked vocabularies (account kinds and
  subkinds, the seven `AMOUNT_RULE_KINDS`, tax characters, `CHAIN_KINDS`,
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

Three gates, all of which should pass before reporting a simulator change done:

```sh
.venv/bin/python -m pytest -q                     # tests
.venv/bin/python -m ruff check src tests          # lint
for s in "$ANALYSIS_DATA_DIR"/scenarios/*/; do    # real scenarios still load
  .venv/bin/analysis sim check "$(basename "$s")"
done
```

Run a single test with `.venv/bin/python -m pytest tests/test_engine.py::test_name -q`
(or `-k pattern` to match by substring).

`ruff check` is clean on `main`; keep it that way.

`mypy` is not installed; there is no type-check gate.

## Conventions

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
- Spell checking (cspell) runs over docs, markdown, and comments only; add
  project-specific words to `.cspell/project-words.txt`.
- `data/`, `out/`, and `state/` are gitignored and protected by a pre-commit
  hook — never try to make code or tests pass by editing files there.
