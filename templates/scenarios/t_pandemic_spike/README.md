# t_pandemic_spike

A post-pandemic-style price spike on top of the long-run rates: two to four years of high prices, with one year when stocks and bonds fall together.

Builds on `t_inflation_series` and `t_market_series`.

## Purpose

A mix-in for a scenario on the templates (`base:t_pandemic_spike`, or with an ensemble). It models an external shock: a short, sharp price spike and the rate hikes that answer it, as from 2021 to 2023. A new pandemic would probably hit the economy differently, so the ranges are wider than 2021–2023's.

- `pandemic_spike_start`, a date marker, is uniform from 2031 to 2064, like `t_great_inflation`'s. Its median, the base case, is late 2047.
- `pandemic_spike_end` is 2 to 4 years after the start, most likely 3.
- From the start until the end, prices hold at a level drawn once per member: CPI-U at 5.6% (from 3.5% to 8%), shelter 5.9%, food 6.4%, energy 10.8% (up to 25%), services 5.5%, apparel 3.2% and auto insurance 10% (up to 18%). Three years is too short to fit a persistence, so these are held rates, not processes.
- Medical care, prescriptions, the Part B premium and tuition keep their long-run paths. They ran below core in 2021–2023 (medical care at 2.2%, against 5.0%), which isn't safe to count on, so the template doesn't model it either way.
- A year after the start, rates rise to meet the spike, held for 12 months: US stocks fall 18% (from −30% to −5%), international stocks 16%, and bonds 12% (from −20% to −3%), together, as in 2022. Cash earns 2.5%.
- Two years after the start, the market series go back to what governed them before the spike; at the end, the price series do (`vary_resume`): the long-run process, resumed with its own shocks, or the series file's rate.
- The engine's rates are yearly, so a crash inside one year (2020's −34% in a month, recovered by December) doesn't show.

Place the episode by overriding the markers by name.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See [Template scenarios](../../../docs/guides/historical-calibration.md#template-scenarios) in the historical calibration guide.

## The templates

- [`t_empty`](../t_empty/README.md): nothing; the root
  - [`t_inflation_factors`](../t_inflation_factors/README.md): inflation's latent factors
    - [`t_inflation_series`](../t_inflation_series/README.md): long-run inflation rates
      - [`t_inflation_ensemble`](../t_inflation_ensemble/README.md): year-by-year inflation paths
        - [`t_inflation_current`](../t_inflation_current/README.md): today's inflation rates, held, then returning
    - [`t_market_factors`](../t_market_factors/README.md): interest-rate and stock factors
      - [`t_market_series`](../t_market_series/README.md): long-run cash, bond and stock rates
        - [`t_market_ensemble`](../t_market_ensemble/README.md): year-by-year market paths
          - [`t_market_current`](../t_market_current/README.md): today's cash yield, held, then returning

Episodes, each a mix-in building on `t_inflation_series` and `t_market_series`:

- [`t_great_inflation`](../t_great_inflation/README.md): a dysfunction: inflation runs for years (1966–1982)
- [`t_financial_crisis`](../t_financial_crisis/README.md): a fiscal shock: a crash, then years of near-zero rates (2008–2019)
- **`t_pandemic_spike`** (this one): an external shock: a short price spike, then rate hikes (2021–2023)
