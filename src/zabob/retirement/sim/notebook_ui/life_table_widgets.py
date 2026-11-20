"""``age_comparison.ipynb``'s table at one age: every life table's values
there, as ``sim lifespan check`` gives them, redrawn as the age changes."""

from __future__ import annotations

from pathlib import Path

import ipywidgets as widgets
from IPython.display import display

from zabob.retirement.sim import registries
from zabob.retirement.sim.lifespan_tables import at_age_frame

__all__ = ["at_age_widget"]


def at_age_widget(data_dir: Path, age: float = 65.0) -> widgets.VBox:
    """An age box (fractional ages are interpolated) above every life
    table's ``q``, expected age at death and years left at that age."""
    box = widgets.BoundedFloatText(
        value=age, min=0.0, max=registries.MAX_AGE_AT_DEATH, step=0.5, description="Age:",
        layout=widgets.Layout(width="max-content"),
    )
    out = widgets.Output()

    def _redraw(*_) -> None:
        frame = at_age_frame(data_dir, box.value)
        with out:
            display(frame.style.format(
                {c: "{:.6f}" if c[-1] == "q" else "{:.2f}" for c in frame.columns}), clear=True)

    box.observe(_redraw, names="value")
    _redraw()
    return widgets.VBox([box, out])
