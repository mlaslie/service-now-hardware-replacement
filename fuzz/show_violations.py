"""Prints fuzz/results/violations.jsonl in a readable form (for the terminal; the report has the same).

    uv run python fuzz/show_violations.py [filter]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fuzz.invariants import read_violations  # noqa: E402


def main() -> None:
    needle = sys.argv[1] if len(sys.argv) > 1 else ""
    for v in read_violations():
        if needle and needle not in json.dumps(v):
            continue
        print(f"[{v['id']} {v['severity']}] {v.get('nodeid') or v.get('source')}\n  {v['detail']}")
        if v.get("data"):
            print("  data: " + json.dumps(v["data"], ensure_ascii=False, default=str)[:1200])
        if v.get("reproducer"):
            print("  reproducer:\n    " + v["reproducer"][:2500].replace("\n", "\n    "))
        print()


if __name__ == "__main__":
    main()
