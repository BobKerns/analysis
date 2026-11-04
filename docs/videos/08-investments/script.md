+++
series = "Planning with the simulator"
episode = "Video 8"
title = "Investments"
subtitle = "Inside the accounts: lots, growth and tax"
data = "prototype_data"

[youtube]
title = "Investments"
summary = """
Inside Alice and Bill's accounts: the lots each one holds, how they grow, \
and where the tax falls, account by account. They buy a fund with one \
command, see what they own by asset class, and use a Roth conversion to \
put their fastest-growing money where it will never be taxed."""
contents = [
  "Lots: what an account holds, and its cost basis (sim account lot list, sim account holdings)",
  "Adding a fund: sim symbol add",
  "Buying it later: sim invest",
  "How a lot grows: its series, its fund's asset classes",
  "What you own, by asset class (sim allocation)",
  "Where the tax falls: cash, brokerage, traditional, Roth",
  "Selling: basis back first, then the gain (sim account history, sim report gains)",
  "A Roth conversion with sim invest, and asset location",
  "The accounts notebook",
  "Asking an AI",
]
+++

<!-- cspell:ignore BALX STKX -->

The eighth video of #366 (issue #371): investments. Alice and Bill's base
scenario from video 3, with video 7's checking sweep and its Roth
conversion (`convert`), looked at from the investment side.

`setup.sh` beside this script builds base and `convert` with the commands
videos 3 to 7 type, then what the scenes make, tagging the data repo before
each scene (`stage/<scene>`) so a tape can make on camera what later scenes
already have. Each scenario is run once at the end and its run renamed to a
fixed time. The tapes work in `~/retirement/data/sim`, as in videos 3 to 7.

> Numbers in the narration were re-checked on 2026-11-04 against `setup.sh`
> with #467's indexed brackets and #468's payouts.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video eight: investments.

So far, an account has been one number. This time, Alice and Bill look
inside: what each account holds, how it grows, and where the tax falls.

## Scene `contents`: Inside the accounts

```slide
template = "card"
heading = "Inside the accounts"
body = [
  "Lots: what an account holds",
  "Adding a fund, and buying it",
  "How a lot grows",
  "Where the tax falls",
  "A Roth conversion, from the investment side",
  "The notebook, and asking an AI",
]
speaker = "bill"
```

First, lots: the pieces an account is made of. Then adding a fund, and
buying it. Then how each lot grows, and where the tax falls, account by
account. Then video seven's Roth conversion, seen from the investment side.
And last, the notebook, and asking an AI.

## Scene `lots`: What an account holds

```slide
template = "card"
heading = "An account is a set of lots"
body = [
  "A lot: one holding, bought at one time",
  "What it holds: a rate series, or a symbol (a stock or fund)",
  "When it was bought: long-term or short-term at a sale",
  "Its cost basis: what was paid, which comes back untaxed",
  "An account you haven't split has one lot, named default",
]
speaker = "alice"
```

An account is really a set of lots. A lot is one holding, bought at one
time. It says what it holds: a rate series, like US stocks, or a symbol,
the ticker of a stock or a fund. It says when it was bought, which decides
whether a gain is long-term or short-term. And it has a cost basis: what
was paid for it. When it's sold, the basis comes back untaxed; only the
gain is taxed. An account you haven't split has just one lot, named
default.

## Scene `holdings`: Alice and Bill's brokerage

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/holdings && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim account lot list Asset_Brokerage_Joint"
Sleep 400ms
Enter
Wait
Sleep 8s
Type "clear && sim account holdings Asset_Brokerage_Joint --opening"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Alice and Bill's brokerage account has two lots, made back in video
three: stocks, growing at the US stock rate, and bonds. Sim account lot
list shows them. Sim account holdings adds what each is worth: the stocks
cost a hundred and fifty thousand and are worth two hundred and forty, so
ninety thousand of that is a gain that hasn't been taxed yet. The bonds
have only five thousand.

