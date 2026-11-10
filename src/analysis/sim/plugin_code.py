"""Importing the data repo's own code: ``strategies/*.py`` (#326, ADR-010
§4) and ``taxes/*.py`` (#510, ADR-011), each a folder of modules that
register kinds.

Data-repo modules are imported from their source text, not their files: the
loader snapshots that text into the intent and the engine imports from the
snapshot, so a run is self-contained and an ensemble worker needs no data
directory. A folder's modules form a synthetic package named after a hash of
all their sources, and relative imports between them (``from ._helpers
import x``) work. Checking a load imports that package once and keeps it;
each run imports its own fresh copy (``fresh=True``) and unloads it when
done, so module-level state (a cache, a counter) never carries from one
run, or ensemble member, to the next.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.util
import itertools
import linecache
import pkgutil
import sys
import traceback
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Any

__all__ = ["Folder", "PluginKinds", "package_name", "read_sources"]

_fresh_ids = itertools.count(1)


@dataclass(frozen=True)
class Folder:
    """One kind of data-repo code: where it lives and what counts as a kind."""

    name: str
    """The data repo folder (``strategies``, ``taxes``)."""
    builtin_package: str
    """The package of the kinds that ship with the simulator."""
    api: str
    """The API's name in messages (``strategy API``)."""
    api_version: Callable[[], int]
    """The API version this code provides (read when importing)."""
    is_kind: Callable[[object], bool]
    """Whether a module attribute is a kind, marked by the API's ``register``."""


def read_sources(data_dir: Path, folder: str) -> dict[str, str]:
    """``<folder>/*.py`` in the data repo: module name to source, sorted.
    No folder means no data-repo code."""
    path = Path(data_dir) / folder
    if not path.is_dir():
        return {}
    return {
        file.stem: file.read_text(encoding="utf-8")
        for file in sorted(path.glob("*.py"))
        if file.stem != "__init__"
    }


def package_name(sources: Mapping[str, str], folder: str = "strategies") -> str:
    """The synthetic package for ``sources``: one name per folder and exact
    set of texts."""
    digest = hashlib.sha256()
    for name in sorted(sources):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(sources[name].encode())
        digest.update(b"\0")
    return f"_{folder}_{digest.hexdigest()[:12]}"


class _SourceFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves one synthetic package's modules from their source text."""

    def __init__(self, package: str, folder: str, sources: Mapping[str, str]) -> None:
        self.package = package
        self.folder = folder
        self.sources = dict(sources)

    def find_spec(self, fullname, path=None, target=None):  # noqa: ARG002
        if fullname == self.package:
            return ModuleSpec(fullname, self, is_package=True)
        prefix, _, name = fullname.rpartition(".")
        if prefix == self.package and name in self.sources:
            return ModuleSpec(fullname, self, origin=f"{self.folder}/{name}.py")
        return None

    def create_module(self, spec):  # noqa: ARG002
        return None

    def exec_module(self, module: types.ModuleType) -> None:
        if module.__name__ == self.package:
            module.__path__ = []
            return
        name = module.__name__.rpartition(".")[2]
        # Named by the sources' hash, not a fresh copy's name: every copy of
        # the same text shares one linecache entry.
        filename = f"{package_name(self.sources, self.folder)}/{self.folder}/{name}.py"
        text = self.sources[name]
        # Let tracebacks and inspect.getsource (parameter docs in `sim
        # strategy kinds`) find the text, which has no file of its own.
        linecache.cache[filename] = (len(text), None, text.splitlines(keepends=True), filename)
        module.__file__ = filename
        code = compile(text, filename, "exec")
        exec(code, module.__dict__)  # noqa: S102 -- the household's own code


