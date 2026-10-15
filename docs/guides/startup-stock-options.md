# Startup stock options: what they're worth, and how to model them

> To read a formatted `.md` file:
> - In Jupyter: right-click on it and select `Open With / Markdown Preview`
> - In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

Stock options in a private company are a real decision with an uncertain
payoff, and a good first example for reading an
[ensemble](ensembles.md). Part 1 covers the essentials in plain terms.
Part 2 goes into more depth for when a topic comes up. Part 3 shows how to
put the bet into a scenario.

The numbers here are invented, and the chances are illustrative judgment,
not data. Tax rules are as of 2026 and summarized; a tax professional
should confirm them for a real decision.

## Part 1: the essentials

### The decision

Alice has **2,000 vested options at a strike price of $4.00**. Buying the
shares ("exercising") costs up to **$8,000**, and she can buy fewer. If
she doesn't exercise before the options expire, they're gone.

Whatever she spends, **plan the household as if it's gone**. Anything that
comes back is a bonus.

### Common and preferred stock: who gets paid first

A startup has two kinds of owners.

- **Investors** buy **preferred** stock. "Preferred" means that when the
  company is sold, they get their money back *first*.
- **Employees** get **common** stock. Common holders split whatever is
  *left over*.

That's why there are two prices:

- The **preferred price** is what investors last paid. It's the headline
  valuation ("a $1 billion company").
- The **409A value** is an independent appraiser's price for a *common*
  share, required at least yearly so options can be priced and taxed. It's
  usually well below the preferred price, often 30–60% of it at a
  later-stage company, because common stands at the back of the line.

A worked example: investors paid **$250M** for 60% of the company, and
employees own the other 40%, 40 million common shares in all. Each investor
takes either their money back or their 60% share, whichever is more (a
"1x non-participating preference", the most common term).

| Company sells for | Investors take | Per common share | Alice's 2,000 shares | Versus $8,000 paid |
| --- | --- | --- | --- | --- |
| $200M | everything | $0 | $0 | −$8,000 |
| $400M | their $250M back | $3.75 | $7,500 | −$500 |
| $1B | 60% | $10.00 | $20,000 | +$12,000 |
| $3B | 60% | $30.00 | $60,000 | +$52,000 |

**A headline "sold for $400 million" can still mean employees lose
money**, because the money goes to the front of the line first. Above about
$417M here, 60% of the price beats $250M, the investors take their share,
and from then on every share gets the same amount.

### When the line is longer: bankruptcy

In a bankruptcy, more people stand in front of the shareholders:

1. Lenders with collateral (secured debt)
2. The costs of the bankruptcy itself
3. Unpaid wages, up to a cap
4. Taxes, then suppliers and other creditors
5. Preferred stock
6. Common stock, almost always last and almost always $0

Two lessons. Being excellent at the work and being able to pay this
month's bills are different things: a company can be celebrated for what it
makes and still fail. And **owners are not equal**: some hold claims,
contract terms or board seats that let them force a sale or a filing, which
the people at the back of the line can't stop and are paid last from.

### How the money comes out

**An IPO: the company goes public.**

1. Bankers help the company sell new shares to the public at an **IPO
   price**. On that day, preferred stock usually **converts to common**:
   the front-of-the-line protection ends, and every share is now the same.
2. The stock trades on an exchange, and the price moves every day with what
   buyers and sellers think it's worth.
3. Employees usually can't sell for a **lockup** of about six months, and
   the price can fall a lot in that time.
4. After that it's an ordinary stock, with the same questions as any stock
   in a brokerage account: how much of our money is in one company, and how
   much can it swing?

That last point applies to any investment:

- **One stock is a concentrated bet.** A single company can drop 50% or more
  and stay there; a broad index fund spreads that risk across hundreds.
- **Prices move on expectations, not just results.** A good quarter can send
  a stock down if people expected a great one.
- **A common rule of thumb** keeps any single stock to a small share of
  one's investments, often 5–10% at most.

**An acquisition: another company buys it.** This is how most startups that
succeed actually pay out. The board and the big investors negotiate a
price, the who-gets-paid-first table above decides each shareholder's cut,
and the check arrives at closing (sometimes shares in the buyer instead).
Often 10–15% is held back for a year or so in case problems turn up after
the sale.

**Shutdown.** The company runs out of money, or sells for less than
investors put in. Common stock gets nothing.

### Picturing the outcomes

Five futures, a few years out, for a well-funded, later-stage private
company:

| Outcome | What happens | Chance |
| --- | --- | --- |
| Lost | Shutdown, or sold for less than investors put in | ~30% |
| Small | Sold near the money raised; investors take most of it | ~25% |
| Fair | Sold near today's value | ~25% |
| Good | Strong sale or IPO | ~15% |
| Great | Breakout | ~5% |

The **most likely** outcome is about nothing, but the **average** is roughly
today's 409A value, propped up by the rare big wins. So whether exercising
is a fair bet depends on how the 409A compares with the strike price. And
nobody knows **when**: anywhere from 2 to 10 years, or never.

### Taxes, briefly

- Exercising incentive stock options (ISOs) creates no regular income tax.
  The gap between the 409A value and the strike counts toward the
  **Alternative Minimum Tax** that year.
