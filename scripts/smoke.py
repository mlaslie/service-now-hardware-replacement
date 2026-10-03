"""Post-deploy smoke test: is the deployed agent up, advertising the right card, and answering?

    uv run python scripts/smoke.py                      # SERVICE_URL from .env (deploy.sh sets the form)
    uv run python scripts/smoke.py --url https://...run.app

Checks, without signing in to ServiceNow and without filing anything:
  1. the agent card is served, its url is the service URL, and it declares A2UI v0.9 and v0.8
  2. a first message gets an answer (the desktop/mobile question, or the sign-in prompt)
Uses your gcloud identity token for Cloud Run IAM (you need run.invoker on the service).
Exit code 0 when everything passed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def identity_token() -> str:
    try:
        return subprocess.run(["gcloud", "auth", "print-identity-token"], capture_output=True, text=True,
                              check=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"Could not get an identity token from gcloud ({type(exc).__name__}). Run gcloud auth login.")


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", help="service URL (default: SERVICE_URL from .env)")
    args = ap.parse_args()
    from app import config  # noqa: F401  (loads .env)
    url = (args.url or os.environ.get("SERVICE_URL", "")).rstrip("/")
    if not url.startswith("https://"):
        sys.exit("Pass --url https://<service>.run.app (or set SERVICE_URL in .env).")
    # The ID token goes in X-Serverless-Authorization: Authorization is the user's ServiceNow token.
    headers = {"X-Serverless-Authorization": f"Bearer {identity_token()}"}
    results = []
    with httpx.Client(timeout=60, headers=headers) as http:
        print(f"Smoke test {url}")
        r = http.get(f"{url}/.well-known/agent-card.json")
        card = r.json() if r.status_code == 200 else {}
        results.append(check("agent card served", r.status_code == 200, f"HTTP {r.status_code}"))
        results.append(check("card url is this service", card.get("url", "").rstrip("/") == url, card.get("url", "-")))
        exts = " ".join(e.get("uri", "") for e in (card.get("capabilities") or {}).get("extensions") or [])
        results.append(check("declares A2UI v0.9 and v0.8", "/v0.9" in exts and "/v0.8" in exts))

        body = {"jsonrpc": "2.0", "id": "smoke", "method": "message/send", "params": {"message": {
            "role": "user", "messageId": str(uuid.uuid4()), "contextId": f"smoke-{uuid.uuid4()}",
            "parts": [{"kind": "text", "text": "hello"}]}}}
        r = http.post(f"{url}/", json=body)
        text = ""
        try:
            result = r.json().get("result") or {}
            parts = (result.get("status") or {}).get("message", {}).get("parts") or result.get("parts") or []
            for a in result.get("artifacts") or []:
                parts += a.get("parts") or []
            text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
        except (ValueError, AttributeError):
            pass
        results.append(check("first message answered", r.status_code == 200 and bool(text.strip()),
                             (text.strip().splitlines() or ["no text"])[0][:80]))
    print("\nAll checks passed." if all(results) else f"\n{results.count(False)} check(s) failed.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
