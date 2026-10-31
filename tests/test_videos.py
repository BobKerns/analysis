"""Video production (#373): script parsing, translations, captions, slides, builds."""
# cspell:ignore HIHR

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from analysis.videos import captions, render
from analysis.videos import script as scripts
from analysis.videos import theme as themes
from analysis.videos.__main__ import TTYD_URL, VHS_URL, check
from analysis.videos.build import VIDEO_DATA_ENV, _Builder, build

SCRIPT = """\
+++
series = "Series"
title = "A <video>"
data = "prototype_data"
+++

# Heading for readers, not spoken

Intro text before the first scene is a note.

## Scene `intro`: Opening

```slide
template = "title"
subtitle = "Sub"
```

Meet **Alice** and [Bill](https://example.com). They plan.

> A production note,
> over two lines.

- a list item
  with a continuation

Second paragraph!

## Scene `demo`: A terminal

```tape
Type "sim check"
Enter
```

Run the `sim check` command.
"""


def _write(folder: Path, text: str, name: str = "script.md") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    return folder


def test_every_repo_video_checks(repo_root: Path) -> None:
    folders = sorted(p for p in (repo_root / "docs" / "videos").iterdir() if (p / "script.md").is_file())
    assert folders, "the pilot video should exist"
    assert check(folders) == 0


def test_scenes_narration_and_visuals(videos_root: Path) -> None:
    s = scripts.load(_write(videos_root / "01-x", SCRIPT))
    assert [sc.id for sc in s.scenes] == ["intro", "demo"]
    intro, demo = s.scenes
    assert intro.title == "Opening"
    assert intro.narration == ("Meet Alice and Bill. They plan.", "Second paragraph!")
    assert intro.sentences == ("Meet Alice and Bill.", "They plan.", "Second paragraph!")
    assert intro.visual.kind == "slide" and intro.visual.fields == {"template": "title", "subtitle": "Sub"}
    assert demo.visual.kind == "tape" and "sim check" in demo.visual.source
    assert demo.narration == ("Run the sim check command.",)
    assert s.meta["title"] == "A <video>"


def test_cjk_sentences_split_on_full_stops() -> None:
    scene = scripts.Scene("a", "a", ("アリスです。ビルです。",), scripts.Visual("slide", ""), 1)
    assert scene.sentences == ("アリスです。", "ビルです。")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("## Scene `a`: A\n\nWords.\n", "no ```tape or ```slide"),
        ("## Scene `a`: A\n\n```slide\ntemplate='title'\n```\n\n## Scene `a`: B\n", "appears twice"),
        ("## Scene `a`: A\n\n```tape\nOutput x.gif\n```\n", "must not set Output"),
        ("## Scene `a`: A\n\n```slide\nheading='x'\n```\n", "needs template"),
        ("## Not a scene\n", "must be ## Scene"),
        ("## Scene `a`: A\n\n```slide\ntemplate='title'\n", "never closed"),
        ("Nothing here.\n", "no scenes"),
    ],
)
def test_script_errors(videos_root: Path, body: str, message: str) -> None:
    folder = _write(videos_root / "01-x", body)
    with pytest.raises(scripts.ScriptError, match=message):
        scripts.load(folder)


TRANSLATION = """\
+++
title = "動画"
+++

## Scene `intro`: 開幕

```slide
subtitle = "サブ"
```

アリスとビルです。

## Scene `demo`: 端末

コマンドを実行します。
"""


def test_translation_overlays_narration_and_slide_fields(videos_root: Path) -> None:
    folder = _write(videos_root / "01-x", SCRIPT)
    _write(folder, TRANSLATION, "script.ja.md")
    assert scripts.languages(folder) == ["en", "ja"]
    ja = scripts.load(folder, "ja")
    intro, demo = ja.scenes
    assert ja.lang == "ja" and ja.meta["title"] == "動画" and ja.meta["series"] == "Series"
    assert intro.narration == ("アリスとビルです。",)
    assert intro.visual.fields == {"template": "title", "subtitle": "サブ"}
    assert demo.visual.kind == "tape" and demo.visual.source == scripts.load(folder).scenes[1].visual.source


