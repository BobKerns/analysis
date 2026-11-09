"""The built-in tax calculators (#507): ``federal`` and ``state_tables``.
Importing the package registers them with :mod:`analysis.sim.tax_api`."""

from analysis.sim.taxes import federal, state_tables

__all__ = ["federal", "state_tables"]
