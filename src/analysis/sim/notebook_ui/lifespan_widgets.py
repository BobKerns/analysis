"""The lifespan notebook's chart (#312): each person's age-at-death
distribution and the household's chance that someone is still alive, drawn
by ``lifespan_curves`` as ``sim person show --graph`` draws it."""

from __future__ import annotations

import pandas as pd
from IPython.display import Image, display

from analysis.sim.connection import ScenarioConnection
from analysis.sim.ensemble_plots import figure_bytes
from analysis.sim.lifespan_curves import household, lifespan_figure

__all__ = ["lifespan_widget"]

_PERCENTILES = (0.05, 0.2, 0.4, 0.5, 0.6, 0.8, 0.95)


def lifespan_widget(scenario: ScenarioConnection, *, show_household: bool = True, **_) -> None:
    """A table of each person's age at death by percentile (and the year of
    the household's last death), then the chart. Displays them itself."""
    persons, notes = scenario.lifespans()
    for note in notes:
        print(f"note: {note}")
    if not persons:
        print("No person in this scenario has a sex for the life table to read "
              "(persons.csv `sex`, f or m).")
        return
    columns = [f"p{round(q * 100):02d}" for q in _PERCENTILES]
    table = pd.DataFrame(
        [[round(p.quantile(q), 1) for q in _PERCENTILES] for p in persons],
        index=pd.Index([p.display for p in persons], name="age at death"), columns=columns,
    )
    display(table)
    if show_household and len(persons) > 1:
        h = household(persons)
        display(pd.DataFrame(
            [[h.last_death_quantile(q).year for q in _PERCENTILES]],
            index=pd.Index(["year of the last death"], name="household"), columns=columns,
        ))
    # A plain Figure rendered to PNG, not pyplot: see ensemble_widgets (#119).
    display(Image(data=figure_bytes(lifespan_figure(persons, show_household=show_household)),
                  format="png"))
