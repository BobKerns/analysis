"""YouTube titles, descriptions and publishing for the videos (#395); the API itself is mocked."""

from __future__ import annotations

import json
import py_compile
from pathlib import Path
from typing import Any

import pytest

from analysis.videos import script as scripts
from analysis.videos import youtube
from analysis.videos.__main__ import check
from analysis.videos.script import ScriptError


def _video(root: Path, name: str, *, youtube_toml: str, scenes: tuple[str, ...] = ("title", "one", "two", "end"),
           ja: str | None = None, episode: str = "Video 1", title: str = "A video") -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    body = [f'+++\nepisode = "{episode}"\ntitle = "{title}"\n{youtube_toml}\n+++\n']
    for sid in scenes:
        body.append(f"## Scene `{sid}`: The {sid} scene\n\n```slide\ntemplate = \"card\"\n```\n\nSome words.\n")
    (folder / "script.md").write_text("\n".join(body), encoding="utf-8")
    if ja is not None:
        tr = [f"+++\n{ja}\n+++\n"]
        for sid in scenes:
            tr.append(f"## Scene `{sid}`: {sid}の場面\n\n言葉。\n")
        (folder / "script.ja.md").write_text("\n".join(tr), encoding="utf-8")
    return folder


YT = '[youtube]\nsummary = "What it shows."\ncontents = ["One thing", "Another"]\ntags = ["extra"]'
YT_JA = 'title = "動画"\n[youtube]\nsummary = "内容。"'


def _lengths(s: scripts.Script, seconds: float = 30.0) -> list[tuple[str, float]]:
    return [(scene.id, seconds) for scene in s.scenes]


def test_description_has_summary_list_chapters_footer_and_neighbors(videos_root: Path) -> None:
    _video(videos_root, "01-first", youtube_toml=YT, title="First")
    folder = _video(videos_root, "02-second", youtube_toml=YT, episode="Video 2", title="Second\\nhalf")
    _video(videos_root, "03-third", youtube_toml=YT, episode="Video 3", title="Third")
    meta = youtube.metadata(scripts.load(folder), [("title", 7.0), ("one", 65.0), ("two", 4.0), ("end", 30.0)])
    assert meta.title == "Retirement Simulator: 02—Second half"
    # The 7 s title card takes in the scene after it, and the 4 s one joins
    # the one before: two chapters, which YouTube wouldn't show, so none.
    assert meta.description == (
        "What it shows.\n\n"
        "In this video:\n• One thing\n• Another\n\n"
        "Alice and Bill are made-up people, and their numbers are examples, not advice.\n\n"
        "Previous: Video 1, First\nNext: Video 3, Third"
    )
    assert meta.tags[-1] == "extra" and meta.privacy == "private" and meta.category_id == "27"
    assert meta.playlist == "Retirement Simulator Guide"


def test_chapters_follow_youtube_rules(videos_root: Path) -> None:
    folder = _video(videos_root, "01-x", youtube_toml=YT + '\nchapters = { two = "Renamed", end = false }',
                    scenes=("title", "one", "short", "two", "three", "end"))
    s = scripts.load(folder)
    marks = youtube.chapters(s, [("title", 12), ("one", 30), ("short", 5), ("two", 3600), ("three", 20),
                                 ("end", 15)], "Intro")
    assert [(youtube.timestamp(c.start), c.title) for c in marks] == [
        ("0:00", "Intro"), ("0:12", "The one scene"), ("0:47", "Renamed"), ("1:00:47", "The three scene")]
    # Fewer than three chapters is none.
    assert youtube.chapters(s, [("title", 30), ("one", 30), ("short", 1), ("two", 2), ("three", 3),
                                ("end", 4)], "Intro") == []


def test_translation_has_its_own_words_and_inherits_its_place(videos_root: Path) -> None:
    folder = _video(videos_root, "01-x", youtube_toml=YT + '\ntitle = "Short"\nnext = ""', ja=YT_JA)
    _video(videos_root, "02-y", youtube_toml=YT)
    ja = scripts.load(folder, "ja")
    assert ja.meta["youtube"] == {"next": "", "summary": "内容。"}  # not the English title, tags or list
    meta = youtube.metadata(ja, _lengths(ja))
    assert meta.title == "Retirement Simulator: 01—動画"
    assert "0:00 はじめに\n0:30 oneの場面" in meta.description
    assert "次回" not in meta.description  # next = "" in script.md: no next video


def test_untitled_translation_scene_keeps_the_english_title(videos_root: Path) -> None:
    folder = _video(videos_root, "01-x", youtube_toml=YT)
    (folder / "script.ja.md").write_text("## Scene `title`\n\n言葉。\n\n## Scene `one`\n\n言葉。\n\n"
                                         "## Scene `two`\n\n言葉。\n\n## Scene `end`\n\n言葉。\n", encoding="utf-8")
    assert scripts.load(folder, "ja").scenes[1].title == "The one scene"


