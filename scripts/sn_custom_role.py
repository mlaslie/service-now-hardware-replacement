"""The u_hardware_requester role: check it, grant it, revoke it.

ServiceNow only lets a session elevated to security_admin create access rules, so the role
itself is created by a background script you run once (see docs/ROLES.md):
    scripts/servicenow/create_hardware_requester_role.js

    uv run python scripts/sn_custom_role.py status            # role and its rules present?
    uv run python scripts/sn_custom_role.py grant jane.doe    # give a user the role
    uv run python scripts/sn_custom_role.py revoke jane.doe
Signs in like the seed tool (an admin): uv run --group seed python seed/sn_seed.py login
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "seed"))
import sn_seed  # noqa: E402

ROLE = "u_hardware_requester"
MARK = "[hardware-agent]"
EXPECTED_RULES = 13


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["status", "grant", "revoke"])
    ap.add_argument("users", nargs="*", metavar="USER_NAME")
    args = ap.parse_args()
    sn = sn_seed.SN(sn_seed.load_client())
    role = sn.one("sys_user_role", f"name={ROLE}", "sys_id,description")
    if not role:
        sys.exit(f"Role {ROLE} not found. Run scripts/servicenow/create_hardware_requester_role.js in ServiceNow "
                 "(Scripts - Background, elevated to security_admin). See docs/ROLES.md.")
    if args.command == "status":
        rules = sn.query("sys_security_acl", f"descriptionSTARTSWITH{MARK}", "name,operation,active", 100)
        print(f"role {ROLE}: present")
        for r in sorted(rules, key=lambda r: (r["name"], r["operation"])):
            print(f"  {r['name']:24} {r['operation']:6} {'active' if r['active'] == 'true' else 'INACTIVE'}")
        holders = sn.query("sys_user_has_role", f"role={role['sys_id']}^inherited=false", "user.user_name", 500)
        print(f"{len(rules)} of {EXPECTED_RULES} rules  |  granted to: "
              + (", ".join(sorted(h.get("user.user_name", "?") for h in holders)) or "nobody"))
        if len(rules) != EXPECTED_RULES:
            sys.exit("Rules missing: run the create script again (elevated).")
        return
    for name in args.users:
        user = sn.one("sys_user", f"user_name={name}", "sys_id")
        if not user:
            print(f"! no user {name}")
            continue
        grant = sn.one("sys_user_has_role", f"user={user['sys_id']}^role={role['sys_id']}^inherited=false")
        if args.command == "grant" and not grant:
            sn.create("sys_user_has_role", {"user": user["sys_id"], "role": role["sys_id"]})
            print(f"granted {ROLE} to {name}")
        elif args.command == "revoke" and grant:
            sn.delete("sys_user_has_role", grant["sys_id"])
            print(f"revoked {ROLE} from {name}")
        else:
            print(f"{name}: nothing to do")


if __name__ == "__main__":
    main()
