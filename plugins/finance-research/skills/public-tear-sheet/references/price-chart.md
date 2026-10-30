# Price chart tab

One chart of the company's share price over time, with a few lines on how to read it. It answers "what has the ride looked like?" at a glance, which the returns table can't.

## Data
- **Month-end closes** for the last 10 years, or since the IPO if shorter. Save them to `tear-sheet-work/prices.csv` (`date,close`, ISO dates, oldest first), and copy that file to `<name>-prices.csv` beside the tabs.
- **Sources,** in order: Stooq's monthly CSV (`https://stooq.com/q/d/l/?s=<ticker>.us&i=m`); Yahoo Finance's historical-data page (monthly). Name the one you used, with its as-of date. Take the closes as published; don't fill gaps by interpolation. A gap stays a gap, and the text says so.
- Check two or three points against another source (the latest close against the quote you used on the tear sheet; one year-end against a second source). If they disagree by more than rounding, say which you charted and why.
- **Splits:** use split-adjusted prices, and say so. A stock that split shows a cliff otherwise.

## The tab
- **Chart:** a line chart of the closes, x = month, y = price in dollars, titled `<ticker> month-end share price, <first month>–<last month>`. In a Claude doc, use the docs connector's chart (hand it the rows, not a picture). In the Markdown fallback, write `<name>-price.md` with the text below and point to `<name>-prices.csv` (a page that renders the bundle draws the chart from it).
- **Text, three to five sentences:** what's charted and from where; that it's price only, so for a dividend payer an investor's total return was higher than the price change; the largest peak-to-trough fall in the period with its months (from the closes; add **drawdown** to the Glossary), and how long the price took to regain that peak, if it has.
- Keep to what the chart shows. No forecasts, no "support levels", no trend lines.
- **Not for private companies:** with no traded price there is no Price chart tab. A recent IPO charts from its first trading month, and says so.
