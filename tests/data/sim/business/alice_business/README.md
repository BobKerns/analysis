# Alice's business

Alice retires in mid-2028 and starts a software product as a single-member
LLC, taxed as a sole proprietorship: its result goes on her Schedule C in the
joint return. It spends $800 a month from its own `business` account (refilled
from checking) and sells $300 a month from 2030, so it runs at a loss, which
offsets the household's other income and carries forward when it is larger.
A federal `tax_accumulator` settles each year's tax in April. The run ends
in 2035: `retire_alice` pays no income tax, and with it the household's
savings run out in 2037.

Builds on: retire_alice

## Purpose

Shows the `business` account subkind and the `business_income` and
`business_expense` tax characters (#231). `alice_business_hobby` is the same
business treated as a hobby, whose expenses aren't deductible.
