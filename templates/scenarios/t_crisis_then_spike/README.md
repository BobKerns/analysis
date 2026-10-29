# t_crisis_then_spike

Two episodes in one lifetime: a 2008-style crash and its years of near-zero rates, then, 1 to 10 years after they end, a post-pandemic-style price spike.

Builds on `t_financial_crisis` and `t_pandemic_spike`.

## Purpose

A mix-in for a scenario on the templates (`base:t_crisis_then_spike`, or with an ensemble), and a component for a pool of the episodes (see [Pooling several kinds of trouble](../../../docs/guides/ensembles.md#pooling-several-kinds-of-trouble)). Each single episode template carries one kind of trouble, but history often has two in 30 years: 1929–1958 held a crisis and three spikes, and 1996–2025 held 2008 and 2021. This one adds that world. Its rows are `t_financial_crisis`'s and `t_pandemic_spike`'s; it changes only when they happen.

- `financial_crisis_start` is uniform from 2031 to 2045, not `t_financial_crisis`'s 2031 to 2064, so the spike after it lands before about 2060 in nearly every member (on a 200-member check, all but 2%). Its median, the base case, is mid-2038. The crisis comes earlier on average than in `t_financial_crisis`. This window assumes a scenario ending around 2060: for one ending much later, widen it, and for one ending much earlier, narrow it, by overriding `financial_crisis_start` by name.
- `financial_crisis_end` is, as in `t_financial_crisis`, 5 to 10 years after the start, most likely 7.
- `pandemic_spike_start` is uniform from 1 to 10 years after `financial_crisis_end`, so the two episodes never overlap. 2008's near-zero rates ended in 2015, and the next spike began in 2021. In the 1930s and 1940s the spikes came during and just after the low-rate years, which this template leaves out, since overlapping rows would cut one episode into the other.
- `pandemic_spike_end` is, as in `t_pandemic_spike`, 2 to 4 years after its start.

Place either episode by overriding its markers by name.

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
- [`t_pandemic_spike`](../t_pandemic_spike/README.md): an external shock: a short price spike, then rate hikes (2021–2023)

And one that puts two of them in one lifetime:

- **`t_crisis_then_spike`** (this one): a crisis, then a spike 1 to 10 years after its near-zero rates end (2008–2023)
