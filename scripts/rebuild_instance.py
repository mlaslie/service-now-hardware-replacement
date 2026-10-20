"""Move the agent to a new (or rebuilt) ServiceNow instance: everything after one paste in ServiceNow.

Developer instances get reclaimed; this turns a rebuild into two commands and one paste:

    uv run --group seed python scripts/rebuild_instance.py bootstrap https://NEW.service-now.com
        Copies one background script to the clipboard (the requester role + the admin tools' OAuth client).
        Paste it in ServiceNow (elevated to security_admin), run it, and copy the SEED_CLIENT=... line it prints.
    uv run --group seed python scripts/rebuild_instance.py run https://NEW.service-now.com
        Does the rest, step by step, and stops at the first failure (resume with --from STEP).

Steps of `run` (each is safe to repeat):
    config     point .env, CLAUDE.local.md (and ../servicenow-mcp) at the instance; .pii-patterns; set aside the old
               seed manifest; note the change in docs/HANDOFF.local.md
    secret     paste the SEED_CLIENT line (hidden input) -> Secret Manager servicenow-seed-oauth
    login      admin sign-in in the browser (sn_seed.py login)
    check      the requester role exists (sn_custom_role.py status); the profile matches (sn_profile.py check)
    seed       demo users, devices and equipment (sn_seed.py set), asset register PDF
    passwords  sets the demo users' passwords; saved only in ~/.config/hw-seed/demo-passwords.json (0600)
    doctor     every agent action for every demo user (sn_doctor.py --as ...)
    deploy     ./scripts/deploy.sh (asks first unless --yes)
    gemini     creates or resets the ServiceNow "Gemini Enterprise" OAuth client with a new secret, creates a
               Gemini Enterprise authorization with it, and points the registered agent at it (or registers the
               agent). The secret goes straight from this process to both systems; it is never shown.
    verify     smoke test of the deployed agent; rebuilds demo/DEMO.local.html

Needs in .env: GOOGLE_CLOUD_PROJECT, and GE_APP_ID (the Gemini Enterprise app id) for the gemini step.
See docs/REBUILD_INSTANCE.md.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "seed"))
sys.path.insert(0, str(ROOT / "scripts"))
from app.config import load_env_file  # noqa: E402

STEPS = ["config", "secret", "login", "check", "seed", "passwords", "doctor", "deploy", "gemini", "verify"]
SECRET_NAME = "servicenow-seed-oauth"
GE_CLIENT_NAME = "Gemini Enterprise"
GE_REDIRECTS = ["https://vertexaisearch.cloud.google.com/console/oauth/default_oauth.html",
                "https://vertexaisearch.cloud.google.com/oauth-redirect"]
PASSWORDS = Path.home() / ".config" / "hw-seed" / "demo-passwords.json"
MCP_REPO = ROOT.parent / "servicenow-mcp"
ROLE_JS = ROOT / "scripts" / "servicenow" / "create_hardware_requester_role.js"
SEED_CLIENT_JS = ROOT / "scripts" / "servicenow" / "create_seed_client.js"
BOOTSTRAP_OUT = ROOT / "seed" / "state" / "bootstrap_instance.js"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step_title(name: str) -> None:
    say(f"\n== {name} " + "=" * max(0, 60 - len(name)))


# --- pure helpers (tested) ---------------------------------------------------------


def normalize_instance(url: str) -> str:
    url = url.strip().rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+", url):
        sys.exit(f"Instance must look like https://dev12345.service-now.com, got {url!r}")
    return url


def instance_label(url: str) -> str:
    """dev12345 for https://dev12345.service-now.com: the name personal-data checks look for."""
    return url.split("//", 1)[1].split(".", 1)[0]


def parse_seed_line(line: str, instance: str) -> tuple[str, str, str]:
    """The SEED_CLIENT=<instance>|<client id>|<secret> line printed by create_seed_client.js."""
    raw = line.strip()
    raw = raw.split("SEED_CLIENT=", 1)[1] if "SEED_CLIENT=" in raw else raw
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3 or not all(parts):
        raise ValueError("expected SEED_CLIENT=<instance>|<client id>|<client secret>")
    got = parts[0].rstrip("/")
    if got != instance:
        raise ValueError(f"the line is for {got}, not {instance}")
    return got, parts[1], parts[2]


