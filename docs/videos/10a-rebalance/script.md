+++
series = "Planning with the simulator"
episode = "Video 10a"
title = "Rebalancing"
subtitle = "A strategy that keeps the mix on target"
data = "prototype_data"

[youtube]
title = "Rebalancing"
summary = """
Alice and Bill's spending chain draws their bonds first, so the mix they \
chose slowly turns into all stocks. They give their portfolio a target, \
add a rebalance strategy that draws from whatever is overweight and \
invests in whatever is underweight, and see what that costs on one \
future and what it buys across eighty."""
contents = [
  "What a strategy is",
  "How a chain's order changes the mix (sim allocation, sim account history)",
  "Asset classes for every lot (sim symbol add ACCOUNT:LOT)",
  "A portfolio and its targets (sim account tag add, sim target set)",
  "A rebalance strategy on the spending chain (sim strategy add)",
  "Investing the overflow where it's underweight",
  "What it did (sim strategy log, sim explain, sim allocation --drift)",
  "Strategy off vs on (sim strategy edit --disable, sim compare)",
  "Across an ensemble (sim ensemble drift, sim ensemble compare)",
  "The accounts and comparison notebooks",
  "Asking an AI",
]
+++

The first of video 10's two parts (issue #410): rebalancing. Alice and
Bill's base scenario from video 3, with video 7's checking sweep, as in
video 9 but without its lifespans.

`setup.sh` beside this script builds base with the commands videos 3 to 7
type, then what the scenes make, tagging the data repo before each scene
(`stage/<scene>`) so a tape can make on camera what later scenes already
have. Each scenario is run once at the end and its run renamed to a fixed
time. The tapes work in `~/retirement/data/sim`, as in videos 3 to 9.

> Numbers in the narration were checked on 2026-11-10 against `setup.sh`.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video ten, part one: rebalancing.

Last time, a strategy cut spending when a guard rail was crossed. This
time, a strategy that keeps Alice and Bill's investments in the mix they
chose.

## Scene `contents`: Keeping the mix

```slide
template = "card"
heading = "Keeping the mix"
body = [
  "What a strategy is",
  "How the mix drifts",
  "Asset classes and targets",
  "A strategy that draws and invests",
  "What it did, and was it worth it",
]
speaker = "bill"
```

First, what a strategy is. Then how Alice and Bill's mix drifts without
one. Then asset classes and a target, a strategy that draws and invests
to stay near it, what it did, and whether it was worth it.

## Scene `strategy`: Rules and strategies

```slide
template = "card"
heading = "A strategy decides as it goes"
body = [
  "A rule says what happens: $7,500 a month to living",
  "A chain says where money comes from, in a fixed order",
  "A strategy decides each time, from that month's balances",
  "It owns a chain, or the rules with a tag",
  "Without one, the chain's own order decides",
]
speaker = "alice"
```

A scheduled rule says what happens: seventy five hundred dollars a month
to living expenses. A chain says where the money comes from, always in
the same order. A strategy decides each time, from that month's
balances. It owns a chain, or the rules carrying a tag, as in video nine.
Switch it off, and the chain's own order decides again.

## Scene `mix`: How the mix drifts

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/mix && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim allocation --opening"
Sleep 400ms
Enter
Wait
Sleep 8s
Type "clear && sim chain show spending"
Sleep 400ms
Enter
Wait
Sleep 7s
Type "clear && sim account history Asset_Brokerage_Joint --lot bonds --yearly --end 2035"
Sleep 400ms
Enter
Wait
Sleep 12s
```

Sim allocation dash dash opening shows what they start with, by asset
class: about seventy eight percent stocks, fifteen percent bonds, and
some cash. The stocks come in two classes, stock and stock US; more on
that in a moment. Their spending chain draws savings first, then the
brokerage's bonds, then its stocks, then Alice's IRA: an order chosen for
taxes. Sim account history shows what that does to the bonds. They hold
steady until 2030, then pay for the bridge years and the car, and by
2033 they're gone. From then on, Alice and Bill hold nothing but stocks,
though nobody decided that.

## Scene `classes`: Every lot needs a class

```slide
template = "card"
heading = "Every lot in the portfolio needs an asset class"
body = [
  "From its symbol's weights (video 8)",
  "Or a row for the lot itself: ACCOUNT:LOT",
  "Or the series it grows at: stock_us, bond, ...",
  "One unclassified lot would skew every share",
]
speaker = "bill"
```

A strategy that steers by asset class has to know every lot's class. A
lot holding a fund gets it from the fund's weights, as in video eight. A
lot with no symbol can have a row of its own, named for its account and
lot. Otherwise its class comes from the growth series it follows. The IRA
and the Roth grow at the series called stock, and the brokerage's stocks
at stock US. Alice and Bill think of all three as US stocks, so they'll
say so.

## Scene `targets`: A portfolio and its target

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/targets && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create rebalance base > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim account tag add Asset_Brokerage_Joint portfolio:invest"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim account tag add Asset_TraditionalIRA_Alice portfolio:invest"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim account tag add Asset_Roth_Bill portfolio:invest"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim symbol add Asset_TraditionalIRA_Alice:default --weight stock_us=100%"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "clear && sim symbol add Asset_Roth_Bill:default --weight stock_us=100%"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim target set main stock_us=80% bond=20%"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim allocation --opening"
Sleep 400ms
Enter
Wait
Sleep 9s
```

