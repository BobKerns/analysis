# Base

The prototype's root scenario: generic defaults that every other scenario
inherits, whatever household it describes.

It holds no accounts or scheduled transactions of its own. Its parents are
the template scenarios `t_inflation_series` and `t_market_series`, read from
the code repository's `templates/scenarios/`: a long-run rate for each
inflation series and fund asset class, and a latent factor for each series,
linked into a tree, so a child scenario or mix-in can vary any of them with
one row. The templates are explained under "Template scenarios" in the
historical calibration guide (`docs/guides/historical-calibration.md`). CPI
itself stays in the shared `aux/inflation__cpi.csv`. Its own `series/` keeps
only the IRMAA threshold and premium series.

`retire_alice` derives from this scenario and adds the household.