def replace_instance(text: str, old: str, new: str) -> str:
    return text.replace(old.rstrip("/"), new.rstrip("/")) if old and old != new else text


def bootstrap_script() -> str:
    # The role first, so the last line printed is the SEED_CLIENT line to copy.
    return ROLE_JS.read_text() + "\n\n" + SEED_CLIENT_JS.read_text()


def authorization_body(instance: str, client_id: str, client_secret: str) -> dict:
    from print_registration import authorization
    urls = authorization(instance, client_id)
    return {"serverSideOauth2": {"clientId": client_id, "clientSecret": client_secret,
                                 "authorizationUri": urls["Authorization URL"], "tokenUri": urls["Token URL"],
                                 "scopes": ["useraccount"]}}


def new_password() -> str:
    # Long and random, with every character class ServiceNow password policies ask for.
    return secrets.token_urlsafe(12).replace("-", "x").replace("_", "y") + "aA7!"


# --- shell helpers -----------------------------------------------------------------


def gcloud() -> str:
    found = shutil.which("gcloud") or os.environ.get("GCLOUD")
    if not found:
        sys.exit("gcloud is not on PATH (or set GCLOUD=/path/to/gcloud).")
    return found


def run(cmd: list[str], **kw) -> None:
    say("$ " + " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, cwd=ROOT, **kw)
    if result.returncode != 0:
        sys.exit(f"Step failed (exit {result.returncode}). Fix it, then rerun with --from to continue.")


def py(*args: str) -> None:
    run([sys.executable, *args])


def gcloud_value(*args: str) -> str:
    return subprocess.run([gcloud(), *args], capture_output=True, text=True, check=True, timeout=60).stdout.strip()


def project() -> str:
    p = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not p:
        sys.exit("Set GOOGLE_CLOUD_PROJECT in .env.")
    return p


def users_file() -> Path:
    own = ROOT / "seed" / "users.json"
    return own if own.exists() else ROOT / "seed" / "users.example.json"


def admin_sn():
    import sn_seed
    return sn_seed.SN(sn_seed.load_client())


# --- steps -------------------------------------------------------------------------


def awake(instance: str) -> bool:
    """An awake instance answers the API with JSON (401 without a token); a hibernating one with a web page."""
    try:
        r = httpx.get(f"{instance}/api/now/table/sys_user?sysparm_limit=1", timeout=30)
    except httpx.HTTPError:
        return False
    return "json" in r.headers.get("content-type", "")


def step_config(instance: str, args) -> None:
    old = os.environ.get("SN_INSTANCE_URL", "").rstrip("/")
    if not awake(instance):
        sys.exit(f"{instance} isn't answering (asleep or not ready). Open it in a browser, then rerun.")
    for path in [ROOT / ".env", ROOT / "CLAUDE.local.md", MCP_REPO / ".env", MCP_REPO / "CLAUDE.local.md"]:
        if path.exists() and old and old != instance and old in path.read_text():
            path.write_text(replace_instance(path.read_text(), old, instance))
            say(f"  updated {path}")
    env = ROOT / ".env"
    if not env.exists() or "SN_INSTANCE_URL=" not in env.read_text():
        sys.exit("Set SN_INSTANCE_URL in .env first (see docs/INSTALL.md step 3).")
    os.environ["SN_INSTANCE_URL"] = instance
    label = instance_label(instance)
    for patterns in [ROOT / ".pii-patterns", MCP_REPO / ".pii-patterns"]:
        if patterns.exists() and label not in patterns.read_text().split():
            with patterns.open("a") as f:
                f.write(("" if patterns.read_text().endswith("\n") else "\n") + label + "\n")
            say(f"  added {label} to {patterns}")
    manifest = ROOT / "seed" / "state" / "manifest.json"
    if manifest.exists():
        seeded = json.loads(manifest.read_text()).get("instance", "").rstrip("/")
        if seeded and seeded != instance:
            aside = manifest.with_name(f"manifest.{instance_label(seeded)}.json")
            manifest.rename(aside)
            say(f"  old seed manifest moved to {aside.name}")
    handoff = ROOT / "docs" / "HANDOFF.local.md"
    if handoff.exists() and old and old != instance:
        note = f"- {date.today()}: ServiceNow instance changed from {old} to {instance} (scripts/rebuild_instance.py)."
        text = handoff.read_text()
        if note not in text:
            heading = "\n## Instance history\n"
            text = text if heading in text else text.rstrip("\n") + "\n" + heading
            handoff.write_text(text.rstrip("\n") + "\n" + note + "\n")
            say("  noted in docs/HANDOFF.local.md")
    say(f"  instance: {instance}" + (" (unchanged)" if old == instance else f" (was {old or 'unset'})"))


def step_secret(instance: str, args) -> None:
    say("Paste the SEED_CLIENT=... line printed by the background script (input is hidden).")
    try:
        _, client_id, client_secret = parse_seed_line(getpass.getpass("SEED_CLIENT line: "), instance)
    except ValueError as exc:
        sys.exit(f"Not stored: {exc}.")
    g, proj = gcloud(), project()
    exists = subprocess.run([g, "secrets", "describe", SECRET_NAME, "--project", proj],
                            capture_output=True, text=True).returncode == 0
    if not exists:
        run([g, "secrets", "create", SECRET_NAME, "--replication-policy=automatic", "--project", proj])
    payload = f"{instance}|{client_id}|{client_secret}"
    result = subprocess.run([g, "secrets", "versions", "add", SECRET_NAME, "--data-file=-", "--project", proj],
                            input=payload, text=True, capture_output=True)
    if result.returncode != 0:
        sys.exit("Could not store the secret: " + result.stderr.strip().splitlines()[-1])
    say(f"  stored a new version of {SECRET_NAME}")


def step_login(instance: str, args) -> None:
    say("A ServiceNow page opens: sign in as admin (if asked) and click Allow.")
    py("seed/sn_seed.py", "login")


def step_check(instance: str, args) -> None:
    py("scripts/sn_custom_role.py", "status")
    py("scripts/sn_profile.py", "check")


def step_seed(instance: str, args) -> None:
    py("seed/sn_seed.py", "set", str(users_file().relative_to(ROOT)))
    py("seed/sn_seed.py", "report")


def step_passwords(instance: str, args) -> None:
    sn = admin_sn()
    store = json.loads(PASSWORDS.read_text()) if PASSWORDS.exists() else {}
    mine = store.setdefault(instance, {})
    for user in json.loads(users_file().read_text()):
        name = user["user_name"]
        row = sn.one("sys_user", f"user_name={name}")
        if not row:
            sys.exit(f"User {name} not found: run the seed step first.")
        mine.setdefault(name, new_password())
        sn._call("PATCH", f"/api/now/table/sys_user/{row['sys_id']}?sysparm_input_display_value=true",
                 json={"user_password": mine[name], "password_needs_reset": "false"})
        say(f"  password set for {name}")
    PASSWORDS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(PASSWORDS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(store, indent=1))
    PASSWORDS.chmod(0o600)
    say(f"  saved in {PASSWORDS} (only you can read it; view with: cat {PASSWORDS})")


def step_doctor(instance: str, args) -> None:
    names = [u["user_name"] for u in json.loads(users_file().read_text())]
    py("scripts/sn_doctor.py", *[a for n in names for a in ("--as", n)])


def step_deploy(instance: str, args) -> None:
    if not args.yes and input("Deploy the agent to Cloud Run now? [y/N] ").strip().lower() != "y":
        say("  skipped (rerun with --from deploy)")
        return
    run(["./scripts/deploy.sh"], env={**os.environ, "SN_INSTANCE_URL": instance})
    probe = MCP_REPO / "scripts" / "deploy_probe.sh"
    if probe.exists():
        say(f"  If you use the MCP probe, redeploy it too: (cd {MCP_REPO} && ./scripts/deploy_probe.sh)")


def discovery_base(location: str) -> str:
    host = "discoveryengine.googleapis.com" if location == "global" else f"{location}-discoveryengine.googleapis.com"
    return f"https://{host}/v1alpha"


def live_agent_card() -> tuple[str, dict]:
    from print_registration import service_url
    url = service_url()
    token = gcloud_value("auth", "print-identity-token")
    r = httpx.get(f"{url}/.well-known/agent-card.json", headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    return url, r.json()


def ensure_ge_client(sn, instance: str) -> tuple[str, str]:
    """The ServiceNow OAuth client Gemini Enterprise signs users in with, with a new secret. Returns (id, secret)."""
    fields = {"type": "client", "inbound_grant_type": "authz_code", "redirect_url": ",".join(GE_REDIRECTS),
              "public_client": "false", "use_pkce": "false", "active": "true", "access_token_lifespan": "1800",
              "refresh_token_lifespan": "8640000", "send_client_credentials_as": "request_body_parameter",
              "scope_restriction_status": "useraccount"}
    row = sn.one("oauth_entity", f"name={GE_CLIENT_NAME}", "sys_id,client_id,redirect_url")
    if row:
        missing = [r for r in GE_REDIRECTS if r not in (row.get("redirect_url") or "")]
        if missing:
            sn._call("PATCH", f"/api/now/table/oauth_entity/{row['sys_id']}",
                     json={"redirect_url": ",".join(filter(None, [row.get("redirect_url"), *missing]))})
        say(f"  ServiceNow client '{GE_CLIENT_NAME}': exists")
    else:
        row = sn._call("POST", "/api/now/table/oauth_entity", json={"name": GE_CLIENT_NAME, **fields})["result"]
        say(f"  ServiceNow client '{GE_CLIENT_NAME}': created")
    scope = sn.one("sys_auth_scope", "name=useraccount")
    if scope and not sn.one("oauth_entity_auth_scope_mapping", f"oauth_entity={row['sys_id']}^auth_scope={scope['sys_id']}"):
        sn._call("POST", "/api/now/table/oauth_entity_auth_scope_mapping",
                 json={"oauth_entity": row["sys_id"], "auth_scope": scope["sys_id"]})
    client_secret = secrets.token_urlsafe(32)
    sn._call("PATCH", f"/api/now/table/oauth_entity/{row['sys_id']}", json={"client_secret": client_secret})
    say("  new client secret set (not shown)")
    return row["client_id"], client_secret


def step_gemini(instance: str, args) -> None:
    app_id = os.environ.get("GE_APP_ID", "")
    if not app_id:
        sys.exit("Set GE_APP_ID in .env: the Gemini Enterprise app id (Gemini Enterprise > Apps > the app's ID).")
    location = os.environ.get("GE_LOCATION", "global")
    proj = project()
    number = gcloud_value("projects", "describe", proj, "--format=value(projectNumber)")
    base = discovery_base(location)
    headers = {"Authorization": f"Bearer {gcloud_value('auth', 'print-access-token')}",
               "x-goog-user-project": proj, "Content-Type": "application/json"}
    service, card = live_agent_card()
    say(f"  agent card from {service}: {card.get('name')}")
    agents_url = f"{base}/projects/{number}/locations/{location}/collections/default_collection/engines/{app_id}/assistants/default_assistant/agents"
    listed = httpx.get(agents_url, headers=headers, timeout=60)
    if listed.status_code != 200:
        sys.exit(f"Can't list the app's agents ({listed.status_code}): {listed.text[:300]}")
    name = os.environ.get("GE_AGENT_NAME") or card["name"]
    agent = next((a for a in listed.json().get("agents", []) if a.get("displayName") == name), None)

    client_id, client_secret = ensure_ge_client(admin_sn(), instance)
    auth_id = f"hardware-replacement-{instance_label(instance)}-{int(time.time())}"
    created = httpx.post(f"{base}/projects/{number}/locations/{location}/authorizations",
                         params={"authorizationId": auth_id}, headers=headers, timeout=60,
                         json={"name": f"projects/{number}/locations/{location}/authorizations/{auth_id}",
                               **authorization_body(instance, client_id, client_secret)})
    if created.status_code != 200:
        sys.exit(f"Creating the Gemini Enterprise authorization failed ({created.status_code}): {created.text[:300]}\n"
                 "The ServiceNow client already has the new secret: rerun with --from gemini.")
    auth_name = created.json()["name"]
    say(f"  Gemini Enterprise authorization created: {auth_id}")

    body = {"displayName": name, "description": card.get("description", ""),
            "a2aAgentDefinition": {"jsonAgentCard": json.dumps(card)},
            "authorizationConfig": {"agentAuthorization": auth_name}}
    if agent:
        old_auth = (agent.get("authorizationConfig") or {}).get("agentAuthorization", "")
        r = httpx.patch(f"{base}/{agent['name']}", headers=headers, json={"name": agent["name"], **body}, timeout=60)
        action = "updated"
    else:
        old_auth = ""
        r = httpx.post(agents_url, headers=headers, json=body, timeout=60)
        action = "registered"
    if r.status_code != 200:
        sys.exit(f"Pointing the agent at the new authorization failed ({r.status_code}): {r.text[:300]}\n"
                 f"In the console, delete '{name}' and add it again with: uv run python scripts/print_registration.py")
    say(f"  agent '{name}' {action}: {r.json().get('name', '').rsplit('/', 1)[-1]} -> {auth_id}")
    if old_auth and old_auth != auth_name:
        say(f"  The previous authorization is unused now. Remove it when you like:\n"
            f"    curl -X DELETE -H \"Authorization: Bearer $(gcloud auth print-access-token)\" "
            f"-H \"x-goog-user-project: {proj}\" {base}/{old_auth}")
    say("  Users authorize again on their next message (the client changed).")


def step_verify(instance: str, args) -> None:
    from print_registration import service_url
    py("scripts/smoke.py", "--url", service_url())
    py("demo/build_demo.py")


RUNNERS = {"config": step_config, "secret": step_secret, "login": step_login, "check": step_check,
           "seed": step_seed, "passwords": step_passwords, "doctor": step_doctor, "deploy": step_deploy,
           "gemini": step_gemini, "verify": step_verify}


def cmd_bootstrap(instance: str | None) -> None:
    script = bootstrap_script()
    BOOTSTRAP_OUT.parent.mkdir(parents=True, exist_ok=True)
    BOOTSTRAP_OUT.write_text(script)
    copied = False
    if shutil.which("pbcopy"):
        copied = subprocess.run(["pbcopy"], input=script, text=True).returncode == 0
    where = f"{instance}/sys.scripts.do" if instance else "System Definition > Scripts - Background"
    say(("Copied to the clipboard" if copied else "Written") + f": {BOOTSTRAP_OUT.relative_to(ROOT)}\n")
    say("In ServiceNow, as admin:")
    say("  1. User menu (top right) > Elevate role > tick security_admin > Update")
    say(f"  2. Open {where}, paste, click Run script")
    say("  3. Copy the last line it prints (SEED_CLIENT=...): the next command asks for it")
    say("\nThen:  uv run --group seed python scripts/rebuild_instance.py run " + (instance or "https://NEW.service-now.com"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bootstrap", help="copy the one ServiceNow background script to the clipboard")
    b.add_argument("instance", nargs="?")
    r = sub.add_parser("run", help="move everything to the instance (after bootstrap)")
    r.add_argument("instance")
    r.add_argument("--from", dest="start", choices=STEPS, default=STEPS[0], help="resume at this step")
    r.add_argument("--only", choices=STEPS, help="run just this step")
    r.add_argument("--yes", action="store_true", help="don't ask before deploying")
    args = ap.parse_args()

    load_env_file()
    if args.cmd == "bootstrap":
        cmd_bootstrap(normalize_instance(args.instance) if args.instance else None)
        return
    instance = normalize_instance(args.instance)
    steps = [args.only] if args.only else STEPS[STEPS.index(args.start):]
    if steps[0] != "config":
        os.environ["SN_INSTANCE_URL"] = instance  # what the config step would have set
    for name in steps:
        step_title(name)
        RUNNERS[name](instance, args)
    say("\nDone." + ("" if args.only else " Next: open the agent in Gemini Enterprise and authorize as a demo user."))


if __name__ == "__main__":
    main()
