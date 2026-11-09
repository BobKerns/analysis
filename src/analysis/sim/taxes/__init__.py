"""The built-in tax calculators (#507, #508): ``federal``, ``state_tables``
and ``local_bands``, and the bases a state calculator builds on
(:mod:`analysis.sim.taxes.state_base`). Importing the package registers them
with :mod:`analysis.sim.tax_api`."""

from analysis.sim.taxes import federal, local, state_tables

__all__ = ["federal", "local", "state_tables"]
