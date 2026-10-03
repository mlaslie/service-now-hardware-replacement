"""The whole fuzz pass: deterministic layer -> model layer (optional) -> report.

    uv run --group dev python fuzz/run_all.py                       # deterministic (ci) + report
    uv run --group dev python fuzz/run_all.py --profile deep        # 10x the examples
    uv run --group dev python fuzz/run_all.py --with-model --workers 6 --budget-minutes 25

Exit status: 0 when nothing was violated, 1 otherwise (the report is written either way).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], env: dict | None = None) -> int:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, env={**os.environ, **(env or {})}).returncode


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", choices=["ci", "deep"], default="ci", help="Hypothesis profile")
    ap.add_argument("--with-model", action="store_true", help="also run the model layer (real Gemini, costs money)")
    ap.add_argument("--seed", type=int, default=1234, help="generator seed for the model layer")
    ap.add_argument("--regenerate", action="store_true", help="regenerate corpus/expanded.yaml from --seed")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--budget-minutes", type=float, default=25)
    ap.add_argument("--count", type=int, default=0, help="model layer: only the first N conversations")
    ap.add_argument("-k", default="", help="pytest -k expression for the deterministic layer")
    args = ap.parse_args()

    started = time.time()
    py = sys.executable
    cmd = [py, "-m", "pytest", "fuzz/deterministic", "-m", "fuzz", "-p", "no:cacheprovider", "-q", "-p", "no:logging"]
    if args.k:
        cmd += ["-k", args.k]
    det = run(cmd, {"HYPOTHESIS_PROFILE": args.profile})
    model = 0
    if args.with_model:
        if args.regenerate:
            run([py, "fuzz/model/generate.py", "--seed", str(args.seed), "--count", "60"])
        mcmd = [py, "fuzz/model/run.py", "--seed", str(args.seed), "--workers", str(args.workers),
                "--budget-minutes", str(args.budget_minutes)]
        if args.count:
            mcmd += ["--count", str(args.count)]
        model = run(mcmd)
        if model == 0:
            import json

            data = json.loads((ROOT / "fuzz" / "results" / "model.json").read_text(encoding="utf-8"))
            model = 0 if data["summary"]["passed"] == data["summary"]["conversations"] else 1
    run([py, "fuzz/report.py"])
    print(f"\ndeterministic exit {det}, model exit {model}, {time.time() - started:.0f}s; "
          f"report: {ROOT / 'fuzz' / 'report' / 'index.html'}")
    sys.exit(0 if det == 0 and model == 0 else 1)


if __name__ == "__main__":
    main()