## Scene `symbol`: Adding a fund

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/symbol && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type `sim symbol add BALX --name "Example Balanced Fund" --weight stock_us=60% --weight bond=40% --no-fetch`
Sleep 400ms
Enter
Wait
Sleep 9s
```

Alice and Bill want to move some savings into a balanced fund. The
simulator needs to know what the fund holds. Sim symbol add records its
name, and its weights: sixty percent US stocks, forty percent bonds. For a
real fund it would fetch the prices too; this one is made up, so dash dash
no fetch. To find a real fund's weights, ask an AI, or use sim symbol
research; the guide shows how.

## Scene `invest`: Buying it

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/invest && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim invest Asset_Savings Asset_Brokerage_Joint balanced --symbol BALX --amount 30000 --date 2027-01"
Sleep 400ms
Enter
Wait
Sleep 9s
Type "clear && sim account lot list Asset_Brokerage_Joint"
Sleep 400ms
Enter
Wait
Sleep 8s
```

Sim invest buys it. From savings, into a new lot of the brokerage, named
balanced, holding the fund: thirty thousand dollars next January. One
command writes the lot, bought in January, and the transfer that pays for
it. A holding Alice and Bill already own would be sim account lot add
instead.

## Scene `growth`: How a lot grows

```slide
template = "card"
heading = "How a lot grows"
body = [
  "Every month, at a yearly rate",
  "Its own series, else its symbol's, else its fund's asset classes",
  "A fund: the blend of its classes (60% stocks, 40% bonds)",
  "Growth is total return: dividends and interest reinvested",
  "In an ensemble, every one of those rates can vary",
]
speaker = "bill"
```

Each lot grows every month, at a yearly rate. Where does the rate come
from? The lot's own series if it has one; otherwise its symbol's; and for
a fund, the blend of what it holds: sixty percent of the stock rate, plus
forty percent of the bond rate. A target date fund, whose mix changes
over the years, grows at the mix of each year. Part of that return is
paid out each year: dividends from the stocks, interest from the bonds,
reinvested in the lot. And in an ensemble, from video six, every one of
those rates can vary.

## Scene `allocation`: What they own

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/done && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim run base"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "clear && sim allocation"
Sleep 400ms
Enter
Wait
Sleep 12s
Type "clear && sim account history Asset_Brokerage_Joint --lot balanced --yearly --end 2032"
Sleep 400ms
Enter
Wait
Sleep 12s
```

After a run, sim allocation adds up everything by asset class: US stocks,
bonds, cash, across every account. That's the mix to check against the
one Alice and Bill meant to have. And sim account history, with dash dash
lot, follows one lot year by year: the balanced fund, bought for thirty
thousand, growing each year. Its dividends and interest add to its basis,
so only its rise in price is still untaxed.

## Scene `taxes`: Where the tax falls

```slide
template = "card"
heading = "Where the tax falls"
body = [
  "Cash: interest taxed every year, as ordinary income",
  "Brokerage: dividends and interest every year; the rest at the sale",
  "Traditional IRA: everything out is ordinary income; RMDs from 75",
  "Roth: nothing, once the five-year rule is met",
  "At a death, a brokerage lot's basis steps up",
]
speaker = "alice"
```

Where the tax falls depends on the account. Cash pays interest, taxed
every year as ordinary income. In a brokerage, dividends and interest are
taxed the year they're paid, dividends at the lower capital gains rates,
even though they're reinvested. The rest of the growth is taxed when it's
sold: the basis comes back untaxed, and the gain is long-term or
short-term, by when the lot was bought. Everything out of a traditional
IRA is ordinary income, and from seventy five, required minimum
distributions make sure it comes out. A Roth taxes nothing, once its
five-year rule is met. And at a death, a brokerage lot's basis steps up,
so the gain until then is never taxed.

## Scene `gains`: Selling

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/done && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim account history Asset_Brokerage_Joint --lot bonds --yearly --start 2029 --end 2034"
Sleep 400ms
Enter
Wait
Sleep 12s
Type "clear && sim report gains --start 2029 --end 2037"
Sleep 400ms
Enter
Wait
Sleep 14s
```

When both have retired, the spending chain sells the bonds first. Each
sale takes the same share of the lot's basis as of its balance. Its
interest was taxed as it was paid, and added to its basis, so in 2031 the
lot falls by seventy four thousand dollars and its basis by seventy two:
nearly all of it comes back untaxed. Sim report gains shows each year's
gains: realized, meaning sold or paid out, and unrealized, still growing
in the accounts.

## Scene `location`: Which money goes where