@pytest.mark.parametrize(
    ("translation", "message"),
    [
        ("## Scene `other`: X\n\nこれ。\n", "isn't in script.md"),
        ("## Scene `intro`: X\n\nこれ。\n", "`demo` has no ja narration"),
        ("## Scene `intro`: X\n\n## Scene `demo`: Y\n\nこれ。\n", "`intro` has no ja narration"),
        ("## Scene `demo`: X\n\n```tape\nType 'x'\n```\n\nこれ。\n", "can't change a tape"),
        ("## Scene `intro`: X\n\n```slide\ntemplate='card'\n```\n\nこれ。\n## Scene `demo`: Y\n\nこれ。\n",
         "can't change a slide's template"),
    ],
)
def test_translation_errors(videos_root: Path, translation: str, message: str) -> None:
    folder = _write(videos_root / "01-x", SCRIPT)
    _write(folder, translation, "script.ja.md")
    with pytest.raises(scripts.ScriptError, match=message):
        scripts.load(folder, "ja")


def test_missing_translation_file(videos_root: Path) -> None:
    folder = _write(videos_root / "01-x", SCRIPT)
    with pytest.raises(scripts.ScriptError, match="no script.fr.md"):
        scripts.load(folder, "fr")


def test_theme_has_a_voice_per_translation_language(repo_root: Path) -> None:
    theme = themes.load(repo_root / "docs" / "videos")
    assert theme.narration("en")["voice"]
    with pytest.raises(scripts.ScriptError, match=r"\[narration.xx\]"):
        theme.narration("xx")


def test_scale_renders_the_layout_at_more_pixels(videos_root: Path) -> None:
    theme = themes.load(videos_root)
    theme.data["video"]["scale"] = 2
    theme.data["terminal"]["font_size"] = 36
    settings = theme.vhs_settings()
    assert (theme.out_width, theme.out_height) == (2 * theme.width, 2 * theme.height)
    assert "Set FontSize 72" in settings and f"Set Width {2 * theme.width}" in settings
    assert f"--width: {theme.width}px;" in theme.css()  # templates lay out in layout pixels
    prompt = theme.data["terminal"]["prompt"]
    assert any(s.startswith("Set WaitPattern /") and prompt in s for s in settings)


def test_slide_html_fills_template_inlines_assets_and_escapes(videos_root: Path) -> None:
    theme = themes.load(videos_root)
    html = render.slide_html(theme, {"series": "S", "title": "A <video>"}, {"template": "title"}, "en")
    assert "A &lt;video&gt;" in html
    for name in ("alice", "bill", "home"):
        assert f'class="character {name}"' in html or f'class="prop {name}"' in html
    assert "--accent:" in html and '<html lang="en">' in html
    with pytest.raises(scripts.ScriptError, match="no asset 'carol'"):
        render.slide_html(theme, {}, {"template": "card", "heading": "h", "speaker": "carol"}, "en")
    with pytest.raises(scripts.ScriptError, match="no slide template 'nope'"):
        render.slide_html(theme, {}, {"template": "nope"}, "en")


def test_captions_are_timed_per_sentence_and_split_long_ones() -> None:
    long = "They're planning their retirement, and they want to know one thing: will the money last?"
    cues = captions.scene_cues(["Short.", long], [1.0, 4.0], start=10.0, gap=0.5)
    assert [(c.start, c.text) for c in cues[:2]] == [(10.0, "Short."), (11.5, cues[1].text)]
    assert len(cues) == 3 and cues[-1].end == pytest.approx(15.5)
    assert " ".join(c.text for c in cues[1:]) == long
    srt = captions.srt(cues)
    assert srt.startswith("1\n00:00:10,000 --> 00:00:11,000\nShort.\n")
    assert all(len(block.splitlines()) <= 4 for block in srt.split("\n\n"))  # number, time, ≤2 lines


