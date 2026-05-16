# Spending Data Guide (Non-SQL)

Data is stored in one file:

- `state/state.sqlite3`

This guide is for asking questions in plain language. You do not need to know SQL.

## What Is In The Data

Think of this as a set of spreadsheets (tables). Each table has rows and named columns.

- `item_explore`: best starting point for human-friendly browsing.
- `items`: full item-level detail (all imported columns).
- `monthly_summaries`: monthly totals by category.
- `events`: activity history (what ran, when, and by which user).
- `categories`: list of known categories.
- `item_category_map`: memory of item-to-category assignments.

## Best Table To Start With

Use `item_explore` first. It has the most useful columns for day-to-day questions:

- `id`: row id (helpful as a stable reference).
- `order_date`: date of order.
- `title`: item name/description.
- `order_status`: order status.
- `item_quantity`: quantity.
- `item_net_total`: item-level net amount.
- `total_amount`: order-level total amount.
- `assigned_category`: category assigned by this project.

If you need extra columns not listed above, ask to use `items`.

## Key Vocabulary You Can Use In Requests

- Category: value in `assigned_category` (example: Medical, Food, Household).
- Item amount: `item_net_total`.
- Order amount: `total_amount`.
- Summary month: `summary_month_start` in `monthly_summaries`.
- Activity log: rows in `events`.
- User: `user` in `events`.

## Example Requests You Can Make

You can ask in English or Japanese. These are English examples.

### Browse and filter

- "Show my most recent 30 items with date, title, category, and amount."
- "Show only Medical purchases from April 2026."
- "Show uncategorized items sorted by largest amount first."
- "Show orders where `order_status` is not complete."

### Category and spending questions

- "What are total purchases by category for April 2026?"
- "What are my top 10 most expensive items this month?"
- "Compare Food spending between March and April 2026."
- "Show category totals using item amounts (not order totals)."

### Monthly summaries

- "Show monthly summary rows for 2026-04-03."
- "Show trend of Medical monthly totals over time."
- "Which categories increased the most month-over-month?"

### Activity/history questions

- "Show the last 50 run-complete events."
- "Show events for user rwk in the last 7 days."
- "Show any errors or interrupted runs."

## How To Ask Better Questions

If you include these details, answers are usually better:

- Date range: "from 2026-04-01 to 2026-04-30"
- Amount field: "use item_net_total" or "use total_amount"
- Grouping: "group by category" or "group by month"
- Sorting: "sort by amount descending"
- Row count: "limit to top 20"

## About The Main Tables

### items

Full detail table. Contains all imported Amazon columns plus project columns like:

- `source_file`
- `normalized_item`
- `assigned_category`

Use this when you need fields that are not in `item_explore`.

### monthly_summaries

One row per month and category, with these key columns:

- `summary_month_start`
- `category`
- `amount_total`

This is for reporting and trend views.

Important: these months are billing-cycle months (Amex-aligned), not strict calendar months.
The default cutoff is day 3:

- Dates on day 1-3 stay in that month.
- Dates on day 4+ roll into the next month.

This helps monthly outputs line up with card statements.

About pending output files:

- A month is marked `_pending` only when today's date falls in that month's open billing interval.
- If the current open interval has no rows, no `_pending` files are written.

### events

Activity history table. Most useful columns:

- `ts`: when the event happened
- `event`: what happened (for example, a run started or finished)
- `message`: a short description
- `user`: who did it
- `payload_json`: extra details about the event (sometimes includes counts, filenames, or other context)

Tip: The `payload_json` column contains more detailed information about each event, stored as a bundle (like a dictionary or JSON object). You can easily access this in Python with pandas, and AI assistants can help you extract or summarize it.

Use this table for troubleshooting, history, or to answer "what happened when" questions.

### categories and item_category_map

These support consistent categorization behavior.

- `categories`: list of available categories.
- `item_category_map`: remembers category chosen for each normalized item.

You usually do not need these for basic spend reports, but they are useful for category maintenance.

## Short Prompt Templates

You can copy and fill these:

- "From `item_explore`, show [columns] where [conditions], sorted by [column], top [N]."
- "Using `items`, calculate [metric] by [category/month] for [date range]."
- "From `monthly_summaries`, show [category] trend for [date range]."
- "From `events`, show [event types] for user [name] in the last [N] days."

## Bottom Line

For most requests, start with `item_explore` and ask in plain language.
If you need more detail, ask to switch to `items`.
If you need trends, ask for `monthly_summaries`.
If you need history/debugging, ask for `events`.
