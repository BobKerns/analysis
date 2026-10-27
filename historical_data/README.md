# Historical calibration data

<!-- cspell:ignore PCEPI -->

The public data behind
[the historical calibration guide](../docs/guides/historical-calibration.md),
as retrieved on 2026-10-05. Each file has a `.retrieved` stamp beside it
giving its retrieval date. This is the default download cache of
`analysis historical`, so the guide's tables are recomputed from these files
without downloading anything, and a series not here yet is downloaded into
it.

Running with `--refresh` downloads every file it uses again and overwrites
the copy here; commit the result to record the
new vintage. The files are kept as published, tabs and all, apart from the
two CSVs noted below.

| Folder | Contents | Source |
| --- | --- | --- |
| `bls/` | CPI-U (`cu`), CPI-W (`cw`) and C-CPI-U (`su`) flat files, series lists and directory listings; the R-CPI-E spreadsheet | [BLS LABSTAT](https://download.bls.gov/pub/time.series/), [R-CPI-E](https://www.bls.gov/cpi/research-series/r-cpi-e-home.htm) |
| `bea/` | Monthly NIPA data and the series register (PCE price indexes) | [BEA NIPA flat files](https://apps.bea.gov/national/Release/TXT/) |
| `damodaran/` | Annual returns on stocks, bonds, bills, real estate and gold, 1928–2025 | [Damodaran](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/histretSP.html) |
| `fred/` | PCEPI and DGS10 | [FRED](https://fred.stlouisfed.org/) |
| `cms/` | 2025 Medicare Trustees Report (PDF) and the 2026 Part B premium fact sheet | [Trustees Report](https://www.cms.gov/oact/tr/2025), [fact sheet](https://www.cms.gov/newsroom/fact-sheets/2026-medicare-parts-b-premiums-deductibles) |
| `hld/` | Japan's 2023 life table (MHLW's Table A) as the Human Life-Table Database's PDF | [Human Life-Table Database](https://www.lifetable.de/) |
| `ssa/` | Social Security COLAs, 1975–2025; the 2023 period life table (2026 Trustees Report) | [SSA COLAs](https://www.ssa.gov/oact/cola/colaseries.html), [life table](https://www.ssa.gov/oact/STATS/table4c6.html) |

Four files are extracted rather than downloaded. This guide reads two of
them, as `csv:historical_data/cms/part-b-premium.csv` and
`csv:historical_data/ssa/cola.csv`; the simulator reads the other two,
`ssa/life_table.csv` and `hld/life_table__jp.csv` (below). `--refresh` doesn't update any of them;
re-extract them by hand before committing a new vintage:

- `cms/part-b-premium.csv` is the standard Part B monthly premium
  (`date,value`) from the Trustees Report's Table III.C2, plus 2026 from the
  fact sheet. Before 1985 the premium ran July to June and the table gives
  only some years; each row is dated when its rate took effect.
- `ssa/cola.csv` is the COLA history table (`date,rate`), dated December of
  each year. COLAs from 1975 to 1982 took effect in June. ssa.gov refuses
  scripted downloads, so the page was saved from a browser.

`ssa/life_table.csv` is the period life table as the aux kind `life_table` reads it
(`effective_date,age,q_female,q_male`), dated December of the table's year,
and `templates/aux/` and `prototype_data/aux/` carry copies. For a new vintage,
save the page from a browser, run
`scripts/extract_ssa_life_table.py PAGE.html historical_data/ssa/life_table.csv`,
and copy the result to both.

`hld/life_table__jp.csv` is Japan's table in the same form, extracted from
the PDF beside it by
`scripts/extract_hld_life_table.py JPN000020232023CU1.pdf historical_data/hld/life_table__jp.csv`
(which needs poppler's `pdftotext`), and `templates/aux/` and
`prototype_data/aux/` carry copies too. The table ends at an open
"105 and over" group; the script extrapolates ages 105 to 108 from a
quadratic in log hazard fitted to ages 90 to 104, ends the table with
everyone dying at 109, and prints the group's remaining life expectancy
from the table and from the extrapolation to compare (1.33 and 1.31 years
for women, 1.05 and 1.04 for men).
