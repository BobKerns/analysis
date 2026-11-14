"""The states and territories a household can live in, for choosing the
state a state tax reads (#483): each one's postal code, name and how
far the simulator models its income tax. The README's "State and territory
income tax" tables say the same; ``tests/test_tax_state.py`` keeps the two
in step. docs/guides/state-income-tax.md gives the reasons, state by state
(#543)."""

from __future__ import annotations

from typing import NamedTuple

SHIPPED = "Shipped"  # its tables come with the simulator
NO_TAX = "No tax"  # no income tax: no state rule
TABLES_NEEDED = "Tables needed"  # the model fits; its tables have to be written
PARTIAL = "Partial"  # the tables work, but some of its rules no table can say
FEDERAL_RULE = "Federal rule"  # a territory whose tax mirrors the federal one (#491)
NOT_SUPPORTED = "Not supported"  # a tax code of its own the model can't express

# Puerto Rico and American Samoa: a code of their own, and the federal tax
# reaches only the income from outside them, so neither the federal rule
# nor state tables can stand in (#491).
OWN_TAX_CODE_ISSUE = "https://github.com/BobKerns/analysis/issues/570"

# The series a state tax reads while the household lives in a state with
# no income tax: templates/aux/ ships it as a 0% table.
NO_TAX_SERIES = "none"


class State(NamedTuple):
    code: str  # the postal code: what residence.csv and the aux file names use
    name: str
    status: str

    @property
    def territory(self) -> bool:
        return self.code in TERRITORIES

    @property
    def taxes_income(self) -> bool:
        """Whether it has a tax on wages and retirement income. Washington
        has none until its 2028 income tax over $1 million, only a tax on
        capital gains (#489), so it's listed with the states without one."""
        return self.status != NO_TAX and self.code not in GAINS_TAX_ONLY


TERRITORIES = frozenset({"AS", "GU", "MP", "PR", "VI"})
# Municipal bond interest (#265): a territory's bonds are exempt in every
# state by federal law (48 U.S.C. §745 for Puerto Rico, and the like), and
# the District of Columbia taxes no state's bonds.
MUNI_EXEMPT_EVERYWHERE = TERRITORIES
TAXES_NO_MUNI_INTEREST = frozenset({"DC"})
GAINS_TAX_ONLY = frozenset({"WA"})

STATES: tuple[State, ...] = (
    State("AL", "Alabama", TABLES_NEEDED),
    State("AK", "Alaska", NO_TAX),
    State("AZ", "Arizona", PARTIAL),
    State("AR", "Arkansas", PARTIAL),
    State("CA", "California", SHIPPED),
    State("CO", "Colorado", PARTIAL),
    State("CT", "Connecticut", PARTIAL),
    State("DE", "Delaware", TABLES_NEEDED),
    State("DC", "District of Columbia", SHIPPED),
    State("FL", "Florida", NO_TAX),
    State("GA", "Georgia", TABLES_NEEDED),
    State("HI", "Hawaii", TABLES_NEEDED),
    State("ID", "Idaho", SHIPPED),
    State("IL", "Illinois", TABLES_NEEDED),
    State("IN", "Indiana", TABLES_NEEDED),
    State("IA", "Iowa", PARTIAL),
    State("KS", "Kansas", SHIPPED),
    State("KY", "Kentucky", TABLES_NEEDED),
    State("LA", "Louisiana", TABLES_NEEDED),
    State("ME", "Maine", PARTIAL),
    State("MD", "Maryland", PARTIAL),
    State("MA", "Massachusetts", TABLES_NEEDED),
    State("MI", "Michigan", PARTIAL),
    State("MN", "Minnesota", PARTIAL),
    State("MS", "Mississippi", TABLES_NEEDED),
    State("MO", "Missouri", TABLES_NEEDED),
    State("MT", "Montana", TABLES_NEEDED),
    State("NE", "Nebraska", SHIPPED),
    State("NV", "Nevada", NO_TAX),
    State("NH", "New Hampshire", NO_TAX),
    State("NJ", "New Jersey", TABLES_NEEDED),
    State("NM", "New Mexico", TABLES_NEEDED),
    State("NY", "New York", PARTIAL),
    State("NC", "North Carolina", SHIPPED),
    State("ND", "North Dakota", TABLES_NEEDED),
    State("OH", "Ohio", TABLES_NEEDED),
    State("OK", "Oklahoma", TABLES_NEEDED),
    State("OR", "Oregon", TABLES_NEEDED),
    State("PA", "Pennsylvania", PARTIAL),
    State("RI", "Rhode Island", PARTIAL),
    State("SC", "South Carolina", TABLES_NEEDED),
    State("SD", "South Dakota", NO_TAX),
    State("TN", "Tennessee", NO_TAX),
    State("TX", "Texas", NO_TAX),
    State("UT", "Utah", TABLES_NEEDED),
    State("VT", "Vermont", PARTIAL),
    State("VA", "Virginia", TABLES_NEEDED),
    State("WA", "Washington", SHIPPED),
    State("WV", "West Virginia", TABLES_NEEDED),
    State("WI", "Wisconsin", PARTIAL),
    State("WY", "Wyoming", NO_TAX),
    State("AS", "American Samoa", NOT_SUPPORTED),
    State("GU", "Guam", FEDERAL_RULE),
    State("MP", "Northern Mariana Islands", FEDERAL_RULE),
    State("PR", "Puerto Rico", NOT_SUPPORTED),
    State("VI", "U.S. Virgin Islands", FEDERAL_RULE),
)

BY_CODE: dict[str, State] = {s.code: s for s in STATES}


def find(answer: str) -> State | None:
    """The state ``answer`` names, by postal code or by name, in any case."""
    key = answer.strip().casefold()
    return next((s for s in STATES if key in (s.code.casefold(), s.name.casefold())), None)


def label(code: str) -> str:
    """``California (CA)``, or the code alone for one not in the list."""
    state = BY_CODE.get(code)
    return f"{state.name} ({code})" if state else code


def own_tax_code(code: str) -> str:
    """Why a territory with a tax code of its own (``NOT_SUPPORTED``) is
    refused, for the CLI and the loader alike."""
    return (f"{label(code)} has a tax code of its own the simulator can't model yet "
            f"({OWN_TAX_CODE_ISSUE})")


def tax_series(code: str, *, has_tables: bool = False) -> str:
    """The series of the tables a state tax reads for a household living in
    ``code`` (#509): its own when it has them (``has_tables``), ships them
    (Washington's gains tax, #489) or taxes income, else the 0% ``none``
    tables, as for a state with no income tax. A territory the federal tax
    stands in for reads ``none`` even with tables of its own, which would
    tax the same income twice; the loader refuses them (#491)."""
    state = BY_CODE.get(code)
    if state is not None and state.status == FEDERAL_RULE:
        return NO_TAX_SERIES
    if has_tables or state is None or state.status == SHIPPED or state.taxes_income:
        return code
    return NO_TAX_SERIES
