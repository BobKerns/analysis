"""The built-in tax calculators (#507, #508): ``federal``, ``state_tables``
and ``local_bands``, and the bases a state calculator builds on
(:mod:`zabob.retirement.sim.taxes.state_base`). Importing the package registers them
with :mod:`zabob.retirement.sim.tax_api`."""

from zabob.retirement.sim.taxes import federal, local, state_tables

__all__ = ["federal", "local", "state_tables"]
