# ADR-004: Reproducibility, activation contract, and gating

- **Status:** Accepted
- **Date:** 2026-05-17
- **Supersedes:** —
- **Amended:** see [Amendments](#amendments) at the end.
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

**Scenario tree id.** Per [ADR-002](ADR-002-storage-schema.md), this is a content-addressed git tree SHA covering the data repo paths the scenario load can read: `scenarios/<name>/`, the folder of every scenario reached through `parent_scenario` (mix-ins included), `persons.csv`, `securities.csv`, `allocations.csv`, `strategies/` (the household's strategy code, [ADR-010](ADR-010-responsive-strategies.md) §4, which the intent also snapshots, so a run carries the code it ran), `aux/`, `prices/`, and `itemizations/`, less any scenario folder's `.disable` ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.1f), which says whether to build a scenario, not what it computes, and its `guard_rails.csv` (ADR-003 §4.11), which reports compare a finished run with, so nothing in a run reads it. Both are also left out of the dirty check: editing either never makes a run stale or stops a build. Clean repo → use `git ls-tree HEAD -- <paths>` and `git mktree` to assemble the synthetic tree object. Dirty repo → use a sidecar index (`GIT_INDEX_FILE=<tmp> git add -- <paths>; git write-tree`) and optionally pin it under `refs/analysis-dirty/<tree-id>` so the object stays reachable through GC.

A template scenario the data repo doesn't have, read from the code repository's `templates/scenarios/` ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.5a), is left out of that scope: it isn't in the data repo, and `code_version` (with `code_dirty`) already pins it.

This means: edit one comment in an unrelated CSV, `scenario_tree_id` doesn't change. Edit a referenced file, it does. The key tracks what the scenario actually depends on, not the whole repo's state.

### 3. Gating logic at run start

The engine performs these checks in order, before any simulation work:

