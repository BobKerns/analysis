"""The states and territories a household can live in, for choosing the
state a state tax rule reads (#483): each one's postal code, name and how
far the simulator models its income tax. The README's "State and territory
income tax" tables say the same; ``tests/test_tax_state.py`` keeps the two
in step."""

from __future__ import annotations

from typing import NamedTuple

SHIPPED = "Shipped"  # its tables come with the simulator
NO_TAX = "No tax"  # no income tax: no state rule
TABLES_NEEDED = "Tables needed"  # the model fits; its tables have to be written
PARTIAL = "Partial"  # the tables work, but some of its rules aren't modeled
FEDERAL_RULE = "Federal rule"  # a territory whose tax mirrors the federal one
NOT_SUPPORTED = "Not supported"  # a tax code of its own the model can't express

# The bracket_series a state tax rule reads after a move to a state with no
# income tax: templates/aux/ ships it as a 0% table.
NO_TAX_SERIES = "none"


class State(NamedTuple):
    code: str  # the postal code: what bracket_series and the aux file names use
    name: str
    status: str

    @property
    def territory(self) -> bool:
        return self.code in TERRITORIES

    @property
    def taxes_income(self) -> bool:
        """Whether it has a tax on wages and retirement income. Washington
        has none, only a tax on large capital gains (not modeled, #489)."""
        return self.status != NO_TAX and self.code not in GAINS_TAX_ONLY


TERRITORIES = frozenset({"AS", "GU", "MP", "PR", "VI"})
GAINS_TAX_ONLY = frozenset({"WA"})

STATES: tuple[State, ...] = (
    State("AL", "Alabama", PARTIAL),
    State("AK", "Alaska", NO_TAX),
    State("AZ", "Arizona", PARTIAL),
    State("AR", "Arkansas", PARTIAL),
    State("CA", "California", SHIPPED),
    State("CO", "Colorado", PARTIAL),
    State("CT", "Connecticut", PARTIAL),
    State("DE", "Delaware", PARTIAL),
    State("DC", "District of Columbia", SHIPPED),
    State("FL", "Florida", NO_TAX),
    State("GA", "Georgia", PARTIAL),
    State("HI", "Hawaii", PARTIAL),
    State("ID", "Idaho", SHIPPED),
    State("IL", "Illinois", PARTIAL),
    State("IN", "Indiana", PARTIAL),
    State("IA", "Iowa", PARTIAL),
    State("KS", "Kansas", SHIPPED),
    State("KY", "Kentucky", PARTIAL),
    State("LA", "Louisiana", PARTIAL),
    State("ME", "Maine", PARTIAL),
    State("MD", "Maryland", PARTIAL),
    State("MA", "Massachusetts", PARTIAL),
    State("MI", "Michigan", PARTIAL),
    State("MN", "Minnesota", PARTIAL),
    State("MS", "Mississippi", PARTIAL),
    State("MO", "Missouri", PARTIAL),
    State("MT", "Montana", TABLES_NEEDED),
    State("NE", "Nebraska", SHIPPED),
    State("NV", "Nevada", NO_TAX),
    State("NH", "New Hampshire", NO_TAX),
    State("NJ", "New Jersey", PARTIAL),
    State("NM", "New Mexico", TABLES_NEEDED),
    State("NY", "New York", PARTIAL),
    State("NC", "North Carolina", SHIPPED),
    State("ND", "North Dakota", TABLES_NEEDED),
    State("OH", "Ohio", PARTIAL),
    State("OK", "Oklahoma", PARTIAL),
    State("OR", "Oregon", PARTIAL),
    State("PA", "Pennsylvania", PARTIAL),
    State("RI", "Rhode Island", PARTIAL),
    State("SC", "South Carolina", PARTIAL),
    State("SD", "South Dakota", NO_TAX),
    State("TN", "Tennessee", NO_TAX),
    State("TX", "Texas", NO_TAX),
    State("UT", "Utah", PARTIAL),
    State("VT", "Vermont", TABLES_NEEDED),
    State("VA", "Virginia", PARTIAL),
    State("WA", "Washington", PARTIAL),
    State("WV", "West Virginia", PARTIAL),
    State("WI", "Wisconsin", PARTIAL),
    State("WY", "Wyoming", NO_TAX),
    State("AS", "American Samoa", NOT_SUPPORTED),
    State("GU", "Guam", FEDERAL_RULE),
    State("MP", "Northern Mariana Islands", PARTIAL),
    State("PR", "Puerto Rico", NOT_SUPPORTED),
    State("VI", "U.S. Virgin Islands", PARTIAL),
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
