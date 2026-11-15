"""Yearly rules for the payments a year of transactions shows are not
monthly (#617), as a shell script for the household to edit and run.

A script and not rules written straight into ``scheduled.csv``: one year of
transactions can't tell a yearly bill from a one-time expense, so somebody who
knows the household has to read the list before any of it becomes a rule.
"""

from __future__ import annotations

import csv
import io
import re
import shlex
from collections.abc import Collection, Mapping
from datetime import date
from pathlib import Path

from analysis.expenses.baseline import Occasional, category_slug
from analysis.sim.imports.holdings import format_dollars
from analysis.sim.imports.mapping import NO_RULE, payee_key

__all__ = ["latest_year", "payee_pattern", "script"]

_FILL_IN = "EDIT_ME"
"""Stands where the script can't know a name: `sim scheduled add` refuses
it, or at a terminal asks for the account in its place."""


def latest_year(each: Occasional) -> tuple[tuple[date, int], ...]:
    """The payments of the twelve months up to ``each``'s last: what a bill
    seen over several years comes to now."""
    last = each.paid[-1][0]
    return tuple((month, cents) for month, cents in each.paid
                 if (last.year - month.year) * 12 + last.month - month.month < 12)


def payee_pattern(payee: str) -> str:
    """A regular expression for ``excluded_payees.csv`` that finds
    ``payee`` however its dates and reference numbers change: the words
    ``payee_key`` keeps, in order, with anything between them."""
    words = payee_key(payee).split()
    return r"\s.*".join(re.escape(word) for word in words)


def _csv_cell(text: str) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow([text])
    return out.getvalue()


def script(
    occasional: Collection[Occasional], *, data_dir: Path, scenario: str, start: str,
    from_account: str | None,
    from_lot: str = "", series: str = "cpi", expense_accounts: Mapping[str, str],
    excluded_payees: Path,
) -> str:
    """A POSIX shell script that adds a yearly rule for each month of the
    year each of ``occasional`` was last paid in, and leaves its payee out
    of the generated rules with a row in ``excluded_payees``.

    Each amount is as paid, with inflation measured from the month it was
    paid in. A category with no account in ``expense_accounts``, and
    ``from_account`` when None, is left to be filled in.
    """
    quote = shlex.quote
    lines = [
        "#!/bin/sh",
        f"# Yearly rules for scenario {scenario}, from the payments `sim import transactions`",
        "# found in only a month or two of a year.",
        "#",
        "# This is a best-effort reading, by the odds: EDIT IT BEFORE RUNNING IT.",
        "#   - Delete the block of anything that was a one-time expense (a repair, a trip):",
        "#     the transactions can't say which a payment seen once was.",
        "#   - Correct an amount or a month that will be different next time.",
        f"#   - Fill in each {_FILL_IN}.",
        "# Then run it once:  sh <this file>",
        "# and run the transactions import again, so the monthly rules stop counting these payees.",
        "set -eu",
        "",
        f"DATA_DIR={quote(str(data_dir))}",
        f"SCENARIO={quote(scenario)}",
        f"FROM={quote(from_account or _FILL_IN)}    # the account that pays",
        f"FROM_LOT={quote(from_lot)}    # its lot, where it needs one",
        f"START={quote(start)}    # the rules' first month",
        f"EXCLUDED={quote(str(excluded_payees))}",
        "",
        "leave_out() {  # a row in excluded_payees.csv: the monthly rules stop counting this payee",
        '    [ -s "$EXCLUDED" ] || { mkdir -p "$(dirname "$EXCLUDED")"; echo payee_pattern > "$EXCLUDED"; }',
        '    [ -z "$(tail -c 1 "$EXCLUDED")" ] || echo >> "$EXCLUDED"',
        '    printf \'%s\\n\' "$1" >> "$EXCLUDED"',
        "}",
        "yearly() {  # NAME TO_ACCOUNT AMOUNT MONTH_OF_YEAR PAID_IN DESCRIPTION",
        '    sim scheduled add "$START" "$FROM" "$2" "$3" --data-dir "$DATA_DIR" --scenario "$SCENARIO" \\',
        '        ${FROM_LOT:+--from-lot "$FROM_LOT"} --name "$1" --cadence annual --month-of-year "$4" \\',
        f'        --series {quote(series)} --base-date "$5" --description "$6"',
        "}",
    ]
    for each in occasional:
        account = expense_accounts.get(each.category)
        if account is None or account == NO_RULE:
            account = _FILL_IN
        paid = latest_year(each)
        when = ", ".join(f"{month:%b %Y} {format_dollars(cents)}" for month, cents in each.paid)
        lines += ["", f"# {_comment(each.payee)} ({_comment(each.category)}): {when}"]
        if not each.repeats:
            lines.append("# Not seen to come round again: a yearly bill, or a one-time expense?")
        if account == _FILL_IN:
            lines.append(f"# No expense account is saved for {_comment(each.category)}: "
                         f"name one in place of {_FILL_IN}.")
        slug = category_slug(each.payee)[:32].strip("_") or category_slug(each.category)
        for month, cents in paid:
            name = f"yearly_{slug}" + (f"_{month:%b}".lower() if len(paid) > 1 else "")
            lines.append(
                f"yearly {quote(name)} {quote(account)} {format_dollars(cents)} {month.month} "
                f"{month:%Y-%m} {quote(f'Yearly: {each.payee} ({each.category})')}")
        pattern = payee_pattern(each.payee)
        if pattern:
            lines.append(f"leave_out {quote(_csv_cell(pattern))}")
    return "\n".join(lines) + "\n"


def _comment(text: str) -> str:
    """``text`` on one line of a comment."""
    return " ".join(text.split())
