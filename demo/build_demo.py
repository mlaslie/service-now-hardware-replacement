"""Builds the demo run sheet from the template and your demo users.

The two demo users come from the seed users file (the one with "demo_role": "clinician" and the one
with "demo_role": "it"), the ServiceNow instance from .env, and file paths from this checkout.

    uv run python demo/build_demo.py              # seed/users.json (else the example) -> demo/DEMO.local.html
    uv run python demo/build_demo.py --example    # seed/users.example.json -> demo/DEMO.html (committed)

DEMO.local.html and seed/users.json are not committed: they hold your own users and instance.
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import load_env_file  # noqa: E402

TEMPLATE = ROOT / "demo" / "DEMO.template.html"


def demo_users(users: list[dict]) -> dict[str, dict]:
    picked = {}
    for role in ("clinician", "it"):
        user = next((u for u in users if u.get("demo_role") == role), None)
        if not user:
            sys.exit(f'No user with "demo_role": "{role}" in the users file (one clinician, one it).')
        picked[role] = user
    return picked


def render(users_file: Path, instance: str, repo: str) -> str:
    users = demo_users(json.loads(users_file.read_text()))
    values = {"INSTANCE": instance.rstrip("/"), "REPO": repo}
    for prefix, role in (("C", "clinician"), ("I", "it")):
        u = users[role]
        values |= {f"{prefix}_FIRST": u["first_name"], f"{prefix}_NAME": f"{u['first_name']} {u['last_name']}",
                   f"{prefix}_EMAIL": u["email"], f"{prefix}_USER": u["user_name"],
                   f"{prefix}_TITLE": u.get("title", ""), f"{prefix}_DEPT": u.get("department", "")}
    html = TEMPLATE.read_text()
    for key, value in values.items():
        html = html.replace("{{" + key + "}}", value)
    if "{{" in html:
        sys.exit("Unfilled placeholder left in the template: " + html[html.index("{{"):html.index("{{") + 30])
    return html


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--example", action="store_true", help="build the committed demo/DEMO.html from the example users")
    args = ap.parse_args()
    if args.example:
        out = ROOT / "demo" / "DEMO.html"
        html = render(ROOT / "seed" / "users.example.json", "https://<instance>.service-now.com",
                      "<path to your checkout>")
    else:
        load_env_file()
        users = ROOT / "seed" / "users.json"
        users = users if users.exists() else ROOT / "seed" / "users.example.json"
        out = ROOT / "demo" / "DEMO.local.html"
        html = render(users, os.environ.get("SN_INSTANCE_URL") or "https://<instance>.service-now.com", str(ROOT))
        print(f"users: {users.relative_to(ROOT)}")
    out.write_text(html)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
