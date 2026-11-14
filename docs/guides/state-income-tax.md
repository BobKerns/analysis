# State income tax, state by state

> To read a formatted `.md` file:
> - In Jupyter: right-click on it and select `Open With / Markdown Preview`
> - In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

The README's "State and territory income tax" table gives each state a
status. This guide says why: for each state with an income tax, the rules
that matter to a retired household and how the simulator handles each one.
It was surveyed in November 2026 for tax year 2026 (#543).

Each rule is marked:

- **shipped**: the tables come with the simulator, with figures;
- **table**: the simulator can express it, but you write the row from your
  state's instructions;
- **household calculator**: no table says it; it needs Python in your data
  repo's `taxes/` ([the guide](./tax-calculators.md));
- **not supported**: the simulator can't model it yet. Where an issue
  tracks it, it's linked.

The tables are in the code repository's
[`templates/aux/README.md`](../../templates/aux/README.md):

| Table | Says |
| --- | --- |
| `state_brackets`, `state_standard_deduction` | Rates and the standard deduction (with a personal exemption folded in), which may shrink with income |
| `state_social_security` | How much federally taxable Social Security the state leaves out, phased by income |
| `state_retirement_exclusion` | Pension, IRA and other retirement income left out, by age, per person, below an income |
| `state_adjustments` | The state's own subtractions, deductions and credits: from tagged spending or income, per filer, from federal tax or the federal deduction |
| `state_ltcg_exclusion`, `state_ltcg_brackets`, `state_stcg_brackets` | A share of long-term gain left out, or gain rates of the state's own |
| `state_surcharge` | A surtax on high income |
| `state_local_tax` | A county or city tax on the state's taxable income |

## Rules that work the same way everywhere

These come up in many states, so they're said once here rather than in
every state's list.

- **A public or military pension, or an employer plan but not an IRA.**
  `state_retirement_exclusion` sorts income only into pensions, retirement
  accounts (IRA and 401(k) alike), interest, dividends and gains. When the
  state's rule turns on the kind of pension or plan, tag the account the
  income comes from (`sim account tag add`, say `tax:public_pension` on the
  pension's income account, or `tax:employer_plan` on a 401(k)) and write a
  `state_adjustments` subtraction on that tagged `income`, with the age,
  cap and income limit: **table**. One that's reduced by the Social
  Security you receive (Idaho, Maryland) isn't: **not supported**
  ([#579](https://github.com/BobKerns/analysis/issues/579)).
- **Spending-based deductions and credits.** A renter's deduction or
  credit, a property tax deduction or credit figured as a share of the tax
  paid, a deduction for 529 contributions, a deduction or credit for
  long-term care premiums: a `state_adjustments` row on tagged spending,
  **table**. A credit the state pays even when it's more than the tax
  (refundable) is held to the tax:
  **not supported** ([#579](https://github.com/BobKerns/analysis/issues/579)).
- **Circuit breakers.** Property tax or rent relief figured from the tax as
  a share of household income, or from a table of income bands (Michigan's
  homestead credit, Minnesota's renter's credit and property tax refund,
  Vermont's, Wisconsin's homestead credit, Maine's, Montana's elderly
  homeowner and renter credit, Massachusetts's senior circuit breaker, the
  District's Schedule H): **household calculator**. Many states pay their
  relief outside the income tax (Pennsylvania's rebate, New Jersey's
  ANCHOR and Senior Freeze, Colorado's); the simulator leaves those to you
  as income.
- **Personal exemptions and exemption credits**, with a phase-out by
  income, and extra ones from 65: `state_adjustments` rows on `filers`,
  **table**.
- **A deduction for federal income tax paid** (Alabama, Missouri, Oregon):
  a `state_adjustments` row on `federal_tax`, with its cap and phase-out,
  **table**.
- **A standard deduction that shrinks as income rises** (Alabama, Maine,
  Minnesota, Rhode Island, South Carolina from 2026, Wisconsin):
  `state_standard_deduction`'s optional phase-out columns, **table**:
  above `phaseout_start` of the state's AGI it falls to nothing at
  `phaseout_end`, or by `phaseout_rate` of each dollar over, not below
  `floor`. With a rate, `phaseout_step` counts the income over in whole
  steps, as a table of $500 rows does; with an end, `phaseout_steps`
  splits the span into equal steps. It tests the state's AGI unless
  `phaseout_agi` is `federal`, as Minnesota's, Rhode Island's and South
  Carolina's do.
- **Married couples filing separately on one return** (Arkansas, Delaware,
  the District, Kentucky, Virginia,
  [#580](https://github.com/BobKerns/analysis/issues/580)): each spouse's
  income runs through the brackets and standard deduction alone, which
  saves tax where the lower brackets or the deduction aren't doubled for a
  joint return. A `state_separate_return__<state>.csv` with `allowed`
  `yes` has the state tax figure both ways, each spouse with the `MFS`
  rows and the income that's theirs (by account owner, joint income split
  evenly), and take the lower. The rule that both spouses itemize or
  neither (Kentucky, Delaware) isn't modeled.
- **Itemized deductions** ([#542](https://github.com/BobKerns/analysis/issues/542)):
  a state with its own `state_itemized_limits__<state>.csv` (California's
  ships) figures them from spending as the federal return does, under its
  own limits, never deducting an income tax. Another state figures none:
  type its whole itemized total by year into `state_deductions.csv`, or set
  `itemized_limits_aux` to `itemized_limits` in its `taxes/state.csv`
  params to figure the federal lines for it. Without either it takes
  `federal_deductions.csv`, which holds only what the lines don't figure.
- **Local income taxes on wages only** (Ohio's municipal and earned income
  school district taxes, Pennsylvania's earned income tax, Kentucky's
  occupational taxes): a retired household's pensions and investments
  don't pay them, so they're left out.
- **A move during the year.** Part-year returns and credits for tax paid
  to another state are not modeled: the year is taxed by the state the
  household lives in at the year's end.

## The states

The states without an income tax (Alaska, Florida, Nevada, New Hampshire,
South Dakota, Tennessee, Texas, Wyoming) are left out. New Hampshire's tax
on interest and dividends ended with 2024.

### Alabama

- Federal income tax paid is deductible: **table**.
- Defined benefit pensions, public and private, are exempt, and from 65 a
  further amount of IRA and other retirement income: **table**
  (`state_retirement_exclusion`). Social Security is exempt.
- The standard deduction shrinks with income: **table**. It falls by a
  fixed amount for each $500 of AGI from $26,000, to a minimum, so
  `phaseout_start` is $25,999, `phaseout_step` $500, `phaseout_rate` the
  amount a step takes over $500 (35% joint, 5% single, 27% head of
  family), and `floor` the minimum. Married filing separately uses $250
  steps from $13,000.

### Arizona

- Social Security is exempt. A government pension subtraction (small) and
  military retirement: **table** (tagged `income`).
- A share of long-term gain is subtracted, but only on assets bought after
  2011: **not supported**; `state_ltcg_exclusion` applies it to all gain.
- The standard deduction follows the federal one from before 2025's
  federal law, not today's.

### Arkansas

- An exclusion of retirement income per person: **table**. Social Security
  is exempt.
- Half of long-term gain is excluded: **table**.
- A personal tax credit per filer: **table**.
- Lower incomes use separate tax tables: **not supported**.
- Spouses may file separately on one return: **table**
  (`state_separate_return`).

### California (shipped)

- Brackets, standard deduction, the 1% surcharge over $1 million, the HSA
  rules, the personal and senior exemption credits and the renter's credit:
  **shipped**. Social Security is exempt.
- No SALT cap, no deduction for state income tax, $1 million of mortgage
  debt and half of AGI for charity (`state_itemized_limits__CA.csv`):
  **shipped** (#542).

### Colorado

- A pension and annuity subtraction from 55, larger from 65, that also
  covers taxable Social Security: **table**.
- Social Security is subtracted in full from 65, and from 55 below an
  income: the age test is **not supported**
  ([#581](https://github.com/BobKerns/analysis/issues/581)).

### Connecticut

- Social Security is exempt below an income; above it, no more than 25% of
  benefits is taxed. The exemption below the income is **table**; the 25%
  cap is **not supported**.
- Pensions, annuities and (from 2026, fully) IRA distributions are exempt
  below an income, phasing out above it in steps: **table**, with a
  straight-line phase-out.
- The personal exemption phases out with income: **table**. The personal
  tax credit, a share of the tax that falls with income: **not
  supported**.
- Benefit recapture, which taxes all income at the top rate at high
  incomes: **not supported**
  ([#549](https://github.com/BobKerns/analysis/issues/549)).
- A property tax credit, limited to filers 65 and over: **table**.

### Delaware

- An exclusion from 60 of pensions, IRA distributions, interest,
  dividends and gains, smaller below 60: **table**. Social Security is
  exempt.
- A larger standard deduction from 65, and personal credits: **table**.
- Spouses may file separately on one return: **table**
  (`state_separate_return`).

### District of Columbia (shipped)

- Brackets and the standard deduction: **shipped**. Social Security is
  exempt.
- The extra standard deduction at 65: **table** (a `filers` row from 65),
  not shipped.
- The property tax credit (Schedule H): **household calculator**.
- Spouses may file separately on one return: **shipped**
  (`state_separate_return__DC.csv`).

### Georgia

- A retirement income exclusion per person, from 62 and larger from 65,
  covering pensions, IRA distributions, interest, dividends, gains and a
  little earned income: **table**. Social Security is exempt.

### Hawaii

- Employer-funded pensions are exempt; IRA and 401(k) distributions are
  taxed: **table**. Social Security is exempt.
- Long-term gain is taxed at no more than 7.25%: **table**
  (`state_ltcg_brackets`).

### Idaho (shipped)

- Brackets and the standard deduction: **shipped**. Social Security is
  exempt.
- The retirement benefits deduction from 65 for some public pensions,
  reduced by Social Security: **not supported** (#579).
- The grocery credit, which is refundable: **not supported** (#579).

### Illinois

- Pensions, IRA distributions and Social Security are exempt: **table**.
- The personal exemption, with an income limit: **table**.
- The property tax credit, a share of the tax on your home: **table**.

### Indiana

- Social Security is exempt. Extra exemptions from 65, one more below an
  income: **table**. The federal civil service annuity deduction:
  **table** (tagged `income`).
- The renter's deduction and the homeowner's property tax deduction:
  **table**.
- County tax, on the state's taxable income: **table**
  (`state_local_tax`).

### Iowa

- Retirement income is exempt from 55, and Social Security always:
  **table**.
- A school district surtax, a share of the state tax: **not supported**.

### Kansas (shipped)

- Brackets and the standard deduction with the personal exemption folded
  in: **shipped**. Social Security is exempt.
- Kansas public pensions, federal civil service and military retirement
  are exempt: **table** (tagged `income`), not shipped.
- The larger standard deduction from 65: **table**, not shipped.
- The personal exemption is lost by a household that itemizes: **table**
  (a `filers` deduction row in place of the folded-in exemption), not
  shipped.

### Kentucky

- A retirement income exclusion per person, IRAs included: **table**.
  Social Security is exempt.
- A personal tax credit from 65: **table**.
- The standard deduction isn't doubled for a joint return, and spouses may
  file separately on one return, each taking one: **table**
  (`state_separate_return`).

### Louisiana

- From 2025, a retirement income exclusion from 65 per person, indexed
  from 2026: **table**. Government pensions are exempt in full: **table**
  (tagged `income`). Social Security is exempt.

### Maine

- The pension deduction ($48,216 a person for 2025), reduced by Social
  Security and phased out above an income: **table**
  (`state_retirement_exclusion` with `less_ss`). Social Security is
  exempt.
- The personal exemption phases out at high income: **table**.
- The standard deduction phases out above an income: **table**, from
  `phaseout_start` (the worksheet's threshold) to nothing at
  `phaseout_end`, the threshold plus its divisor ($150,000 joint, $75,000
  single). The start and end must index together (or not at all), so the
  span grows with inflation where the law's divisor doesn't: close, not
  exact.

### Maryland

- The pension exclusion from 65 covers employer plans (401(a), 403, 457(b)),
  not IRAs, and is reduced by Social Security: **not supported** (#579).
  Social Security is exempt.
- The senior tax credit from 65, below an income: **table**. The personal
  exemption's phase-out and the extra exemption at 65: **table**.
- A 2% surtax on capital gains for households with income over $350,000:
  **not supported** (it turns on total income, not the gain).
- County tax, on the state's taxable income: **table**
  (`state_local_tax`).

### Massachusetts

- Personal exemptions and the extra exemption at 65: **table**.
- Massachusetts and federal public pensions are exempt; private pensions
  and IRAs are taxed: **table** (tagged `income`). Social Security is
  exempt.
- Short-term gain at 8.5%: **table** (`state_stcg_brackets`). The 4%
  surtax over about $1 million: **table** (a top row of each table, or
  `state_surcharge`).
- The rent deduction: **table**. The senior circuit breaker:
  **household calculator**, and refundable (#579).

### Michigan

- From 2026 the retirement deduction is open to every age, up to $67,610
  ($135,220 joint): **table**. Social Security is exempt.
- City income taxes on a base of their own: **not supported**.
- The homestead property tax credit: **household calculator**.

### Minnesota

- The Social Security subtraction falls in $4,000 steps of income:
  **table**, as a straight line.
- The standard deduction phases out at high income, by 3% of AGI over
  one threshold and 10% over a second, losing at most 80%: **table**,
  approximately. One `phaseout_rate` of 10% from the second threshold
  less a third of the 3% band's width matches the law above the second
  threshold and is a little off within the band; `floor` is 20% of the
  amount. `phaseout_agi` is `federal`.
- A 1% surtax on net investment income over $1 million: **not supported**.
- The renter's credit and property tax refund: **household calculator**,
  and refundable (#579).

### Mississippi

- Pensions and IRA distributions are exempt, and Social Security: **table**.

### Missouri

- Social Security is exempt from 2024. Public pensions are exempt up to a
  cap below an income, and a smaller private pension amount: **table**
  (tagged `income`).
- From 2025 all capital gains, short and long, are subtracted: **table**
  (a 100% `state_ltcg_exclusion` and a 0% `state_stcg_brackets`).
- Federal income tax paid is partly deductible: **table**.

### Montana

- Social Security is taxed as on the federal return: **table**
  (`state_social_security` with nothing excluded).
- Long-term gain at lower rates: **table** (`state_ltcg_brackets`).
- A subtraction from 65 per taxpayer: **table**.
- Montana starts from federal taxable income, so its standard deduction
  is the federal one.

### Nebraska (shipped)

- Brackets and the standard deduction: **shipped**. Social Security is
  exempt from 2024.
- The personal exemption credit: **table**, not shipped.
- The school district property tax credit, a share of the school tax paid,
  which is refundable: **table**, held to the tax (#579).

### New Jersey

- The pension and IRA exclusion below an income, falling in steps to
  nothing above $150,000: **table**. Social Security is exempt.
- Personal exemptions, and the extra one at 65: **table**.
- The property tax deduction (or credit): **table**.

### New Mexico

- Social Security is exempt below an income, taxed in full above it:
  **table** (a phase-out that starts and ends at the same income).
- A deduction from 65 that falls with income: **table**.
- A capital gains deduction: **table** (`state_ltcg_exclusion` with a
  minimum and a cap).

### New York

- From 59½, $20,000 a person of private pensions and IRA distributions:
  **table**. New York and federal public pensions are exempt in full:
  **table** (tagged `income`). Social Security is exempt.
- Benefit recapture at high incomes: **not supported** (#549).
- New York City tax: **table** (`state_local_tax`). The Yonkers
  surcharge, a share of the state tax: **not supported**.

### North Carolina (shipped)

- The flat rate and standard deduction: **shipped**. Social Security is
  exempt.
- Government pensions vested before August 1989 (Bailey) and military
  retirement: **table** (tagged `income`), not shipped.

### North Dakota

- Social Security is exempt. 40% of long-term gain is excluded: **table**.

### Ohio

- Personal exemptions by income band, gone above $500,000: **table** (one
  row per band, each with its income limit).
- The retirement income credit and the senior credit: **table**.
  Social Security is exempt.
- A traditional school district tax: **table** (`state_local_tax`).
  Municipal and earned income school district taxes fall on wages.

### Oklahoma

- A retirement income exclusion per person, and Social Security: **table**.

### Oregon

- The federal tax subtraction, capped and phased out by income: **table**.
- The personal exemption credit, ended above an income: **table**. The
  retirement income credit: **table**.
- Social Security is exempt.
- Portland-area taxes: **table** (`state_local_tax`).
- The surplus "kicker" credit, set each other year by revenue: left out.

### Pennsylvania

- Pensions and IRA distributions after retirement age are exempt, and
  Social Security: **table**. There's no standard deduction (a 0% table).
- Income is taxed in classes, and a loss in one class doesn't offset
  income in another: **not supported**.
- Philadelphia's tax on investment income: **not supported**. Local
  earned income taxes fall on wages.

### Rhode Island

- Social Security and up to $50,000 a person (2025) of pensions and
  employer plans, not IRAs, are exempt from full retirement age below an
  income ($107,000, or $133,500 joint, for 2025). The pension modification
  is **table** (tagged `income` from 67). The age test on Social Security
  is **not supported** (#581).
- The standard deduction and exemptions phase out at high income in 20%
  steps: **table**. For the standard deduction, `phaseout_end` is five
  steps ($7,250 each for 2025) above `phaseout_start` and `phaseout_steps`
  is 5, so each started step takes a fifth, and `phaseout_agi` is
  `federal`. The exemptions are
  a `state_adjustments` `filers` row, phased out as a straight line.

### South Carolina

- From 2026 (H.4216), South Carolina starts from federal AGI with a
  deduction of its own that phases down with income: **table**
  (`phaseout_start` and `phaseout_end`: $80,000 to $190,000 joint,
  $40,000 to $95,000 single; `phaseout_agi` `federal`). Two rates, the upper one with a subtraction: **table**.
- The retirement deduction, and the deduction from 65: **table**. Social
  Security is exempt.
- A share of long-term gain excluded: **table**. Check whether H.4216
  kept it.

### Utah

- The taxpayer credit, a share of the federal deduction phased out by
  income: **table** (`federal_deduction` base). The Social Security and
  retirement credits, phased out by income: **table**.

### Vermont

- Social Security is exempt below an income, phased out over the next
  $10,000: **table**.
- For income over $150,000 the tax is at least 3% of federal AGI: **not
  supported**.
- A capital gain exclusion for assets held over three years: **not
  supported** as such; `state_ltcg_exclusion` applies to all long-term
  gain.

### Virginia

- An age deduction from 65 that falls a dollar for each dollar of income
  over a limit: **table**. Social Security is exempt.
- Spouses may file separately on one return: **table**
  (`state_separate_return`).

### Washington (shipped)

- The capital gains tax, and the income tax from 2028 with its credit for
  the gains tax: **shipped**.
- The charitable and family-business deductions from the gains tax:
  **not supported**.

### West Virginia

- Social Security is exempt in full from 2026.
- The senior income exclusion from 65: **table**.

### Wisconsin

- From 2025, up to $24,000 a person of retirement income is excluded from
  67: **table**. Social Security is exempt.
- 30% of long-term gain is excluded: **table**.
- The standard deduction slides to nothing as income rises: **table**
  (`phaseout_rate`: 19.778% joint, 12% single). Head of household falls
  at 22.515% until it meets the single figure and then follows it, which
  one row can't say: its own line to nothing is close, not exact.
- The homestead credit: **household calculator**.

## Gaps left for after beta

These are rare for a retired household, or need a design of their own:

- A tax figured from the state tax (New York's Yonkers surcharge, Iowa's
  school district surtax), or a minimum tax (Vermont's 3% of AGI).
- A surtax on investment income gated by total income (Maryland's 2% on
  gains above $350,000 of income, Minnesota's 1% above $1 million).
- Gain exclusions with a holding or acquisition test (Arizona, Vermont).
- Benefit recapture: Connecticut's, and New York's supplemental tax, two
  states ([#549](https://github.com/BobKerns/analysis/issues/549)).
- Pennsylvania's income classes, and local taxes on a base of their own
  (Michigan's cities, Philadelphia's).
- Connecticut's 25% cap on taxed Social Security, and its personal tax
  credit.
- Arkansas's low-income tax tables.

## How this was checked

The survey started from each state's 2026 rates and standard deduction in
the Tax Foundation's "2026 State Income Tax Rates and Brackets" (February
2026), then checked the retirement rules most likely to have changed
against the states' own publications: Missouri's capital gains
subtraction (Department of Revenue), Michigan's 2026 retirement deduction
(Office of Retirement Services), Louisiana's 2025 reform (Revenue
Information Bulletin 25-012), Maryland's senior rules (Technical Bulletin
51), Maine's pension deduction (the Maine Public Employees Retirement System), Connecticut's retirement
income (Office of Legislative Research 2025-R-0152), Vermont's minimum tax
(32 V.S.A. § 5822) and Social Security exemption, Rhode Island's 2025
retirement income guide, Montana's 2024 simplification, South Carolina's
H.4216, Wisconsin's 2025 Act 15 and Iowa's school surtax. The other rules
are as the survey understood them, without each state's figures; read
your state's instructions for the year before writing a table.