def test_skipped_videos_are_not_neighbors(videos_root: Path) -> None:
    _video(videos_root, "00-pilot", youtube_toml="[youtube]\nskip = true")
    folder = _video(videos_root, "01-x", youtube_toml=YT)
    assert youtube.neighbors(scripts.load(folder)) == (None, None)


@pytest.mark.parametrize(("extra", "message"), [
    ('\nsurprise = 1', "unknown field 'surprise'"),
    ('\nchapters = { nope = "x" }', "scene `nope`"),
    ('\ncontents = "one"', "contents must be a list"),
    ('\ntitle = "' + "x" * 120 + '"', "title is 1"),
    ('\ntitle = "a <b>"', "refuses < and >"),
    ('\nprevious = "99-missing"', "no such video"),
])
def test_youtube_fields_are_checked(videos_root: Path, extra: str, message: str) -> None:
    folder = _video(videos_root, "01-x", youtube_toml="[youtube]\nsummary = \"x\"" + extra)
    with pytest.raises(ScriptError, match=message):
        youtube.check(scripts.load(folder))
    assert check([folder]) == 1


def test_publish_needs_a_summary(videos_root: Path) -> None:
    folder = _video(videos_root, "01-x", youtube_toml='[youtube]\ncontents = ["x"]')
    youtube.check(scripts.load(folder))  # fine until it's published
    with pytest.raises(ScriptError, match="needs a summary"):
        youtube.metadata(scripts.load(folder), _lengths(scripts.load(folder)))


def _built(out: Path, folder: Path, lang: str, lengths: list[tuple[str, float]]) -> None:
    out.mkdir(exist_ok=True)
    stem = f"{folder.name}.{lang}"
    for suffix in (".mp4", ".srt", ".jpg"):
        (out / f"{stem}{suffix}").write_bytes(b"x")
    youtube.write_scene_lengths(out / f"{stem}.mp4", lengths)


class FakeYouTube:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: dict[str, Any], repo: Path) -> dict[str, Any]:
        self.requests.append(request)
        if request.get("upload"):
            return {"video": f"id{len(self.requests)}"}
        return {"video": request["video"]}


def test_publish_uploads_then_links_the_languages(videos_root: Path, tmp_path: Path) -> None:
    folder = _video(videos_root, "01-x", youtube_toml=YT, ja=YT_JA)
    s = scripts.load(folder)
    out = tmp_path / "out"
    for lang in ("en", "ja"):
        _built(out, folder, lang, _lengths(s, 20.0))
    api = FakeYouTube()
    youtube.publish([folder], out_dir=out, repo=tmp_path, worker=api, log=lambda _: None)

    uploads = [r for r in api.requests if r.get("upload")]
    assert [r["captions"]["language"] for r in uploads] == ["en", "ja"]
    assert uploads[0]["upload"]["status"]["privacyStatus"] == "private"
    assert uploads[1]["playlist"] == "Retirement Simulator (日本語)"
    updates = {r["video"]: r["snippet"]["description"] for r in api.requests if not r.get("upload")}
    assert "日本語版: https://youtu.be/id2" in updates["id1"]
    assert "English version: https://youtu.be/id1" in updates["id2"]
    assert all("status" not in r for r in api.requests if not r.get("upload"))  # privacy stays as set in Studio

    published = youtube.load_published(folder)
    assert published.video("en") == "id1" and published.lengths("ja") == _lengths(s, 20.0)

    # Again: nothing to upload, descriptions refreshed from the recorded lengths, not a rebuild's.
    _built(out, folder, "en", _lengths(s, 99.0))
    again = FakeYouTube()
    youtube.publish([folder], out_dir=out, repo=tmp_path, worker=again, log=lambda _: None)
    assert not any(r.get("upload") for r in again.requests)
    assert "0:20 The one scene" in again.requests[0]["snippet"]["description"]


def test_publishing_a_video_refreshes_its_neighbors_links(videos_root: Path, tmp_path: Path) -> None:
    first = _video(videos_root, "01-x", youtube_toml=YT)
    second = _video(videos_root, "02-y", youtube_toml=YT, episode="Video 2", title="Second")
    out = tmp_path / "out"
    for folder in (first, second):
        _built(out, folder, "en", _lengths(scripts.load(folder)))
    youtube.publish([first], out_dir=out, repo=tmp_path, worker=FakeYouTube(), log=lambda _: None)
    api = FakeYouTube()
    youtube.publish([second], out_dir=out, repo=tmp_path, worker=api, log=lambda _: None)
    updates = {r["video"]: r["snippet"]["description"] for r in api.requests if not r.get("upload")}
    assert "Next: Video 2, Second https://youtu.be/id1" in updates["id1"]  # 01's, now linking to 02


