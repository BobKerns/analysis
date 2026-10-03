# JupyterHub on the Pi: setup plan (not yet implemented)

Decided 2026-10-01, written up for reference — not acted on yet. Goal: give each
household member their own isolated JupyterLab (separate kernel, separate Python
environment) reachable from any of their own devices over Tailscale, without
exposing anything to the public internet.

## Decisions made

- **Host**: Raspberry Pi 4 or 5, running Linux.
- **Hub**: [The Littlest JupyterHub (TLJH)](https://tljh.jupyter.org) — the
  single-server-sized JupyterHub distribution, not the Kubernetes-based
  "Zero to JupyterHub."
- **Auth**: PAM, using the Linux accounts both of you already have on the box —
  no separate account/password database to maintain.
- **Isolation**: TLJH's default per-user Python environment, one per account.
- **Repo layout**: separate full clones, not a shared clone with worktrees.
  More familiar workflow, avoids any shared-branch conflicts between the two of
  you. The existing remote stays the single source of truth / backup; each
  clone just pushes/pulls against it like today.
- **Access**: Tailscale only (already set up) — no public exposure, so no
  TLS/reverse-proxy complexity is needed for that reason alone.

## Why this over plain JupyterLab

Side-by-side editing was the deciding factor: a single shared JupyterLab
process means a shared kernel, so simultaneous use by two people (or even one
person running Jupyter and an editor against the same checkout) can step on
each other's state. Per-user TLJH environments solve the two-person version of
that; a git worktree (see below) solves the one-person, two-tools version of
the same problem, independent of whether TLJH happens at all.

## Setup sketch

1. **Install TLJH.** SSH into the Pi and run the official bootstrap
   (`curl https://tljh.jupyter.org/bootstrap.py | sudo python3 - --admin
   <your-username>`). Check tljh.jupyter.org for the current exact invocation
   before running it — this is the kind of command that drifts over time, and
   this note shouldn't be trusted as gospel by the time it's acted on. This
   gets you a running hub as a systemd service with a single admin account.
2. **Confirm/switch auth to PAM.** TLJH supports more than one authenticator;
   check at setup time whether PAM is already the default or needs an
   explicit `tljh-config set auth.type ...` — again, verify the exact
   class/module name against current docs rather than this note. Since both
   accounts already exist on the box, no new user creation should be needed —
   just confirm both usernames can log into the hub.
3. **Verify Tailscale reachability.** Reach the hub via the Pi's Tailscale
   IP/hostname on whatever port TLJH's nginx front-end uses (usually 80).
   TLS is skippable for now since Tailscale's own network is already private.
4. **Per-user environment.** Log in as each of you once to confirm TLJH
   actually spawned a separate environment/server per account (its usual
   default), then install this project's dependencies inside each person's
   own environment.
5. **Separate clones.** Each of you clones the private data repo into your own
   home directory on the Pi (e.g. `git clone <remote> ~/finance-data`) — same
   remote both of you already push/pull against today. No shared working
   directory, no worktrees between the two of you, just the clone-per-person
   workflow you already use.
6. **Worktree, just for you.** Separately from the above: consider a git
   worktree for your own Jupyter-vs-VSCode split on whichever machine you edit
   from day to day — one checkout VSCode has open, a second
   (`git worktree add ../finance-data-jupyter <branch>`) that Jupyter points
   at instead, so the two tools stop contending for the same working
   directory. This doesn't need TLJH at all and could be done any time,
   independent of this plan's timeline.

## Open questions to settle at setup time

- Exact current TLJH bootstrap command and default authenticator — check
  tljh.jupyter.org directly.
- Pi 4 vs Pi 5, and how much RAM is on hand — two environments/kernels running
  at once needs more headroom than one.
- Whether TLS is wanted anyway (likely unnecessary given Tailscale) or the hub
  stays on plain HTTP within the tailnet.

## Fallback

If TLJH turns out to be more friction than it's worth, plain JupyterLab with a
pre-configured password per device/user remains available with far less setup
(one process, one environment, one filesystem view). The worktree idea above
is worth doing either way.

## Separately tracked, not covered here

The local git-automation task (switch branch → pull main → update prices →
commit → push → merge back to main → push) is still open and doesn't depend
on any of the above — likely a plain shell script plus `launchd` (macOS) or
`cron` (Linux) on whichever machine ends up running it.
