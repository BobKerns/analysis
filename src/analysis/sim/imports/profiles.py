"""Sources described by a profile: a YAML file, not code (issue #527).

A brokerage's positions download is a flat table, and what differs from one
to the next is only which column means what, where the account and the
as-of date are, and where the holdings stop. A profile says those:

```yaml
description: Fidelity positions download (CSV)
holdings:
  layout: 'Positions: "Download"'
  columns:
    account: Account number
    account_name: Account name
    asset: Symbol
    name: Description
    value: Current value
    quantity: Quantity
    cost_basis: Cost basis total
    type: Type
  as_of: {last_lines: 'Date downloaded (\\w{3}-\\d{2}-\\d{4})', format: '%b-%d-%Y'}
  strip_asset: '*'
```

The built-in profiles are the ``source_profiles/*.yaml`` files beside this module.
A household's own is ``imports/<source>/profile.yaml`` in the data dir,
beside that source's mapping files, and replaces a built-in of its name.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from analysis.sim.imports.forms import Holding, Snapshot

__all__ = ["PROFILE", "HoldingsProfile", "Profile", "ProfileError", "built_in", "load", "user"]

PROFILE = "profile.yaml"
_BUILT_IN = Path(__file__).parent / "source_profiles"

# What a holdings profile's ``columns`` may name. ``asset`` and ``value``
# are the two a holding can't do without.
_COLUMNS = (
    "account", "account_name", "asset", "name", "value", "quantity", "cost_basis",
    "unrealized_gain", "acquired", "type",
)
_HOLDINGS_KEYS = {
    "layout", "columns", "account", "as_of", "blank", "strip_asset", "acquired_format",
}
_AS_OF_PLACES = ("heading", "last_lines", "file_name")
# How far down a file its header row may be: a download may open with a
# title and a date.
_HEADER_SEARCH = 30
# The money at the start of a cell: '$1,234.56', '-$16,423.62 (-6.68%)',
# '(300.00)'.
_MONEY = re.compile(r"^(?P<open>\()?\s*(?P<sign>[-+])?\s*\$?\s*(?P<number>[\d,]*\.?\d+)\s*(?P<close>\))?")


class ProfileError(ValueError):
    """A profile that can't be used, or a file it can't read."""


@dataclass(frozen=True)
class HoldingsProfile:
    source: str
    where: str
    """The profile's file, for messages."""
    layout: str
    columns: Mapping[str, str]
    account_pattern: re.Pattern[str] | None = None
    """The outside account, from the file's name, when no column has it."""
    as_of_place: str = ""
    as_of_pattern: re.Pattern[str] | None = None
    as_of_format: str = "%m/%d/%Y"
    acquired_format: str = "%m/%d/%Y"
    blank: frozenset[str] = frozenset()
    strip_asset: str = ""

    def _header(self, rows: list[list[str]]) -> int | None:
        """The index of the header row: the first near the top with every
        column the profile names."""
        wanted = set(self.columns.values())
        for index, row in enumerate(rows[:_HEADER_SEARCH]):
            if wanted <= {cell.strip() for cell in row}:
                return index
        return None

    def matches(self, path: Path) -> bool:
        try:
            return self._header(_rows(path, _HEADER_SEARCH)) is not None
        except (OSError, UnicodeDecodeError, csv.Error):
            return False

    def read(self, path: Path, account: str | None = None) -> Snapshot:
        """The holdings in ``path``. ``account`` names the outside account of
        a file that has no column for it, whatever the file is called."""
        if account is not None and "account" in self.columns:
            raise ProfileError(
                f"{path}: a {self.source} file names its accounts itself, so "
                "--outside-account doesn't apply")
        rows = _rows(path)
        start = self._header(rows)
        if start is None:
            raise ProfileError(
                f"{path}: no header row with the columns {self.where} names: "
                + ", ".join(sorted(set(self.columns.values()))))
        header = [cell.strip() for cell in rows[start]]
        at = {key: header.index(name) for key, name in self.columns.items()}
        file_account = account if account is not None else self._file_account(path)
        holdings: list[Holding] = []
        end = len(rows)
        for number in range(start + 1, len(rows)):
            row = rows[number]
            # The holdings stop at the first empty row: what follows is the
            # outside program's notes.
            if not any(cell.strip() for cell in row):
                end = number
                break

            def cell(key: str, row: list[str] = row) -> str:
                index = at.get(key)
                text = row[index].strip() if index is not None and index < len(row) else ""
                return "" if text in self.blank else text

            line = f"{path} line {number + 1}"
            if not cell("value"):
                continue
            asset = cell("asset").strip(self.strip_asset) if self.strip_asset else cell("asset")
            outside = cell("account") if "account" in at else file_account
            if not outside:
                raise ProfileError(f"{line}: no account")
            value = _cents(cell("value"), f"{line}, {self.columns['value']}")
            basis: int | None = None
            if cell("cost_basis"):
                basis = _cents(cell("cost_basis"), f"{line}, {self.columns['cost_basis']}")
            elif cell("unrealized_gain"):
                basis = value - _cents(
                    cell("unrealized_gain"), f"{line}, {self.columns['unrealized_gain']}")
            holdings.append(Holding(
                account=outside, value_cents=value, asset=asset,
                quantity=_quantity(cell("quantity"), f"{line}, {self.columns.get('quantity')}"),
                cost_basis_cents=basis,
                acquired=_date(cell("acquired"), self.acquired_format, line),
                hints={key: found for key in ("account_name", "name", "type") if (found := cell(key))},
            ))
        return Snapshot(source=self.source, holdings=tuple(holdings),
                        as_of=self._as_of(path, header, rows[end:]))

    def _file_account(self, path: Path) -> str:
        if self.account_pattern is None:
            return ""
        found = self.account_pattern.search(path.name)
        if found is None:
            raise ProfileError(
                f"{path}: the file's name doesn't say which account it is ({self.where} "
                f"expects {self.account_pattern.pattern!r}); name it with --outside-account")
        return (found.group(1) if found.groups() else found.group(0)).strip()

    def _as_of(self, path: Path, header: list[str], tail: list[list[str]]) -> dt.date | None:
        if self.as_of_pattern is None:
            return None
        texts = {"heading": header, "file_name": [path.name],
                 "last_lines": [cell for row in tail for cell in row]}[self.as_of_place]
        for text in texts:
            if (found := self.as_of_pattern.search(text)) is not None:
                return _date(found.group(1) if found.groups() else found.group(0),
                             self.as_of_format, f"{path}, its date")
        return None


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    where: str
    holdings: HoldingsProfile | None = None
    built_in: bool = field(default=False, compare=False)