def test_dry_run_sends_and_writes_nothing(videos_root: Path, tmp_path: Path) -> None:
    folder = _video(videos_root, "01-x", youtube_toml=YT)
    _built(tmp_path / "out", folder, "en", _lengths(scripts.load(folder)))
    lines: list[str] = []
    youtube.publish([folder], out_dir=tmp_path / "out", repo=tmp_path, dry_run=True,
                    worker=lambda *_: pytest.fail("sent"), log=lines.append)
    assert not (folder / youtube.PUBLISHED_FILE).exists()
    assert any("would upload 01-x.en.mp4 as private" in line for line in lines)
    assert any("Retirement Simulator: 01—A video" in line for line in lines)


def test_a_failed_step_after_the_upload_keeps_the_id(videos_root: Path, tmp_path: Path) -> None:
    folder = _video(videos_root, "01-x", youtube_toml=YT)
    _built(tmp_path / "out", folder, "en", _lengths(scripts.load(folder)))
    with pytest.raises(ScriptError, match="403: quota.*youtu.be/abc is up"):
        youtube.publish([folder], out_dir=tmp_path / "out", repo=tmp_path, log=lambda _: None,
                        worker=lambda *_: {"video": "abc", "error": "403: quota"})
    assert youtube.load_published(folder).video("en") == "abc"


def test_publish_refuses_a_stale_or_missing_build(videos_root: Path, tmp_path: Path) -> None:
    folder = _video(videos_root, "01-x", youtube_toml=YT)
    with pytest.raises(ScriptError, match="not built"):
        youtube.publish([folder], out_dir=tmp_path / "out", repo=tmp_path, worker=FakeYouTube(), log=lambda _: None)
    _built(tmp_path / "out", folder, "en", [("title", 1.0)])
    with pytest.raises(ScriptError, match="other scenes"):
        youtube.publish([folder], out_dir=tmp_path / "out", repo=tmp_path, worker=FakeYouTube(), log=lambda _: None)


def test_published_toml_round_trips(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    p = youtube.Published(tmp_path / youtube.PUBLISHED_FILE)
    p.record("en", "abc", [("title", 7.25), ("end", 4.0)], datetime(2026, 10, 31, tzinfo=UTC))
    again = youtube.load_published(tmp_path)
    assert again.video("en") == "abc" and again.lengths("en") == [("title", 7.25), ("end", 4.0)]


def test_scene_lengths_file_is_json(tmp_path: Path) -> None:
    path = youtube.write_scene_lengths(tmp_path / "v.en.mp4", [("a", 1.23456)])
    assert path.name == "v.en.scenes.json" and json.loads(path.read_text()) == [["a", 1.235]]


def test_upload_worker_compiles(repo_root: Path) -> None:
    py_compile.compile(str(repo_root / "scripts" / "videos" / "youtube_api.py"), doraise=True)


def test_every_published_repo_video_has_a_description(repo_root: Path) -> None:
    root = repo_root / "docs" / "videos"
    for folder in youtube.video_folders(root):
        for lang in scripts.languages(folder):
            s = scripts.load(folder, lang)
            if not youtube.skipped(s):
                youtube.metadata(s, _lengths(s))


@pytest.mark.slow
def test_build_writes_the_scene_lengths_and_the_description(videos_root: Path, repo_root: Path,
                                                             tmp_path: Path) -> None:
    from playwright.sync_api import Error

    from analysis.videos.build import build

    folder = _video(videos_root, "01-x", youtube_toml=YT, scenes=("title", "end"))
    try:
        result = build(folder, out_dir=tmp_path / "out", engine="silent", repo=repo_root, log=lambda _: None)
    except Error as error:
        pytest.skip(f"no browser: {error.message.splitlines()[0]}")
    lengths = json.loads(youtube.scene_lengths_path(result.video).read_text())
    assert [sid for sid, _ in lengths] == ["title", "end"]
    assert result.description is not None
    assert result.description.read_text().startswith("Retirement Simulator: 01—A video\n\nWhat it shows.")
    # And publish finds that build.
    assert youtube.built(scripts.load(folder), tmp_path / "out").lengths == [tuple(x) for x in lengths]


def test_worker_answer_is_its_last_line() -> None:
    # A library printing before the answer (the sign-in prompt did) doesn't hide the id.
    assert youtube.worker_answer('Sign in: https://x\n{"video": "abc"}\n', 0) == {"video": "abc"}
    assert youtube.worker_answer('{"video": "abc", "error": "403: quota"}\n', 1)["error"] == "403: quota"
    assert youtube.worker_answer("Traceback ...\n", 1) == {"video": None, "error": "Traceback ..."}
