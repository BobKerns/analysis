"""Execute notebooks headless and save what they draw, without touching them.

    python .claude/skills/run-analysis/nbrun.py [--timeout SECS] OUT_DIR NOTEBOOK ...

For each notebook: run every cell in a fresh kernel, with the notebook's own
directory as the working directory (as Jupyter would), then write to OUT_DIR
  <name>.html            the executed notebook
  <name>-<nnn>.png       each chart it drew, in order
and print one line per notebook (ok/FAILED, cells, charts) plus one line per
error. A cell that runs longer than --timeout seconds (default 120) fails
the notebook, naming the cell, instead of hanging.

The charts here are drawn into ipywidgets Output widgets after a cell
finishes, so neither their images nor their exceptions are cell outputs:
nbclient reports a cell as fine while its chart raised. Both are read from
the stored widget state instead, and a widget error counts as a failure.

The tracked .ipynb is read, never written: outputs stay out of git (they can
hold real figures). nbstripout strips kernelspec.display_name, which nbformat
requires, so it is put back in memory before executing.

Needs an activated data repo ($ANALYSIS_DATA_DIR), like the notebooks do.
Exit status is 1 if any notebook failed.
"""

from __future__ import annotations

import base64
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError, CellTimeoutError
from nbconvert import HTMLExporter

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_WIDGET_STATE = "application/vnd.jupyter.widget-state+json"


def _outputs(nb) -> Iterator[dict]:
    """Every output: the cells' own, then each Output widget's."""
    for cell in nb.cells:
        yield from cell.get("outputs", [])
    state = nb.metadata.get("widgets", {}).get(_WIDGET_STATE, {}).get("state", {})
    for model in state.values():
        yield from model.get("state", {}).get("outputs", []) or []


def _where(output: dict) -> str:
    """The deepest frame in this repo's src/, or the cell, for an error."""
    frames = [_ANSI.sub("", line).split("\n")[0]
              for line in output.get("traceback", [])
              if "src/analysis" in line or "Cell In" in line]
    return frames[-1].strip() if frames else ""


def run_one(path: Path, out_dir: Path, timeout: int) -> bool:
    nb = nbformat.read(path, as_version=4)
    ks = nb.metadata.setdefault("kernelspec", {})
    ks.setdefault("name", "python3")
    ks.setdefault("display_name", "Python 3")
    ks.setdefault("language", "python")

    client = NotebookClient(
        nb,
        timeout=timeout,
        kernel_name="python3",
        store_widget_state=True,
        resources={"metadata": {"path": str(path.parent.resolve())}},
    )
    errors: list[str] = []
    raised = ""
    try:
        client.execute()
    except CellExecutionError as e:
        # The failing cell's error output says the same, with its frame;
        # this is the fallback if it has none.
        raised = _ANSI.sub("", str(e)).strip().splitlines()[-1][:300]
    except CellTimeoutError:
        cell = next((i for i, c in enumerate(nb.cells)
                     if c.cell_type == "code" and c.get("execution_count") is None), "?")
        source = nb.cells[cell].source.strip().splitlines()[0][:80] if cell != "?" else ""
        errors.append(f"timed out after {timeout}s in cell {cell}: {source}")

    stem = path.stem
    charts = 0
    for output in _outputs(nb):
        png = output.get("data", {}).get("image/png")
        if png:
            charts += 1
            (out_dir / f"{stem}-{charts:03d}.png").write_bytes(base64.b64decode(png))
        if output.get("output_type") == "error":
            where = _where(output)
            errors.append(f"{output.get('ename')}: {_ANSI.sub('', output.get('evalue', ''))[:200]}"
                          + (f"\n         at {where}" if where else ""))
    if raised and not errors:
        errors.append(raised)
    html, _ = HTMLExporter().from_notebook_node(nb)
    (out_dir / f"{stem}.html").write_text(html)

    status = "FAILED" if errors else "ok"
    print(f"{status:6} {path.name}: {len(nb.cells)} cells, {charts} chart(s)")
    for error in errors:
        print(f"       {error}")
    return not errors


def main(argv: list[str]) -> int:
    args = argv[1:]
    timeout = 120
    if args[:1] == ["--timeout"] and len(args) > 1:
        timeout, args = int(args[1]), args[2:]
    if len(args) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    out_dir = Path(args[0])
    out_dir.mkdir(parents=True, exist_ok=True)
    results = [run_one(Path(p), out_dir, timeout) for p in args[1:]]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