1. **Activation contract.** `$ANALYSIS_DATA_DIR` must be set and resolve to an existing directory. `$ANALYSIS_USER` must be set and must appear as a row `id` in `$ANALYSIS_DATA_DIR/persons.csv`. Failure → exit non-zero with a one-line "run `source activate` first" message.
2. **Code repo discovery.** Compute `code_version`, `code_dirty`. No gating on dirty code, but `code_dirty=1` always prints a one-line stderr banner: `WARNING: code repo dirty (uncommitted changes)`. Recorded as `allow_dirty_code = 1`.
3. **Data repo discovery.** Compute `data_version`, `data_dirty` scoped to the **scenario's input paths only** (the same path set used for `scenario_tree_id`). Changes elsewhere in the data repo are not considered for gating. A single `git status --porcelain -- <paths>` over the scoped set is the test.
4. **Dirty-data gate.** If `data_dirty = 1` and `--allow-dirty-data` was **not** passed: exit with `DataDirtyRefused: <list of dirty paths>`. No partial run is created. Suggested remediation message: "commit/stash the changes, or re-run with `--allow-dirty-data` to record an unreproducible run."
5. **Dirty-data accepted path.** If `data_dirty = 1` and `--allow-dirty-data` *was* passed: compute `scenario_tree_id` via the sidecar-index mechanism, set `allow_dirty_data = 1`, print stderr banner `WARNING: data dirty; run is not reproducible from <data_version> alone`. The run proceeds.
6. **User validation.** `$ANALYSIS_USER` is checked against the `id` column of `$ANALYSIS_DATA_DIR/persons.csv`. Unknown → exit `UnknownUser: <name>`. Persons and operator identities live in the same file (see [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.1); a row's `role` column is informational in v1 and is not gated on: any `id` validates, though the prompt in §4 offers only the `primary` persons to choose from. Example rows:

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
- Take `ANALYSIS_USER` from the environment, else from the remembered `.analysis_user`. If it is unset, or `persons.csv` (column 1) has no such id, list the `primary` persons (id and display), read an id or display name, re-prompt on miss, abort on EOF, and remember the choice in `.analysis_user`. The list and prompt are `python -m analysis.sim.choose_user`, shared with `scripts/link-data.sh`.
- Define `analysis_prompt_info`, which prints `[analysis:$ANALYSIS_DATA_REPO_NAME:$ANALYSIS_USER:<scenario>]` and a trailing space, reading `<scenario>` from `.current_scenario` (§4a) at each prompt and leaving it and its colon out when there is none, and prefix `PS1` with a literal `$(analysis_prompt_info)` only if it is not already present (so re-sourcing is idempotent; zsh gets `setopt prompt_subst`). `deactivate-analysis` strips it again.
- Define a `deactivate` function that restores all saved values and unsets itself.

The script is shell-agnostic at the user-visible level (works in bash and zsh); fish gets a separate `activate.fish` if/when needed.

`scripts/link-data.sh` (run last by `scripts/setup.sh`, and by CI) links the two repositories: it symlinks the code repo's `data/shared` to the data repo, writes the code repo's path to `sim/.analysis` and asks which primary person the user is, as above. Given no path (a blank answer, `--template`, or no terminal), it instead builds `data/shared` from the code repo's `templates/` (whose `persons.csv` and `.analysis_user` supply a `test` person and whose one scenario is `base`), so tooling and tests always find the same layout.

A Python equivalent `src/analysis/activate.py` is **not** sourceable but performs the same validations when called from `analysis sim`'s entry — so direct CLI use with manually-set env vars works without sourcing.

### 4a. The current scenario (`.current_scenario`)

The data repo records the scenario the user is currently working on in a `.current_scenario` file at its root (one line, the scenario folder name). It is per-user working state, not data: `activate` adds it to the data repo's `.gitignore`, and it is outside the scenario's scoped paths, so it never affects `data_version`/`scenario_tree_id` or dirty gating.

Every `sim` command that takes an optional scenario treats it the same way (`cli._resolve_scenario`):

- **Named:** the name is validated against `scenarios/` and becomes the current scenario; when that changes it, the command says so on stderr, `Current scenario is now <name> (was <old>)`. A name that is only a reference — a new scenario's parent or mix-ins — does not; `sim scenario create` and `sim ensemble create` make the new scenario current instead, with the same notice. Commands whose scenario argument is required (`rename`, `delete`, `compare`, the ensemble reports) leave the file alone, except that renaming or deleting the current scenario follows it, with the same notice (`No scenario is current now (was <old>)` after a delete).
- **Omitted:** the name in `.current_scenario` is used (an error if it names a missing scenario), and the command says so with one `Using scenario <name>` line on stderr, once per command.
- **Omitted, with no `.current_scenario`:** a usage error asking for a name and listing the scenarios. The CLI never guesses.

The prompt (§4) shows the current scenario, so the default a command will take is always in view. `sim switch` prints it; `sim switch <name>` makes a scenario current with nothing else done, printing the old and new names.

Notebooks resolve an omitted scenario via `analysis.sim.loader.resolve_scenario_name`, which keeps fallbacks for a fresh data repo:

1. the explicitly supplied name (validated against `scenarios/`),
2. the name recorded in `.current_scenario` (error if it names a missing scenario),
3. the sole scenario folder, if there is exactly one,
4. `base` (`loader.DEFAULT_SCENARIO_NAME`; the data repo's own root scenario is conventionally named this).

### 4b. The working set (`state/builder/working_set`)

The scenarios most recently used are recorded, newest first, in `state/builder/working_set` (one scenario folder name a line), so the background builder (§12) builds what is likely wanted next first. It is per-user working state kept with the builder's other files, so, like them, it is untracked and outside every scenario's tree.

Using a scenario moves it to the top (`sim/working_set.py`):

- **CLI:** every scenario a command names or defaults to (`cli._resolve_scenario`), so every edit, and `sim switch` even when it leaves the current scenario unchanged. A command that compares scenarios (`sim compare`, `sim ensemble compare`) uses each, the baseline topmost; `sim ensemble summary` uses its ensemble. A name that is only a reference (a new scenario's parent or mix-ins) is not use, and nor is anything a background build does.
- **Notebooks:** picking a scenario or recipe in a picker (including the sensitivity and draws widgets') uses every scenario the picker then shows, the baseline topmost. The pick a picker starts on is not use, and it doesn't change `.current_scenario`.

Every write drops names that are no longer scenarios or are disabled ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.1f) and keeps the 50 most recent (`working_set.LIMIT`): further down, a name says nothing about what is wanted next. Creating, copying or renaming a scenario is use of the new name; `sim scenario delete` drops the deleted one. The file is replaced whole, so a reader never sees half of it; writing it is best effort, so a `state/` that can't be written, or a damaged file (read as empty), stops no command, and two writers racing can at worst lose one move to the top. The builder only reads it, ignoring a name that is no longer a scenario or is disabled, so it never has to write it.

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

Exit `0` if all checks pass; `1` if any error; `0` with `WARN:` lines if only warnings. Warnings about something defined but not read yet (an unused latent factor, a `set_rate` row nothing reads) are printed only by `sim check`; the other commands that load leave them out.

`analysis sim check --all` checks every scenario in the data repo in turn, each under a `Checking <scenario>` line. A failing scenario doesn't stop the rest; the command ends with a summary and exits `1` if any scenario failed. Giving both a scenario name and `--all` is a usage error. `--all` leaves out the defined-but-not-read warnings, which base and mix-in scenarios have many of by design, unless `--verbose` is given.

### 7. Comparison across runs

`analysis sim compare <runfile_a> <runfile_b>`:

- Reports the diff in `(code_version, scenario_tree_id, data_repo_name)` at the top so the human knows which axis varies.
- If `data_repo_name` differs, refuse with `CrossRepoCompare: <a> vs <b>` unless `--allow-cross-repo` is passed. Comparing a household run against a prototype run is almost always a mistake.
- Otherwise, ATTACH both files (per [ADR-002](ADR-002-storage-schema.md)) and run a fixed set of comparison queries: net worth trajectory, terminal balances by account, terminal tax-bucket totals.

### 8. Stamp-induced filename collisions

Run filenames embed `<timestamp>_<scenario>_<shortTreeId>` ([ADR-002](ADR-002-storage-schema.md) §File layout). Two runs of a scenario started in the same second would want the same name, so the timestamp takes `-<n>`, starting at 2 (`<timestamp>-2_<scenario>_<shortTreeId>`), as a batch folder's does. The name is claimed by creating the file exclusively, never by checking first, so two processes can't both get it (`locks.reserve_run_path`). Readers order runs by timestamp and then `<n>` (`locks.run_name_key`): plain name order would put `-2_` before `_`. The `simulation_run` row is the source of truth; the filename is for human convenience.

A load rewrites the scenario's intent files in place (`state/intent/<scenario>.sqlite3`, its `base_case.sqlite3`), so `sim load` and `sim run` hold an exclusive lock on the scenario (`state/intent/.locks/<scenario>.lock`, an `flock`) from the load to the end of the run. A second build of the same scenario waits, saying once on stderr who holds it; other scenarios build alongside. Pruning (§10) takes no lock; it already leaves anything under ten minutes old alone.

### 9. Install and upgrade behavior

- First-time install: `analysis sim load <scenario>` builds the intent file. If `state/` doesn't exist, it's created. If the data repo has no `persons.csv`, the engine refuses with a one-line how-to.
- Code upgrade with a schema change: the intent file is silently rebuilt (it's a cache; [ADR-002](ADR-002-storage-schema.md) §Schema changes). Run files are never migrated: re-run a scenario to compare it with runs made under the current schema. Each file records the `SCHEMA_VERSION` it was written under (`intent_manifest.schema_version`, `simulation_run.schema_version`).

### 10. Pruning `state/` (`.retention.yaml`)

Run files and ensemble batches accumulate under `state/runs/`, quickly with ensembles, and each batch leaves its member intents under `state/intent/<scenario>/<batch>/`. There is no prune command: `sim load` and `sim run` (and so `sim all`) prune after their own work, by rules in `.retention.yaml` at the root of the data dir (`$ANALYSIS_DATA_DIR`). When the file is missing, they first write it with the default rules below (`retention.DEFAULT_RETENTION`) and say so. The file is per-user housekeeping, not data: `activate` adds it to the data repo's `.gitignore`, like `.current_scenario` (§4a), so it never affects `data_version` or dirty gating. `templates/` and `prototype_data/` ship the same defaults, with no pins.

```yaml
intents:          # member-intent batches, state/intent/<scenario>/<batch>/
  - keep: 5
    since: 7 days
runs:             # single run files and run batches, state/runs/
  - keep: 5
    since: 7 days
pins:             # relative to the data repo root, the folder holding state/
  - state/runs/2026-10-17T214106Z_<scenario>_49373e7.sqlite3
logs:             # the background builder's daily log files, state/builder/logs/
  - keep: 7
    since: 30 days
```

- **What is counted.** Items are grouped by scenario and kind — single run files, run batches, intent batches — and ranked newest first by the timestamp in their names. Only names `sim run` writes are recognized (`<stamp>_<scenario>_<version>.sqlite3`, a `<stamp>[-N]` batch folder); anything else in `state/`, including a scenario's own intent file and its `base_case.sqlite3`, is never pruned.
- **Rules.** An item is kept when any rule in its section keeps it. A rule keeps it when any condition it gives holds: `keep: N` (among its group's N newest) or `since: <duration>` (made within that long; minutes, hours, days or weeks). So the defaults keep each scenario's five newest and everything from the last week, as backup tools combine keep-last and keep-within. A missing or empty section keeps everything of its kind.
- **Always kept.** Each group's newest item (a failed run's latest attempt included), and each scenario's newest successful run file and newest successful run batch (one with a batch manifest, no recorded failures, and every member `succeeded`), and anything written in the last ten minutes, which a concurrent `sim run` may still be writing.
- **Builder logs.** `logs` rules rank the background builder's daily log files (§12) newest first by the date in their names, and age each from the end of that day: a late write to an older file comes from a build that started that day, so it belongs there. The newest is always kept. The builder, not `sim load`, applies them, as it starts and as it starts each new day's file; it never writes a missing `.retention.yaml`, so without one (or without a `logs` section) every log file is kept.
- **Batches go whole.** A batch folder is deleted with all its members, never member by member. A pin naming a batch or anything inside it keeps the batch, and keeps both its run folder and its member intents.
- **Never fatal.** A malformed file is a warning naming the problem and prunes nothing; the load or run itself still succeeds. A run that fails still prunes afterwards, since its run file stays behind. Each prune prints one `pruned …` line when it deleted something.

### 11. `sim all` skips what can't have changed

A run is a function of its code, its Python and its data (§1-2), all recorded in `simulation_run`. So `sim all` skips a scenario when rerunning it couldn't change its numbers: its latest run (or Monte Carlo batch, whichever is newer) finished, and

- the code it ran with and the code now have the same trees for `src/`, `templates/`, `pyproject.toml` and `uv.lock`, the paths results depend on (by tree, not commit: a rebase or a docs-only commit changes nothing), with none of them dirty now and `code_dirty` 0 then;
- its Python version is the one running now;
- its `scenario_tree_id` is the scenario's now, with `data_dirty` 0 then and its scope clean now;
- for a batch, every member reached a final status and the batch has the scenario's own `ensemble_size` members (one made by `sim run --ensemble-size N` at another size isn't what `sim all` would make);
- for a pool ([ADR-009](ADR-009-pooled-ensembles.md) §5), which runs nothing itself: its definition and size are the ones its latest batch recorded, the data repo is clean with the tree recorded, and each component is unchanged by this section with the recorded batch still its latest. `sim all` builds pools after everything else, reusing a component it built in the same pass.

A disabled scenario ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.1f) isn't built at all; `sim all` names them once. A skipped scenario prints `unchanged since <run>; skipped`, and the summary counts them. `--force` reruns every scenario. A dirty tree is never trusted, on either side: its state is in no tree. A skipped scenario isn't reloaded either, so it doesn't become the current scenario (§4a). (`sim/freshness.py`.)

### 12. Background builds (`sim builder`)

A workspace pair (a code worktree and the data repo it links, §4) can keep every scenario's runs current in the background. One daemon per pair rebuilds whatever §11 says a rerun could change, so the background builds are what `sim all` would build.

- **Opt-in, per workspace.** `sim builder install [BRANCH]` checks that the pair names each other (the code worktree's `data/shared` is the data repo, and the data dir's `.analysis` names the code worktree), records it in the data repo's `state/builder/config.json`, and installs the data repo's hooks. `scripts/setup.sh` runs it when it links a data repo, except under CI or an agent. Template data isn't a git repo and can't opt in. `ANALYSIS_BUILDER=off` turns everything below off (the tests set it); `sim builder uninstall` opts out.
- **Triggers.** `post-commit`, `post-checkout`, `post-merge` and `post-rewrite` hooks in both repos (the code repo's in `.githooks/`; the data repo's in its hooks directory, a stub that finds its pair through the committing worktree's `sim/.analysis`; a hook already there, in any language, is renamed `<hook>.before-builder` and the stub runs it first with its arguments and input, keeping its exit status; when that name is taken too, `install` refuses and writes nothing) run `sim builder notify` in the background. It leaves a request and starts the daemon if none is running. A hook never blocks or fails the git command.
- **The daemon.** Its files are in `state/builder/` (one per data worktree): `daemon.lock`, held for its life (at most one per workspace); `status.json`; `requests/`; `logs/` (below); `working_set` (§4b). Each request makes it rebuild its queue from the trees as they are now (less disabled scenarios, ADR-003 §4.1f: nobody waits for one, and one being built when disabled is cancelled), so a checkout flushes and rebuilds the queue, and an unchanged tree just recreates it. It builds one scenario at a time, each as a child `sim load <scenario>` under the scenario's lock (§8), which doesn't change the current scenario (§4a). Order: scenarios someone is waiting on, builds they preempted, the working set's scenarios most recently used first (§4b; the current scenario counts as most recently used when the working set lacks it), then the rest, plain runs before ensembles and ensembles before pools ([ADR-009](ADR-009-pooled-ensembles.md)), then by name. Using a scenario reorders the queue at the next rescan but never stops a build: only waiting does. A scenario someone is waiting on preempts a build nobody is waiting on (one asked for while it runs counts as waited on): that build is cancelled and requeued right after it, so opening one scenario never waits on another's build. It exits after its queue has been empty for two minutes.
- **Code must match `main`.** It builds only while the code paths of §11 are committed and their trees equal the local `main` branch's. `sim builder install BRANCH` names another branch, recorded in `config.json` with the pair, so every shell, notebook and daemon of the workspace agrees (an environment variable could differ between them). Otherwise it is disabled: it builds nothing and nobody waits. A local branch, not `origin/main`: a background fetch, or choosing not to pull, changes nothing.
- **Stale builds stop.** A build whose code trees or scenario data tree no longer match is cancelled (its process group is killed; the partial run is left for §10) and requeued. A change to the code restarts the daemon, so it never builds with code it imported earlier. A scenario whose data scope is dirty waits. Every edit command (each writes through `staging.staged_edit`) asks a running daemon to look again once its edit is written, so the build of the edited scenario, or of one built on it, is cancelled at once; their data is now dirty, so they wait for a commit rather than being requeued. The daemon's status records each cancellation, of a build in progress or a queued one dropped because its data became dirty or it was disabled, and keeps them across restarts; a reader waiting on that build says it was cancelled, and why, rather than done.
- **Failures.** A failed build is recorded with the code and data trees it ran with, and isn't retried until one of them changes. So is a build that exits cleanly but leaves the scenario stale by §11, as an ensemble with failed members does.
- **Readers wait.** `latest_run_path`, `latest_ensemble_batch` and `scenario_run_path` (the CLI's reports and the notebooks) first ask whether a build of the scenario is pending: queued or building, or stale by §11 while the code matches `main`. If so, they move it to the front, start the daemon if needed, and wait, printing a line and progress every 30 seconds. Ctrl-C gives up the wait and opens the latest existing run. A reader asks again only once the daemon's status, the pair's `config.json`, or either repo's reflog or index has changed since it last asked. Only opening a run waits. Looking a scenario up (`resolve_scenario_name`) never does: its `ScenarioRef.run` is a `PendingRun`, which, like a future, waits only when its `result()` (`ref.run_path`) is asked for; `done()` asks whether it would wait and `latest()` reads the run on hand, as `ScenarioRef.describe()` does. So a command or `ScenarioConnection` that only names a scenario or reads its intent, edits included, never waits for the build an edit is about to make stale, while one that goes on to open the run (the reports, `sim explain`, `sim holdings` from a run) still waits for it. Listing what has runs (the notebooks' recipe lists and the controls built from them, `sim scenario show`, the CLI's choices of scenarios with runs) reads with `wait=False`, as `freshness` does. A build (`sim load`, `sim run`, `sim all`) never waits on the builder: it may hold the lock the builder's build of that scenario is waiting for. A run file is reserved empty (§8); readers skip it until it is written.
- **The log.** The daemon writes every line of it: its events unindented as `YYYY-MM-DD HH:MM:SS <text>` (local time: started, building, built with the time taken, failed or built incomplete, cancelled (after the rest of its output), stopped), and each build's output, read from the child's pipe, indented two spaces. A record is an event line and the lines after it up to the next. Only the daemon's own stderr (a traceback) writes other unindented lines; the daemon moves its stderr to each new day's file, and lines before a file's first event are dated by the file's last write. One file a day, `logs/builder-YYYY-MM-DD.log`, switched at the first event of the day so a record never straddles two files; together, oldest first, they are the log, pruned by `.retention.yaml`'s `logs` rules (§10). `sim builder log` shows records newest first, by default those of the last hour (`--since <duration>`, `-n N` the newest N however old, `-a` all); `-v` adds the build output, and without it a failure still shows the last ten lines of its build's output and an unindented line is always shown. `-f` prints the same records oldest first and then each line added, across the daily switch, until Ctrl-C (a build already running when it starts still gets its failure tail).
- **Commands.** `sim builder status` (running or not, why it's disabled, the build in progress, the queue, scenarios waiting on dirty data, failures), `start`, `stop`, `notify`, `log`, `install`, `uninstall`.

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

## Amendments

Each entry changed this ADR's design. The sections it names were rewritten in place to describe the current design; git history has the earlier text.

- [ADR-006](ADR-006-ensemble-scenarios.md) (ensemble scenarios): an ensemble member's provenance is this ADR's stamps unchanged (`scenario_tree_id` is still the *recipe's*, copied via the intent copy) plus a new `ensemble_manifest` row recording which member and `ensemble_draw` rows recording what it drew — not a new parallel determinism mechanism.
- [#59](https://github.com/BobKerns/analysis/issues/59): the scenario tree id covers mix-in scenarios' folders too.
- [#106](https://github.com/BobKerns/analysis/issues/106): `sim check --all`.
- [#131](https://github.com/BobKerns/analysis/issues/131): `scripts/link-data.sh` links the data repo at `data/shared` or builds template data there, and the user prompt lists the primary persons (§4).
- [#204](https://github.com/BobKerns/analysis/issues/204): only `sim check` prints defined-but-not-read warnings, and `sim check --all` only with `--verbose` (§6).
- [#214](https://github.com/BobKerns/analysis/issues/214): every CLI command naming a scenario makes it current, a defaulted one is announced, the CLI no longer falls back past `.current_scenario`, and the prompt shows the current scenario (§4, §4a).
- [#195](https://github.com/BobKerns/analysis/issues/195): `sim load` and `sim run` prune old runs and batches by the data repo's `.retention.yaml`, written with defaults when missing and kept out of git by `activate` (§10).
- [#236](https://github.com/BobKerns/analysis/issues/236): `sim switch` shows the current scenario or makes a named one current (§4a).
- [#233](https://github.com/BobKerns/analysis/issues/233): a command that changes the current scenario, including by renaming or deleting it, says so (§4a).
- [#282](https://github.com/BobKerns/analysis/issues/282): a template scenario read from the code repository is covered by `code_version`, not `scenario_tree_id` (§2).
- [#298](https://github.com/BobKerns/analysis/issues/298): `sim all` skips a scenario whose latest run has the same code trees, Python and data tree, all clean, and reruns it with `--force` (new §11).
- [#300](https://github.com/BobKerns/analysis/issues/300): run-file names collide-proof by exclusive create with a `-<n>` timestamp suffix, and a per-scenario build lock (§8); background builds (new §12); a scenario's `.disable` is outside its tree (§1) and `sim all` skips it (§11).
- [#309](https://github.com/BobKerns/analysis/issues/309): looking a scenario up never waits for its background build (`PendingRun`); an edit cancels the stale build of the scenario and those built on it (§12).
- [#314](https://github.com/BobKerns/analysis/issues/314): the builder's log is records of indented build output under timestamped events, one file a day pruned by a `logs` retention section; `sim builder log` shows recent records newest first (§10, §12).
- [#322](https://github.com/BobKerns/analysis/issues/322): a working set of recently used scenarios, `state/builder/working_set` (new §4b), orders the background builder's queue (§12).
- [#305](https://github.com/BobKerns/analysis/issues/305): a scenario folder's `guard_rails.csv` is outside its tree and the dirty check (§1).
- [#339](https://github.com/BobKerns/analysis/issues/339): the data repo's `strategies/` folder is in a scenario's tree (§2).
- [#292](https://github.com/BobKerns/analysis/issues/292) ([ADR-009](ADR-009-pooled-ensembles.md)): a pool is unchanged when its definition and data tree are as recorded and each component is unchanged with the batch it took; pools build after ensembles (§11, §12).