def test_compose_holds_the_picture_and_delays_the_narration(videos_root: Path, tmp_path: Path) -> None:
    theme = themes.load(videos_root)
    args = render.compose_args("ffmpeg", theme, visual=tmp_path / "s.png", still=True, audio=tmp_path / "n.wav",
                               length=7.5, lead=0.6, out=tmp_path / "c.mp4")
    graph = args[args.index("-filter_complex") + 1]
    assert "-loop" in args and "adelay=600" in graph and "fade=t=out:st=7.000" in graph
    assert "fade=t=in" in graph
    opening = render.compose_args("ffmpeg", theme, visual=tmp_path / "s.png", still=True, audio=None,
                                  length=7.5, lead=0.6, out=tmp_path / "c.mp4", fade_in=False)
    assert "fade=t=in" not in opening[opening.index("-filter_complex") + 1]  # frame 0 is the preview
    closing = render.compose_args("ffmpeg", theme, visual=tmp_path / "s.png", still=True, audio=None,
                                  length=7.5, lead=0.6, out=tmp_path / "c.mp4", fade_out=False)
    assert "fade=t=out" not in closing[closing.index("-filter_complex") + 1]  # the last card stays up
    tape = render.compose_args("ffmpeg", theme, visual=tmp_path / "t.mp4", still=False, audio=None,
                               length=12.0, lead=0.6, out=tmp_path / "c.mp4")
    graph = tape[tape.index("-filter_complex") + 1]
    assert "tpad=stop_mode=clone" in graph and "fade" not in graph and "anullsrc" in " ".join(tape)


SLIDES_ONLY = """\
+++
series = "Series"
title = "Slides"
+++

## Scene `intro`: Opening

```slide
template = "title"
```

Meet Alice and Bill.

## Scene `end`: Ending

```slide
template = "card"
heading = "The end"
```

Goodbye for now.
"""


@pytest.mark.slow
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_narration_edit_rebuilds_only_that_scene(videos_root: Path, repo_root: Path, tmp_path: Path) -> None:
    folder = _write(videos_root / "01-slides", SLIDES_ONLY)
    out = tmp_path / "out"
    from playwright.sync_api import Error

    try:
        first = build(folder, out_dir=out, engine="silent", repo=repo_root, log=lambda _: None)
    except Error as error:
        pytest.skip(f"no browser: {error.message.splitlines()[0]}")
    assert [(s.narration, s.visual, s.clip) for s in first.scenes] == [("made", "made", "made")] * 2
    assert first.video.is_file() and first.captions.read_text().count("-->") == 2
    assert render.duration(first.video) == pytest.approx(first.length, abs=0.2)
    assert first.thumbnail.is_file() and first.thumbnail.suffix == ".jpg"

    (folder / "script.md").write_text(SLIDES_ONLY.replace("Goodbye for now.", "See you next time."))
    second = build(folder, out_dir=out, engine="silent", repo=repo_root, log=lambda _: None)
    states = {s.id: (s.narration, s.visual, s.clip) for s in second.scenes}
    assert states == {"intro": ("cached", "cached", "cached"), "end": ("made", "cached", "made")}
    assert "See you next time." in second.captions.read_text()


