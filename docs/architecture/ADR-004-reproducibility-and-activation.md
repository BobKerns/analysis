# ADR-004: Reproducibility, activation contract, and gating

- **Status:** Accepted
- **Date:** 2026-05-17
- **Supersedes:** —
- **Amended by:** [ADR-006](ADR-006-ensemble-scenarios.md) (ensemble scenarios): an ensemble member's provenance is this ADR's stamps unchanged (`scenario_tree_id` is still the *recipe's*, copied via the intent copy) plus a new `ensemble_manifest` row recording which member and `ensemble_draw` rows recording what it drew — not a new parallel determinism mechanism.
- **Related:** [ADR-001](ADR-001-overall-architecture.md) (overall architecture — summarizes reproducibility & activation), [ADR-002](ADR-002-storage-schema.md) (storage schema — `intent_manifest`, `simulation_run` stamps), [ADR-003](ADR-003-registries-and-csv-schemas.md) (registries used by `--check`).

## Context

[ADR-001](ADR-001-overall-architecture.md) establishes the rule: a run file must be reproducible from its stamps, and the only way to find or load a data repo is through environment variables set by a sourced `activate` script. This ADR fills in the mechanism — the exact stamp set, the dirty/clean gating logic, the override semantics, the activation contract on disk, and the failure modes the engine must produce.

Three goals constrain everything below:

1. **A run file is a self-describing artifact.** Six months from now, sharing a `.sqlite3` to a new machine should let `analysis sim explain <runfile>` work standalone, and `analysis sim verify <runfile>` should be able to attempt a regeneration if the matching code and data are available.
2. **Mixing data repos must be impossible by accident.** The household's real data and prototype demo data and a hypothetical second household's data all use the same code; the engine must refuse to compute against the wrong one silently.
3. **Dirty state is sometimes legitimate** (active editing during scenario authoring) but must always be **visible**. Stamps record it; UI shows it; `compare` and `verify` treat dirty-stamped runs as second-class.

## Decision

### 1. Stamp set on every run

The single-row `simulation_run` manifest ([ADR-002](ADR-002-storage-schema.md) §Ledger DDL) carries the following columns. The single-row `intent_manifest` carries the data-side subset so an intent rebuild can be confirmed against an existing run.

| column | type | source | notes |
| --- | --- | --- | --- |
| `id` | int | constant 1 | `CHECK (id = 1)` |
| `code_version` | text | `git -C <code_repo> rev-parse HEAD` | full 40-char SHA |
| `code_dirty` | int 0/1 | `git -C <code_repo> status --porcelain` non-empty | excludes untracked |
| `code_repo_path` | text | absolute path | informational |
| `data_version` | text | `git -C $ANALYSIS_DATA_DIR rev-parse HEAD` | full 40-char SHA |
| `data_dirty` | int 0/1 | `git -C $ANALYSIS_DATA_DIR status --porcelain` non-empty | scoped per §3 |
| `scenario_tree_id` | text | git tree SHA over scenario folder + referenced shared paths | per [ADR-002](ADR-002-storage-schema.md) §scenario_tree_id |
| `data_repo_path` | text | `$ANALYSIS_DATA_DIR` (resolved) | absolute |
| `data_repo_name` | text | `$ANALYSIS_DATA_REPO_NAME` | display label |
| `run_by` | text | `$ANALYSIS_USER` | validated; see §4 |
| `host` | text | `socket.gethostname()` | informational |
| `python_version` | text | `sys.version` short form | e.g. `3.14.0` |
| `started_at` | text | ISO-8601 UTC `Z` | run wall-clock start |
| `finished_at` | text | ISO-8601 UTC `Z` | NULL until terminal status |
| `status` | text | `running` / `succeeded` / `failed` / `halted` | terminal once non-`running`; matches [ADR-002](ADR-002-storage-schema.md) enum |
| `failure_reason` | text | NULL on success; structured message on halt/fail | per [ADR-001](ADR-001-overall-architecture.md) principle 7 |
| `allow_dirty_data` | int 0/1 | 1 only if `--allow-dirty-data` was passed | dual record alongside `data_dirty` |
| `allow_dirty_code` | int 0/1 | 1 if dirty code was present and accepted | dirty code is allowed by default; the column records "we knew" |
| `scenario_name` | text | argv | redundant with intent snapshot but stable for filtering |
| `run_args_json` | text | full argv as parsed | for replay |
| `engine_options_json` | text | non-default engine flags, sorted | excludes argv ordering noise |

**Determinism key** is `(code_version, scenario_tree_id)`. Two runs with the same key and the same `engine_options_json` must produce byte-identical ledger rows modulo non-deterministic stamps (`started_at`, `finished_at`, `host`, `run_by`). The `verify` command implements this comparison as a logical diff.

**Ensemble members ([ADR-006](ADR-006-ensemble-scenarios.md)).** A member's `scenario_name`/`code_version`/`data_version`/`scenario_tree_id` describe the *recipe* scenario it was resolved from (copied through the intent-file copy, untouched) — a member is not a scenario in its own right for reproducibility purposes. What makes a member reproducible is the ensemble's own master `seed` plus its `member_index`, from which every row's draw is deterministically re-derivable; `ensemble_manifest`/`ensemble_draw` record that separately, alongside the recipe's stamps, rather than inventing a second determinism key.

### 2. What "version" means for code and data

**Code version.** Always the HEAD SHA of the code repo containing the installed `analysis` package. Located at import time by walking up from `analysis.__file__` until a `.git` is found. If no `.git` is found (pip-installed from a wheel into a venv with no source tree), `code_version` is set to the package version string (e.g. `0.4.2+<install_hash>`) and `code_dirty` to 0; this case is flagged in `engine_options_json` as `{"code_source": "wheel"}` so `verify` can short-circuit.

**Data version.** Always the HEAD SHA of the data repo at `$ANALYSIS_DATA_DIR`. If the data dir is not a git repo at all (rare; only legitimate inside the prototype tree before it's been initialized), `data_version` is set to the literal string `"unversioned"` and the run is treated as dirty for gating purposes (§3).

**Scenario tree id.** Per [ADR-002](ADR-002-storage-schema.md), this is a content-addressed git tree SHA covering exactly the files the scenario load actually read: `scenarios/<name>/`, the folder of every scenario reached through `parent_scenario`, `persons.csv`, `aux/`, and `itemizations/`. Clean repo → use `git ls-tree HEAD -- <paths>` and `git mktree` to assemble the synthetic tree object. Dirty repo → use a sidecar index (`GIT_INDEX_FILE=<tmp> git add -- <paths>; git write-tree`) and optionally pin it under `refs/analysis-dirty/<tree-id>` so the object stays reachable through GC.

This means: edit one comment in an unrelated CSV, `scenario_tree_id` doesn't change. Edit a referenced file, it does. The key tracks what the scenario actually depends on, not the whole repo's state.

### 3. Gating logic at run start

The engine performs these checks in order, before any simulation work:

1. **Activation contract.** `$ANALYSIS_DATA_DIR` must be set and resolve to an existing directory. `$ANALYSIS_USER` must be set and must appear as a row `id` in `$ANALYSIS_DATA_DIR/persons.csv`. Failure → exit non-zero with a one-line "run `source activate` first" message.
2. **Code repo discovery.** Compute `code_version`, `code_dirty`. No gating on dirty code, but `code_dirty=1` always prints a one-line stderr banner: `WARNING: code repo dirty (uncommitted changes)`. Recorded as `allow_dirty_code = 1`.
3. **Data repo discovery.** Compute `data_version`, `data_dirty` scoped to the **scenario's input paths only** (the same path set used for `scenario_tree_id`). Changes elsewhere in the data repo are not considered for gating. A single `git status --porcelain -- <paths>` over the scoped set is the test.
4. **Dirty-data gate.** If `data_dirty = 1` and `--allow-dirty-data` was **not** passed: exit with `DataDirtyRefused: <list of dirty paths>`. No partial run is created. Suggested remediation message: "commit/stash the changes, or re-run with `--allow-dirty-data` to record an unreproducible run."
5. **Dirty-data accepted path.** If `data_dirty = 1` and `--allow-dirty-data` *was* passed: compute `scenario_tree_id` via the sidecar-index mechanism, set `allow_dirty_data = 1`, print stderr banner `WARNING: data dirty; run is not reproducible from <data_version> alone`. The run proceeds.
6. **User validation.** `$ANALYSIS_USER` is checked against the `id` column of `$ANALYSIS_DATA_DIR/persons.csv`. Unknown → exit `UnknownUser: <name>`. Persons and operator identities live in the same file (see [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.1); a row's `role` column is informational in v1 and is not gated on. Example rows:

   ```csv
   id,display,birthdate,role,meta_json
   rwk,RWK,1965-03-15,primary,{}
   mwk,MWK,1967-08-20,primary,{}
   ```

   Future ADRs may gate write operations by `role`.

### 4. The `activate` script

The data repo ships a `activate` shell script at its root (sourced from any shell), modeled on Python venv:

```sh
# In the data repo, sourced by the user:
#   source /path/to/data-repo/activate
```

Required behavior:

- Resolve its own location via `BASH_SOURCE`/`(%):-%x` so it works regardless of caller `cwd`.
- Set `ANALYSIS_DATA_DIR` to that absolute path.
- Set `ANALYSIS_DATA_REPO_NAME` to the basename, unless already set by the user.
- Prompt for `ANALYSIS_USER` if unset: read one line from `/dev/tty`, validate against `persons.csv` (column 1), re-prompt on miss, abort on EOF.
- Define `analysis_prompt_info`, which prints `[analysis:$ANALYSIS_DATA_REPO_NAME:$ANALYSIS_USER] `, and prefix `PS1` with a literal `$(analysis_prompt_info)` only if it is not already present (so re-sourcing is idempotent; zsh gets `setopt prompt_subst`). `deactivate-analysis` strips it again.
- Define a `deactivate` function that restores all saved values and unsets itself.

The script is shell-agnostic at the user-visible level (works in bash and zsh); fish gets a separate `activate.fish` if/when needed.

A Python equivalent `src/analysis/activate.py` is **not** sourceable but performs the same validations when called from `analysis sim`'s entry — so direct CLI use with manually-set env vars works without sourcing.

### 4a. The current scenario (`.current_scenario`)

The data repo records the scenario the user is currently working on in a `.current_scenario` file at its root (one line, the scenario folder name). It is per-user working state, not data: `activate` adds it to the data repo's `.gitignore`, and it is outside the scenario's scoped paths, so it never affects `data_version`/`scenario_tree_id` or dirty gating.

`analysis sim load <scenario>` writes the file; any command (or notebook) that takes an optional scenario resolves an omitted one via `analysis.sim.loader.resolve_scenario_name`, in this order:

1. the explicitly supplied name (validated against `scenarios/`),
2. the name recorded in `.current_scenario` (error if it names a missing scenario),
3. the sole scenario folder, if there is exactly one,
4. `base` (`loader.DEFAULT_SCENARIO_NAME`; the data repo's own root scenario is conventionally named this).

`resolve_scenario_name` returns a `ScenarioRef` named tuple — `name`, `data_dir`, `path` (the scenario folder), `intent_path` (its `state/intent` DB), `runs_dir` / `run_path` (the scenario's latest run file, or `None`), and the data repo's `commit` / `branch` / `dirty` — so callers can report exactly which inputs they are working with. Both state dirs are overridable. `ScenarioRef.describe()` renders that as markdown, and `_repr_markdown_` makes a bare `display(ref)` in a notebook show the same summary.

### 5. The `verify` command

`analysis sim verify <runfile>` attempts to reproduce a run file and report deviation:

1. Read `simulation_run` stamps from the run file.
2. Check `code_version` against the current code repo HEAD; if different, refuse with `CodeMismatch: runfile=<sha> current=<sha>`. (Reproducing requires checking out the matching code first.)
3. Check `data_version` and `scenario_tree_id` against the current data repo; if `scenario_tree_id` matches, proceed. If `data_version` matches but `scenario_tree_id` doesn't (theoretically impossible for clean runs but possible for `allow_dirty_data` runs whose sidecar tree object was GC'd), refuse with `DirtyScenarioMissing: tree=<sha>`.
4. Re-execute the run into a temp file with all stamps disabled except `code_version` and `scenario_tree_id`.
5. Logical-diff the two run files: every ledger table compared row-by-row sorted by natural key (`transfer_event`: `(date, reason_id, parent_event_id, amount)` etc). Identity-stamp columns excluded.
6. Exit `0` if zero rows differ; `1` with a unified diff if any do.

This is the operational definition of "reproducible from stamps."

### 6. The `check` command

`analysis sim check <scenario>` runs the lints listed across ADRs without simulating:

- [ADR-001](ADR-001-overall-architecture.md) principle 7: every reachable account is in the account model; every scheduled `from_account`/`to_account` resolves to an account or a chain; every chain step and every `refill_chain`/`overflow_chain` resolves.
- [ADR-003](ADR-003-registries-and-csv-schemas.md) §1: every account's `(kind, subkind)` is in the locked vocabulary. Catch-all subkinds (`other_*`) emit a non-fatal warning.
- [ADR-003](ADR-003-registries-and-csv-schemas.md) §2: every `amount_rule_kind` is registered; every required JSON param is present and typed; default priorities not crossed without explicit override (warn).
- [ADR-003](ADR-003-registries-and-csv-schemas.md) §3: every `calendar_event.kind` is registered; every payload validates; rule-synthesizing events reference accounts that exist.
- ADR-004 §1: a `--check stamps` mode dry-runs the stamp computation and reports what `scenario_tree_id` would be, so authors can confirm scope before committing.

Exit `0` if all checks pass; `1` if any error; `0` with `WARN:` lines if only warnings.

### 7. Comparison across runs

`analysis sim compare <runfile_a> <runfile_b>`:

- Reports the diff in `(code_version, scenario_tree_id, data_repo_name)` at the top so the human knows which axis varies.
- If `data_repo_name` differs, refuse with `CrossRepoCompare: <a> vs <b>` unless `--allow-cross-repo` is passed. Comparing a household run against a prototype run is almost always a mistake.
- Otherwise, ATTACH both files (per [ADR-002](ADR-002-storage-schema.md)) and run a fixed set of comparison queries: net worth trajectory, terminal balances by account, terminal tax-bucket totals.

### 8. Stamp-induced filename collisions

Run filenames embed `<timestamp>_<scenario>_<shortTreeId>` ([ADR-002](ADR-002-storage-schema.md) §File layout). If two runs collide on filename (sub-second invocations against an identical scenario), append `_<n>` starting at 2. The `simulation_run` row is the source of truth; the filename is for human convenience.

### 9. Migration / install behavior

- First-time install: `analysis sim load <scenario>` builds the intent file. If `state/` doesn't exist, it's created. If the data repo has no `persons.csv`, the engine refuses with a one-line how-to.
- Code upgrade with breaking schema change: the intent file is silently rebuilt (it's a cache; [ADR-002](ADR-002-storage-schema.md) makes this explicit). Run files are read-mostly with lazy additive migration on open; an ADR-bump that introduces a non-additive change requires a new run-file major version, which `verify` and `compare` refuse to cross. This is captured in `intent_manifest.schema_version` and `simulation_run.schema_version` (additive columns to add when the first such migration is needed).

## Amendments applied to [ADR-002](ADR-002-storage-schema.md) alongside this ADR

These amendments to `simulation_run` ([ADR-002](ADR-002-storage-schema.md) §Ledger DDL) accommodate the §1 stamp set; all rolled into [ADR-002](ADR-002-storage-schema.md):

1. Added `code_repo_path TEXT NOT NULL`.
2. Added `python_version TEXT NOT NULL`.
3. Added `allow_dirty_data INTEGER NOT NULL DEFAULT 0` and `allow_dirty_code INTEGER NOT NULL DEFAULT 0`.
4. Added `run_args_json TEXT NOT NULL DEFAULT '{}'` and `engine_options_json TEXT NOT NULL DEFAULT '{}'`.
5. Extended the `status` CHECK enum from `('running','succeeded','failed')` to `('running','succeeded','failed','halted')` to match [ADR-001](ADR-001-overall-architecture.md) principle 7 (halt-and-persist).

## Consequences

### Positive

- **Self-describing run files.** A `.sqlite3` carries every fact needed to identify, replay, or refuse to replay itself. No external metadata store, no companion files.
- **Cross-repo mixing is structurally impossible.** Data dir comes only from a sourced script; user is validated against the data repo's own user list; banners show which repo is active; `compare` refuses cross-repo without override. The pre-commit hook in the code repo ([ADR-001](ADR-001-overall-architecture.md)) closes the last gap.
- **Dirty state is visible, not silent.** Banners on every run with dirty state; stamps record the override; `compare`/`verify` know about it. Authoring stays fluid, but unreproducible runs can't pose as reproducible ones.
- **`scenario_tree_id` is scoped.** Changes elsewhere in the data repo don't invalidate unrelated scenarios. This matters in a multi-scenario household where one scenario is under active edit while another should remain reproducible.

### Negative / accepted

- **Dependency on git** is now load-bearing for reproducibility. A data repo not under git is treated as dirty/unversioned (no silent fallback to file mtimes). Acceptable: git is the simplest tool that solves "content-addressed identity for a folder," and the user already uses it.
- **`activate` is a shell script per shell family.** Bash/zsh share one; fish would need a separate file. Tolerable; the code repo's own venv has the same constraint.
- **Sidecar tree objects for dirty runs may leak** if the user never garbage-collects refs/analysis-dirty/*. Acceptable: tree objects are small and rare. The `verify` failure mode (`DirtyScenarioMissing`) is informative enough.
- **`run_by`, `host` aren't part of the determinism key**, so two users on two laptops running the same `(code_version, scenario_tree_id)` get logically-identical runs with different identity stamps. Compare logic ignores them; humans see them in `explain`. Tradeoff: byte-identical files would be nice but make every run's filename redundant with its stamps, defeating the human-readable-filename goal.

## Open questions deferred

- Whether `analysis sim verify` should optionally **auto-checkout** the matching `code_version` in a worktree to bridge a `CodeMismatch`. Compelling but adds significant complexity (must trust the runfile, must not touch the user's worktree). Defer to an ADR if the need is concrete.
- Whether dirty-code runs should also be refused by default under some condition (currently only warned). Will revisit after some real-use experience.
- Whether to capture `pip freeze` of the venv as part of the stamp set. Strict reproducibility argues yes; cost is a multi-KB JSON blob per run. Defer until a non-stdlib dependency actually affects ledger output (today: only `click`, which is interactive-surface-only).
