# Series

Growth-rate and inflation series for this scenario, one CSV file per
series. A file named `rate__<series>.csv` is a growth rate (for an
account's or lot's `series`, or a fund's asset class);
`inflation__<series>.csv` is an inflation rate (for a scheduled
transaction's `series`).

Each file has two columns:

- `effective_date`: the month the rate takes effect; it holds until the
  next row's date.
- `annual_rate`: the annual rate as a percent (`4.5` is 4.5%).

A file here replaces the parent scenario's file of the same name; a series
with no file here is inherited from the parent. CPI is the exception: it
lives in `aux/inflation__cpi.csv`, not here.

Details: ADR-003 sections 4.1d and 4.4 in the analysis code repository.