- Selling more than **1 year after exercising** (and 2 after the grant)
  makes the whole gain a long-term capital gain.
- If the shares end up worthless, what was paid becomes a capital loss.
- After leaving the company, exercising within 3 months keeps these rules;
  later, the gap is taxed as wages instead.

## Part 2: in more depth

### The paperwork

- **Accepting a grant** (often through an equity platform's email) signs
  the option agreement; it is not an exercise. Some companies won't allow an
  exercise until the grant is accepted.
- **Exercising** is a separate step with its own payment. The platform
  usually shows the 409A value as "fair market value" on the exercise
  screen, with the cost and an estimated tax.
- **Expiry.** Options last up to 10 years from grant while employed. After
  leaving, the window is often 90 days.

### If the 409A is below the strike

That happens after a valuation cut. Exercising then means paying more than
the appraiser thinks a share is worth today, and only a large outcome makes
it pay. There's no AMT in that case, since there's no gap.

### AMT, worked through

Suppose the 409A is $7.00 and Alice exercises all 2,000 options at $4.00:

- The gap: ($7.00 − $4.00) × 2,000 = **$6,000**, added to income for AMT
  that year (if she still holds the shares at year end).
- A married couple's AMT exemption is about **$140,000** in 2026, and most
  households' AMT comes out well below their regular tax. A $6,000 addition
  creates AMT only if the two were already close.
- AMT that is paid isn't all lost: it becomes a **credit** against regular
  tax in later years.
- The company sends **Form 3921** in January after an ISO exercise; Form
  6251 works out the AMT.

### Holding periods and "disqualifying dispositions"

Selling more than 2 years after the grant and 1 year after exercising is a
**qualifying disposition**: the whole gain is long-term capital gain.
Selling sooner, which a quick acquisition can force, is a
**disqualifying disposition**: the gap on exercise day is taxed as ordinary
income, and only the rest as capital gain.

### Non-qualified options

Options that aren't ISOs (or ISOs exercised more than 3 months after leaving)
are taxed differently: the gap at exercise is ordinary income that year,
with withholding if she's still an employee, and there's no AMT
preference.

### If the shares become worthless

The amount paid is a capital loss: it offsets other gains, plus up to $3,000
a year of ordinary income, carried forward until used. It counts only once
the shares are sold or truly worthless, and a private company can drift for
years before that's clear.

### QSBS (Section 1202)

Qualifying small-business stock held long enough can exclude much or all of
a gain from federal tax. The test applies when shares are issued, which is
at exercise, and a well-funded company has usually outgrown the size limit
($75M in assets for stock issued since July 2025) by then. Worth asking,
not worth counting on.

### Other ways to sell before an exit

- **Tender offers.** The company or a new investor offers to buy some
  employee shares at a set price, usually capped (say, up to 10–20% of
  one's shares). Selling some turns part of the bet into cash and keeps the
  rest.
- **Secondary sales.** Selling to a private buyer through a marketplace, if
  the company allows it; many don't, especially for small holdings. Prices
  for most companies sit below the last preferred price.
- **Company rights.** The stock agreement may let the company buy shares
  back, or buy them first when one tries to sell (a right of first refusal).

### Acquisitions in more detail

- **Cash or stock.** A cash deal is taxed in the year of the sale. A
  stock-for-stock deal can often defer the tax until the new shares are
  sold.
- **Escrow and earn-outs.** Part of the price may be held back for claims
  after the sale, or paid only if targets are met later.
- **Messier cap tables.** Real companies have several funding rounds, each
  with its own price and terms, so the line can have several places in it.

### Dilution

Each funding round typically issues 10–25% more shares, so an existing
holder's slice shrinks. That's fine if the company's value grows faster.

## Part 3: modeling it

The `discrete` distribution ([Ensembles](ensembles.md), "Choosing a
distribution") draws one of the outcomes for each ensemble member, at its
weight. Put the exercise cost and the exit into the scenario:

1. **The exercise**, an ordinary `one_off_transfer` of the cost out of
   checking on the exercise date.
2. **The exit**, a `one_off_transfer` into the brokerage account with a
   `discrete` amount, one outcome per row of the table above (the account
   names are the prototype scenario's):

   ```sh
   sim calendar add transfer 2031-06-30 Income_Inheritance Asset_Brokerage_Joint 1.00 \
     --to-lot default --scenario inflation_1 --name stock_exit \
     --vary-target amount --vary-distribution discrete \
     --vary-outcomes '30%: 0; 25%: 1000..6000; 25%: 6000..20000; 15%: 20000..55000; 5%: 55000..140000'
   ```

A row varies its amount or its date, not both, so the exit date here is
fixed; a second scenario with a later date shows what waiting does.

Reading the results:

- **Member 0, the base case, is the median outcome**, which for this bet is
  probably the zero or close to it.
- **The mean is above the median**, held up by a few members with big
  outcomes.
- **The bottom of the fan chart only drops, by the exercise cost.** The
  worst futures are the ones where nothing came back, so the bet raises the
  average and the top without making the bad cases any better. That's the signature of a
  lottery-like bet, and the reason to plan the household as if the money is
  gone.
