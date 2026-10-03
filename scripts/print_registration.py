"""Prints what Gemini Enterprise asks for when you add the agent, ready to paste.

    uv run python scripts/print_registration.py                  # client id prompted, never stored
    uv run python scripts/print_registration.py --client-id ID   # or SN_GE_CLIENT_ID in the environment

1. Agent card JSON (Agents > Add agent > Custom agent via A2A > Agent Card JSON). After a change to the
   `agent` block in config/organization.yaml, edit the agent and paste this again; no need to re-add it.
2. Authorization values (the agent's authorization: ServiceNow OAuth, authorization code, PKCE off).
   The client secret is never printed: paste it from ServiceNow (System OAuth > Application Registry).

Reads .env for SERVICE_URL (set by deploy.sh) or computes it from the project, region and service name.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REDIRECT = "https://vertexaisearch.cloud.google.com/oauth-redirect"


def service_url() -> str:
    """The URL deploy.sh advertises: https://SERVICE-PROJECT_NUMBER.REGION.run.app."""
    if os.environ.get("SERVICE_URL", "").startswith("https://"):
        return os.environ["SERVICE_URL"].rstrip("/")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    region = os.environ.get("REGION", "us-central1")
    service = os.environ.get("SERVICE_NAME", "hardware-replacement-agent")
    try:
        number = subprocess.run(["gcloud", "projects", "describe", project, "--format=value(projectNumber)"],
                                capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        number = "PROJECT_NUMBER"
    return f"https://{service}-{number}.{region}.run.app"


def authorization(instance: str, client_id: str) -> dict[str, str]:
    instance = instance.rstrip("/")
    return {
        "Client ID": client_id,
        "Client secret": "(paste from ServiceNow: System OAuth > Application Registry; never stored here)",
        "Authorization URL": (f"{instance}/oauth_auth.do?response_type=code&client_id={quote(client_id)}"
                              f"&redirect_uri={quote(REDIRECT, safe='')}&scope=useraccount"),
        "Token URL": f"{instance}/oauth_token.do",
        "Scopes": "useraccount",
        "PKCE": "off",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--client-id", default=os.environ.get("SN_GE_CLIENT_ID", ""),
                    help="the ServiceNow OAuth client Gemini Enterprise uses")
    ap.add_argument("--card-only", action="store_true", help="print only the agent card JSON")
    args = ap.parse_args()

    from app import config  # noqa: F401  (loads .env)
    os.environ["SERVICE_URL"] = service_url()
    config.SERVICE_URL = os.environ["SERVICE_URL"]
    from app.server import build_agent_card

    card = build_agent_card().model_dump(mode="json", by_alias=True, exclude_none=True)
    print("== Agent card JSON (paste into Gemini Enterprise)\n")
    print(json.dumps(card, indent=2))
    if args.card_only:
        return
    instance = os.environ.get("SN_INSTANCE_URL", "https://INSTANCE.service-now.com")
    client_id = args.client_id or (input("\nServiceNow OAuth client ID for Gemini Enterprise: ").strip()
                                   if sys.stdin.isatty() else "CLIENT_ID")
    print("\n== Authorization (the agent's ServiceNow sign-in)\n")
    for key, value in authorization(instance, client_id or "CLIENT_ID").items():
        print(f"{key:18} {value}")


if __name__ == "__main__":
    main()
