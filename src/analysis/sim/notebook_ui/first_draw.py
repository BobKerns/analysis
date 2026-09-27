"""Run a widget's first draw after the cell that created it has finished.

A widget drawn while its cell is still executing is drawn before its view exists in
the browser, and under Jupyter that first draw can come out clipped or missing; a
redraw from a control change, which happens later, is fine. ``Connections.show``
collects the first draws of the widgets its function builds and runs them once the
cell is done (IPython's ``post_execute`` hook), or straight away when nothing is
executing (a redraw triggered by a picker) or there is no kernel (tests, scripts).
Widgets call :func:`first_draw` where they would otherwise draw at construction.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

__all__ = ["deferring_first_draws", "first_draw", "run_when_cell_is_done"]

_collecting: list[Callable[[], None]] | None = None


def first_draw(draw: Callable[[], None]) -> None:
    """Draw now, or later if a ``Connections.show`` is collecting first draws."""
    if _collecting is None:
        draw()
    else:
        _collecting.append(draw)


@contextmanager
def deferring_first_draws() -> Iterator[list[Callable[[], None]]]:
    """Collect the :func:`first_draw` calls made inside the block instead of running them."""
    global _collecting
    outer, pending = _collecting, []
    _collecting = pending
    try:
        yield pending
    finally:
        _collecting = outer


def _shell_running_a_cell():
    """The IPython shell if a notebook cell is executing right now, else None."""
    try:
        from IPython import get_ipython

        shell = get_ipython()
        if shell is None:
            return None
        parent = shell.kernel.get_parent()
        return shell if parent.get("header", {}).get("msg_type") == "execute_request" else None
    except Exception:
        return None


def run_when_cell_is_done(draws: list[Callable[[], None]]) -> None:
    """Run ``draws`` after the executing cell finishes, or now if none is executing."""
    if not draws:
        return

    def run() -> None:
        for draw in draws:
            draw()

    shell = _shell_running_a_cell()
    if shell is None:
        run()
        return

    def once(*_args) -> None:
        shell.events.unregister("post_execute", once)
        run()

    shell.events.register("post_execute", once)
