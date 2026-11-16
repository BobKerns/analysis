# A private remote for the data repo

> To read a formatted `.md` file:
> - In Jupyter: right-click on it and select `Open With / Markdown Preview`
> - In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

The data repo holds your household's real finances: names, accounts,
balances, income, plans. Its git history keeps every past version of them.
This guide covers where to keep a second copy, so that two people can share
it and so that a lost or broken disk doesn't take it with it.

**Never make the data repo public.** Not on GitHub, not on a file share
anyone can read, not "just for a moment". Removing a file later doesn't help:
its history keeps every version, and a public copy may already have been
cloned or indexed. Keep the code repo and the data repo apart (the code repo
is the one that can be public), and check where a remote points before the
first push.

## Two tiers

A good setup has two layers, which protect against different things:

| Tier | What it is | What it covers |
| --- | --- | --- |
| 1 | A bare git repo on a separate server, reached over SSH with keys | Sharing between people and machines; first-level recovery (a lost laptop, a bad commit) |
| 2 | An encrypted backup to cloud storage | Disaster recovery (the house and the server both gone, ransomware) |

Tier 1 is the data repo's `origin`. After the background builder merges new
prices into `main`, it pushes `main` there when `main` is strictly ahead of
`origin/main` (see [Setup](../SETUP.md)); without an `origin` it just says
it isn't pushing.

### Tier 1: a bare repo on another machine, over SSH

Any machine you control that stays on will do: a small home server, a
Raspberry Pi, a NAS that runs SSH. On it, make a bare repo (one with no
working files, only history), here for a user `alice` on a host `home-server`:

```sh
ssh alice@home-server 'git init --bare ~/repos/retirement-data.git'
```

On each machine that works on the data, use a key rather than a password
(the builder pushes in the background, where nobody can type one):

```sh
ssh-keygen -t ed25519            # once per machine, if you have no key yet
ssh-copy-id alice@home-server     # lets that key log in
```

Then, in the data repo, add the remote and push `main` with tracking, so
`git pull`, `git push` and the builder all know where `main` belongs:

```sh
git remote add origin alice@home-server:repos/retirement-data.git
git push -u origin main
```

A second person clones it (`git clone alice@home-server:repos/retirement-data.git`)
and links it as their own data repo. Each person works in their own clone
and shares by pushing and pulling.

### Tier 2: an encrypted backup to cloud storage

Cloud storage is someone else's computer, so what goes there must be
encrypted before it leaves, with a key you keep somewhere else (a password
manager, a printed copy in a safe). A backup you can't decrypt is no backup,
so test a restore once.

Two straightforward ways:

- A backup tool that encrypts, such as `restic` or `borg`, run on a schedule
  over the repo folder, sending to cloud storage (S3, Backblaze B2 and
  others).
- A single-file snapshot of the whole history, encrypted, then uploaded:

  ```sh
  git bundle create data.bundle --all
  age -p -o data.bundle.age data.bundle   # or: gpg -c data.bundle
  ```

  `git clone data.bundle` restores it after decrypting.

Tier 2 can run either at the shared repo (the server backs up the bare
repo, which has every pushed commit) or locally (each machine backs up its
own clone, including work not yet pushed). Backing up the shared repo is one
job for everyone; backing up locally also catches work not yet pushed.

## Tier 1 alternatives

When a separate server isn't available, a second git repo somewhere else
still helps, with these limits:

| Where the second repo lives | Trade-off |
| --- | --- |
| Another folder on the same drive | Guards against a bad commit or a deleted folder, not a failed drive: limited redundancy, a single point of failure |
| Another attached or removable drive | A smaller single point of failure; a removable drive only protects while it's kept up to date and stored apart |
| A file share (SMB, NFS) | Exposed to whatever can reach the share, and to git's trouble with network file locking: various security and reliability risks |
| Dropbox, iCloud Drive, OneDrive or similar | **Never use for sharing.** A sync service copies files one by one, so two machines writing the same repo can leave it corrupt. At most a one-person copy, and better encrypted as in tier 2 |

Each of these is set up the same way: `git init --bare` at the place, then
`git remote add origin <path>` and `git push -u origin main`.

## Not recommended: several people in one worktree

Two people working in the same checkout (one shared folder, or one login)
step on each other: one's uncommitted edits land in the other's commit, the
current scenario changes under the other's feet. Give each person their own clone, and share
through tier 1.
