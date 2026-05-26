# Itemizations

Optional money rollups that can be referenced from any monetary CSV field using `@name`.

Example:
- Put `@home_basis` in `opening_cost_basis`.
- Loader resolves it from `itemizations/home_basis.csv` by summing the `Value` column.

Required columns:
- `Value` (decimal dollars, e.g. `1234.56`)

Suggested columns:
- `Item`
- `Note`
