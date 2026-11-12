"""The saved mappings from outside names to a scenario's (issue #527).

One folder in the data dir, ``imports/``, shared by every scenario: the
outside world's names don't change from one scenario to the next, so each is
mapped once, and the targets are checked against the scenario being written.

```text
imports/
  expense_accounts.csv        category -> expense account ('-' = no rule)
  <source>/
    accounts.csv              (outside account, asset) -> (account, lot), or skip
    categories.csv            outside category -> category (blank or '-' excludes)
    splits.csv                a category divided by the outside account that paid
    excluded_payees.csv       payees whose expenses the scenario models itself
```

Every outside name must be mapped or explicitly skipped: a new or renamed
outside account is then an error (or, at a terminal, a question), never a
balance quietly left out.
"""

from __future__ import annotations

import csv
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ACCOUNTS", "ACCOUNTS_HEADER", "CATEGORIES", "CATEGORIES_HEADER", "EXCLUDED_PAYEES",
    "EXCLUDED_PAYEES_HEADER", "EXPENSE_ACCOUNTS", "EXPENSE_ACCOUNTS_HEADER", "IMPORTS", "NO_RULE",
    "SPLITS", "SPLITS_HEADER", "AccountMap", "AccountTarget", "ExpenseAccounts", "expense_accounts_path",
    "imports_dir", "load_account_map", "load_category_map", "load_category_splits",
    "load_excluded_payees", "load_expense_accounts", "rename_account", "source_dir",
]

IMPORTS = "imports"
ACCOUNTS = "accounts.csv"
CATEGORIES = "categories.csv"
SPLITS = "splits.csv"
EXCLUDED_PAYEES = "excluded_payees.csv"
EXPENSE_ACCOUNTS = "expense_accounts.csv"

ACCOUNTS_HEADER = ("outside_account", "asset", "action", "account", "lot", "note")
CATEGORIES_HEADER = ("outside_category", "category")
SPLITS_HEADER = ("category", "outside_account", "target_category")
EXCLUDED_PAYEES_HEADER = ("payee_pattern",)
EXPENSE_ACCOUNTS_HEADER = ("category", "account")
NO_RULE = "-"
"""An ``expense_accounts.csv`` account meaning the category gets no rule."""


def imports_dir(data_dir: Path) -> Path:
    return Path(data_dir) / IMPORTS


def source_dir(data_dir: Path, source: str) -> Path:
    return imports_dir(data_dir) / source


def expense_accounts_path(data_dir: Path) -> Path:
    return imports_dir(data_dir) / EXPENSE_ACCOUNTS


def _read(path: Path, header: tuple[str, ...]) -> Iterable[tuple[int, dict[str, str]]]:
    """Each row of ``path`` with its line number, cells stripped; the header
    must be exactly ``header``."""
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if tuple(reader.fieldnames or ()) != header:
            raise ValueError(
                f"{path}: expected header {list(header)}, got {list(reader.fieldnames or [])}"
            )
        for number, row in enumerate(reader, start=2):
            yield number, {key: (row[key] or "").strip() for key in header}


def _write(path: Path, header: tuple[str, ...], rows: Iterable[Sequence[str]]) -> None:
    """Replace ``path`` whole, or not at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(scratch, path)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise


# ---------- accounts ----------

@dataclass(frozen=True)
class AccountTarget:
    """Where one outside account (or one asset in it) goes."""

    action: str
    """``map`` or ``skip`` (acknowledged, deliberately not imported)."""
    account: str = ""
    lot: str = ""
    """Blank for the account's only lot."""
    note: str = ""


@dataclass
class AccountMap:
    """``imports/<source>/accounts.csv``. A row with a blank ``asset`` covers
    everything in the outside account that has no row of its own."""

    path: Path
    entries: dict[tuple[str, str], AccountTarget] = field(default_factory=dict)

    def lookup(self, outside_account: str, asset: str = "") -> AccountTarget | None:
        return self.entries.get((outside_account, asset)) or self.entries.get((outside_account, ""))

    def set(self, outside_account: str, asset: str, target: AccountTarget) -> None:
        self.entries[(outside_account, asset)] = target

    def save(self) -> None:
        _write(self.path, ACCOUNTS_HEADER, (
            (outside, asset, target.action, target.account, target.lot, target.note)
            for (outside, asset), target in self.entries.items()
        ))