```slide
template = "card"
heading = "Asset location"
body = [
  "Same investments, different accounts, different tax",
  "Roth: the fastest growth, never taxed",
  "Traditional IRA: taxed as income however it grows",
  "Brokerage: stocks, whose gains wait for a sale, or a step-up",
  "A conversion moves money from the IRA to the Roth",
]
speaker = "bill"
```

That makes where an investment sits matter, not only what it is. Growth
in a Roth is never taxed, so it's the place for the fastest growing
money. A traditional IRA is taxed as income however it grows. And in a
brokerage, stocks' gains wait for a sale, or a step-up. In video seven,
Alice converted part of her IRA to a Roth each year. Here's that
conversion again, from the investment side.

## Scene `roth-lots`: The conversion, account by account

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/done && cd ~/retirement/data/sim && sim switch convert > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim account history Asset_TraditionalIRA_Alice --scenario convert --yearly --start 2030 --end 2040"
Sleep 400ms
Enter
Wait
Sleep 12s
Type "clear && sim account holdings Asset_Roth_Alice --scenario convert --month 2036-12"
Sleep 400ms
Enter
Wait
Sleep 12s
```

In convert, Alice's IRA shrinks by fifty to eighty thousand dollars a
year from 2031, and stops at about two hundred and thirteen thousand in
today's dollars, under a third of what it reaches in base. So her required
distributions, and the tax on them, are smaller for good. The Roth holds
what came over: its basis is what was converted, and everything above
that will never be taxed.

## Scene `invest-roth`: A one-off conversion

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/invest-roth && cd ~/retirement/data/sim && sim switch convert > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create roth-fund convert"
Sleep 400ms
Enter
Wait
Sleep 6s
Type `clear && sim invest Asset_TraditionalIRA_Alice Asset_Roth_Alice fund_2030 --source-lot default --subkind roth_ira --amount 40000 --date 2030-01 --symbol STKX --symbol-name "Example Stock Index Fund" --weight stock_us=100% --no-fetch`
Sleep 400ms
Enter
Wait
Sleep 12s
```

A single conversion is a sim invest too, from the IRA into a Roth lot.
Here, forty thousand dollars in January 2030, a year before the yearly
ones start, into a stock index fund. Dash dash subkind says the new lot is
a Roth IRA. The money leaving the IRA is taxed as income that year, and
the new lot's basis is what was converted. Alice is over fifty nine and a
half; someone younger should give each conversion its own lot, since each
starts its own five-year clock.

## Scene `location-table`: By account type

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/done && cd ~/retirement/data/sim && sim switch roth-fund > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim allocation --by tax_treatment --scenario roth-fund --month 2040-12"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Sim allocation, by tax treatment, shows where each kind of investment
sits: taxable, tax deferred, or tax free. In 2040, most of Alice's IRA
has moved to the Roth. And the comparison from video five gives the
result: the extra conversion pays about six thousand dollars less tax
over the run than convert alone, and ends about four thousand dollars
ahead.

> Numbers: roth-fund vs convert from `sim compare base convert roth-fund`
> (Δ tax −$79.7k vs −$73.8k; 2060 after tax +$150.8k vs +$146.4k). Show
> the compare on screen too, or keep it narrated?

## Scene `nb-accounts`: The accounts notebook

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "accounts.ipynb"
RunAll
Scroll "Assets held within an account"
Show
Sleep 6s
Select "Account:" "Asset_Brokerage_Joint"
Sleep 5s
Scroll "Portfolio by asset class."
Sleep 8s
```

The accounts notebook has the same views. The holdings table lists an
account's lots, with their basis and gain; further down, the portfolio by
asset class, with a chart.

> To check at build: the holdings widget's control labels and the
> allocation widget's month picker.

## Scene `ai`: Asking an AI

```slide
template = "card"
heading = "Asking an AI about investments"
body = [
  "\"What's in this fund? Write it as sim symbol add.\"",
  "\"Which of our lots have the biggest untaxed gains?\"",
  "\"Should our bonds be in the IRA or the brokerage?\"",
]
speaker = "alice"
```

An AI assistant helps here too. It can look up what a fund holds, and
write the sim symbol add command for you; check it against the fund's own
fact sheet. It can read a run, and find the lots with the biggest untaxed
gains. And it can talk through which investments belong in which account.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: advanced"
body = "Lifespans, guard rails and strategies: plans that respond."
speaker = "bill"
```

Next time: advanced topics. Lifespans, guard rails, and strategies: plans
that respond to what happens.
