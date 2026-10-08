"""``Connections.show`` draws after the cell that called it has finished (notebook_ui.first_draw)."""

from __future__ import annotations

import pytest

from analysis.sim.notebook_ui.first_draw import when_cell_is_done
from analysis.sim.notebook_ui.pickers import Connections


class FakeEvents:
    def __init__(self) -> None:
        self.callbacks: list = []

    def register(self, name: str, callback) -> None:
        assert name == "post_execute"
        self.callbacks.append(callback)

    def unregister(self, name: str, callback) -> None:
        self.callbacks.remove(callback)

    def fire(self) -> None:
        for callback in list(self.callbacks):  # IPython walks a copy, too
            callback()


class FakeShell:
    def __init__(self, msg_type: str) -> None:
        self.events = FakeEvents()
        self.msg_type = msg_type
        self.kernel = type("Kernel", (), {
            "get_parent": lambda _self: {"header": {"msg_type": self.msg_type}},
        })()


@pytest.fixture(scope="function")
def executing_cell(monkeypatch: pytest.MonkeyPatch) -> FakeShell:
    shell = FakeShell("execute_request")
    monkeypatch.setattr("IPython.get_ipython", lambda: shell)
    return shell


def test_with_no_kernel_the_callback_runs_at_once() -> None:
    drawn: list[str] = []
    when_cell_is_done(lambda: drawn.append("now"))
    assert drawn == ["now"]


def test_the_callback_waits_for_the_end_of_the_executing_cell(executing_cell: FakeShell) -> None:
    drawn: list[str] = []
    when_cell_is_done(lambda: drawn.append("a"))
    when_cell_is_done(lambda: drawn.append("b"))
    assert drawn == []  # the cell is still running
    executing_cell.events.fire()
    assert drawn == ["a", "b"]
    executing_cell.events.fire()
    assert drawn == ["a", "b"]  # each hook removed itself


def test_a_widget_message_is_not_deferred(executing_cell: FakeShell) -> None:
    executing_cell.msg_type = "comm_msg"  # a picker changed; no cell is running
    drawn: list[str] = []
    when_cell_is_done(lambda: drawn.append("a"))
    assert drawn == ["a"] and executing_cell.events.callbacks == []


def test_work_started_by_the_deferred_callback_runs_inside_it(executing_cell: FakeShell) -> None:
    drawn: list[str] = []
    when_cell_is_done(lambda: when_cell_is_done(lambda: drawn.append("nested")))
    executing_cell.events.fire()
    assert drawn == ["nested"] and executing_cell.events.callbacks == []


def test_a_failing_callback_still_removes_its_hook_and_resets(executing_cell: FakeShell) -> None:
    def boom() -> None:
        raise RuntimeError("draw failed")

    when_cell_is_done(boom)
    with pytest.raises(RuntimeError):
        executing_cell.events.fire()
    assert executing_cell.events.callbacks == []
    drawn: list[str] = []
    when_cell_is_done(lambda: drawn.append("later"))  # not stuck in "deferred" mode
    executing_cell.events.fire()
    assert drawn == ["later"]


class _Connections(Connections):
    def _invoke(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def test_connections_show_draws_after_the_cell_and_redraws_at_once_on_a_change(
    executing_cell: FakeShell,
) -> None:
    drawn: list[str] = []
    connections = _Connections()
    out = connections.show(lambda: drawn.append("draw"))
    assert drawn == [] and out is not None  # deferred: the cell is running
    executing_cell.events.fire()
    assert drawn == ["draw"]
    executing_cell.msg_type = "comm_msg"  # the kernel is now handling a picker change
    connections.notify_changed()
    assert drawn == ["draw", "draw"]