They make a new scenario, rebalance. A portfolio is the accounts carrying
one tag: portfolio colon invest goes on the brokerage, Alice's IRA and
Bill's Roth. Sim symbol add, with the account and lot in place of a
ticker, classes the IRA as US stocks, and the same for the Roth. Sim
target set main: eighty percent US stocks, twenty percent bonds. Sim
allocation now shows two kinds of investment, and a mix that's already
five points heavy in stocks.

## Scene `add`: A rebalance strategy

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/add && cd ~/retirement/data/sim && sim switch rebalance > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim strategy kinds rebalance"
Sleep 400ms
Enter
Wait
Sleep 14s
Type "clear && sim strategy add draw --kind rebalance --chain spending --param portfolio=portfolio:invest --param targets=main"
Sleep 400ms
Enter
Wait
Sleep 8s
```

Sim strategy kinds rebalance describes the built-in kind. While every
class is within its band, five points by default, the chain's own order
decides, so the tax-driven order still holds most of the time. Past the
band, each slice of a draw comes from whichever lot brings the mix
closest to the target. Sim strategy add draw: kind rebalance, on the
spending chain, with the portfolio tag and the target set. Left out, the
command asks for each of these.

## Scene `invest`: Investing where it's underweight

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/invest && cd ~/retirement/data/sim && sim switch rebalance > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim chain add invest Asset_Brokerage_Joint --role destination --lot stocks"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim chain step add invest Asset_Brokerage_Joint --lot bonds"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim account lot edit Asset_Checking --overflow-chain invest --overflow-lot ''"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "clear && sim strategy add place --kind rebalance --chain invest --param portfolio=portfolio:invest --param targets=main"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "clear && sim strategy list"
Sleep 400ms
Enter
Wait
Sleep 8s
```

Money flows in, too. In video seven, cash above thirty thousand dollars in
checking is swept into the brokerage's stocks. They make a destination
chain, invest, with the stocks and then the bonds, point checking's
overflow at it, and give it a strategy of its own, place, with the same
portfolio and target. Now the overflow goes to whatever is underweight.
Sim strategy list shows the two.

## Scene `log`: What it did

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/log && cd ~/retirement/data/sim && sim switch rebalance > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim run rebalance --commit"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim strategy log | head -n 8"
Sleep 400ms
Enter
Wait
Sleep 14s
Type "clear && sim explain | grep -A 2 '^strategies'"
Sleep 400ms
Enter
Wait
Sleep 9s
Type "clear && sim allocation --drift --chart"
Sleep 400ms
Enter
Wait
Sleep 14s
```

They run it; dash dash commit saves the changes first. Sim strategy log
lists each decision. In August 2026 bonds are fifteen percent against a
twenty percent target, so the sweep puts sixty seven hundred dollars into
bonds. In 2029 stocks are above eighty five percent, so the next refill
of checking comes from stocks. Sim explain totals it: four hundred and
thirty seven draws, one point two million dollars, and ten placements,
about half a million. Sim allocation dash dash drift charts how far the
mix strays at each year end: about five points, the edge of the band,
for the whole run.

## Scene `nb-drift`: The accounts notebook

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "accounts.ipynb"
RunAll
Scroll "Drift from target allocation."
Show
Sleep 4s
ScrollBy 450
Sleep 12s
```

The accounts notebook draws the same drift month by month, with the band
shaded and a tick on the axis for each month the strategy stepped in.

