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

The result is `out/videos/00-pilot.en.mp4` with captions beside it in
`out/videos/00-pilot.en.srt`.

## Layout

| Path | What it holds |
| --- | --- |
| `docs/videos/<nn>-<slug>/script.md` | One video: its scenes, narration beside the commands and slides |
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
  a fresh, committed scratch folder and points `ANALYSIS_DATA_DIR` at it, so
  every recording starts from the same state;
- puts this checkout's `sim`/`analysis` on `PATH`;
- sets the prompt (`alice@home $`, from the theme).

Set-up the viewer shouldn't see goes between `Hide` and `Show`. Pace the
typing with `Sleep` so the picture keeps up with the narration; the
narration starts `lead` seconds into the scene. A tape must not set
`Output`.

The demo data's bytes are part of a tape's cache key, but the CLI's
behavior isn't, so after a CLI change rebuild with `--rerecord`.

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
| `card` | `series`, `heading`, `body`, `speaker` (one asset, optional) |

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
| Tape | Its commands, the terminal theme, the demo data; or `--rerecord` |
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

## What's committed

Sources only: scripts, templates, assets, the theme, the builder. Videos,
audio and the voice model stay out of git (`out/` is ignored, the model is
in the tool cache).

## Still to decide

- Hosting: YouTube (it can auto-translate captions, unevenly) or release
  assets, and where the docs link to them.
- Target length: 5 to 10 minutes per video, as #373 suggests.
- Notebook scenes, background music, and pronunciation hints for words the
  voice gets wrong.
