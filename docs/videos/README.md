# Instructional videos

How the videos for [#366](https://github.com/BobKerns/analysis/issues/366) are
made. Everything a video needs is committed here as text: its script, the
terminal sessions it records, its slides and the shared look. The pictures
and sound are rebuilt from those, so a CLI change means re-running a build,
not re-recording by hand, and fixing a sentence re-renders one scene.

**Docker is recommended.** The easiest way to build a video is in the video
container, which has every tool and font the build uses, so the only
thing to install is [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(macOS, Windows) or Docker Engine (Linux); see
[Building in a container](#building-in-a-container). A native build works
too, but needs its own list of tools (see [Building and caching](#building-and-caching)).
Publishing on YouTube needs only `uv`, on the machine whose browser can
sign in to the channel.

```sh
analysis video build 00-pilot      # or a path: docs/videos/00-pilot
analysis video check               # every script parses, every slide renders
```

`analysis video` builds and checks in the container when Docker is running
here, and on this machine when it isn't (saying so); `--docker` or `--local`
chooses. The result is `out/videos/00-pilot.en.mp4`, with its captions
(`00-pilot.en.srt`) and thumbnail (`00-pilot.en.jpg`) beside it. A native
build needs `analysis video setup` once first (voice model, VHS, ttyd).
`python -m analysis.videos` and `scripts/videos/docker.sh` are the two
halves underneath, and work on their own too.

## Layout

| Path | What it holds |
| --- | --- |
| `docs/videos/<nn>-<slug>/script.md` | One video: its scenes, narration beside the commands and slides |
| `docs/videos/<nn>-<slug>/setup.sh` | Optional: prepares the demo data the video's tapes start from |
| `docs/videos/<nn>-<slug>/script.<lang>.md` | A translation of that video's narration and slide text |
| `docs/videos/<nn>-<slug>/published.toml` | Written by `publish`: each language's YouTube video id, and its scenes' lengths |
| `docs/videos/theme/theme.toml` | The shared look: palette, fonts, terminal colors, voices, timing |
| `docs/videos/theme/templates/*.html` | Slide templates (Jinja2): `title` opens every video, `card` for breaks and endings, `figure` for a picture a command made, `window` for a screen the tapes can't record |
| `docs/videos/youtube.toml` | The channel's settings and the wording around each description |
| `docs/videos/assets/*.svg` | Reusable characters and props: Alice, Bill, their home |
| `src/analysis/videos/` | The builder |
| `scripts/videos/tts_kokoro.py` | The narration worker (runs in its own environment) |

`00-pilot` exists to prove the pipeline. Real videos are numbered in the
order of #366's outline (`01-intro`, `02-setup`, ...).

## Writing a script

A script is markdown with TOML front matter between `+++` lines. The front
matter's fields are available to every slide; `data` names the demo data a
tape starts from (a repo folder such as `prototype_data`; leave it out to use
[the video's own data repo](#each-videos-data-repo)).

````markdown
+++
series = "Planning with the simulator"
episode = "Video 1"
title = "Will the money last?"
subtitle = "Running Alice's retirement scenario"
data = "prototype_data"
+++

# Will the money last?

Text before the first scene is for readers of the script, not spoken.

## Scene `title`: Title card

```slide
template = "title"
```

Meet Alice and Bill.

## Scene `networth`: Run the scenario

```tape
Type "sim networth"
Enter
Wait
Sleep 6s
```

Then sim networth shows the result.

> Production note: block quotes, lists and HTML comments aren't spoken.
````

Each `## Scene` has:

- **An id**, the backticked word. Translations refer to scenes by id, so it
  stays put when the heading is reworded. Ids are lowercase letters, digits,
  `-` and `_`.
- **One visual**: a ```` ```tape ````, ```` ```slide ```` or ```` ```notebook ```` block.
- **Narration**: every plain paragraph. Links keep their text and lose
  their URL; emphasis and backticks are dropped. Write for the ear: say
  `retire alice`, not `retire_alice`, and put the reason in a note.

A scene lasts as long as its narration (plus the theme's `lead` and `tail`)
or its tape, whichever is longer; the last frame holds until the narration
ends. A slide with no narration stays up for its `duration` field, or 2.5
seconds.

### Tapes: terminal scenes

A tape is a list of [VHS](https://github.com/charmbracelet/vhs) commands
(`Type`, `Enter`, `Sleep`, `Hide`/`Show`, ...). The builder adds the output
file, the theme's terminal settings, and a hidden setup that:

- copies the front matter's `data` folder (the prototype personas' data),
  or the video's own data repo, to a fresh, committed scratch folder, after
  the video's `setup.sh` (if any) has run in it, and points
  `ANALYSIS_DATA_DIR` at it, so every recording starts from the same state;
- puts this checkout's `sim`/`analysis` on `PATH`;
- sets `ANALYSIS_NO_BUILDER=1`, so a recorded `sim init` (video 2) doesn't
  link this checkout's `data/shared` or turn on background builds;
- sets the prompt (`alice@home $`, from the theme).

Each tape starts from the same state, so a tape can't rely on an earlier
one: what a scene shows has to exist before it records. That's what
`setup.sh` is for (below). A little set-up the viewer shouldn't see can also
go between `Hide` and `Show`. After a command's `Enter`, put `Wait`: it
waits for the prompt to come back, however long the command takes (a fixed
`Sleep` breaks when a first run is slow). Pace the rest with `Sleep` so the
picture keeps up with the narration; the narration starts `lead` seconds
into the scene. A tape must not set `Output`.

A tape whose output is wider or longer than the terminal (84 columns by 22
rows) can start with `Set FontSize <n>`, in layout pixels like the theme's
`font_size`: 24 fits about 126 columns by 33 rows, and 4K keeps it sharp.
Only `FontSize` can be set this way, and only on the tape's first lines.

The demo data's bytes are part of a tape's cache key, but the CLI's
behavior isn't, so after a CLI change rebuild with `--rerecord`.

### Setting up the demo

A video that shows more than the prototype data's starting point (a
scenario already run, an ensemble, an edit) keeps the commands that build it
in `setup.sh`, beside its script. The builder runs it once, before the first
tape records, with `bash -euo pipefail`, inside the fresh copy of the demo
data, with the tapes' environment (`sim` on `PATH`, `ANALYSIS_DATA_DIR`
set). Whatever it leaves uncommitted is committed after it, under a fixed
author and date. Every tape then starts from a copy of the result, its
`state/runs` included, so `sim explain` in one scene doesn't need `sim run`
recorded in the scene before.

`sim run` refuses uncommitted data, so a setup that changes a scenario and
then runs it commits in between (`git add -A && git commit -q -m ...`; the
identity is already set). Setup's output isn't shown; if it fails, the build
stops with its last lines. Its text is part of every tape's cache key, so
editing it re-records the video's tapes. `01-intro/setup.sh` is an example.

### Notebook scenes

A ```` ```notebook ```` block records JupyterLab: Playwright opens one of
this checkout's notebooks on the demo data and works its controls, as a
viewer would. It's written like a tape, one step per line, and a `#` starts a
comment:

```text
Set DataDir "home/retirement/data/sim"   # where the notebooks read data
Hide
Open "net_worth.ipynb"
RunAll
Scroll "Net worth:"
Show
Sleep 6s
Click "Pre-tax"
Drag "Years:" 2028 2040
Select "Accounts:" "Asset_Checking"
Sleep 4s
```

| Step | What it does |
| --- | --- |
| `Set DataDir "<path>"` | Before the first step: the data repo the notebooks read, a path inside the demo (default: the demo data itself, `data`). `Set Zoom <n>` draws JupyterLab larger (default 1.4) so it reads at 1080p |
| `Open "<notebook>"` | Opens a notebook from `notebooks/` and waits for its kernel |
| `RunAll` | Runs every cell and waits until they're done, then goes back to the top |
| `Scroll "<text>" [n]` | Smoothly brings a heading, or the `n`th text that reads so (a widget's label, say), to the top |
| `ScrollBy <pixels>` | Smoothly scrolls down (or up, negative) by that much of JupyterLab's layout, to bring a chart or table below the controls into view |
| `Click "<label>"` | Moves the pointer to the on-screen button with that label (a toggle such as `Pre-tax`) and clicks it, then waits for the charts to redraw |
| `Select "<label>" "<option>"` | Picks an option in the on-screen dropdown or list labeled so (`Accounts:`) |
| `Drag "<label>" <low> <high>` | Drags the on-screen slider labeled so (the `Years:` range) to those values |
| `Wait` | Waits for the kernel to finish |
| `Hide`, `Show`, `Sleep <time>` | As in a tape: nothing between `Hide` and `Show` is recorded |

So the usual scene opens and runs its notebook and scrolls while hidden,
then shows the part it's about. A step that finds nothing to act on stops
the build and names its line. The pointer is drawn into the page, since a
headless browser has none.

Each recording runs JupyterLab on its own fresh copy of the prepared demo
(its `setup.sh` included), with the notebooks copied in and
`ANALYSIS_DATA_DIR` set, so a notebook sees the demo's runs and nothing
else. JupyterLab shows the notebook alone, every cell drawn (so scrolling
never shows a blank), with no news prompt. Chromium's screencast sends a
frame whenever the page changes, at the video's full size; ffmpeg holds each
frame until the next, which keeps the recording's real timing.

A notebook scene is cached on its steps, the notebooks it opens, the theme's
size and the demo data and `setup.sh`; like a tape, it doesn't see code
changes, so rebuild with `--rerecord` after changing a widget. A translation
can't change one.

### Slides

A slide block is TOML: `template` names a file in `theme/templates/`, and the
other keys are fields that template reads, overriding front matter of the
same name. A template can show any asset with `{{ asset("alice") | safe }}`,
which inlines the SVG so the theme's CSS can restyle it.

| Template | Fields |
| --- | --- |
| `title` | `series`, `episode`, `title`, `subtitle`, `cast` (assets to show; default home, Alice, Bill) |
| `card` | `series`, `heading`, `body` (text, or a list shown one item per line), `speaker` (one asset, optional) |
| `figure` | `series`, `heading` (the command, shown as typed), `demo_image` (a picture `setup.sh` made) |
| `window` | `series`, `heading`, `title` (the title bar), `look` (`terminal`, `light`, `files` or `editor`), `lines`, `address` (`files`), `status` (`editor`), `note` (below the window) |

A `demo_image` is a path under the demo root (`home/chart.png` would be
the file `setup.sh` wrote to `~/chart.png`); the builder runs the
setup, reads the file and gives the template its `image` as a `data:` URI.
The slide is cached like a tape, under the demo data and `setup.sh`.

A tape can show a picture a command draws in the terminal, such as
`sim chain show --graph`, as it appears (video 3's `chains-graph`). Stock
VHS records only the terminal's text; the builder uses a VHS build from
[a fork](https://github.com/BobKerns/vhs/releases) that adds image capture
([charmbracelet/vhs#783](https://github.com/charmbracelet/vhs/pull/783)),
and every tape turns it on (`Set Sixel true`, which also covers the iTerm2
images `--graph` sends). `setup` and the container fetch that build; a
build refuses another VHS, such as Homebrew's.

A `window` draws a screen another system shows, which VHS can't record:
PowerShell, File Explorer, VS Code (video 2a, on WSL). Each of its `lines`
is text, or a table: `prompt` and `typed` show a command line with the typed
part in the accent color, and `secret = true` puts a cursor after the
prompt. It draws what the screen says, not a copy of it, so it rebuilds and
translates like any slide; a translation can override `heading` and `note`
and leave the screen's own text as it is.

A new template extends `base.html`, which supplies the background, the
ground, the theme's CSS variables (`--bg`, `--accent`, `--font-heading`, ...)
and the canvas size.

## Characters and other reusable elements

Assets are SVG files in `docs/videos/assets/`, drawn flat in the theme's
palette: `alice.svg`, `bill.svg`, `home.svg`. Each SVG's root has a class
naming it (`character alice`), and each part has a class (`skin`, `hair`,
`top`, ...) scoped under it, so a template can recolor a part without
touching the drawing. A new pose or expression is a new file named
`<who>-<pose>.svg` (`alice-thinking.svg`), with the same classes. Keep
real people's names out: Alice and Bill are the only personas.

The palette, fonts and terminal colors live in `theme.toml` alone; slides
and tapes both read it, so one edit restyles every video, and the cache
re-renders exactly the scenes it affects.

## Narration

Narration is synthesized with [Kokoro](https://github.com/thewh1teagle/kokoro-onnx),
an open text-to-speech model that runs on a CPU. `[narration.<lang>]` in
`theme.toml` picks the voice (`af_heart` for English), language and speed;
the [voice list](https://github.com/thewh1teagle/kokoro-onnx) covers English,
Japanese, Chinese, Spanish, French, Hindi, Italian and Portuguese. Each
scene's narration is synthesized one sentence at a time, which also times
the captions.

`setup` downloads the model (about 330 MB) to `~/.cache/analysis-videos`
(or `$ANALYSIS_VIDEO_CACHE`). The worker runs with `uv run --script` in its
own environment, so the project doesn't depend on it.

`build --silent` replaces the voice with silence of about the right length:
quick drafts of timing and pictures, no model needed.

A human recording can replace synthesis later; it would be another engine,
reading a WAV per scene.

### Pronunciation

When the voice says a word wrong, respell it. The captions keep the word as
written; only the voice changes, and only the scenes that use it re-voice.

- **Everywhere**: add it to `[narration.<lang>.pronounce]` in `theme.toml`,
  such as `Jupyter = "Jupiter"`. Whole words, case-sensitive.
- **In one place**: write `{shown|said}` in the narration, such as
  `every run {records|ree-cords} it`. Use this for a word whose sound
  depends on its part of speech (records, live, read), which the voice
  guesses from context and sometimes gets wrong.

Either kind can give IPA phonemes between slashes instead of a respelling:
`{records|/ɹᵻkˈɔːɹdz/}`, `IRA = "/ˌaɪˌɑːɹˈeɪ/"`. They go to the voice as
written. To see how the voice says a word, and borrow its phonemes from a
sentence it gets right, ask its phonemizer, from `uv run --script` in the
narration worker's environment:
`Kokoro(model, voices).tokenizer.phonemize("She records it.", "en-us")`.

## Languages and captions

`script.md` is the English master. A translation, `script.<lang>.md`, has
the same scenes by id, each with its narration and (optionally) a slide
block of fields to override, such as a translated `heading`. It can't add
scenes, change a template, or carry tapes: the terminal shows the CLI, which
is in English. Its front matter overrides the master's (a translated
`title`). Each language needs a `[narration.<lang>]` table in the theme.

A translation shows the English CLI, so it writes a command or scenario name
as the screen shows it and respells it for the voice:
`{sim explain|シム・エクスプレイン}`, `{retire alice|リタイア・アリス}`.
`01-intro/script.ja.md` is an example. The theme's fonts end with Noto Sans
CJK (in the video image) and Hiragino (macOS) for Japanese slides.

Japanese narration is phonemized with misaki's OpenJTalk front end, as
Kokoro's own pipeline does; espeak-ng, which kokoro-onnx uses otherwise,
reads kanji and kana as "Chinese letter" and "Japanese letter". Its
dictionary (about 23 MB) downloads on first use.

`build` makes every language the video has, or one with `--lang`: each its
own MP4 and SRT (`00-pilot.ja.mp4`, `00-pilot.ja.srt`). Captions are a
separate file, not burned into the picture, which is what YouTube and most
players want. A caption is one sentence, split into parts of at most two
lines.

## Building and caching

Every product is cached in `out/videos/.cache` under a hash of exactly
what went into it:

| Product | Rebuilt when this changes |
| --- | --- |
| Narration | The scene's sentences, the voice settings |
| Slide | The page its template renders: template, assets, theme, fields; for a `demo_image`, also the demo data and `setup.sh` |
| Tape | Its commands, the terminal theme, the demo data, the video's `setup.sh`; or `--rerecord` |
| Notebook scene | Its steps, the notebooks it opens, the video's size, the demo data, `setup.sh`; or `--rerecord` |
| Scene clip | Its narration, its visual, the timing |

So editing one sentence re-synthesizes and re-assembles that scene only. The
build prints what it made and what it reused:

```text
00-pilot [en]
  title               7.5s  narration cached  visual cached  clip cached
  networth           16.1s  narration made    visual cached  clip made
  end                 4.9s  narration cached  visual cached  clip cached
```

Requirements for a native build: `ffmpeg`, `uv`, Playwright's Chromium
(`uv run playwright install chromium`, again after a Playwright upgrade;
an installed Google Chrome stands in until then), and for tapes VHS and ttyd (`setup`
downloads VHS, and ttyd on Linux; `brew install ttyd` on macOS). Japanese narration
also needs a C++ compiler and `cmake` the first time the narration worker's
environment is made, since OpenJTalk builds from source (Xcode's
command-line tools and `brew install cmake` on macOS). The container has them
all.

## Building in a container

`scripts/videos/docker.sh` builds videos in a container (the image is
`docker/videos/Dockerfile`), so the only thing the host needs is Docker. The
image has ffmpeg, VHS, ttyd, Playwright's Chromium, the Kokoro voice model,
the project's Python and Node.js, the theme's fonts, and the notebooks that
notebook scenes record. It builds for amd64
and arm64 (Docker Desktop on Apple silicon).

```sh
analysis video build 00-pilot [--lang ja] [--silent] [--rerecord]
analysis video check                 # every video, or name some
analysis video shell 01-intro        # bash, with the video's data repo
```

(`analysis video` runs `scripts/videos/docker.sh` for these when Docker is
running; `shell` needs it.)

Each run rebuilds the `analysis-videos` image from the checkout first. The
first build takes several minutes (the model and Chromium are most of it);
after that it's a quick check unless the code changed, which rebuilds only
the code's layers. So a tape records this checkout's CLI. `docs/videos` is
mounted read-only rather than copied, so editing a script needs no rebuild.
Videos and the build cache land in `out/videos` (or `$ANALYSIS_VIDEO_OUT`),
as in a native build.

### Each video's data repo

A video that builds its own scenario, rather than starting from
`prototype_data`, keeps its data in its own data repo: the named Docker
volume `analysis-video-<video>`, mounted at `/data`. Its script leaves `data`
out of the front matter, and each tape starts from a fresh copy of that repo,
so recordings stay repeatable and the CLI's changes during one tape don't
leak into the next.

Create the repo from `shell` with `sim init /data --no-builder` (#289;
the container has no background builds), which makes the
simulator's tree at `/data/sim`; `ANALYSIS_DATA_DIR` is already `/data/sim`
there and `sim` is on `PATH`, so the CLI changes it as it would any data
repo.
`docker volume rm analysis-video-<video>` discards it. Outside the container
the same fallback reads `$ANALYSIS_VIDEO_DATA`, never `$ANALYSIS_DATA_DIR`,
so a native build can't record the household's real data.

## Resolution

Slides and the terminal are laid out at 1920x1080 and rendered at
`[video] scale` times that: 2, so the videos are 4K (3840x2160). YouTube
encodes 4K uploads at a much higher bitrate, which keeps terminal text sharp
even for viewers watching at 1080p. The terminal font is 36 layout pixels,
about 84 columns by 22 rows; keep a command's output within that, or set a
smaller font for that tape (see Tapes).

## Thumbnail, preview and ending

Slides fade in from and out to the background color, with two exceptions.
The last scene doesn't fade out, so the closing card is what stays on screen.
The first scene doesn't fade in, so frame 0, which players and file browsers
show as the preview, is the title card. The build also writes the title card
(or the first slide, if there's no `title` slide) as a 1280x720 JPEG beside
the video and embeds it in the MP4 as cover art.

## Publishing on YouTube

`publish` uploads a built video, with its thumbnail, captions and settings,
and keeps its title and description up to date (#395):

```sh
analysis video publish 03-base-scenario --dry-run   # shows what it would send
analysis video publish 03-base-scenario [--lang ja]
analysis video publish --refresh     # every published video's title and description
```

It works from the build in `out/videos` (`--out` for another), so build
first. Each language is its own video. An upload:

- sends the MP4, as **private**, so you can look it over and make it public
  in Studio;
- sets the title card as the custom thumbnail, and uploads that
  language's captions;
- adds it to the language's playlist (`playlist` in `youtube.toml`, by
  title; the playlist must exist);
- records its id in `published.toml` beside the script. Commit that file:
  ids are public, and they're how a later `publish` updates the video
  instead of uploading it again, and how descriptions link to it.

A video already up only has its title, description and tags updated, and
the same goes for the videos before and after it, whose descriptions link
to it. Privacy is never touched after the upload. YouTube can't replace a
video's picture, so after a rebuild, `--new-upload` makes a new video (with
a new link); delete the old one in Studio.

### Titles and descriptions

A script's front matter holds what only that video says, in a `[youtube]`
table:

```toml
[youtube]
title = "Creating a base scenario"  # optional; default: the front matter's title
summary = """
Alice and Bill build their base scenario: the household as it is today, \
and the plan as it stands."""
contents = [                        # the "In this video" list
  "Add a person to the household (sim person add)",
]
tags = ["base scenario"]            # after the channel's and the language's
chapters = { contents = "What goes in the base", end = false }
```

The description is put together from it and `youtube.toml`: the summary,
the list, the chapters, the language's footer, a link to the video in each
other language, and the previous and next videos (linked once they're
up). The title is the language's `title` pattern, such as
`Retirement Simulator: {number}—{title}`, where `{number}` is the folder's
prefix.

**A title must show whole.** YouTube cuts long titles off in lists, on
phones and beside a playing video, so `check` refuses one wider than
`title_width` in `youtube.toml` (50, a CJK character counting as two).
Shorten it with `[youtube] title`, which needn't match the title card:
video 1's card says "What it does, and how it works", its YouTube title
"Introduction".

Chapters are the scenes, timed from the build that was uploaded, so they
stay right when narration changes. Each is named by its scene's heading;
the first is the language's `first_chapter` (Introduction). `chapters`
renames one by scene id, and `false` folds it into the one before. As
YouTube requires, a scene shorter than ten seconds joins the chapter
before it, and a video with fewer than three chapters lists none.

A translation's `[youtube]` has its own title, summary, list, tags and
chapter names; it takes `skip`, `previous` and `next` from `script.md`.
Previous and next default to the folders before and after, leaving out
any with `skip = true` (the pilot); `previous = "01-intro"` names one, and
`""` means none. A translation's scene headings name its chapters, so give
each scene its title (``## Scene `id`: タイトル``).

`build` writes the result beside the video, `03-base-scenario.en.youtube.txt`,
to read before uploading; `check` checks the `[youtube]` fields and
YouTube's limits (title length, no `<` or `>`, description size).

### One-time setup

1. In the [Google Cloud console](https://console.cloud.google.com/), make a
   project, and enable **YouTube Data API v3** for it.
2. Under Google Auth Platform (formerly the OAuth consent screen), set
   **Audience** to **External** and leave it in **Testing**, and add the
   Google account that owns or manages the channel as a test user. A
   project made under a Google Workspace organization defaults to
   Internal, which only lets that organization's accounts sign in ("can
   only be used within its organization"). Then, under Clients, make an
   OAuth client of type **Desktop app**. Download its JSON as
   `~/.config/analysis-videos/client_secret.json` (or into
   `$ANALYSIS_VIDEO_SECRETS`).
3. The first `publish` opens a browser to sign in to the channel's account,
   and keeps the token in `token.json` beside the client file. Neither file
   goes in git. While the consent screen is in testing, Google ends a
   sign-in after seven days, and `publish` asks again.

The worker that talks to YouTube, `scripts/videos/youtube_api.py`, runs with
`uv run --script` in its own environment, like the narration worker.

Two of Google's limits:

- A project that hasn't passed Google's API audit can only upload private
  videos, which is the default here anyway; make them public in Studio.
- The default quota, 10,000 units a day, covers only a few uploads (an
  upload is by far the costliest call). Updating descriptions is cheap.

The upload's settings (license, made for kids, embedding, synthetic
content, notifying subscribers, category) come from `youtube.toml`. A few
others the API can't set, such as age restriction, paid promotion,
caption certification and remixing; `youtube.toml` lists them, to set
once in Studio's upload defaults.

The API can't do cards, end screens, or a translation as a second audio
track. Each language is its own video, and its description links to the
others; add a card to the other language's video in Studio if you want one.

## What's committed

Sources only: scripts, templates, assets, the theme, the builder. Videos,
audio and the voice model stay out of git (`out/` is ignored, the model is
in the tool cache).

## Still to decide

- Target length: 5 to 10 minutes per video, as #373 suggests.
- Background music.