@pytest.fixture
def demo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private DEMO_ROOT: the real one is a fixed path, shared by parallel test workers."""
    root = tmp_path / "demo-root"
    monkeypatch.setattr(render, "DEMO_ROOT", root)
    return root


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_prepare_demo_runs_setup_and_commits_what_it_leaves(videos_root: Path, tmp_path: Path, demo_root: Path) -> None:
    theme = themes.load(videos_root)
    data = tmp_path / "data"
    data.mkdir()
    (data / "scenario.csv").write_text("name\nbase\n")
    setup = tmp_path / "setup.sh"
    setup.write_text('echo "$ANALYSIS_DATA_DIR" > "$ANALYSIS_DATA_DIR/where.txt"\n')
    store = render.prepare_demo(data, setup, theme, tmp_path / "store")
    assert (store / "data" / "where.txt").read_text().strip() == str(demo_root / "data")
    status = render.run(["git", "status", "--porcelain"], cwd=store / "data").stdout
    assert status == ""
    log = render.run(["git", "log", "--format=%s %an"], cwd=store / "data").stdout.split("\n")
    assert log[:2] == ["setup Demo", "demo data Demo"]
    assert (store / ".videorc").is_file() and not demo_root.exists()


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_prepare_demo_reports_a_failing_setup(videos_root: Path, tmp_path: Path, demo_root: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "scenario.csv").write_text("name\nbase\n")
    setup = tmp_path / "setup.sh"
    setup.write_text("echo before\nfalse\necho never\n")
    with pytest.raises(scripts.ScriptError, match=r"setup\.sh failed \(1\)"):
        render.prepare_demo(data, setup, themes.load(videos_root), tmp_path / "store")


def test_a_tape_can_set_its_own_font_size(videos_root: Path) -> None:
    theme = themes.load(videos_root)
    tape = render.full_tape("Set FontSize 24\nType \"sim explain\"\nEnter\n", theme, "/rc", Path("/out.mp4"))
    lines = tape.splitlines()
    theme_size = next(i for i, line in enumerate(lines) if line.startswith("Set FontSize"))
    assert lines[theme_size + 1 :].index(f"Set FontSize {24 * theme.scale}") < lines.index("Hide")
    assert "Set FontSize 24" not in lines[lines.index("Hide") :]
    with pytest.raises(scripts.ScriptError, match="only FontSize"):
        render.full_tape("Set Width 800\nEnter\n", theme, "/rc", Path("/out.mp4"))


def test_respellings_change_the_voice_not_the_captions(videos_root: Path) -> None:
    folder = _write(videos_root / "01-say", SCRIPT.replace(
        "Second paragraph!", "Every run {records|ree-cords} it, in Jupyter or {Jupyter|/dʒˈuːpɪɾɚ/}."))
    intro = scripts.load(folder).scenes[0]
    assert intro.sentences[-1] == "Every run records it, in Jupyter or Jupyter."
    lexicon = {"Jupyter": "Jupiter", "run": "/ɹˈʌn/"}
    assert intro.spoken(lexicon)[-1] == "Every ⟦ɹˈʌn⟧ ree-cords it, in Jupiter or ⟦dʒˈuːpɪɾɚ⟧."
    assert intro.spoken()[-1] == "Every run ree-cords it, in Jupyter or ⟦dʒˈuːpɪɾɚ⟧."


def test_an_unclosed_respelling_is_an_error(videos_root: Path) -> None:
    folder = _write(videos_root / "01-say", SCRIPT.replace("Second paragraph!", "Say {records here."))
    with pytest.raises(scripts.ScriptError, match=r"script\.md:\d+: a respelling is written"):
        scripts.load(folder)


def test_card_body_list_is_one_item_per_line(videos_root: Path) -> None:
    theme = themes.load(videos_root)
    html = render.slide_html(theme, {}, {"template": "card", "heading": "H", "body": ["One?", "<Two>"]}, "en")
    assert '<div class="line">One?</div><div class="line">&lt;Two&gt;</div>' in html


def test_window_draws_command_lines_files_and_a_blind_prompt(videos_root: Path) -> None:
    theme = themes.load(videos_root)
    lines = [{"prompt": "PS C:\\>", "typed": "wsl --install"}, "<done>", {"prompt": "New password:", "secret": True}]
    html = render.slide_html(theme, {}, {"template": "window", "title": "Ubuntu", "lines": lines}, "en")
    assert 'class="window terminal"' in html  # the default look
    assert '<div class="command">PS C:\\&gt; <span class="typed">wsl --install</span></div>' in html
    assert "<div>&lt;done&gt;</div>" in html
    assert '<div class="command">New password:<span class="cursor"></span></div>' in html
    files = render.slide_html(
        theme, {}, {"template": "window", "look": "files", "address": "\\\\wsl$", "lines": ["data"]}, "en")
    assert '<div class="address">\\\\wsl$</div>' in files
    assert '<div class="row"><span class="folder"></span>data</div>' in files


def _tape_builder(videos_root: Path, repo_root: Path, tmp_path: Path, front: str) -> _Builder:
    folder = _write(videos_root / "01-own-data", SCRIPT.replace('data = "prototype_data"\n', front))
    theme = themes.load(videos_root)
    return _Builder(scripts.load(folder), theme, tmp_path, repo_root, engine="silent", rerecord=False)


def test_tape_data_front_matter_wins_over_the_videos_own_repo(
    videos_root: Path, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VIDEO_DATA_ENV, str(tmp_path))
    builder = _tape_builder(videos_root, repo_root, tmp_path, 'data = "prototype_data"\n')
    assert builder.tape_data() == repo_root / "prototype_data"


def test_tape_data_falls_back_to_the_videos_own_repo(
    videos_root: Path, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = tmp_path / "own"
    (own / "scenarios").mkdir(parents=True)  # a partial sim init: no persons.csv
    builder = _tape_builder(videos_root, repo_root, tmp_path, "")
    monkeypatch.delenv(VIDEO_DATA_ENV, raising=False)
    # Never the planner's real data repo, even when it's set.
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(repo_root / "prototype_data"))
    with pytest.raises(scripts.ScriptError, match="needs data"):
        builder.tape_data()
    monkeypatch.setenv(VIDEO_DATA_ENV, str(own))
    with pytest.raises(scripts.ScriptError, match="doesn't exist yet.*docker.sh shell 01-own-data"):
        builder.tape_data()
    (own / "persons.csv").write_text("id\n")
    assert builder.tape_data() == own


def test_video_image_pins_the_builders_tool_versions(repo_root: Path) -> None:
    dockerfile = (repo_root / "docker" / "videos" / "Dockerfile").read_text()
    args = dict(re.findall(r"^ARG (\w+)=(\S+)$", dockerfile, re.MULTILINE))
    assert f"/v{args['VHS_VERSION']}/vhs_{args['VHS_VERSION']}_" in VHS_URL
    assert f"/{args['TTYD_VERSION']}/ttyd." in TTYD_URL
    assert args["KOKORO_RELEASE"] == render.KOKORO_RELEASE
    for name in render.KOKORO_FILES:
        assert name in dockerfile


def test_long_cjk_captions_break_after_commas() -> None:
    sentence = (
        "この年代の多くの人と同じように、ふたりの貯蓄はいくつかの種類の口座に分かれていて、聞きたいことがたくさんあります。"
    )
    cues = captions.scene_cues([sentence], [6.0], start=0.0, gap=0.3)
    assert [c.text for c in cues] == [
        "この年代の多くの人と同じように、",
        "ふたりの貯蓄はいくつかの種類の口座に分かれていて、",
        "聞きたいことがたくさんあります。",
    ]
    assert cues[-1].end == pytest.approx(6.0)
    assert [c.text for c in captions.scene_cues(["短い文です。"], [1.0], start=0.0, gap=0.3)] == ["短い文です。"]


FIGURE = """\
+++
series = "Series"
title = "Figure"
data = "demo"
+++

