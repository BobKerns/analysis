+++
series = "Planning with the simulator"
episode = "Video 3"
title = "Your base scenario"
subtitle = "Accounts, income and spending, and where the money comes from"
data = "prototype_data"
+++

The third video of #366 (issue #368): filling in the base scenario that
`sim init` started in video 2. Accounts and their lots, the scheduled rules
that move money each month, a first look at the result, a chain that keeps
checking topped up, and calendar events for one-time changes.

Like video 2's, `setup.sh` beside this script makes Alice's data repo in a
home folder of its own. It then builds the whole base scenario with the
same commands the scenes type, and tags the data repo where each scene
starts (`stage/<scene>`). Each tape resets to its tag while hidden, so it
types its own commands into the state the scene before left. A scene
that only shows a result runs the scenario while hidden first.

The tapes work in `~/retirement/data/sim`, so the CLI's messages name files
as `scenarios/base/...`, which fits the terminal.

Notebooks are left to video 4, until the pipeline can record them.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video three: your base scenario.

Last time, Alice made her data folder. Its base scenario is empty. This
time, she fills it in: their accounts, their income and spending, and the
rules that move money between them. By the end, it runs, and the money goes
where it should.

## Scene `contents`: What goes in base

```slide
template = "card"
heading = "What goes in base"
body = [
  "Accounts, and what's in them today",
  "Rules: income and spending, month by month",
  "Chains: where money comes from when it runs short",
  "The calendar: one-time changes on a date",
]
speaker = "alice"
```

Base holds the household as it is today, and the plan as it stands. First,
the accounts, and what's in them now. Then the rules that move money every
month: paychecks and Social Security coming in, spending going out. Then
chains, which say where money comes from when an account runs short. And
last, the calendar, for one-time changes on a date. Every other scenario
builds on base, so this is the one to get right.

## Scene `person`: Bill joins the household

```tape
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/person && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim person add bill --display 'Bill Example' --birthdate 1964-08-20 --sex m --role primary"
Sleep 400ms
Enter
Wait
Sleep 8s
```

Sim init put Alice in the household. Bill comes next, with sim person add:
an id, his name, his birth date, and his role. Primary means he's one of the
two spouses the taxes and the survivor rules are about. People are shared by
every scenario, so this changes them all.

## Scene `kinds`: Four kinds of account

```slide
template = "card"
heading = "Four kinds of account"
body = [
  "Assets: checking, savings, investments, a home",
  "Liabilities: a mortgage, a loan",
  "Income: where money comes from, such as a salary",
  "Expenses: where it goes, such as living costs",
]
speaker = "bill"
```

Everything the simulator tracks is an account, and there are four kinds.
Assets are what they own: checking, savings, investments, a home.
Liabilities are what they owe, like a mortgage. The other two are the
edges of the picture. An income account is where money comes from, such as
Bill's salary, and an expense account is where it goes, such as living
costs. Every dollar moves from one account to another, so nothing appears or
disappears without a trace.

## Scene `accounts`: Adding accounts

```tape
Set FontSize 30
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/accounts && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim account add Asset_Checking asset cash --owner joint --opening-balance 15000"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "sim account add Asset_Savings asset cash --owner joint --series cash --tax-character ordinary --opening-balance 60000"
Sleep 400ms
Enter
Wait
Sleep 7s
Type "sim account add Asset_TraditionalIRA_Alice asset retirement --owner alice --lot-subkind traditional_ira --series stock --opening-balance 500000"
Sleep 400ms
Enter
Wait
Sleep 9s
```

Sim account add makes an account: its name, its kind, and a subkind that
says what sort it is. Checking is cash, owned jointly, with fifteen thousand
dollars in it today. Savings is cash too, but it earns interest: the series
names the rate it grows at, here the cash rate from base's market
assumptions, and the interest is taxed as ordinary income. Alice's
traditional IRA is a retirement account. It's hers alone, the lot subkind
says it's a traditional IRA, so its money is taxed when it comes out, and
it's invested in stocks. Each command says what it wrote, and where. These
are plain files you could also edit by hand.

> Production note: the names, such as `Asset_Checking`, are on screen; the
> narration says what they are rather than reading them out.