## Scene `off`: Strategy off, strategy on

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/off && cd ~/retirement/data/sim && sim switch rebalance > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create no-rebalance rebalance > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim strategy edit draw --disable"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim strategy edit place --disable"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim run no-rebalance --commit"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim compare no-rebalance rebalance --pre-tax --section strategies --start 2026 --end 2040"
Sleep 400ms
Enter
Wait
Sleep 16s
Type "clear && sim compare no-rebalance rebalance --pre-tax --section net-worth --chart"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Was it worth it? A scenario built on rebalance, with both strategies
switched off: sim strategy edit dash dash disable. Everything else is the
same. Sim compare's strategies section sets the two side by side. Without
the strategy, the mix passes the band in 2026 and is twenty points off
from 2033, all stocks. With it, it stays near five. And net worth, before
tax so that only the strategy differs: rebalancing ends 2060 about four
hundred and fifty thousand dollars behind. That's not a mistake. In one
future with steady returns, twenty percent bonds simply grow less than
all stocks.

## Scene `why`: What rebalancing is for

```slide
template = "card"
heading = "Rebalancing is about the bad futures"
body = [
  "One future: less in bonds grows more",
  "Many futures: bonds cushion the bad ones",
  "Expect a lower middle and a better worst case",
  "Judge it across an ensemble",
]
speaker = "alice"
```

One steady future can't show what rebalancing is for. With more in
stocks, the good futures are better and the bad ones are worse. A fixed
mix gives up some of the middle to protect the bottom. To see that, they
need many futures: an ensemble.

## Scene `mc`: Eighty futures each way

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/mc && cd ~/retirement/data/sim && sim switch no-rebalance > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble create rebalance-mc rebalance t_inflation_current t_market_current > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim ensemble create no-rebalance-mc no-rebalance t_inflation_current t_market_current > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim run rebalance-mc --commit"
Sleep 400ms
Enter
Wait@180s
Sleep 3s
Type "clear && sim run no-rebalance-mc"
Sleep 400ms
Enter
Wait@180s
Sleep 3s
```

As in video six, two ensembles of eighty members with inflation and the
markets varying: rebalance MC and no rebalance MC. Built from the same
templates with the same seed, member five in one sees the same markets as
member five in the other. Running them takes a minute or two each.

## Scene `mc-compare`: What it bought

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/done && cd ~/retirement/data/sim && sim switch rebalance-mc > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble drift no-rebalance-mc | head -n 12"
Sleep 400ms
Enter
Wait
Sleep 8s
Type "clear && sim ensemble drift rebalance-mc | head -n 12"
Sleep 400ms
Enter
Wait
Sleep 12s
Type "clear && sim ensemble compare no-rebalance-mc rebalance-mc --chart"
Sleep 400ms
Enter
Wait
Sleep 20s
```

Sim ensemble drift shows the spread. Without the strategy, every future
is twenty points off by 2033. With it, the typical future stays near the
band; in the worst years, one future in twenty strays to about thirteen
points, since the strategy only steers with money that moves anyway. Sim
ensemble compare pairs the futures. In the middle, rebalancing is behind,
more so as the decades pass, and it's ahead in only about one future in
five. But the bottom chart is the point: the worst five percent of
futures end most years from 2044 to 2056 about thirty thousand dollars better
off, and the worst twenty percent about fifteen thousand. Less upside, a
better floor. Whether that trade is worth it is Alice and Bill's call.

## Scene `nb-compare`: The comparison notebook

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "scenario_compare.ipynb"
RunAll
Scroll "Strategies: on vs off"
Show
Sleep 4s
ScrollBy 450
Sleep 12s
```

The scenario comparison notebook draws each run's drift together, with
the band shaded, and below it what each scenario's strategies moved. The
ensemble notebook has the same drift for every member.

## Scene `ai`: Asking an AI

```slide
template = "card"
heading = "Asking an AI about rebalancing"
body = [
  "\"Is a five-point band right for us?\"",
  "\"Should our target change as we age?\"",
  "\"Which account should hold the bonds?\"",
]
speaker = "bill"
```

An AI assistant can help here too. It can talk through how wide a band
to allow, and try a few in ensembles. It can explain glide paths, where
the target changes as you age; each change is another target set, with
dash dash as of. And it can talk through which account should hold the
bonds, which changes the taxes as well as the mix.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: spending strategies"
body = "Spending that changes with the markets: thresholds, guardrails, ceilings and floors."
speaker = "alice"
```

Next time: spending strategies. Spending that changes with the markets,
and what that does to the plan.
