"""Notebook-facing scenario pickers and display widgets.

The pattern: get a picker (this subpackage), which hands back live, already
-open :class:`~analysis.sim.connection.RunConnection` /
:class:`~analysis.sim.connection.ScenarioConnection` instances; pass those
into the widget-builder functions here. Notebooks should be left holding
almost no data-transform code of their own.
"""

from __future__ import annotations