def _import_package(
    folder: Folder, sources: Mapping[str, str], *, fresh: bool = False,
) -> tuple[str, list[str]]:
    """Import every module of ``sources``; the package name and any failures.
    ``fresh`` imports a copy of its own, under a name no other call uses."""
    package = package_name(sources, folder.name)
    if fresh:
        package = f"{package}_{next(_fresh_ids)}"
    if not any(isinstance(f, _SourceFinder) and f.package == package for f in sys.meta_path):
        sys.meta_path.append(_SourceFinder(package, folder.name, sources))
    problems: list[str] = []
    version = folder.api_version()
    for name in sorted(sources):
        if name.startswith("_") or not name.isidentifier():
            if not name.isidentifier():
                problems.append(f"{folder.name}/{name}.py: not a Python module name")
            continue
        try:
            module = importlib.import_module(f"{package}.{name}")
        except Exception:  # the module's own error, shown whole
            problems.append(
                f"{folder.name}/{name}.py failed to import:\n"
                + "".join(traceback.format_exc(limit=-3)).rstrip()
            )
            continue
        wanted = getattr(module, "API_VERSION", version)
        if not isinstance(wanted, int) or wanted > version:
            problems.append(
                f"{folder.name}/{name}.py is written for {folder.api} {wanted!r}; "
                f"this code provides {version}"
            )
    return package, problems


def _kinds_in(prefix: str, is_kind: Callable[[object], bool]) -> list[tuple[str, Any]]:
    """``(module, class)`` for every registered class defined in ``prefix``'s modules."""
    out = []
    for module_name, module in sorted(sys.modules.items()):
        if module is None or not module_name.startswith(prefix + "."):
            continue
        for value in vars(module).values():
            if is_kind(value) and value.__module__ == module_name:
                out.append((module_name.rpartition(".")[2], value))
    return out


def builtin_kinds(folder: Folder) -> dict[str, Any]:
    """The kinds that ship with the simulator, by kind."""
    package = importlib.import_module(folder.builtin_package)
    for info in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{folder.builtin_package}.{info.name}")
    return {cls.kind: cls for _module, cls in _kinds_in(folder.builtin_package, folder.is_kind)}


class PluginKinds[K]:
    """Every kind a scenario can name, and where each came from: the
    built-ins, then the data repo's modules (``sources``). Import problems
    are in ``problems`` rather than raised, so a loader can report them all."""

    folder: Folder

    def __init__(self, sources: Mapping[str, str], *, fresh: bool = False) -> None:
        self.sources = dict(sources)
        self.package: str | None = None
        self.problems: list[str] = []
        self.kinds: dict[str, type[K]] = dict(builtin_kinds(self.folder))
        self.origin: dict[str, str] = dict.fromkeys(self.kinds, "built-in")
        if not self.sources:
            return
        package, self.problems = _import_package(self.folder, self.sources, fresh=fresh)
        if fresh:
            self.package = package
        for module, cls in _kinds_in(package, self.folder.is_kind):
            where = f"{self.folder.name}/{module}.py"
            if cls.kind in self.kinds:
                self.problems.append(
                    f"{where}: kind '{cls.kind}' is already defined "
                    f"({self.origin[cls.kind]}); rename it"
                    + (" or promote it" if self.origin[cls.kind] == "built-in" else "")
                )
                continue
            self.kinds[cls.kind] = cls
            self.origin[cls.kind] = where

    def from_data_repo(self, kinds: Mapping[str, object] | set[str] | list[str]) -> bool:
        """Whether any of ``kinds`` comes from the data repo, so a run needs
        the folder's source."""
        return any(self.origin.get(kind, "built-in") != "built-in" for kind in kinds)

    def unload(self) -> None:
        """Forget a fresh package's modules (their classes live on in what
        was made from them). Nothing for a shared one."""
        if self.package is None:
            return
        prefix = self.package + "."
        for name in [n for n in sys.modules if n == self.package or n.startswith(prefix)]:
            del sys.modules[name]
        sys.meta_path[:] = [
            f for f in sys.meta_path if not (isinstance(f, _SourceFinder) and f.package == self.package)
        ]
        self.package = None
