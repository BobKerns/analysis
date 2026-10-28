"""Finding strategy kinds: the built-ins and the data repo's ``strategies/``
modules (#326, ADR-010 §4).

Data-repo modules are imported from their source text, not their files: the
loader snapshots that text into the intent (``strategy_source``) and the
engine imports from the snapshot, so a run is self-contained and an ensemble
worker needs no data directory. The modules form a synthetic package named
after a hash of all their sources, so two versions of the same module (two
scenarios loaded in one process, a test and a notebook) never share state,
and relative imports between them (``from ._helpers import x``) work.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.util
import linecache
import pkgutil
import sys
import traceback
import types
from collections.abc import Mapping
from importlib.machinery import ModuleSpec
from pathlib import Path

from analysis.sim import strategy_api
from analysis.sim.strategy_api import Strategy, StrategyError

__all__ = [
    "STRATEGIES_DIR", "StrategyKinds", "builtin_kinds", "package_name", "read_sources",
    "strategy_kinds",
]

STRATEGIES_DIR = "strategies"
"""The data repo folder of strategy modules (ADR-003 §4.0)."""

_BUILTIN_PACKAGE = "analysis.sim.strategies"


def read_sources(data_dir: Path) -> dict[str, str]:
    """``strategies/*.py`` in the data repo: module name to source, sorted.
    No folder means no data-repo strategies."""
    folder = Path(data_dir) / STRATEGIES_DIR
    if not folder.is_dir():
        return {}
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(folder.glob("*.py"))
        if path.stem != "__init__"
    }


def package_name(sources: Mapping[str, str]) -> str:
    """The synthetic package for ``sources``: one name per exact set of texts."""
    digest = hashlib.sha256()
    for name in sorted(sources):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(sources[name].encode())
        digest.update(b"\0")
    return f"_strategies_{digest.hexdigest()[:12]}"


class _SourceFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves one synthetic package's modules from their source text."""

    def __init__(self, package: str, sources: Mapping[str, str]) -> None:
        self.package = package
        self.sources = dict(sources)

    def find_spec(self, fullname, path=None, target=None):  # noqa: ARG002
        if fullname == self.package:
            return ModuleSpec(fullname, self, is_package=True)
        prefix, _, name = fullname.rpartition(".")
        if prefix == self.package and name in self.sources:
            return ModuleSpec(fullname, self, origin=f"{STRATEGIES_DIR}/{name}.py")
        return None

    def create_module(self, spec):  # noqa: ARG002
        return None

    def exec_module(self, module: types.ModuleType) -> None:
        if module.__name__ == self.package:
            module.__path__ = []
            return
        name = module.__name__.rpartition(".")[2]
        filename = f"{self.package}/{STRATEGIES_DIR}/{name}.py"
        text = self.sources[name]
        # Let tracebacks and inspect.getsource (parameter docs in `sim
        # strategy kinds`) find the text, which has no file of its own.
        linecache.cache[filename] = (len(text), None, text.splitlines(keepends=True), filename)
        module.__file__ = filename
        code = compile(text, filename, "exec")
        exec(code, module.__dict__)  # noqa: S102 -- the household's own strategy code


def _import_package(sources: Mapping[str, str]) -> tuple[str, list[str]]:
    """Import every module of ``sources``; the package name and any failures."""
    package = package_name(sources)
    if not any(
        isinstance(f, _SourceFinder) and f.package == package for f in sys.meta_path
    ):
        sys.meta_path.append(_SourceFinder(package, sources))
    problems: list[str] = []
    for name in sorted(sources):
        if name.startswith("_") or not name.isidentifier():
            if not name.isidentifier():
                problems.append(f"{STRATEGIES_DIR}/{name}.py: not a Python module name")
            continue
        try:
            module = importlib.import_module(f"{package}.{name}")
        except Exception:  # the module's own error, shown whole
            problems.append(
                f"{STRATEGIES_DIR}/{name}.py failed to import:\n"
                + "".join(traceback.format_exc(limit=-3)).rstrip()
            )
            continue
        wanted = getattr(module, "API_VERSION", strategy_api.API_VERSION)
        if not isinstance(wanted, int) or wanted > strategy_api.API_VERSION:
            problems.append(
                f"{STRATEGIES_DIR}/{name}.py is written for strategy API {wanted!r}; "
                f"this code provides {strategy_api.API_VERSION}"
            )
    return package, problems


def _kinds_in(prefix: str) -> list[tuple[str, type[Strategy]]]:
    """``(module, class)`` for every registered class defined in ``prefix``'s modules."""
    out = []
    for module_name, module in sorted(sys.modules.items()):
        if module is None or not module_name.startswith(prefix + "."):
            continue
        for value in vars(module).values():
            if strategy_api.is_registered(value) and value.__module__ == module_name:
                out.append((module_name.rpartition(".")[2], value))
    return out


def builtin_kinds() -> dict[str, type[Strategy]]:
    """The strategies that ship with the simulator."""
    package = importlib.import_module(_BUILTIN_PACKAGE)
    for info in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{_BUILTIN_PACKAGE}.{info.name}")
    return {cls.kind: cls for _module, cls in _kinds_in(_BUILTIN_PACKAGE)}


class StrategyKinds:
    """Every kind a scenario can name, and where each came from."""

    def __init__(self, sources: Mapping[str, str]) -> None:
        self.sources = dict(sources)
        self.problems: list[str] = []
        self.kinds: dict[str, type[Strategy]] = dict(builtin_kinds())
        self.origin: dict[str, str] = dict.fromkeys(self.kinds, "built-in")
        if not self.sources:
            return
        package, self.problems = _import_package(self.sources)
        for module, cls in _kinds_in(package):
            where = f"{STRATEGIES_DIR}/{module}.py"
            if cls.kind in self.kinds:
                self.problems.append(
                    f"{where}: kind '{cls.kind}' is already defined "
                    f"({self.origin[cls.kind]}); rename it"
                    + (" or promote it" if self.origin[cls.kind] == "built-in" else "")
                )
                continue
            self.kinds[cls.kind] = cls
            self.origin[cls.kind] = where

    def get(self, kind: str) -> type[Strategy]:
        try:
            return self.kinds[kind]
        except KeyError:
            raise StrategyError(f"unknown strategy kind {kind!r}") from None


def strategy_kinds(sources: Mapping[str, str]) -> StrategyKinds:
    """The built-in kinds plus those ``sources`` define; import problems are
    in ``.problems`` rather than raised, so a loader can report them all."""
    return StrategyKinds(sources)
