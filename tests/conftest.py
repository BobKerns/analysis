"""Test-wide safety net: never let a forgotten override touch real user data.

Without this, a test that omits ``--data-dir`` (or a matching ``monkeypatch``)
silently falls back to whatever ``$ANALYSIS_DATA_DIR`` happens to be set to in
the *ambient* shell — which, on a dev machine with the data repo activated,
is the developer's real, personal data directory. That happened: a test run
overwrote the real data repo's ``.current_scenario`` with a prototype
scenario name. Clearing these here makes any such test fail loudly with
"ANALYSIS_DATA_DIR not set" instead of quietly mutating real data.
"""

from __future__ import annotations

import pytest

_ANALYSIS_ENV_VARS = (
    "ANALYSIS_DATA_DIR",
    "ANALYSIS_SCENARIO",
    "ANALYSIS_DATA_MODE",
)


@pytest.fixture(autouse=True)
def _no_ambient_analysis_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ANALYSIS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_current_scenario_writes(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make the ``sim`` CLI's ``write_current_scenario`` a no-op by default.

    It writes into whatever ``--data-dir`` points at — real data repo,
    activated data repo, or the tracked ``prototype_data/`` fixture — none of
    which a test should ever mutate. Tests that specifically verify the
    recording behavior opt back in with
    ``@pytest.mark.uses_current_scenario_write``.
    """
    if request.node.get_closest_marker("uses_current_scenario_write"):
        return
    monkeypatch.setattr("analysis.sim.cli.write_current_scenario", lambda *args, **kwargs: None)
