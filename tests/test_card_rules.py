"""A credit card's rules (#641): a payment with a blank amount pays what was
owed when the month opened, and ``card_interest`` charges what the month's
payments left unpaid."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from analysis.sim.loader import LoadError, LoadResult

CARD, INTEREST = "Liability_Card", "Expense_Interest_Card"


@pytest.fixture
def card(
    credit_card: str, fund_asset_savings: Callable[..., None],
    append_scheduled_row: Callable[..., None],
) -> Callable[..., None]:
    """The ``credit_card`` (owing $1,000) with $500 a month of living costs
    charged to it. Returns ``rule(name, **columns)``, which adds a rule from
    the scenario's start: by default a ``fixed_amount`` from checking to the card."""
    assert credit_card == CARD
    fund_asset_savings()

    def rule(name: str, **columns: str) -> None:
        append_scheduled_row(**{
            "name": name, "from_account": "Asset_Checking", "to_account": CARD,
            "amount_rule_kind": "fixed_amount", "tax_character": "none",
            "start": "2026-05-31", **columns,
        })

    rule("charges", from_account=CARD, to_account="Expense_Living", amount="500.00")
    return rule


def _posted(run: Path, rule: str) -> list[int]:
    """The cents ``rule`` posted, month by month."""
    with closing(sqlite3.connect(run)) as con:
        return [row[0] for row in con.execute(
            "SELECT SUM(e.amount) FROM transfer_event e "
            "JOIN scheduled_transaction s ON s.id = e.reason_id AND e.reason_kind = 'rule' "
            "WHERE s.name = ? GROUP BY e.date ORDER BY e.date", (rule,))]


def _owed(run: Path) -> list[int]:
    """The card's balance at each month's end."""
    with closing(sqlite3.connect(run)) as con:
        return [row[0] for row in con.execute(
            "SELECT SUM(s.balance) FROM lot_state s JOIN account_lot l ON l.id = s.lot_id "
            "JOIN account a ON a.id = l.account_id WHERE a.name = ? "
            "GROUP BY s.step_id ORDER BY s.step_id", (CARD,))]


@pytest.mark.parametrize("priority", ["", "100", "900"])
def test_blank_amount_pays_what_the_month_opened_owing(
    card: Callable[..., None], run_test_copy: Callable[[], Path], priority: str,
) -> None:
    """Wherever the payment sits among the month's rules, it pays last
    month's balance and leaves this month's charges for the next."""
    card("card_payment", priority=priority)
    card("card_interest", from_account=CARD, to_account=INTEREST,
         amount_rule_kind="card_interest", rate="24%")
    run = run_test_copy()
    assert _posted(run, "card_payment")[:3] == [100_000, 50_000, 50_000]
    assert _owed(run)[:3] == [50_000, 50_000, 50_000]
    assert _posted(run, "card_interest") == []  # paid in full: the grace period


def test_interest_is_charged_on_what_the_payments_left(
    card: Callable[..., None], run_test_copy: Callable[[], Path],
) -> None:
    card("card_payment", amount="300.00")
    card("card_interest", from_account=CARD, to_account=INTEREST,
         amount_rule_kind="card_interest", rate="24%")
    run = run_test_copy()
    # 2% a month of (1000 - 300), then of (1214 - 300): the month's own
    # charges carry no interest until they have been left unpaid.
    assert _posted(run, "card_interest")[:2] == [1_400, 1_828]
    assert _owed(run)[:2] == [121_400, 143_228]


def test_a_payment_and_a_blank_one_share_the_month(
    card: Callable[..., None], run_test_copy: Callable[[], Path],
) -> None:
    """A blank amount pays what earlier payments in the month left."""
    card("part", amount="300.00", priority="100")
    card("rest", priority="200")
    run = run_test_copy()
    assert _posted(run, "rest")[:2] == [70_000, 20_000]


def test_blank_amount_draws_through_a_chain(
    card: Callable[..., None], run_test_copy: Callable[[], Path],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    chains = retire_alice_csv("chains") / "source"
    chain = sorted(p.stem for p in chains.glob("*.csv"))[0]
    card("card_payment", from_account=chain)
    assert _posted(run_test_copy(), "card_payment")[:2] == [100_000, 50_000]


@pytest.mark.parametrize(("columns", "refusal"), [
    ({"to_account": "Asset_Checking", "from_account": "Asset_Savings"},
     "card_interest charges a credit card"),
    ({"from_account": "Liability_Mortgage"}, "a mortgage's interest is its mortgage_payment's"),
    ({"to_account": "Asset_Checking"}, "charged to an expense account"),
    ({"rate": ""}, "needs a rate"),
    ({"amount": "5.00"}, "takes no amount"),
    ({"priority": "150"}, "takes no priority"),
    ({"cadence": "annual", "month_of_year": "1"}, "needs cadence monthly"),
    ({"ytd_preroll": "1"}, "ytd_preroll"),
])
def test_card_interest_refusals(
    card: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    columns: dict[str, str], refusal: str,
) -> None:
    card("card_interest", **{
        "from_account": CARD, "to_account": INTEREST, "amount_rule_kind": "card_interest",
        "rate": "24%", **columns,
    })
    with pytest.raises(LoadError, match=refusal):
        load_test_copy()


@pytest.mark.parametrize(("columns", "refusal"), [
    ({"series": "cpi", "base_date": "2026-05-31"}, "takes no series"),
    ({"ytd_preroll": "1"}, "takes no ytd_preroll"),
    ({"cadence": "biweekly"}, "monthly or annual cadence"),
])
def test_blank_amount_refusals(
    card: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    columns: dict[str, str], refusal: str,
) -> None:
    card("card_payment", **columns)
    with pytest.raises(LoadError, match=refusal):
        load_test_copy()
