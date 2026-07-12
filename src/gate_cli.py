"""Run a language gate on a file — a ready-made `done_when` for `assign()`.

Exit 0 if the file PASSES its gate, 1 if it FAILS, 2 if skipped/unknown. Lets you use the
fast per-task gates (esp. the C++ heuristic lint, which has no real compile) as the objective
acceptance check in an `assign(...)` loop.

Usage:  python src/gate_cli.py <role> <file>
  e.g.  python src/gate_cli.py cpp_implementer Token.cpp
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: gate_cli.py <role> <file>")
        return 2
    role, fpath = argv[0], argv[1]
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config" / "qwen.json")
                     .read_text(encoding="utf-8"))
    code = Path(fpath).read_text(encoding="utf-8")
    result = gate.run_gate(f"```\n{code}\n```", role, cfg)  # role selects the language
    print(f"gate: {result.status} ({result.check})")
    if result.error_text:
        print(result.error_text)
    return 0 if result.status == "pass" else (2 if result.status == "skipped" else 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