def load_account_map(path: Path) -> AccountMap:
    """The account map at ``path``; an empty one if there's no file yet."""
    found = AccountMap(Path(path))
    if not found.path.is_file():
        return found
    for number, row in _read(found.path, ACCOUNTS_HEADER):
        if not row["outside_account"]:
            continue
        key = (row["outside_account"], row["asset"])
        where = f"{path} row {number}"
        if row["action"] not in {"map", "skip"}:
            raise ValueError(f"{where}: action must be 'map' or 'skip', got {row['action']!r}")
        if row["action"] == "map" and not row["account"]:
            raise ValueError(f"{where}: a 'map' row needs an account")
        if key in found.entries:
            raise ValueError(f"{where}: duplicate outside_account {' / '.join(filter(None, key))!r}")
        found.entries[key] = AccountTarget(row["action"], row["account"], row["lot"], row["note"])
    return found


# ---------- categories ----------

def load_category_map(path: Path | str) -> tuple[dict[str, str], set[str]]:
    """Outside category merges and exclusions from a two-column CSV.

    A blank or ``-`` target excludes the outside category. All other targets
    merge the outside category into that category.
    """
    merges: dict[str, str] = {}
    exclusions: set[str] = set()
    for number, row in _read(Path(path), CATEGORIES_HEADER):
        source, target = row["outside_category"], row["category"]
        if not source:
            continue
        if source in merges or source in exclusions:
            raise ValueError(f"{path} row {number}: duplicate category {source!r}")
        if not target or target == "-":
            exclusions.add(source)
        else:
            merges[source] = target
    return merges, exclusions


def load_category_splits(path: Path | str) -> dict[str, dict[str, str]]:
    """Splits of one category by the outside account that paid.

    Each row sends ``category`` expenses charged to ``outside_account`` to
    ``target_category``. A row with a blank ``outside_account`` is that
    category's default: it takes every row no named account claims, including
    Amazon summaries, which have no account.
    """
    splits: dict[str, dict[str, str]] = {}
    for number, row in _read(Path(path), SPLITS_HEADER):
        category, account, target = row["category"], row["outside_account"], row["target_category"]
        if not category:
            continue
        if not target:
            raise ValueError(f"{path} row {number}: target category is required")
        targets = splits.setdefault(category, {})
        if account in targets:
            raise ValueError(
                f"{path} row {number}: duplicate split of {category!r} for account {account!r}"
            )
        targets[account] = target
    return splits


def load_excluded_payees(path: Path | str) -> tuple[str, ...]:
    """Payee patterns for expenses the scenario models separately.

    Each ``payee_pattern`` is a case-insensitive regular expression searched
    for anywhere in the payee.
    """
    patterns: list[str] = []
    for number, row in _read(Path(path), EXCLUDED_PAYEES_HEADER):
        pattern = row["payee_pattern"]
        if not pattern:
            continue
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(
                f"{path} row {number}: invalid payee pattern {pattern!r}: {error}"
            ) from error
        patterns.append(pattern)
    return tuple(patterns)


@dataclass
class ExpenseAccounts:
    """``imports/expense_accounts.csv``: the expense account each category's
    rule pays into, or ``NO_RULE``. Shared by every source, since a budget
    and a year of transactions name the same categories."""

    path: Path
    accounts: dict[str, str] = field(default_factory=dict)

    def save(self) -> None:
        _write(self.path, EXPENSE_ACCOUNTS_HEADER, self.accounts.items())


def load_expense_accounts(path: Path | str) -> ExpenseAccounts:
    """The category accounts at ``path``; none if there's no file yet."""
    found = ExpenseAccounts(Path(path))
    if not found.path.is_file():
        return found
    for number, row in _read(found.path, EXPENSE_ACCOUNTS_HEADER):
        category, account = row["category"], row["account"]
        if not category:
            continue
        if category in found.accounts:
            raise ValueError(f"{path} row {number}: duplicate category {category!r}")
        if not account:
            raise ValueError(
                f"{path} row {number}: an account is required ({NO_RULE!r} for no rule)")
        found.accounts[category] = account
    return found


# ---------- following a rename ----------

def rename_account(data_dir: Path, old: str, new: str) -> list[str]:
    """Follow a renamed scenario account in every saved mapping; the files
    changed, relative to the data dir."""
    root = imports_dir(data_dir)
    if not root.is_dir():
        return []
    changed: list[str] = []
    for path in sorted(root.glob(f"*/{ACCOUNTS}")):
        found = load_account_map(path)
        renamed = {key: AccountTarget(t.action, new, t.lot, t.note)
                   for key, t in found.entries.items() if t.action == "map" and t.account == old}
        if renamed:
            found.entries.update(renamed)
            found.save()
            changed.append(str(path.relative_to(data_dir)))
    expenses = load_expense_accounts(root / EXPENSE_ACCOUNTS)
    if old in expenses.accounts.values():
        expenses.accounts = {c: new if a == old else a for c, a in expenses.accounts.items()}
        expenses.save()
        changed.append(str(expenses.path.relative_to(data_dir)))
    return changed
