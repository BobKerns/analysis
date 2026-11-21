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

from zabob.retirement.expenses.baseline import CARD_PAYMENT_TAG, Cards, Occasional, Payer, category_slug, payer_slug
from zabob.retirement.sim.imports.holdings import format_dollars
from zabob.retirement.sim.imports.mapping import EXCLUDED_PAYEES, NO_RULE, payee_key

__all__ = ["latest_year", "payee_pattern", "script"]

_SCHEDULED = "scheduled.csv"
_FILL_IN = "EDIT_ME"
"""Stands where the script can't know a name: `sim scheduled add` refuses
it, or at a terminal asks for the account in its place."""


def latest_year(each: Occasional) -> tuple[tuple[date, int], ...]:
    """The payments of the twelve months up to ``each``'s last: what a bill
    seen over several years comes to now. A payment that another follows
    eleven months later is that one's last year, paid a month later in the
    year (a bill's date moves), and not a second bill."""
    def months(month: date) -> int:
        return month.year * 12 + month.month

    last = months(each.paid[-1][0])
    paid_in = {months(month) for month, _ in each.paid}
    return tuple((month, cents) for month, cents in each.paid
                 if last - months(month) < 12 and months(month) + 11 not in paid_in)


def payee_pattern(payee: str) -> str:
    """A regular expression for ``excluded_payees.csv`` that finds the
    payees of ``payee``'s key (``payee_key``) and no others, however their
    dates and reference numbers change: the whole text, being the key's
    words in order with only words that have a digit before, between and
    after them. Anything looser would leave out other payees unseen:
    ``corner store`` must not find ``corner store deli``, which is another
    key and may be paid every week.

    The words are ``payee``'s own in lower case, and not the key's: the
    key is case-folded, which a case-insensitive search is not (``ß`` is
    ``ss`` to the one and itself to the other), and a row that doesn't
    find its own payee would leave the bill counted twice."""
    words = [word.lower() for word in payee.split() if not any(c.isdigit() for c in word)]
    assert words, "a candidate's payee has a word to know it by"
    numbered = r"\S*\d\S*"
    pattern = (rf"^\s*(?:{numbered}\s+)*" + rf"\s+(?:{numbered}\s+)*".join(re.escape(word) for word in words)
               + rf"(?:\s+{numbered})*\s*$")
    assert re.search(pattern, payee, re.IGNORECASE), f"{pattern!r} finds the payee it was made from"
    return pattern


def _csv_cell(text: str) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow([text])
    return out.getvalue()


