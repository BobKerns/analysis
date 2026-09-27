"""Run a widget's first draw after the cell that created it has finished.

A widget drawn while its cell is still executing is drawn before its view exists in
the browser, and under Jupyter that first draw can come out clipped or missing; a
redraw from a control change, which happens later, is fine. ``Connections.show``
therefore does its first draw with :func:`when_cell_is_done`: after the cell, via
IPython's ``post_execute`` hook, so it behaves exactly like a redraw. When no cell is
executing (a picker changed, so the kernel is handling a widget message) or there is
no kernel (tests, scripts) the callback runs at once.
"""

from __future__ import annotations

from collections.abc import Callable

__all__ = ["when_cell_is_done"]

_running_deferred = False


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


def when_cell_is_done(callback: Callable[[], None]) -> None:
    """Run ``callback`` after the executing cell finishes, or now if none is executing."""
    shell = None if _running_deferred else _shell_running_a_cell()
    if shell is None:
        callback()
        return

    def once(*_args) -> None:
        global _running_deferred
        shell.events.unregister("post_execute", once)
        # The hook runs while the cell is still the kernel's parent message, so anything
        # it triggers must run now rather than queue another hook behind it.
        _running_deferred = True
        try:
            callback()
        finally:
            _running_deferred = False

    shell.events.register("post_execute", once)
