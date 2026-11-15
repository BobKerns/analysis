"""Yearly rules for the payments a year of transactions shows are not
monthly (#617), as a shell script for the household to edit and run.

A script and not rules written straight into ``scheduled.csv``: one year of
transactions can't tell a yearly bill from a one-time expense, nor say that
the household already has rules of its own for a bill, so somebody who
knows the household has to read the list before any of it becomes a rule.
Each payee is therefore a choice, and only one seen to come round again has
its commands live: the rest are commented out until somebody chooses.
"""

from __future__ import annotations

import csv
import io
import itertools
import re
import shlex
from collections.abc import Collection, Mapping
from datetime import date
from pathlib import Path

from analysis.expenses.baseline import Occasional, category_slug
from analysis.sim.imports.holdings import format_dollars
from analysis.sim.imports.mapping import EXCLUDED_PAYEES, NO_RULE, payee_key

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
    ``payee_key`` keeps, whole and in order, with anything between them.
    Whole words, as a payee map's pattern is read (``payee_matches``):
    ``art`` must not leave out ``walmart``. Whitespace bounds them and not
    ``\\b``, which a word ending in punctuation (``co.``) would defeat."""
    words = payee_key(payee).split()
    assert words, "a candidate's payee has a word to know it by"
    return r"(?<!\S)" + r"\s(?:.*\s)?".join(re.escape(word) for word in words) + r"(?!\S)"


def _csv_cell(text: str) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow([text])
    return out.getvalue()


def script(
    occasional: Collection[Occasional], *, data_dir: Path, scenario: str, start: str,
    from_account: str | None, from_lot: str = "", series: str = "cpi",
    expense_accounts: Mapping[str, str], accounts: Collection[str] = (), source: str,
) -> tuple[str, int, int]:
    """A POSIX shell script that adds a yearly rule for each month of the
    year each of ``occasional`` was last paid in, and leaves its payee out
    of the generated rules with a row in the ``excluded_payees.csv`` of
    each source its payments came from (``source``, for one that doesn't
    say); how many payees have those commands live; and how many have
    them commented out, waiting for a choice.

    The commands are live for a payee seen to come round again
    (``Occasional.repeats``). Any other may as well be a one-time expense,
    so its commands are commented out under the choices: all of them for a
    bill, the row alone for a one-time expense or a bill that has rules
    already, none to keep it spread over the months.

    Each amount is as paid, with inflation measured from the month it was
    paid in. A category's account is found as the import finds it: its
    row in ``expense_accounts``, else the scenario account (``accounts``)
    of the category's own name. With neither, and for ``from_account``
    when None, a name is left to be filled in.

    Two kinds of candidate are in neither count. A category whose saved
    account is ``-`` has no rule to replace, so it is only noted. A payee
    also paid in other categories is written commented out, and not as a
    choice: the row that leaves it out would drop those payments too.
    """
    quote = shlex.quote
    lines = [
        "#!/bin/sh",
        f"# Yearly rules for scenario {scenario}, from the payments `sim import transactions`",
        "# found in only a few months of a year.",
        "#",
        "# This is a best-effort reading, by the odds: EDIT IT BEFORE RUNNING IT.",
        "# Each payee below is a choice, which the transactions can't make for you:",
        "#   - a bill that comes round: its `yearly` lines, one for each month it is paid in,",
        "#     and its `leave_out` line, so the monthly rules stop counting it;",
        "#   - a one-time expense (a repair, a trip), or a bill you already have rules for:",
        "#     its `leave_out` line alone;",
        "#   - to keep it spread over the months as it is: none of its lines.",
        "# A payee seen to come round again has its lines ready to run. Any other has them",
        "# commented out: remove the `# ` from the lines you choose.",
        "# Then:",
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
        "",
        "leave_out() {  # SOURCE PATTERN: a row in the source's excluded_payees.csv, so the",
        "               # monthly rules stop counting the payee",
        f'    excluded="$DATA_DIR/imports/$1/{EXCLUDED_PAYEES}"',
        '    [ -s "$excluded" ] || { mkdir -p "$(dirname "$excluded")"; echo payee_pattern > "$excluded"; }',
        '    [ -z "$(tail -c 1 "$excluded")" ] || echo >> "$excluded"',
        '    printf \'%s\\n\' "$2" >> "$excluded"',
        "}",
        "yearly() {  # NAME TO_ACCOUNT AMOUNT MONTH_OF_YEAR PAID_IN DESCRIPTION",
        '    sim scheduled add "$START" "$FROM" "$2" "$3" --data-dir "$DATA_DIR" --scenario "$SCENARIO" \\',
        '        ${FROM_LOT:+--from-lot "$FROM_LOT"} --name "$1" --cadence annual --month-of-year "$4" \\',
        f'        --series {quote(series)} --base-date "$5" --description "$6"',
        "}",
    ]
    named: set[str] = set()
    written = waiting = 0
    for each in occasional:
        when = ", ".join(f"{month:%b %Y} {format_dollars(cents)}" for month, cents in each.paid)
        lines += ["", f"# {_comment(each.payee)} ({_comment(each.category)}): {when}"]
        # As the import resolves it (`_category_accounts`).
        account = expense_accounts.get(each.category) or (each.category if each.category in accounts else None)
        if account == NO_RULE:
            lines.append(f"# Left out: expense_accounts.csv gives {_comment(each.category)} no rule "
                         f"({NO_RULE!r}), so no monthly rule spreads this, and none is added here.")
            continue
        if account is None:
            account = _FILL_IN
            lines.append(f"# No expense account is saved for {_comment(each.category)}: "
                         f"name one in place of {_FILL_IN}.")
        # Not the payee as one transaction has it: its reference numbers
        # and dates would name the rule for one payment.
        slug = category_slug(payee_key(each.payee))[:32].strip("_") or category_slug(each.category)
        paid = latest_year(each)
        block: list[str] = []
        for month, cents in paid:
            base = f"yearly_{slug}"
            month_part = f"_{month:%b}".lower() if len(paid) > 1 else ""
            # One payee in two categories, or two long names alike in their
            # first words: a repeated name would stop the script halfway.
            name = next(candidate for candidate in itertools.chain(
                [base + month_part, f"{base}_{category_slug(each.category)}{month_part}"],
                (f"{base}_{count}{month_part}" for count in itertools.count(2)))
                if candidate not in named)
            named.add(name)
            block.append(
                f"yearly {quote(name)} {quote(account)} {format_dollars(cents)} {month.month} "
                f"{month:%Y-%m} {quote(f'Yearly: {each.payee} ({each.category})')}")
        cell = quote(_csv_cell(payee_pattern(each.payee)))
        block += [f"leave_out {quote(origin)} {cell}" for origin in each.sources or (source,)]
        if each.elsewhere:
            lines += [
                "# Commented out: this payee is paid in other categories too, and leaving it out would",
                "# drop those payments from the monthly rules as well. To use these, first give these",
                "# payments a payee or category pattern of their own, or accept that.",
                *(f"# {line}" for line in block)]
        elif each.repeats:
            lines += [
                "# Seen to come round again. If you already have rules for it, delete its `yearly` lines",
                "# and keep `leave_out`; to keep it spread over the months, delete them all.",
                *block]
            written += 1
        else:
            lines += [
                "# Not seen to come round again: a bill, or a one-time expense? Choose, and uncomment:",
                "#   every line, for a bill that comes round;",
                "#   only `leave_out`, for a one-time expense or a bill you already have rules for;",
                "#   none, to keep it spread over the months.",
                *(f"# {line}" for line in block)]
            waiting += 1
    return "\n".join(lines) + "\n", written, waiting


def _comment(text: str) -> str:
    """``text`` on one line of a comment."""
    return " ".join(text.split())
