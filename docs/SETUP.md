# Setup

> To read a formatted `.md` file:
> - In Jupyter: right-click on it and select `Open With / Markdown Preview`
> - In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

Run the setup script (idempotent — safe to re-run):

```sh
./scripts/setup.sh
```

It first makes sure there's a C compiler, a C++ compiler and `make`, which some
dependencies need to build. On Linux it installs them if they're missing:
`build-essential` with apt on Ubuntu and Debian (WSL included), using `sudo`,
or the equivalent with dnf, zypper, pacman or apk. On macOS it checks for the
Xcode command-line tools. If it can't, it says what to install and stops.
It puts `~/.local/bin`, where uv and `ripwire` are installed, on your `PATH`
if it isn't already there, adding it to your bash and zsh startup files
(`.bashrc` and the bash login file, `.zshrc`); for another shell it reminds
you to add it yourself.
It installs [uv](https://github.com/astral-sh/uv) if missing, configures the
`.githooks` hooks (which keep runtime data and real account names out of
commits), sets `merge.ff false` and `pull.ff true` in this repo and the linked
data repo (an explicit merge always makes a merge commit; a pull still
fast-forwards when it can), runs `uv sync --group dev` (installing the Python
version pinned in `.python-version` only if `uv sync` can't find a usable
interpreter), and installs the notebook-cleaning `nbstripout` filter (strips
outputs and per-run/per-machine metadata churn like `kernelspec.display_name`
— see [notebooks/README.md](../notebooks/README.md)).
It also installs the `ripwire` CLI if it is missing. The CLI is used to save
token usage and improve quality by creating and using
deterministic repository maps and code-navigation checks; its optional MCP
server is registered separately per coding-agent client below.
Finally, it installs the spelling and markdown linters (`cspell`,
`markdownlint-cli`) at the versions pinned in `package.json` /
`package-lock.json`. They need Node.js 22.22.2+, 24.15+, or 26+ (see `engines`
in `package.json`); if `node` is missing or older, the script downloads the
version in `.node-version` from nodejs.org, checks it against the published
SHA-256 sums, unpacks it under `~/.local/share/node`, and links `node`, `npm`
and `npx` into `~/.local/bin`. That one then comes first on your `PATH`, for
everything, not just this repo. If that fails, the script warns and carries
on: the simulator and notebooks don't need Node.js, only those two
verification gates do.

Last, it links the data and code repositories together. It asks for the path to
your data repo, makes `data/shared` point to it, and asks which of the data
repo's primary persons you are. Leave the path blank and it fills `data/shared`
with the template data instead, used as a `test` person, which is what CI does.
To start a data repo of your own, finish setup with the template data, remove
`data/shared`, and run `.venv/bin/sim init DIR`: it links `data/shared` to the
new repo and turns on background builds. Either way, this then gives you a working environment:

```sh
source data/shared/sim/activate-analysis
```

With a data repo linked, it then turns on background scenario builds
(`sim builder install`; [ADR-004](architecture/ADR-004-reproducibility-and-activation.md) §12):
commits, checkouts, merges and rebases in either repo rebuild, in the
background, every scenario whose results could have changed, and opening a
scenario's run waits for a pending build of it. It builds only while the code
is committed and matches your local `main` (or the branch
`sim builder install BRANCH` names). Setup skips this under CI or an
agent (`CI`, `CLAUDECODE`, `CLAUDE_CODE_REMOTE` or `AI_AGENT` set) or with
`ANALYSIS_NO_BUILDER=1`; `sim builder install` turns it on later, and
`sim builder uninstall` off. `sim builder status` says what it's doing,
and `sim builder log` what it did in the last hour (`-v` adds build output).

Finally, if Claude Code (`claude`) is on your `PATH`, it offers to install the
finance-research Claude plugin, which writes tear sheets on funds and
companies from public sources. The answer defaults to no, and it isn't asked
under CI or an agent. Each run of the plugin browses the web for several
minutes and uses around 150k tokens. Its settings (tab languages, tax
state) come from the data repo's `research.yaml`, and setup says which it
used; once installed, a re-run of setup updates it. Install or remove it by
hand with `.venv/bin/analysis research-plugin install` or `uninstall`; see
[the finance research guide](guides/finance-research.md).

## What the script does, step by step (for reference or manual setup)

1. Install a C toolchain: on Ubuntu, Debian or WSL,
   `sudo apt-get install build-essential`; on macOS, `xcode-select --install`.
2. Put `~/.local/bin` on `PATH`, where the next two steps install their
   binaries. If it isn't there already, add this line to your shell startup
   file (`~/.zshrc`, or `~/.bashrc` and your bash login file):
    ```sh
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
    ```
3. Install [uv](https://github.com/astral-sh/uv)
4. Install and verify `ripwire`:
    ```sh
    RIPWIRE_REPO=redhat-et/ripwire bash -c "$(curl -fsSL https://raw.githubusercontent.com/redhat-et/ripwire/main/scripts/install.sh)"
    ripwire --version
    ```
5. Configure the git hooks (pre-commit and commit-msg) and merges:
    ```sh
    git config core.hooksPath .githooks
    git config merge.ff false
    git config pull.ff true
    ```
    Run the last two in the data repo as well.
6. Run `uv sync --group dev` (installing the pinned Python from
   `.python-version` first with `uv python install` only if this fails)

   This also puts `analysis_truststore.pth` in the venv's site-packages. It
   runs `truststore.inject_into_ssl()` at the start of every Python process
   there (the JupyterLab server, kernels, the CLI), so HTTPS everywhere is
   verified with the operating system's trust store and a root you've
   trusted locally (a corporate proxy, a home CA) is honored, even by
   libraries that bring certifi's bundle.
7. Install the notebook-cleaning filter:
    ```sh
    uv run nbstripout --install
    git config filter.nbstripout.clean '"$(git rev-parse --show-toplevel)/.venv/bin/python" -m nbstripout --extra-keys "metadata.kernelspec.display_name metadata.language_info.version"'
    git config diff.ipynb.textconv '"$(git rev-parse --show-toplevel)/.venv/bin/python" -m nbstripout -t --extra-keys "metadata.kernelspec.display_name metadata.language_info.version"'
    ```
    (`--install --extra-keys` in one call does *not* persist the extra keys in
    this nbstripout version — the additional commands are required. The second
    `git config` enables it for `git diff`)
8. Install the pinned lint tools (needs [Node.js](https://nodejs.org) 22.22.2+,
   24.15+, or 26+, such as the version in `.node-version`; `.npmrc` sets
   `engine-strict`, so `npm ci` refuses an unsupported version):
    ```sh
    npm ci
    ```
    This puts `cspell` and `markdownlint` in `node_modules/` (gitignored). Run
    them with `npm run spell` and `npm run markdownlint`. To change a version, edit
    `package.json` and run `npm install` to update the lockfile.
9. Link the data and code repositories:
    ```sh
    scripts/link-data.sh /path/to/data-repo   # or --template for template data
    ```
    With no argument it asks for the path. Once `data/shared` exists, re-running
    it leaves it alone; remove `data/shared` to link a different data repo.
10. Turn on background scenario builds, for a linked data repo:
    ```sh
    .venv/bin/sim builder install --data-dir data/shared/sim
    ```
11. Optionally, install the finance-research Claude plugin:
    ```sh
    .venv/bin/analysis research-plugin install --data-dir data/shared/sim
    ```

## Drawing chain flowcharts in the terminal

`sim chain show CHAIN --graph` draws a chain's flowchart as an image, right in
the terminal. Two things make that work.

**A browser to draw with.** The chart is drawn by Mermaid in a headless browser
through [Playwright](https://playwright.dev/python/), which `uv sync` installs.
An installed Google Chrome is enough; without one, fetch Playwright's own
Chromium once:

```sh
uv run playwright install chromium
```

Mermaid's script ships with the code, so `--graph` and the notebooks'
flowchart work offline, and the chart never leaves your machine.

**A terminal that shows images.** Recommended, all free:

- [iTerm2](https://iterm2.com) (macOS)
- [kitty](https://sw.kovidgoyal.net/kitty/) (macOS, Linux)
- [Ghostty](https://ghostty.org) (macOS, Linux)
- [WezTerm](https://wezterm.org) (macOS, Linux, Windows)
- VS Code's integrated terminal, with `terminal.integrated.enableImages` on

On an iPad, connecting over SSH to the machine that runs the code, use
[gterm](https://apps.apple.com/us/app/gterm-ghostty-ssh/id6774837597) from the
App Store. It is built on Ghostty's terminal library and is the one iPad
terminal we have found that shows the images. It has far fewer features than
other SSH apps, but everything this project needs works. Termius has just
added the same library, but in testing (October 2026) it did not work yet: one
try gave a blank screen, another hung.

iTerm2 gets its own image protocol. Elsewhere `--graph` asks the terminal
whether it takes the kitty graphics protocol, and falls back to iTerm2's
protocol for older VS Code and WezTerm. In any other terminal, including macOS
Terminal and inside tmux, it writes the chart to a PNG file and prints its
path. If it guesses your terminal wrong, set `ANALYSIS_IMAGE_PROTOCOL` to
`kitty`, `iterm` or `none`.

## Jupyter and filesystem MCP servers

Two project-level MCP servers are configured for coding agents (Copilot CLI,
and any other client that reads `.mcp.json` / `.github/mcp.json`):

- **filesystem** — [`@modelcontextprotocol/server-filesystem`](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem),
  scoped to the repository root. Defined in `.github/mcp.json` (committed —
  no secrets involved).
- **jupyter** — [`jupyter-mcp-server`](https://pypi.org/project/jupyter-mcp-server/),
  which talks to a running JupyterLab instance. Defined in `.mcp.json`
  (gitignored — holds a per-machine token).

Both files are loaded together; server names don't conflict, so no merging
surprises. Run JupyterLab through the CLI rather than starting it directly —
it generates `.mcp.json` with a fresh random token on first run (never
overwriting an existing one), then launches `jupyter lab` with that same
token, so the Jupyter MCP server always talks to the instance you started:

```sh
analysis jupyter
```

Any extra arguments are passed through to `jupyter lab` verbatim, e.g.
`analysis jupyter --no-browser`. If `.mcp.json` gets corrupted, delete it and
rerun `analysis jupyter` to regenerate it.

## Optional MCP registration

The `ripwire` CLI works without MCP. To register its optional stdio MCP server
with a coding agent, ask ripwire for that client's configuration recipe:

```sh
ripwire wrap cursor
ripwire wrap claude
ripwire wrap --all
```

For Cursor, the project-level configuration is:

```json
{
    "mcpServers": {
        "ripwire": { "command": "ripwire", "args": ["--mcp"] }
    }
}
```

Use an absolute path for `command` if the agent does not inherit your shell's
`PATH` (for example, `~/.local/bin/ripwire`). MCP registration is intentionally
not performed by `setup.sh` because it changes user- and client-specific
configuration.