## Scene `graph`: A picture

```slide
template = "figure"
heading = "sim chain show spending --graph"
demo_image = "home/graph.png"
```

The chain as a flowchart.
"""


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_a_slide_shows_a_picture_setup_made(videos_root: Path, tmp_path: Path, demo_root: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / "demo").mkdir(parents=True)
    (repo / "demo" / "scenario.csv").write_text("name\nbase\n")
    folder = _write(videos_root / "03-figure", FIGURE)
    (folder / "setup.sh").write_text('mkdir -p ../home && printf "PNG one" > ../home/graph.png\n')
    pages: list[str] = []

    def screenshot(html: str, theme: themes.Theme, out: Path) -> None:
        pages.append(html)
        out.write_bytes(b"png")

    monkeypatch.setattr(render, "render_slide", screenshot)
    scene = scripts.load(folder).scenes[0]
    cache = tmp_path / "cache"
    cache.mkdir()

    def visual() -> tuple[str, str]:
        builder = _Builder(scripts.load(folder), themes.load(videos_root), cache, repo, engine="silent",
                           rerecord=False, setup=folder / "setup.sh")
        try:
            _, _, state, key = builder.visual(scene)
        finally:
            if builder.prepared is not None:
                shutil.rmtree(builder.prepared)
        return state, key

    made, key = visual()
    assert made == "made" and "data:image/png;base64,UE5HIG9uZQ==" in pages[-1]
    assert visual() == ("cached", key)  # read from the cache without preparing the demo again
    (folder / "setup.sh").write_text('mkdir -p ../home && printf "PNG two" > ../home/graph.png\n')
    state, new_key = visual()
    assert state == "made" and new_key != key and "UE5HIHR3bw==" in pages[-1]
    (folder / "setup.sh").write_text("true\n")
    with pytest.raises(scripts.ScriptError, match="setup.sh made no home/graph.png"):
        visual()
