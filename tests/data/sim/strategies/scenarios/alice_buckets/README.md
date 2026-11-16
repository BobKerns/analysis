# alice_buckets

Alice and Bill keep their portfolio in three buckets: money-market lots in
the brokerage and Alice's Roth IRA (cash), bond funds in her traditional and
Roth IRAs (bonds), and stock funds in the brokerage and the Roth (stocks).
The built-in `buckets` strategy owns checking's refill chain, spends from
cash first, and keeps cash at two years and bonds at five years of $40,000,
refilled from stocks when stocks gained (#463).

Builds on: retire_alice
