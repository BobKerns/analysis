# Glossary seed (funds)

The Glossary tab includes every term on the Overview or Tear sheet that a newcomer to investing wouldn't already know — skip everyday words and fund/issuer names — typically 20–35 entries. Start from these; for each included term, add a final **Here** bullet with this fund's actual figure from the tear sheet. When the tear sheet uses a term not listed, write a new entry in the same format.

Format per entry (alphabetical on the tab):

```markdown
## Term (abbreviation)

Plain definition.

- *How it's calculated:* …
- *How to read it:* …
- *Here:* this fund's figure.
```

Write each entry as Markdown in this shape: the term as a heading of its own (`##` on the Glossary tab, under its title), the definition as a paragraph, then a bullet for each of *How it's calculated*, *How to read it* and *Here* that applies. The same shape renders in a doc, in the Markdown files and in a Word or PDF export; an entry run together on one line with inline `**` and `*` is what shows up as raw Markdown when it lands as text.

## Entries

### 12b-1 fee

An annual marketing/distribution charge some mutual funds include in their expense ratio.

- *How to read it:* index funds and most no-load funds have none.

### Active share

The percentage of a fund's holdings that differs from its benchmark.

- *How to read it:* below ~60% suggests a fund that mostly mirrors its index while charging active fees ("closet indexing").

### Active vs passive (index) fund

An active fund's managers choose what to buy; a passive fund copies an index by rule.

- *How to read it:* most active funds trail their index after fees over long periods; the question for any active fund is whether its record and fee justify the difference.

### Asset allocation

The split of a portfolio among stocks, bonds and cash.

### Benchmark

The index a fund is measured against.

- *How to read it:* a fair comparison uses the same period and total returns.

### Capital-gains distribution

Profits a fund realizes from selling holdings, paid out to shareholders — taxable that year in a taxable account even if reinvested.

- *How to read it:* ETFs and index funds usually distribute little; high-turnover active funds can distribute a lot.

### Category

A peer group of similar funds (e.g. "Large Growth") used for comparisons, typically Morningstar's.

### Closet indexing

An active fund that holds nearly the same things as its index while charging more.

### Concentration / top-10 share

How much of the fund sits in its largest holdings.

- *How it's calculated:* sum of the top 10 weights.
- *How to read it:* a broad US index fund is ~30–40% in its top 10 today; above ~50% means a few companies drive results.

### Distribution yield

The last year's (or annualized recent) payouts divided by price.

- *How to read it:* not standardized; compare funds using SEC yield instead.

### Drawdown

A fall from a previous peak, in percent.

- *How to read it:* the maximum drawdown shows the worst ride an investor actually had.

### Duration

A bond fund's sensitivity to interest rates, in years.

- *How to read it:* price change ≈ −duration × change in rates; duration 6 means about −6% if rates rise 1 percentage point.

### Effective number of holdings

How many equally-weighted holdings would give the same concentration.

- *How it's calculated:* 1 ÷ sum of squared weights.
- *How to read it:* a fund with 100 holdings can behave like one with 30.

### ETF (exchange-traded fund)

A fund that trades on an exchange all day like a stock.

- *How to read it:* usually lower cost and more tax-efficient than a mutual fund holding the same things.

### Expense ratio (gross / net)

The annual cost of running the fund as a percentage of your investment, taken out of returns automatically. Net is after any temporary fee waiver.

- *How to read it:* 0.03–0.20% is typical for broad index funds; 0.50–1.00% for active funds.
- *Here:* also in dollars per $10,000.

### Fee drag

How much an expense ratio reduces an investment's value over time compared with a cheaper fund.

- *How it's calculated:* by fund_calc, compounding both at the same assumed return.

### Fee waiver

A temporary reduction of fees by the issuer, with an end date in the prospectus.

### Glide path

How a target-date fund's mix shifts from stocks toward bonds as the target year approaches and passes.

### Index

A rule-defined list of securities with weights (e.g. Nasdaq-100, S&P 500). The rules decide what's in and how much.

### Money market fund

A mutual fund holding very short-term, high-quality debt, aiming to keep its price at $1.00 and pay interest.

- *How to read it:* not FDIC-insured; government money funds hold mainly Treasury bills, agency debt and repurchase agreements.

### Mutual fund

A fund bought from and sold to the issuer once a day at the closing NAV.

### NAV (net asset value)

The value of one fund share — holdings minus liabilities, divided by shares.

- *How to read it:* ETFs also trade at a market price that can differ slightly (premium/discount).

### Premium / discount

How far an ETF's market price is above or below its NAV.

- *How to read it:* usually tiny (<0.1%) for large ETFs.

### Prospectus / summary prospectus

The legal document describing a fund's objective, strategies, risks, fees and managers.

### Repurchase agreement (repo)

A very short loan backed by securities like Treasuries; a large part of many money funds.

### SEC yield (7-day, 30-day)

A standardized yield formula set by the SEC — 7-day for money funds, 30-day for bond funds.

- *How to read it:* the fair basis for comparing yields between funds.

### Share class

Different versions of the same fund with different fees or minimums (e.g. Investor vs Admiral).

### Standard deviation (volatility)

How widely returns swing around their average.

- *How to read it:* higher means a bumpier ride.

### State-tax exemption (US government obligations)

Interest from US Treasury securities is exempt from state income tax; a fund reports what share of its income came from them.

- *How to read it:* matters most in high-tax states; repo income usually doesn't qualify, and some states have thresholds.

### Tax-advantaged account (IRA, 401(k), Roth)

An account where investment income isn't taxed year by year.

- *How to read it:* fund-level tax points such as state exemption, capital-gains distributions and ETF tax efficiency don't apply inside it. Traditional-account withdrawals are taxed as ordinary income; qualified Roth withdrawals are tax-free.

### Target-date fund

An all-in-one fund for people retiring around a given year, holding other funds and following a glide path.

### Total return

Price change plus reinvested distributions.

- *How to read it:* the right measure for comparing funds.

### Total net assets (AUM, assets under management)

The total value of the fund (or of one share class).

### Tracking difference / tracking error

How far an index fund's return differs from its index (difference = gap in return; error = how much the gap varies).

- *How to read it:* the difference is usually close to the expense ratio.

### Turnover

The share of holdings replaced in a year.

- *How to read it:* index funds are often under 10%; high turnover means more trading costs and often more taxable distributions.

### Unit investment trust (UIT)

An older fund structure with fixed rules — e.g. it can't lend securities or reinvest dividends before paying them out. SPY uses it; check the current prospectus for any given fund, since some have converted.

### Volatility decay

Why a daily-leveraged fund loses value in choppy markets even if the index ends flat.

### WAM / WAL (weighted average maturity / life)

How soon, on average, a money fund's holdings mature, in days.

- *How to read it:* shorter means the yield follows rate changes faster; SEC rules cap WAM at 60 days and WAL at 120.

### Yield to maturity

The return a bond fund's holdings would earn if held to maturity at today's prices, before fees.