def _rows(path: Path, limit: int | None = None) -> list[list[str]]:
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        rows = []
        for row in csv.reader(fh):
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
        return rows


def _cents(text: str, where: str) -> int:
    found = _MONEY.match(text)
    try:
        if found is None:
            raise InvalidOperation
        cents = Decimal(found["number"].replace(",", "")) * 100
        if cents != cents.to_integral_value():
            raise InvalidOperation
    except InvalidOperation:
        raise ProfileError(f"{where}: {text!r} is not an amount of money") from None
    negative = found["sign"] == "-" or (found["open"] and found["close"])
    return -int(cents) if negative else int(cents)


def _quantity(text: str, where: str) -> Decimal | None:
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ProfileError(f"{where}: {text!r} is not a number") from None


def _date(text: str, form: str, where: str) -> dt.date | None:
    if not text:
        return None
    try:
        return dt.datetime.strptime(text, form).date()
    except ValueError:
        raise ProfileError(f"{where}: {text!r} is not a date like {form!r}") from None


def _pattern(text: object, where: str) -> re.Pattern[str]:
    try:
        return re.compile(str(text))
    except re.error as error:
        raise ProfileError(f"{where}: {text!r} is not a pattern ({error})") from None


def _holdings(source: str, where: str, data: object) -> HoldingsProfile:
    if not isinstance(data, dict):
        raise ProfileError(f"{where}: 'holdings' must be a mapping")
    if unknown := sorted(set(data) - _HOLDINGS_KEYS):
        raise ProfileError(f"{where}: unknown holdings key(s) {unknown}; known: {sorted(_HOLDINGS_KEYS)}")
    columns = data.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise ProfileError(f"{where}: 'holdings' needs 'columns'")
    if unknown := sorted(set(columns) - set(_COLUMNS)):
        raise ProfileError(f"{where}: unknown column(s) {unknown}; known: {list(_COLUMNS)}")
    if missing := [key for key in ("asset", "value") if not columns.get(key)]:
        raise ProfileError(f"{where}: 'columns' must name {' and '.join(missing)}")
    account: Any = data.get("account") or {}
    if not isinstance(account, dict) or set(account) - {"file_name"}:
        raise ProfileError(f"{where}: 'account' may only be {{file_name: PATTERN}}")
    if bool(account.get("file_name")) == bool(columns.get("account")):
        raise ProfileError(
            f"{where}: say where the account is, once: a column ('columns: account') or "
            "the file's name ('account: {file_name: PATTERN}')")
    as_of: Any = data.get("as_of") or {}
    if not isinstance(as_of, dict) or set(as_of) - {*_AS_OF_PLACES, "format"}:
        raise ProfileError(f"{where}: 'as_of' takes one of {list(_AS_OF_PLACES)}, and 'format'")
    places = [place for place in _AS_OF_PLACES if as_of.get(place)]
    if len(places) > 1:
        raise ProfileError(f"{where}: 'as_of' takes one of {list(_AS_OF_PLACES)}, not {places}")
    blank = data.get("blank") or []
    if not isinstance(blank, list):
        raise ProfileError(f"{where}: 'blank' must be a list")
    return HoldingsProfile(
        source=source, where=where, layout=str(data.get("layout") or "a holdings file"),
        columns={str(key): str(name) for key, name in columns.items()},
        account_pattern=_pattern(account["file_name"], where) if account.get("file_name") else None,
        as_of_place=places[0] if places else "",
        as_of_pattern=_pattern(as_of[places[0]], where) if places else None,
        as_of_format=str(as_of.get("format") or "%m/%d/%Y"),
        acquired_format=str(data.get("acquired_format") or "%m/%d/%Y"),
        blank=frozenset(str(each) for each in blank),
        strip_asset=str(data.get("strip_asset") or ""),
    )


