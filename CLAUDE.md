# Claude Configuration

Project guidance (architecture, invariants, verification gates, conventions) is
shared with Copilot; edit it there, not here:

@.github/copilot-instructions.md

## Branches and worktrees

- Branch only when the live branch (`git branch --show-current`) is `main`;
  otherwise the current branch is the one to use.
- A branch for a GitHub issue is `issue/<number>-<brief-kebab-description>`
  (e.g. `issue/40-cert-verify`). Never name a branch plain `issue`.
- Base new branches on `origin/main` after `git fetch`, not local `main`, and
  don't update local `main`.
- Keep branches after their PR merges, locally and on GitHub.
- Work in a worktree at `../analysis-worktrees/<branch with / flattened to ->`,
  not in the main checkout. Remove the worktree (not the branch) once its PR
  is merged.

The nbstripout filter runs `<toplevel>/.venv/bin/python`, which a fresh worktree
lacks, so bypass the filter for the initial checkout only:

```sh
git fetch origin
git -c filter.nbstripout.clean=cat -c filter.nbstripout.smudge=cat \
    -c filter.nbstripout.required=false \
    worktree add --no-track -b <branch> ../analysis-worktrees/<flattened-branch> origin/main
cd ../analysis-worktrees/<flattened-branch> && env -u VIRTUAL_ENV uv sync
```

After `uv sync` the filter works normally. Run commands there with
`env -u VIRTUAL_ENV uv run ...`, since an editor terminal's `VIRTUAL_ENV` may
point at the main checkout's `.venv`.

## ripwire — deterministic codebase maps (on PATH as `ripwire`)

Reach for it BEFORE blind grep + whole-file reads. First call ~1s cold; after that warm, ~0.1s.
- Orient on a task: `ripwire <dir> --for="<task in words>"` — ranked, quality-annotated
  signatures. Paste symbol/file names from the issue verbatim; named mentions get anchored.
- One task: `--pack-task="<task>"`; before parallel agents: `--plan-lanes=N --task="<goal>"`, then read `lanes[].execution`.
- Have a stack trace / build error: `ripwire <dir> --from-trace=FILE` (`-` = stdin) —
  paste the error, don't paraphrase it into a query.
- Who calls X: `--callers=SYM`. "Is it safe to change X?" needs the full blast radius:
  `--impact=SYM` (transitive) plus `--uses=SYM` (every read/write/import site).
- Apply a whole-symbol edit without a whole-file Read: `--replace-symbol-body=SYM` plus `--edit-payload=FILE|-`
  (or insert-before/after); the receipt carries region, blob_sha, edit_check, tests_to_run + ONE next= — no re-read after it; `--edit-check=SYM` is for a contract question WITHOUT an edit in hand.
- Before writing a new fn/class/helper: `--exemplar="<what you're writing>"` — duplicates are born on small tasks.
- Before calling work done: `--quality-delta` (what you made worse), then `--test-gate`.
- Trust notes: counts marked counts_floor are floors, not totals; a zero means "none
  found", never "none exists".
Defaults to break (less context is measurably MORE accurate, not just cheaper — code-repair
accuracy fell 29% -> 3% as context grew 32K -> 256K tokens, LongCodeBench):
- Do NOT open a file you have not located first: rank with `--for`/`--grep`, then read what it names.
- Do NOT read a whole file to understand one symbol: `--expand=SYM` gives the body + callee sigs.
- Do NOT fan reads across several files to learn one thing: `--pack-task="<task>"` is one call.