## Scene `lots`: An account with two lots

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/lots && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim account add Asset_Brokerage_Joint asset brokerage_taxable --owner joint"
Sleep 400ms
Enter
Sleep 1500ms
Type "n"
Sleep 600ms
Enter
Wait
Sleep 3s
Type "sim account lot add"
Sleep 400ms
Enter
Sleep 1500ms
Type "Asset_Brokerage_Joint"
Sleep 600ms
Enter
Sleep 1s
Type "stocks"
Sleep 600ms
Enter
Sleep 1s
Enter
Sleep 700ms
Enter
Sleep 700ms
Enter
Sleep 1s
Type "stock_us"
Sleep 600ms
Enter
Sleep 1s
Enter
Sleep 1s
Type "240000"
Sleep 600ms
Enter
Sleep 1s
Type "150000"
Sleep 600ms
Enter
Sleep 1s
Enter
Sleep 5s
Enter
Wait
Sleep 4s
Type "sim account lot add Asset_Brokerage_Joint bonds --series bond --opening-balance 160000 --opening-cost-basis 155000"
Sleep 400ms
Enter
Wait
Sleep 6s
```

Every account is really a set of lots. Most accounts have one, but their
brokerage account holds two things that grow differently: a stock fund and
a bond fund. So the account starts empty, and each holding is a lot of its
own. Any command that needs more than you gave it asks for the rest, so
here's sim account lot add with nothing at all. It asks for the account, a
name for the lot, and then a question at a time, with a line of help above
each. The stocks lot grows at the US stock rate. Since it's the account's
first lot to grow, it also asks how that growth is taxed, offering capital
gains. Its balance today is two hundred forty thousand dollars, and they
paid a hundred fifty thousand for it, its cost basis, so the simulator knows
the gain when they sell. It shows the lot, and the command that does the
same thing in one line, to keep for next time. The bonds lot uses that one
line form.

> Production note: the guided lot add's answers, in order: account, lot,
> symbol (blank), acquired (blank), quantity (blank), series, tax character
> (the offered `gains`), balance, basis, balance management (the offered
> `n`), then `write`.

## Scene `more-accounts`: The rest of the accounts

```tape
Set FontSize 22
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/scheduled && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim account list"
Sleep 400ms
Enter
Wait
Sleep 18s
```

The rest go in the same way: Bill's Roth IRA, an income account for each
salary and each Social Security benefit, and expense accounts for living
costs, health care and federal tax. Sim account list shows them all, with
the balances they start from and the rate each one grows at. The brokerage
account's two lots grow at two rates, so both series show. A few accounts
here were made for you: capital gains, interest and the like, where an
investment's growth comes from, so it can be taxed the right way.

## Scene `rules`: Rules move the money

```slide
template = "card"
heading = "Rules move the money"
body = [
  "A fixed amount, monthly or yearly, that can grow with inflation",
  "Social Security, from a claiming age",
  "Required minimum distributions",
  "Each year's income tax, and more",
]
speaker = "alice"
```

Accounts hold money; rules move it. A scheduled rule moves money from one
account to another, on a schedule, by an amount it works out. The simplest
kind is a fixed amount, which can grow with inflation. Other kinds work out
a Social Security benefit from a claiming age, a required minimum
distribution from Alice's IRA once she reaches that age, a mortgage payment,
or each year's income tax.

## Scene `scheduled`: Income and spending

```tape
Set FontSize 30
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/scheduled && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scheduled add 2026-01 Income_Salary_Alice Asset_Checking 8000 --name salary_alice --person alice --series cpi --end 2028-06"
Sleep 400ms
Enter
Wait
Sleep 9s
Type "sim scheduled add 2026-01 Income_Salary_Bill Asset_Checking 7000 --name salary_bill --person bill --series cpi --end 2030-12"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "sim scheduled add 2026-01 Asset_Checking Expense_Living 7500 --name living --person joint --series cpi"
Sleep 400ms
Enter
Wait
Sleep 9s
```

Sim scheduled add takes a start date, the account the money comes from, the
one it goes to, and the amount. Alice's salary: eight thousand dollars a
month, from her salary account into checking, starting this January, and
growing with inflation, the CPI series. It ends in June 2028, when she
retires. The person says whose rule it is, so it also ends if she dies
first. Bill's is the same, ending in December 2030. And their living costs:
seventy five hundred a month out of checking. Its person is joint, the
household, so it lasts as long as either of them does.

## Scene `more-scheduled`: The rest of the rules

```tape
Set FontSize 22
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/first-look && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scheduled list"
Sleep 400ms
Enter
Wait
Sleep 20s
```

The rest are other kinds, added with dash dash kind: health care, as
another fixed amount; Social Security for each of them, Alice claiming at
sixty seven and Bill at seventy; Alice's required distributions; and a
federal income tax, settled each April for the year before. Sim scheduled
list shows every rule. The ones at the top, named growth, you didn't write:
they're made from each lot's series, and grow it every month. Video seven
goes into the taxes.

## Scene `first-look`: A first look

```tape
Set FontSize 23
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/first-look && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim run"
Sleep 400ms
Enter
Wait
Sleep 7s
Type "sim explain"
Sleep 400ms
Enter
Wait
Sleep 14s
```

That's enough to run. Sim run steps through every month to 2060, and
already it has a warning: in October 2033, checking went below zero. Sim
explain gives a first look at the run. At the bottom, the same warning;
above it, net worth each year. The run kept going, and the plan looks rich:
net worth keeps climbing, to more than five million dollars by 2060. But
nothing ever takes money out of the savings and investments. Once the
paychecks stop, the bills come out of checking, and checking just keeps
sinking. A shortfall like this doesn't stop the run, so you can see it, and
fix the plan.

> Production note: the run's warning is once per time an account goes
> negative, so one line covers the years it stays below zero.

## Scene `chains-card`: Chains: where money comes from

```slide
template = "card"
heading = "Chains: where money comes from"
body = [
  "A list of accounts and lots, tried in order",
  "Checking keeps a minimum; below it, refill from the chain",
  "Savings first, then bonds, then stocks, then the IRA",
]
speaker = "bill"
```

A chain is the answer to "where does the money come from?". It's a list of
accounts and lots, tried in order: take what's needed from the first, and
if that runs out, the next. Alice and Bill want checking to keep at least
five thousand dollars. When a payment would take it lower, checking
refills from a chain: savings first, then the bonds, then the stocks, and
the IRA last, since every dollar out of it is taxed.

## Scene `chains`: Making a chain

```tape
Set FontSize 26
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/chains && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim chain add spending Asset_Savings --role source"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "sim chain step add spending Asset_Brokerage_Joint --lot bonds"
Sleep 400ms
Enter
Wait
Sleep 1s
Type "sim chain step add spending Asset_Brokerage_Joint --lot stocks"
Sleep 400ms
Enter
Wait
Sleep 1s
Type "sim chain step add spending Asset_TraditionalIRA_Alice --lot default"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "sim account lot edit Asset_Checking --minimum-balance 5000 --refill-chain spending"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "sim chain show"
Sleep 400ms
Enter
Wait
Sleep 9s
```

Sim chain add makes the chain, called spending, starting with savings. It's
a source: money is drawn from it. Sim chain step add puts the bonds, the
stocks and the IRA after it, in that order. Then checking's lot gets a
minimum balance of five thousand dollars, and the chain to refill from. Sim
chain show draws the chain as a tree: each step takes all that's left of what's
needed, and the chain refills checking. A step can also take a share, or
stop at an amount; the chains guide has recipes.

## Scene `chains-graph`: The chain as a picture

```slide
template = "figure"
heading = "sim chain show spending --graph"
demo_image = "home/spending-chain.png"
```

Add dash dash graph, and sim chain show draws the same chain as a
flowchart, in a terminal that can show pictures, such as {iTerm2|eye term two}, kitty, or
VS Code's. Follow the arrows: savings first, then the bonds, the stocks, and
last the IRA, all flowing into checking whenever it would dip below five
thousand dollars. Dash dash graph file saves the picture instead, to share or
to keep with the plan.

## Scene `chains-run`: Run it again

```tape
Set FontSize 23
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/chains-run && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim run"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "sim explain"
Sleep 400ms
Enter
Wait
Sleep 12s
```

Run it again, and there's no warning. Sim explain says there were no
negative balances. Net worth in 2060 is lower than before, and that's
right: before, checking went deeper into the red at no cost, while the
money that should have paid the bills stayed invested and kept growing.
Now the bills are paid from savings and the bonds, and this is the honest
number.

## Scene `calendar-card`: The calendar: one-time changes

```slide
template = "card"
heading = "The calendar: one-time changes"
body = [
  "A transfer on one date: a new car, a gift, an inheritance",
  "A rule's amount changes from a date on",
  "A rule stops, or a rate changes",
]
speaker = "alice"
```

Not everything happens every month. The calendar holds one-time changes, on
a date. A transfer that happens once, like a new car, a gift or an
inheritance. A rule whose amount changes from some date on. A rule that
stops, or a rate that changes. Each is one row, so the steady rules stay
simple.

## Scene `calendar`: Adding calendar events

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/calendar && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim calendar add transfer 2031-06 Asset_Checking Expense_Living 40000 --description 'a new car'"
Sleep 400ms
Enter
Wait
Sleep 7s
Type "sim calendar add set-amount 2036-01 living 6500 --description 'less travel'"
Sleep 400ms
Enter
Wait
Sleep 9s
Type "sim calendar list"
Sleep 400ms
Enter
Wait
Sleep 9s
```

