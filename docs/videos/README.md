# Instructional videos

How the videos for [#366](https://github.com/BobKerns/analysis/issues/366) are
made. Everything a video needs is committed here as text: its script, the
terminal sessions it records, its slides and the shared look. The pictures
and sound are rebuilt from those, so a CLI change means re-running a build,
not re-recording by hand, and fixing a sentence re-renders one scene.

```sh
uv run python -m analysis.videos setup                     # once: voice model, VHS, ttyd
uv run python -m analysis.videos build docs/videos/00-pilot
uv run python -m analysis.videos check                     # every script parses, every slide renders
```

The result is `out/videos/00-pilot.en.mp4`, with its captions
(`00-pilot.en.srt`) and thumbnail (`00-pilot.en.jpg`) beside it.

## Layout

| Path | What it holds |
| --- | --- |
| `docs/videos/<nn>-<slug>/script.md` | One video: its scenes, narration beside the commands and slides |
| `docs/videos/<nn>-<slug>/setup.sh` | Optional: prepares the demo data the video's tapes start from |
| `docs/videos/<nn>-<slug>/script.<lang>.md` | A translation of that video's narration and slide text |
| `docs/videos/theme/theme.toml` | The shared look: palette, fonts, terminal colors, voices, timing |
| `docs/videos/theme/templates/*.html` | Slide templates (Jinja2): `title` opens every video, `card` for breaks and endings |
| `docs/videos/assets/*.svg` | Reusable characters and props: Alice, Bill, their home |
| `src/analysis/videos/` | The builder |
| `scripts/videos/tts_kokoro.py` | The narration worker (runs in its own environment) |

`00-pilot` exists to prove the pipeline. Real videos are numbered in the
order of #366's outline (`01-intro`, `02-setup`, ...).

## Writing a script

A script is markdown with TOML front matter between `+++` lines. The front
matter's fields are available to every slide; `data` names the demo data a
tape starts from.

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
- **One visual**: a ```` ```tape ```` block or a ```` ```slide ```` block.
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

- copies the front matter's `data` folder (the prototype personas' data) to
  a fresh, committed scratch folder, after the video's `setup.sh` (if any)
  has run in it, and points `ANALYSIS_DATA_DIR` at it, so every recording
  starts from the same state;
- puts this checkout's `sim`/`analysis` on `PATH`;
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

Notebook scenes (Playwright driving JupyterLab) aren't supported yet; they'll
be a third visual kind beside `tape` and `slide`.

### Slides

A slide block is TOML: `template` names a file in `theme/templates/`, and the
other keys are fields that template reads, overriding front matter of the
same name. A template can show any asset with `{{ asset("alice") | safe }}`,
which inlines the SVG so the theme's CSS can restyle it.

| Template | Fields |
| --- | --- |
| `title` | `series`, `episode`, `title`, `subtitle`, `cast` (assets to show; default home, Alice, Bill) |
| `card` | `series`, `heading`, `body` (text, or a list shown one item per line), `speaker` (one asset, optional) |

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
| Slide | The page its template renders: template, assets, theme, fields |
| Tape | Its commands, the terminal theme, the demo data, the video's `setup.sh`; or `--rerecord` |
| Scene clip | Its narration, its visual, the timing |

So editing one sentence re-synthesizes and re-assembles that scene only. The
build prints what it made and what it reused:

```text
00-pilot [en]
  title               7.5s  narration cached  visual cached  clip cached
  networth           16.1s  narration made    visual cached  clip made
  end                 4.9s  narration cached  visual cached  clip cached
```

Requirements: `ffmpeg`, `uv`, Playwright's Chromium (`uv run playwright
install chromium`), and for tapes VHS and ttyd (`setup` downloads them on
Linux; `brew install vhs` on macOS).

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

By hand, in YouTube Studio:

1. Upload the MP4. On the Details step, choose the `.jpg` as the custom
   thumbnail (custom thumbnails need a verified channel).
2. Under Subtitles (or the Video elements step), Add, then Upload file, With
   timing, and pick the `.srt`, with its language set to match.
3. Each translation's `.srt` goes on the same video under its own language.
   A translation's narration can go on as an extra audio track where the
   channel has multi-language audio; otherwise it's its own video.

The YouTube Data API (`videos.insert`, `thumbnails.set`, `captions.insert`)
could script this once there are several videos; it needs a one-time OAuth
setup.

## What's committed

Sources only: scripts, templates, assets, the theme, the builder. Videos,
audio and the voice model stay out of git (`out/` is ignored, the model is
in the tool cache).

## Still to decide

- Hosting: YouTube (it can auto-translate captions, unevenly) or release
  assets, and where the docs link to them.
- Whether to script the YouTube upload.
- Target length: 5 to 10 minutes per video, as #373 suggests.
- Notebook scenes and background music.
