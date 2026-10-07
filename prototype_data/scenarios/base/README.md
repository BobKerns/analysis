# Base

The prototype's root scenario: generic defaults that every other scenario
inherits, whatever household it describes.

It holds no accounts or scheduled transactions of its own. Its `series/` has
a long-run rate for each inflation series and fund asset class, and its
`calendar.csv` has a latent factor for each series, linked into a tree, so a
child scenario or mix-in can vary any of them with one row. The rates and the
factor tree are explained under "The prototype's defaults" in the historical
calibration guide (`docs/guides/historical-calibration.md`). CPI itself
stays in the shared `aux/inflation__cpi.csv`.

`retire_alice` derives from this scenario and adds the household.