def script(
    occasional: Collection[Occasional], *, data_dir: Path, scenario: str, start: str,
    from_account: str | None, from_lot: str = "", series: str = "cpi",
    expense_accounts: Mapping[str, str], accounts: Collection[str] = (), source: str,
    taken: Collection[str] = (), sim: str = "sim", from_budget: Collection[str] = (),
    cards: Cards | None = None,
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
    row in ``expense_accounts``, else the category's own name, and in
    either case one of the scenario's accounts (``accounts``): the saved
    rows are shared by every scenario. Without one, and for
    ``from_account`` when None, a name is left to be filled in.

    A payee every payment of which was charged to one card of ``cards``
    (#641) has its rules paid from that card, named on their lines; any
    other is paid from ``from_account``, as a payee paid from several is.
    A card so charged gets a ``pay_card`` line, the rule that pays it in
    full each month, by the name the import gives that rule: the import
    writes one only for a card its monthly rules charge, and a card whose
    only charges are these would be paid by nothing. The line is live when
    a live rule charges the card, and left out when ``taken`` has the name.

    No rule is given a name in ``taken``, the scenario's scheduled names,
    nor one used earlier in the script: `sim scheduled add` refuses a
    name it has, which would stop the script halfway. Run again after
    stopping for any other reason, the script skips the rules and rows it
    has already added.

    ``sim`` is the command the script runs, unless the environment's
    ``SIM`` names another: the one that wrote the script, so that it
    doesn't depend on a PATH that may have none, or another checkout's.

    Three kinds of candidate are in neither count. A category whose saved
    account is ``-`` has no rule to replace, so it is only noted. So is
    one in ``from_budget``, the categories whose rules a budget makes: its
    rules have the category's yearly payments already. A payee
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
        "# Then run it once:  sh <this file>  (quoted, if its name has a space)",
        "# and run the transactions import again, so the monthly rules stop counting these payees.",
        "set -eu",
        "",
        f"DATA_DIR={quote(str(data_dir))}",
        f"SCENARIO={quote(scenario)}",
        f"FROM={quote(from_account or _FILL_IN)}    # the account that pays what no card was charged",
        f"FROM_LOT={quote(from_lot)}    # its lot, where it needs one",
        f"START={quote(start)}    # the rules' first month",
        f"SIM=${{SIM:-{quote(sim)}}}    # the command that wrote this script",
        "",
        "leave_out() {  # SOURCE PATTERN: a row in the source's excluded_payees.csv, so the",
        "               # monthly rules stop counting the payee",
        f'    excluded="$DATA_DIR/imports/$1/{EXCLUDED_PAYEES}"',
        '    [ -s "$excluded" ] || { mkdir -p "$(dirname "$excluded")"; echo payee_pattern > "$excluded"; }',
        '    [ -z "$(tail -c 1 "$excluded")" ] || echo >> "$excluded"',
        "    # Not twice, when the script is run again.",
        '    grep -qxF -e "$2" "$excluded" || printf \'%s\\n\' "$2" >> "$excluded"',
        "}",
        "yearly() {  # NAME TO_ACCOUNT AMOUNT MONTH_OF_YEAR PAID_IN DESCRIPTION [CARD [CARD_LOT]]",
        "    # Paid from the card it was charged to, where it names one; else from FROM.",
        '    if [ $# -ge 7 ]; then from=$7; lot=${8:-}; else from=$FROM; lot=$FROM_LOT; fi',
        "    # A rule this script added before it stopped, when it is run again.",
        f'    if grep -q "^$1," "$DATA_DIR/scenarios/$SCENARIO/{_SCHEDULED}" 2>/dev/null; then',
        '        echo "already added: $1"; return 0',
        "    fi",
        '    "$SIM" scheduled add "$START" "$from" "$2" "$3" --data-dir "$DATA_DIR" --scenario "$SCENARIO" \\',
        '        ${lot:+--from-lot "$lot"} --name "$1" --cadence annual --month-of-year "$4" \\',
        f'        --series {quote(series)} --base-date "$5" --description "$6"',
        "}",
        "pay_card() {  # NAME CARD [CARD_LOT]: the card paid in full each month, from FROM",
        f'    if grep -q "^$1," "$DATA_DIR/scenarios/$SCENARIO/{_SCHEDULED}" 2>/dev/null; then',
        '        echo "already added: $1"; return 0',
        "    fi",
        '    "$SIM" scheduled add "$START" "$FROM" "$2" "" --data-dir "$DATA_DIR" --scenario "$SCENARIO" \\',
        '        ${FROM_LOT:+--from-lot "$FROM_LOT"} ${3:+--to-lot "$3"} --name "$1" --cadence monthly \\',
        f'        --tags {quote(CARD_PAYMENT_TAG)} --description "Card payment: $2 in full"',
        "}",
    ]
    named = set(taken)
    # Each card the rules charge, and whether a live one does.
    charged: dict[Payer, bool] = {}
    written = waiting = 0
    for each in occasional:
        when = ", ".join(f"{month:%b %Y} {format_dollars(cents)}" for month, cents in each.paid)
        lines += ["", f"# {_comment(each.payee)} ({_comment(each.category)}): {when}"]
        # As the import resolves it (`_category_accounts`).
        account = expense_accounts.get(each.category) or each.category
        if each.category in from_budget:
            lines.append(f"# Left out: {_comment(each.category)} is made from the budget, whose rules "
                         "have its yearly payments already.")
            continue
        if account == NO_RULE:
            lines.append(f"# Left out: expense_accounts.csv gives {_comment(each.category)} no rule "
                         f"({NO_RULE!r}), so no monthly rule spreads this, and none is added here.")
            continue
        if account not in accounts:
            lines.append(
                (f"# No expense account is saved for {_comment(each.category)}" if account == each.category else
                 f"# {_comment(each.category)} is saved as {_comment(account)}, which {scenario} doesn't have")
                + f": name one in place of {_FILL_IN}.")
            account = _FILL_IN
        # Not the payee as one transaction has it: its reference numbers
        # and dates would name the rule for one payment.
        slug = category_slug(payee_key(each.payee))[:32].strip("_") or category_slug(each.category)
        paid = latest_year(each)
        payers = {(cards or {}).get(pair) for pair in each.accounts}
        card = next(iter(payers)) if len(payers) == 1 else None
        if card is None and payers - {None}:
            lines.append("# Charged to a card only in part: the rules below pay from FROM.")
        paid_from = "".join(f" {quote(part)}" for part in card or () if part)
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
                f"{month:%Y-%m} {quote(f'Yearly: {_comment(each.payee)} ({_comment(each.category)})')}"
                + paid_from)
        cell = quote(_csv_cell(payee_pattern(each.payee)))
        block += [f"leave_out {quote(origin)} {cell}" for origin in each.sources or (source,)]
        if card is not None:
            charged[card] = charged.get(card, False) or (each.repeats and not each.elsewhere)
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
    payments = [(f"card_payment_{payer_slug(card)}", card, live) for card, live in sorted(charged.items())]
    if payments := [each for each in payments if each[0] not in taken]:
        lines += [
            "",
            "# The cards charged above want paying: each line adds the rule that pays one in full each",
            "# month, as the transactions import does for a card its monthly rules charge. Delete a line",
            "# if you already pay that card with a rule of your own; uncomment it with the card's rules.",
            *(("" if live else "# ") + f"pay_card {quote(name)}" + "".join(f" {quote(part)}" for part in card if part)
              for name, card, live in payments)]
    return "\n".join(lines) + "\n", written, waiting


def _comment(text: str) -> str:
    """``text`` on one line of a comment."""
    return " ".join(text.split())