def load(path: Path, name: str, *, built_in: bool = False) -> Profile:
    """The profile in ``path``, for the source ``name``."""
    where = str(path)
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ProfileError(f"{where}: {error}") from None
    if not isinstance(data, dict):
        raise ProfileError(f"{where}: a profile is a mapping")
    if unknown := sorted(set(data) - {"name", "description", "holdings"}):
        raise ProfileError(f"{where}: unknown key(s) {unknown}; known: description, holdings")
    if data.get("name") not in (None, name):
        raise ProfileError(f"{where}: its name is {data['name']!r}, but its folder says {name!r}")
    if "holdings" not in data:
        raise ProfileError(f"{where}: a profile needs 'holdings'")
    return Profile(
        name=name, description=str(data.get("description") or name), where=where,
        holdings=_holdings(name, where, data["holdings"]), built_in=built_in,
    )


def built_in() -> dict[str, Profile]:
    return {path.stem: load(path, path.stem, built_in=True) for path in sorted(_BUILT_IN.glob("*.yaml"))}


def user(imports: Path) -> dict[str, Profile]:
    """The household's own profiles: ``imports/<source>/profile.yaml``."""
    if not imports.is_dir():
        return {}
    return {path.parent.name: load(path, path.parent.name)
            for path in sorted(imports.glob(f"*/{PROFILE}"))}
