"""The chain flowchart as an image: `sim chain show --graph|--graph-file` (issue #142)."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import chain_graph

# A PNG header claiming 640 x 200 pixels, which is all png_size() reads.
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + (640).to_bytes(4) + (200).to_bytes(4) + b"\0" * 5000


def _sim(data_dir: Path, *args: str):
    return CliRunner().invoke(
        main, ["sim", "chain", "show", *args, "--scenario", "retire_alice", "--data-dir", str(data_dir)],
        terminal_width=200,
    )


def test_kitty_sequence_sends_the_png_in_chunks() -> None:
    sequence = chain_graph.kitty_sequence(FAKE_PNG, columns=40)
    chunks = sequence.removesuffix("\n").split("\x1b\\")[:-1]
    assert len(chunks) == 2  # 5000-odd bytes is more than one 4096-byte chunk of base64
    assert chunks[0].startswith("\x1b_Ga=T,f=100,q=2,c=40,m=1;")
    assert chunks[1].startswith("\x1b_Gm=0;")
    data = "".join(chunk.split(";", 1)[1] for chunk in chunks)
    assert base64.standard_b64decode(data) == FAKE_PNG
    assert ",c=" not in chain_graph.kitty_sequence(FAKE_PNG)  # no width: the terminal picks


def test_iterm_sequence() -> None:
    sequence = chain_graph.iterm_sequence(FAKE_PNG, columns=40)
    assert sequence.startswith(f"\x1b]1337;File=inline=1;size={len(FAKE_PNG)};width=40;preserveAspectRatio=1:")
    assert sequence.endswith("\x07\n")


def test_terminal_scale_matches_the_terminal_text() -> None:
    assert chain_graph.terminal_scale(None) == 2.0  # the terminal doesn't say
    # A Retina cell is twice an ordinary one in the pixels the terminal draws
    # images in, so the chart is drawn twice as large and shows the same size.
    assert chain_graph.terminal_scale((16.0, 38.4)) == 2 * chain_graph.terminal_scale((8.0, 19.2)) == 2.0


def test_png_size_and_fit_columns() -> None:
    assert chain_graph.png_size(FAKE_PNG) == (640, 200)
    assert chain_graph.fit_columns(640, 200, 8.0) == 80  # fits: its own width
    assert chain_graph.fit_columns(6400, 200, 8.0) == 200  # too wide: shrinks to the terminal
    assert chain_graph.fit_columns(640, 200, None) is None  # cell size unknown


@pytest.mark.parametrize(("reply", "term_program", "expected"), [
    (b"\x1b_Gi=31;OK\x1b\\\x1b[?62;22c", None, "kitty"),
    (b"\x1b[?62;22c", "vscode", "iterm"),  # no kitty answer: an older VS Code
    (b"\x1b_Gi=31;OK\x1b\\\x1b[?62;22c", "iTerm.app", "iterm"),  # its own protocol, though it answers
    (b"\x1b[?62;22c\x1b_Gi=0;OK\x1b\\", "WezTerm", "kitty"),  # any id, in either order
    (b"\x1b[?62;22c", "Apple_Terminal", None),
])
def test_terminal_protocol_asks_the_terminal(
    monkeypatch: pytest.MonkeyPatch, reply: bytes, term_program: str | None, expected: str | None,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ANALYSIS_IMAGE_PROTOCOL", raising=False)
    if term_program is None:
        monkeypatch.delenv("TERM_PROGRAM", raising=False)
    else:
        monkeypatch.setenv("TERM_PROGRAM", term_program)
    monkeypatch.setattr(chain_graph, "_ask_terminal", lambda query: reply)
    assert chain_graph.terminal_protocol() == expected


@pytest.mark.parametrize(("forced", "expected"), [("kitty", "kitty"), ("iterm", "iterm"), ("none", None)])
def test_image_protocol_can_be_forced(
    monkeypatch: pytest.MonkeyPatch, forced: str, expected: str | None,
) -> None:
    monkeypatch.setenv("ANALYSIS_IMAGE_PROTOCOL", forced)
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.setattr(chain_graph, "_ask_terminal", lambda query: b"\x1b_Gi=31;OK\x1b\\")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert chain_graph.terminal_protocol() == expected


def test_no_protocol_inside_tmux_or_when_piped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANALYSIS_IMAGE_PROTOCOL", raising=False)
    monkeypatch.setattr(chain_graph, "_ask_terminal", lambda query: b"\x1b_Gi=31;OK\x1b\\")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert chain_graph.terminal_protocol() is None
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    assert chain_graph.terminal_protocol() is None


@pytest.fixture
def stub_render(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Draws nothing: records each chart and format asked for, and returns FAKE_PNG."""
    calls: list[tuple[str, str]] = []

    def render(source: str, fmt: str = "png", scale: float = 2.0) -> bytes:
        calls.append((source, fmt))
        return FAKE_PNG

    monkeypatch.setattr(chain_graph, "render", render)
    return calls


def test_graph_options_are_one_of_three(guide_chains: Path, tmp_path: Path) -> None:
    both = _sim(guide_chains, "bridge", "--mermaid", "--graph")
    assert both.exit_code == 2 and "use one of --mermaid, --graph and --graph-file" in both.output
    gif = _sim(guide_chains, "bridge", "--graph-file", str(tmp_path / "chart.gif"))
    assert gif.exit_code == 2 and "must end in .png or .svg" in gif.output


def test_graph_file_writes_the_chart(
    guide_chains: Path, tmp_path: Path, stub_render: list[tuple[str, str]],
) -> None:
    result = _sim(guide_chains, "ss_split", "--rules", "--graph-file", str(tmp_path / "chart.svg"))
    assert result.exit_code == 0, result.output
    assert (tmp_path / "chart.svg").read_bytes() == FAKE_PNG
    (source, fmt), = stub_render
    assert fmt == "svg" and source.startswith("flowchart LR") and "rule ss_alice" in source


def test_graph_without_an_image_terminal_writes_a_png(
    guide_chains: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_render: list[tuple[str, str]],
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    result = _sim(guide_chains, "bridge", "--graph")  # CliRunner's output is no terminal
    assert result.exit_code == 0, result.output
    written = next(tmp_path.glob("chain-*.png"))
    assert f"wrote {written}" in result.output and written.read_bytes() == FAKE_PNG
    assert "iTerm2, kitty, Ghostty" in result.output


def test_graph_in_a_kitty_terminal(
    guide_chains: Path, monkeypatch: pytest.MonkeyPatch, stub_render: list[tuple[str, str]],
) -> None:
    monkeypatch.setattr(chain_graph, "terminal_protocol", lambda: "kitty")
    monkeypatch.setattr(chain_graph, "cell_size", lambda: None)
    result = _sim(guide_chains, "bridge", "--graph")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("\x1b_Ga=T,f=100,q=2,m=1;")


def test_graph_error_is_one_line(guide_chains: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(source: str, fmt: str = "png", scale: float = 2.0) -> bytes:
        raise chain_graph.GraphError("no browser to draw the chart with")

    monkeypatch.setattr(chain_graph, "render", fail)
    result = _sim(guide_chains, "bridge", "--graph")
    assert result.exit_code == 1 and "Error: no browser to draw the chart with" in result.output


def test_render_draws_a_png_with_mermaid() -> None:
    """The real thing, where a browser and a copy of Mermaid are at hand."""
    try:
        png = chain_graph.render("flowchart LR\n    a[Alice] -->|1: all| b([bridge])\n")
    except chain_graph.GraphError as error:
        pytest.skip(str(error))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = chain_graph.png_size(png)
    assert width > height > 0
