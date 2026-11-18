"""The saved mappings from outside names to a scenario's (issue #527).

One folder in the data dir, ``imports/``, shared by every scenario: the
outside world's names don't change from one scenario to the next, so each is
mapped once, and the targets are checked against the scenario being written.

```text
imports/
  expense_accounts.csv        category -> expense account ('-' = no rule)
  income_accounts.csv         category -> income account, for a budget's income
  <source>/
    profile.yaml              the source itself, when it is the household's own (profiles.py)
    accounts.csv              (outside account, asset) -> (account, lot), or skip, or split
    lot_splits.csv            one outside number divided among an account's lots
    categories.csv            outside category -> category (blank or '-' excludes)
    splits.csv                a category divided by the outside account that paid
    excluded_payees.csv       payees whose expenses the scenario models itself
    payees.csv                payee text -> category, for a source with no categories
```

Every outside name must be mapped or explicitly skipped: a new or renamed
outside account is then an error (or, at a terminal, a question), never a
balance quietly left out.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "MAP_FILES", "MapFile", "categories", "map_files", "read_map_rows", "sources_with_maps", "write_map_rows",
    "ACCOUNTS", "ACCOUNTS_HEADER", "LOT_SPLITS", "LOT_SPLITS_HEADER", "LotPart", "LotSplits", "divide",
    "load_lot_splits", "BUDGET_CHOICES", "BUDGET_CHOICES_HEADER", "CATEGORIES",
    "CATEGORIES_HEADER", "EXCLUDED_PAYEES", "EXCLUDED_PAYEES_HEADER", "EXPENSE_ACCOUNTS",
    "EXPENSE_ACCOUNTS_HEADER", "IMPORTS", "INCOME_ACCOUNTS", "NO_RULE", "PAYEES", "PAYEES_HEADER", "SPLITS",
    "SPLITS_HEADER", "TRANSFER", "AccountMap", "AccountTarget", "BudgetChoice", "BudgetChoices",
    "ExpenseAccounts", "PayeeMap", "budget_choices_path", "expense_accounts_path", "imports_dir",
    "income_accounts_path", "load_account_map", "load_budget_choices", "load_category_map",
    "load_category_splits", "load_excluded_payees", "load_expense_accounts", "load_payee_map", "payee_key",
    "payee_matches", "rename_account", "rename_lot", "source_dir",
]

IMPORTS = "imports"
ACCOUNTS = "accounts.csv"
LOT_SPLITS = "lot_splits.csv"
CATEGORIES = "categories.csv"
SPLITS = "splits.csv"
BUDGET_CHOICES = "budget_choices.csv"
EXCLUDED_PAYEES = "excluded_payees.csv"
EXPENSE_ACCOUNTS = "expense_accounts.csv"
INCOME_ACCOUNTS = "income_accounts.csv"
PAYEES = "payees.csv"

ACCOUNTS_HEADER = ("outside_account", "asset", "action", "account", "lot", "note")
LOT_SPLITS_HEADER = ("outside_account", "asset", "lot", "amount", "cost_basis")
CATEGORIES_HEADER = ("outside_category", "category")
SPLITS_HEADER = ("category", "outside_account", "target_category")
EXCLUDED_PAYEES_HEADER = ("payee_pattern",)
EXPENSE_ACCOUNTS_HEADER = ("category", "account")
BUDGET_CHOICES_HEADER = ("category", "use", "budget", "actual")
PAYEES_HEADER = ("payee_pattern", "category")
TRANSFER = "transfer"
"""A ``payees.csv`` category for a move between the household's own accounts
(a card's payment, a sweep to savings): not spending."""
NO_RULE = "-"
"""An ``expense_accounts.csv`` account meaning the category gets no rule."""


def imports_dir(data_dir: Path) -> Path:
    return Path(data_dir) / IMPORTS


def source_dir(data_dir: Path, source: str) -> Path:
    return imports_dir(data_dir) / source


def income_accounts_path(data_dir: Path) -> Path:
    """``imports/income_accounts.csv``: as ``expense_accounts.csv``, for the
    income categories of a budget whose income is used."""
    return imports_dir(data_dir) / INCOME_ACCOUNTS


def expense_accounts_path(data_dir: Path) -> Path:
    return imports_dir(data_dir) / EXPENSE_ACCOUNTS


def budget_choices_path(data_dir: Path) -> Path:
    return imports_dir(data_dir) / BUDGET_CHOICES


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


def _write(
    path: Path, header: tuple[str, ...], rows: Iterable[Sequence[str]],
    check: Callable[[Path], object] | None = None,
) -> None:
    """Replace ``path`` whole, or not at all. ``check`` reads what is to
    be written before it takes ``path``'s place; the ValueError it raises
    leaves ``path`` as it was, and names ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
        if check is not None:
            try:
                check(Path(scratch))
            except ValueError as e:
                raise ValueError(str(e).replace(scratch, str(path))) from None
        os.replace(scratch, path)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise


# ---------- accounts ----------

@dataclass(frozen=True)
class AccountTarget:
    """Where one outside account (or one asset in it) goes."""

    action: str
    """``map``, ``skip`` (acknowledged, deliberately not imported), or
    ``split``: the file's one number is divided among lots of ``account``,
    as ``lot_splits.csv`` says."""
    account: str = ""
    lot: str = ""
    """Blank for the account's only lot; always blank for a ``split``."""
    note: str = ""

    @property
    def label(self) -> str:
        """``account``, or ``account:lot``: how a message names the target."""
        return f"{self.account}:{self.lot}" if self.lot else self.account


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
        if row["action"] not in {"map", "skip", "split"}:
            raise ValueError(f"{where}: action must be 'map', 'skip' or 'split', got {row['action']!r}")
        if row["action"] != "skip" and not row["account"]:
            raise ValueError(f"{where}: a {row['action']!r} row needs an account")
        if row["action"] == "split" and row["lot"]:
            raise ValueError(
                f"{where}: a 'split' row names no lot: {LOT_SPLITS} beside this file says which lots, "
                "and how much goes to each")
        if key in found.entries:
            raise ValueError(f"{where}: duplicate outside_account {' / '.join(filter(None, key))!r}")
        found.entries[key] = AccountTarget(row["action"], row["account"], row["lot"], row["note"])
    return found


# ---------- one number divided among lots ----------

@dataclass(frozen=True)
class LotPart:
    """One lot's part of a number a file gives whole."""

    lot: str
    cents: int | None
    """Signed as the file's number is; None for whatever the other parts leave."""
    basis_cents: int | None = None


@dataclass
class LotSplits:
    """``imports/<source>/lot_splits.csv``: for a holding whose ``accounts.csv``
    row says ``split``, the lots of that row's account it is divided among.
    The amounts are those of the import that asked, kept as the next one's
    defaults; the part with no amount takes the rest, so a split with one
    still fits a number that has changed."""

    path: Path
    parts: dict[tuple[str, str], list[LotPart]] = field(default_factory=dict)

    def save(self) -> None:
        from analysis.sim.imports.forms import format_dollars

        def money(cents: int | None) -> str:
            return "" if cents is None else format_dollars(cents)

        _write(self.path, LOT_SPLITS_HEADER, (
            (outside, asset, part.lot, money(part.cents), money(part.basis_cents))
            for (outside, asset), parts in self.parts.items() for part in parts))


def load_lot_splits(path: Path | str) -> LotSplits:
    """The lot splits at ``path``; none if there's no file yet."""
    from analysis.sim.units import dollars_to_cents

    found = LotSplits(Path(path))
    if not found.path.is_file():
        return found
    for number, row in _read(found.path, LOT_SPLITS_HEADER):
        if not row["outside_account"]:
            continue
        where = f"{path} row {number}"
        parts = found.parts.setdefault((row["outside_account"], row["asset"]), [])
        if not row["lot"]:
            raise ValueError(f"{where}: a lot is required")
        if any(part.lot == row["lot"] for part in parts):
            raise ValueError(f"{where}: lot {row['lot']!r} has a part of {row['outside_account']!r} already")
        if not row["amount"] and any(part.cents is None for part in parts):
            raise ValueError(
                f"{where}: only one lot of a split can have a blank amount (it takes the rest)")
        try:
            cents = dollars_to_cents(row["amount"]) if row["amount"] else None
            basis = dollars_to_cents(row["cost_basis"]) if row["cost_basis"] else None
        except ValueError as e:
            raise ValueError(f"{where}: {e}") from None
        parts.append(LotPart(row["lot"], cents, basis))
    return found


def divide(parts: Sequence[LotPart], total_cents: int) -> list[tuple[LotPart, int]] | None:
    """Each of ``parts`` with its cents of ``total_cents``; None when they
    don't come to it: no part takes the rest and the others add up to
    another number, or the rest would be of the other sign (more is given
    out than there is)."""
    rest = total_cents - sum(part.cents for part in parts if part.cents is not None)
    if not parts or (rest and (all(part.cents is not None for part in parts) or rest * total_cents < 0)):
        return None
    return [(part, rest if part.cents is None else part.cents) for part in parts]


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


@dataclass(frozen=True)
class BudgetChoice:
    """Which of the two a category's rule is made from, and the two monthly
    amounts, in cents, the choice was made between."""

    use: str
    """``budget`` or ``actual``."""
    budget_cents: int
    actual_cents: int


@dataclass
class BudgetChoices:
    """``imports/budget_choices.csv``: for each category that both a budget
    and the transactions have, which one its rule is made from. Shared by
    every scenario, as the categories' accounts are."""

    path: Path
    choices: dict[str, BudgetChoice] = field(default_factory=dict)

    def save(self) -> None:
        from analysis.sim.imports.holdings import format_dollars

        _write(self.path, BUDGET_CHOICES_HEADER, (
            (category, each.use, format_dollars(each.budget_cents), format_dollars(each.actual_cents))
            for category, each in self.choices.items()))


def load_budget_choices(path: Path | str) -> BudgetChoices:
    """The choices at ``path``; none if there's no file yet."""
    found = BudgetChoices(Path(path))
    if not found.path.is_file():
        return found
    for number, row in _read(found.path, BUDGET_CHOICES_HEADER):
        category = row["category"]
        if not category:
            continue
        if category in found.choices:
            raise ValueError(f"{path} row {number}: duplicate category {category!r}")
        if row["use"] not in ("budget", "actual"):
            raise ValueError(f"{path} row {number}: use must be budget or actual, not {row['use']!r}")
        try:
            amounts = [round(float(row[column] or 0) * 100) for column in ("budget", "actual")]
        except (ValueError, OverflowError):  # 'inf' is a float that no amount is
            raise ValueError(f"{path} row {number}: budget and actual are dollar amounts") from None
        found.choices[category] = BudgetChoice(row["use"], *amounts)
    return found


# ---------- following a rename ----------

def _retarget(data_dir: Path, moved: Callable[[AccountTarget], AccountTarget | None]) -> list[str]:
    """Rewrite every source's account map, each ``map`` or ``split`` row's
    target replaced by ``moved``'s answer where it gives one; the files changed, relative to
    the data dir. Every map is read before any is written, so one that
    doesn't read leaves them all as they were."""
    maps = [load_account_map(path) for path in sorted(imports_dir(data_dir).glob(f"*/{ACCOUNTS}"))]
    changed: list[str] = []
    for found in maps:
        renamed = {key: new for key, target in found.entries.items()
                   if target.action != "skip" and (new := moved(target)) is not None}
        if renamed:
            found.entries.update(renamed)
            found.save()
            changed.append(str(found.path.relative_to(data_dir)))
    return changed


def categories(data_dir: Path) -> set[str]:
    """Every category the saved maps can produce: what an outside category
    is merged into, and what a split sends it on to."""
    found: set[str] = set()
    root = imports_dir(data_dir)
    for path in root.glob(f"*/{CATEGORIES}"):
        found.update(load_category_map(path)[0].values())
    for path in root.glob(f"*/{SPLITS}"):
        for targets in load_category_splits(path).values():
            found.update(targets.values())
    return found


def rename_lot(data_dir: Path, account: str, old: str, new: str) -> list[str]:
    """Follow a renamed lot of ``account`` in every saved account mapping,
    and in the lot splits of the rows that divide a number among the
    account's lots; the files changed, relative to the data dir."""
    # Read, and so checked, before anything is rewritten.
    splits = [(load_account_map(path.with_name(ACCOUNTS)), load_lot_splits(path))
              for path in sorted(imports_dir(data_dir).glob(f"*/{LOT_SPLITS}"))]
    changed = _retarget(data_dir, lambda t: AccountTarget(t.action, t.account, new, t.note)
                        if t.action == "map" and (t.account, t.lot) == (account, old) else None)
    for accounts, found in splits:
        before = {key: list(parts) for key, parts in found.parts.items()}
        for key, parts in found.parts.items():
            target = accounts.entries.get(key)
            if target is not None and target.action == "split" and target.account == account:
                found.parts[key] = [dataclasses.replace(part, lot=new) if part.lot == old else part
                                    for part in parts]
        if found.parts != before:
            found.save()
            changed.append(str(found.path.relative_to(data_dir)))
    return changed


def _income_categories(data_dir: Path, account: str) -> set[str]:
    """The budget categories whose generated income rules are paid from
    ``account`` in any scenario. No map lists a budget's income categories,
    so the rules a budget's import wrote are where they are found."""
    from analysis.expenses.baseline import INCOME_GENERATED_TAG

    found: set[str] = set()
    for path in (data_dir / "scenarios").glob("*/scheduled.csv"):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if row.get("from_account") != account or INCOME_GENERATED_TAG not in (row.get("tags") or ""):
                    continue
                try:
                    category = json.loads(row.get("meta_json") or "{}").get("category")
                except ValueError:
                    continue
                if isinstance(category, str):
                    found.add(category)
    return found


def rename_account(data_dir: Path, old: str, new: str) -> list[str]:
    """Follow a renamed scenario account in every saved mapping; the files
    changed, relative to the data dir. A category that had no row because
    the account had its name gets one, so its rule still finds the account.
    The scenarios' own files are renamed first: an income category that
    relied on the account's name is known by the rule a budget gave it."""
    root = imports_dir(data_dir)
    # Read, and so checked, before anything is rewritten.
    expenses = load_expense_accounts(root / EXPENSE_ACCOUNTS)
    income = load_expense_accounts(root / INCOME_ACCOUNTS)
    implicit = {
        expenses.path: old not in expenses.accounts and old in categories(data_dir),
        income.path: old not in income.accounts and old in _income_categories(data_dir, new),
    }
    changed = _retarget(data_dir, lambda t: AccountTarget(t.action, new, t.lot, t.note)
                        if t.account == old else None)
    for saved in (expenses, income):
        if implicit[saved.path] or old in saved.accounts.values():
            saved.accounts = {c: new if a == old else a for c, a in saved.accounts.items()}
            if implicit[saved.path]:
                saved.accounts[old] = new
            saved.save()
            changed.append(str(saved.path.relative_to(data_dir)))
    return changed


def payee_key(text: str) -> str:
    """What of a bank's payee text says who was paid: its words in lower
    case, less any with a digit in it (a date, a store or card number, a
    reference), since those differ from one purchase to the next."""
    return " ".join(word for word in text.casefold().split() if not any(c.isdigit() for c in word))


def payee_matches(pattern: str, key: str) -> bool:
    """Whether ``pattern``'s words are found in ``key``, whole and in their
    order (both as ``payee_key`` gives them): ``art`` is not in ``walmart``."""
    return bool(pattern) and f" {pattern} " in f" {key} "


@dataclass
class PayeeMap:
    """``imports/<source>/payees.csv``: the category of a transaction that
    comes with none, by its payee. A row's pattern is words found together
    anywhere in the payee, both read as ``payee_key`` reads them; the first
    row found wins. The category is one of the plan's, ``transfer``, or ``-`` (blank
    too) to leave the rows out."""

    path: Path
    rows: list[tuple[str, str]] = field(default_factory=list)

    def category(self, payee: str) -> str | None:
        """The category of ``payee``; None when no row is found in it."""
        return self.category_of(payee_key(payee))

    def category_of(self, key: str) -> str | None:
        """As ``category``, of a payee already read by ``payee_key``."""
        return next((category for pattern, category in self.rows if payee_matches(pattern, key)), None)

    def add(self, pattern: str, category: str) -> None:
        self.rows.append((payee_key(pattern), category))

    def save(self) -> None:
        _write(self.path, PAYEES_HEADER, self.rows)


def load_payee_map(path: Path) -> PayeeMap:
    """The payee map at ``path``; an empty one if there's no file yet."""
    found = PayeeMap(Path(path))
    if not found.path.is_file():
        return found
    for number, row in _read(found.path, PAYEES_HEADER):
        pattern = payee_key(row["payee_pattern"])
        if not pattern:
            if row["payee_pattern"]:
                raise ValueError(
                    f"{path} row {number}: {row['payee_pattern']!r} has no word without a digit, "
                    "so it could match no payee")
            continue
        found.rows.append((pattern, row["category"] or NO_RULE))
    return found


# ---------- every file, for `sim import map` ----------

@dataclass(frozen=True)
class MapFile:
    """One kind of mapping file: what `sim import map` needs to list one
    and to change a row of it."""

    name: str
    header: tuple[str, ...]
    keys: int
    """How many leading columns say what a row is about; the rest say what
    it means, and are what a change asks for."""
    check: Callable[[Path], object]
    """The file's own loader: a change it refuses is not kept."""
    shared: bool = False
    """In ``imports/`` itself, for every source."""
    account_kinds: tuple[str, ...] = ("asset", "liability")
    """The kinds of account its ``account`` column takes."""
    words: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """For a column, the values its loader reads as something other than a
    name (``-`` for no rule, ``transfer``): offered beside the names."""


MAP_FILES: tuple[MapFile, ...] = (
    MapFile(EXPENSE_ACCOUNTS, EXPENSE_ACCOUNTS_HEADER, 1, load_expense_accounts, shared=True,
            account_kinds=("expense",), words={"account": (NO_RULE,)}),
    MapFile(INCOME_ACCOUNTS, EXPENSE_ACCOUNTS_HEADER, 1, load_expense_accounts, shared=True,
            account_kinds=("income",), words={"account": (NO_RULE,)}),
    MapFile(BUDGET_CHOICES, BUDGET_CHOICES_HEADER, 1, load_budget_choices, shared=True),
    MapFile(ACCOUNTS, ACCOUNTS_HEADER, 2, load_account_map),
    MapFile(LOT_SPLITS, LOT_SPLITS_HEADER, 3, load_lot_splits),
    MapFile(CATEGORIES, CATEGORIES_HEADER, 1, load_category_map, words={"category": (NO_RULE,)}),
    # A split's target is an ordinary category: '-' there would only be one's name.
    MapFile(SPLITS, SPLITS_HEADER, 2, load_category_splits),
    MapFile(PAYEES, PAYEES_HEADER, 1, load_payee_map, words={"category": (TRANSFER, NO_RULE)}),
    MapFile(EXCLUDED_PAYEES, EXCLUDED_PAYEES_HEADER, 1, load_excluded_payees),
)


def sources_with_maps(data_dir: Path) -> list[str]:
    """The sources that have a folder under ``imports/``."""
    root = imports_dir(data_dir)
    return sorted(path.name for path in root.iterdir() if path.is_dir()) if root.is_dir() else []


def map_files(data_dir: Path, source: str | None = None) -> list[tuple[Path, MapFile]]:
    """The mapping files there are: ``source``'s, or with none the shared
    ones and then every source's."""
    root = imports_dir(data_dir)
    places = [root / source] if source else [root, *(root / each for each in sources_with_maps(data_dir))]
    return [(place / kind.name, kind) for place in places for kind in MAP_FILES
            if kind.shared == (place == root) and (place / kind.name).is_file()]


def read_map_rows(path: Path, kind: MapFile) -> list[dict[str, str]]:
    """``path``'s rows as written, less blank ones."""
    return [row for _, row in _read(path, kind.header) if any(row.values())]


def write_map_rows(path: Path, kind: MapFile, rows: Sequence[Mapping[str, str]]) -> None:
    """Replace ``path``'s rows, unless the file's loader refuses them: its
    ValueError is raised and ``path`` is as it was. The rows are checked
    before they take the file's place, so nothing unchecked is ever there
    for another command to read."""
    _write(path, kind.header, ([row[column] for column in kind.header] for row in rows), check=kind.check)
