# Template scenarios

Scenarios the analysis code repository ships for every data repo to build
on: a vetted inflation and market structure, with no household data.

**Starting a new data repo:** copy `base` (this folder's only scenario
without a `t_` name) into your data repo's `scenarios/` as your starting
point. Don't copy the `t_` scenarios. A data repo that names a `t_`
scenario it has no folder for reads it from here, so the templates stay
current as the code changes. To change one, copy just that folder into your
`scenarios/`; it then replaces the template whole.
`scripts/link-data.sh --template` copies this folder without them.

`base` builds on `t_inflation_series` and `t_market_series`: long-run
rates, with every future running at them. For an ensemble that starts from
today's rates, add `t_inflation_current` and `t_market_current` as mix-ins:

```sh
sim ensemble create my_now base t_inflation_current t_market_current
```

## The templates

Each one builds on the one above it and adds one thing:

- [`t_empty`](t_empty/README.md): nothing; the root
  - [`t_inflation_factors`](t_inflation_factors/README.md): inflation's latent factors
    - [`t_inflation_series`](t_inflation_series/README.md): long-run inflation rates
      - [`t_inflation_ensemble`](t_inflation_ensemble/README.md): year-by-year inflation paths
        - [`t_inflation_current`](t_inflation_current/README.md): today's inflation rates, held, then returning
    - [`t_market_factors`](t_market_factors/README.md): interest-rate and stock factors
      - [`t_market_series`](t_market_series/README.md): long-run cash, bond and stock rates
        - [`t_market_ensemble`](t_market_ensemble/README.md): year-by-year market paths
          - [`t_market_current`](t_market_current/README.md): today's cash yield, held, then returning

Episodes, each a mix-in building on `t_inflation_series` and `t_market_series`:

- [`t_great_inflation`](t_great_inflation/README.md): a dysfunction: inflation runs for years (1966–1982)
- [`t_financial_crisis`](t_financial_crisis/README.md): a fiscal shock: a crash, then years of near-zero rates (2008–2019)
- [`t_pandemic_spike`](t_pandemic_spike/README.md): an external shock: a short price spike, then rate hikes (2021–2023)

And one that puts two of them in one lifetime:

- [`t_crisis_then_spike`](t_crisis_then_spike/README.md): a crisis, then a spike 1 to 10 years after its near-zero rates end (2008–2023)

How each layer's values were chosen, and why, is in
[Template scenarios](../../docs/guides/historical-calibration.md#template-scenarios)
in the historical calibration guide. Template scenarios are specified in
[ADR-003 §4.5a](../../docs/architecture/ADR-003-registries-and-csv-schemas.md#45a-template-scenarios-t_-names).
