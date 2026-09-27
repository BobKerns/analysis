"""First draws run after the cell that made the widget has finished (notebook_ui.first_draw)."""

from __future__ import annotations

import pytest
import traitlets

from analysis.sim.notebook_ui import first_draw as fd
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
        for callback in list(self.callbacks):
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


@pytest.fixture(scope="function")
def widget_message(monkeypatch: pytest.MonkeyPatch) -> FakeShell:
    """A kernel that is busy with a widget message (a picker changed), not a cell."""
    shell = FakeShell("comm_msg")
    monkeypatch.setattr("IPython.get_ipython", lambda: shell)
    return shell


def test_without_a_collector_a_first_draw_runs_at_once() -> None:
    drawn: list[str] = []
    fd.first_draw(lambda: drawn.append("now"))
    assert drawn == ["now"]


def test_draws_are_collected_and_run_immediately_when_there_is_no_kernel() -> None:
    drawn: list[str] = []
    with fd.deferring_first_draws() as pending:
        fd.first_draw(lambda: drawn.append("a"))
        assert drawn == []
    fd.run_when_cell_is_done(pending)
    assert drawn == ["a"]


def test_draws_wait_for_the_end_of_the_executing_cell(executing_cell: FakeShell) -> None:
    drawn: list[str] = []
    with fd.deferring_first_draws() as pending:
        fd.first_draw(lambda: drawn.append("a"))
        fd.first_draw(lambda: drawn.append("b"))
    fd.run_when_cell_is_done(pending)
    assert drawn == []  # the cell is still running
    executing_cell.events.fire()
    assert drawn == ["a", "b"]
    executing_cell.events.fire()
    assert drawn == ["a", "b"]  # the hook removed itself


def test_a_picker_driven_redraw_is_not_deferred(widget_message: FakeShell) -> None:
    drawn: list[str] = []
    with fd.deferring_first_draws() as pending:
        fd.first_draw(lambda: drawn.append("a"))
    fd.run_when_cell_is_done(pending)
    assert drawn == ["a"] and widget_message.events.callbacks == []


def test_collectors_nest_and_are_restored() -> None:
    with fd.deferring_first_draws() as outer:
        with fd.deferring_first_draws() as inner:
            fd.first_draw(lambda: None)
        fd.first_draw(lambda: None)
    assert (len(inner), len(outer)) == (1, 1)
    drawn: list[str] = []
    fd.first_draw(lambda: drawn.append("free"))  # collecting has ended
    assert drawn == ["free"]


class _Connections(Connections):
    value = traitlets.Any()

    def _invoke(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def test_connections_show_runs_widget_first_draws_after_the_cell(executing_cell: FakeShell) -> None:
    drawn: list[str] = []

    def build():
        fd.first_draw(lambda: drawn.append("first draw"))
        return None

    out = _Connections().show(build)
    assert drawn == [] and out is not None
    executing_cell.events.fire()
    assert drawn == ["first draw"]


def test_connections_show_redraws_at_once_when_a_picker_changes(executing_cell: FakeShell) -> None:
    drawn: list[str] = []
    connections = _Connections()
    connections.show(lambda: fd.first_draw(lambda: drawn.append("draw")))
    assert drawn == []  # deferred: the cell is running
    executing_cell.events.fire()
    assert drawn == ["draw"]
    executing_cell.msg_type = "comm_msg"  # the kernel is now handling a picker change
    connections.value = "another scenario"
    assert drawn == ["draw", "draw"]
