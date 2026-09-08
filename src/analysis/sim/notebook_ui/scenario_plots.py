# Scenario descriptions for the notebook UI

from IPython.display import display

from analysis.sim.notebook_ui.scenario_view import calendar_view, chains_view


def truncate(width):
    """A .style.format formatter that renders any value's first `width` characters."""
    return lambda value: str(value)[:width]

def show_chains(sc):
    """
    Show only the interior chains (the ones not auto-created for a single account)
    from the scenario connection `sc`.

    Formats the "maximum", "amount", and "percent" columns for display.
    """
    chains = chains_view(sc)
    interior_chains = chains[chains["step"].notna()].copy()
    # threshold=0 means "no minimum" -- not meaningfully different from n/a.
    interior_chains["maximum"] = interior_chains["maximum"].mask(interior_chains["maximum"] == 0)
    styled = interior_chains.style.format({"maximum": "${:,.2f}",
                                  "amount": "${:,.2f}",
                                  "percent": "{:,.2f}%"},
                                 na_rep="-")
    display(styled , clear=True)


def show_scheduled_view(sc):
    """
    Show the scheduled items from the scenario connection `sc`.
    """
    from analysis.sim.notebook_ui.scenario_view import scheduled_view
    return scheduled_view(sc).style.format(
            {"amount": "${:,.2f}", "start": truncate(7), "end": truncate(7)},
            na_rep="-",
        ).set_properties(subset=["start", "end"], **{"width": "7ch", "white-space": "nowrap"})


def show_calendar_view(sc):
    """
    Show the calendar items from the scenario connection `sc`.
    """
    return calendar_view(sc).style.format(
        {"amount": "${:,.2f}", "date": truncate(7)}, na_rep="-"
    ).set_properties(subset=["date"], **{"width": "7ch", "white-space": "nowrap"})
