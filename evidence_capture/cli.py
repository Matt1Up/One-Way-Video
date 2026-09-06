"""Console entry point for the ``evcap`` command.

``evcap <args>`` is exactly ``python3 bin/control_all.py <args>``. The controller
itself stays in bin/ (it is a script, not a library); this shim only finds it,
using the same repo-root logic as the rest of the package (``EVCAP_HOME`` or the
directory that contains ``evidence_capture/``), and replaces the current process
with it so signals and the interactive REPL behave normally.

Installed by ``pip install -e .`` (see pyproject.toml, [project.scripts]).
"""
from __future__ import annotations

import os
import sys

from .paths import BIN, PY, ROOT


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else list(argv)
    script = BIN / "control_all.py"
    if not script.is_file():
        sys.exit(
            f"evcap: controller not found at {script}\n"
            f"       repo root resolved to {ROOT}. Run from an editable install "
            f"(pip install -e .) of a checkout, or set EVCAP_HOME to the checkout."
        )
    os.environ.setdefault("EVCAP_PROG", "evcap")   # so the controller's usage line says "evcap"
    os.execv(str(PY), [str(PY), str(script), *args])


if __name__ == "__main__":  # pragma: no cover
    main()