Alice and Bill plan to replace their car in June 2031, for forty thousand
dollars: sim calendar add transfer moves it from checking to living
expenses, once. They expect to travel less from 2036, so their living
costs drop to sixty five hundred a month then: set amount changes the
living rule from that date on, and leaves the years before alone. Sim
calendar list shows both events, by date. Base's ensemble settings are left
out unless you ask; video six is about those.

## Scene `last-run`: The plan, after tax

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/last-run && cd ~/retirement/data/sim && sim run > /dev/null 2>&1 && clear"
Enter
Sleep 8s
Show
Sleep 500ms
Type "sim networth"
Sleep 400ms
Enter
Wait
Sleep 14s
```

With the calendar in, sim networth shows net worth by year, after tax and
in today's dollars. The car is the dip in 2031. Deferred tax is what the
IRA, and the investments' gains, still owe when they're spent, and it comes
off the total. Alice and Bill's base scenario now
says what they have, what comes in, what goes out, and where the money comes
from when it runs short. Every what-if from here builds on it.

> Production note: the run happens while hidden; the scenes before showed
> two runs already.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: looking at the results"
body = "Reports for income, expenses and tax, and the notebooks' charts."
speaker = "alice"
```

Next time: looking at the results, with reports for income, expenses and
tax, and the notebooks' charts.
