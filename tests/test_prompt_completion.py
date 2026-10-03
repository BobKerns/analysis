"""Tab completion at the CLI's interactive prompts.

Readline completes one word and breaks words at hyphens and spaces, which the
names offered here contain; completing ``set-`` once listed every choice.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time

import pytest

from analysis.sim.cli import _line_completions

KINDS = ["latent-factor", "retire-scheduled", "set-amount", "set-rate", "transfer"]


@pytest.mark.parametrize(("line", "word_start", "expected"), [
    # The whole line is one word (no word breaks in force).
    ("set-", 0, ["set-amount", "set-rate"]),
    ("SET-R", 0, ["set-rate"]),
    ("", 0, KINDS),
    ("x", 0, []),
    # The library broke the word at the hyphen: offer only what follows it.
    ("set-", 4, ["amount", "rate"]),
    ("set-a", 4, ["amount"]),
    ("retire-", 7, ["scheduled"]),
])
def test_line_completions_match_the_whole_line(
    line: str, word_start: int, expected: list[str],
) -> None:
    assert _line_completions(line, word_start, KINDS) == expected


def test_line_completions_handle_names_with_spaces() -> None:
    accounts = ["Alice Checking", "Alice Savings", "Bill Checking"]

    assert _line_completions("Alice ", 6, accounts) == ["Checking", "Savings"]
    assert _line_completions("alice s", 0, accounts) == ["Alice Savings"]


_PROMPT_SCRIPT = (
    "from analysis.sim.cli import _prompt_with_completion\n"
    f"print('ANSWER=' + _prompt_with_completion('kind', choices={KINDS!r}))\n"
)


@pytest.fixture
def typed_at_a_terminal():
    """Type keys at a real ``_prompt_with_completion`` prompt in a
    pseudo-terminal, where Tab reaches readline; returns the answer read."""
    pty = pytest.importorskip("pty")
    pytest.importorskip("readline")
    children: list[tuple[subprocess.Popen[bytes], int]] = []

    def run(keys: bytes) -> str:
        # A child process on a pty, not pty.fork(): forking a threaded test
        # process is unsafe.
        fd, terminal = pty.openpty()
        child = subprocess.Popen(
            [sys.executable, "-c", _PROMPT_SCRIPT],
            stdin=terminal, stdout=terminal, stderr=terminal, start_new_session=True,
        )
        os.close(terminal)
        children.append((child, fd))
        output = ""

        def read_until(marker: str, timeout: float = 20.0) -> None:
            nonlocal output
            deadline = time.monotonic() + timeout
            while marker not in output and time.monotonic() < deadline:
                if select.select([fd], [], [], 0.1)[0]:
                    try:
                        output += os.read(fd, 4096).decode(errors="replace")
                    except OSError:
                        break

        read_until("kind")
        os.write(fd, keys)
        read_until("ANSWER=")
        read_until("\n", timeout=2.0)
        return output.rsplit("ANSWER=", 1)[-1].split()[0] if "ANSWER=" in output else output

    yield run

    for child, fd in children:
        os.close(fd)
        child.kill()
        child.wait()


@pytest.mark.parametrize(("keys", "answer"), [
    (b"set-a\t\n", "set-amount"),
    (b"set-r\t\n", "set-rate"),
    (b"re\t\n", "retire-scheduled"),
    (b"SET-R\t\n", "set-rate"),
])
def test_tab_completes_a_hyphenated_choice_at_a_terminal(
    typed_at_a_terminal, keys: bytes, answer: str,
) -> None:
    assert typed_at_a_terminal(keys) == answer
