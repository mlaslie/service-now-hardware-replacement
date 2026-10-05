"""Check the organization profile against your ServiceNow, and import its choice lists.

The agent holds no ServiceNow credentials of its own, so it never reads choice lists at run
time. Instead an admin checks the profile against the instance, and imports a choice list
into it when the organization wants exactly its own options shown.

    uv run python scripts/sn_profile.py check                          # profile vs. ServiceNow
    uv run python scripts/sn_profile.py choices subcategory --dependent hardware
    uv run python scripts/sn_profile.py import-issues subcategory --dependent hardware --group personal

`import-issues` prints YAML for an `issues:` group (paste it into config/organization.yaml):
one problem per active choice, label and order from ServiceNow, servicenow_value = the choice
value, keeping the photo/urgency rules of entries you already have. Map it onto the ticket with
`ticket_fields: {<field>: "{issue_value}"}`.

Signs in like the seed tool (an admin): uv run --group seed python seed/sn_seed.py login
"""

from __future__ import annotations

import argparse
import string
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "seed"))
sys.path.insert(0, str(ROOT))
from app import profile as profile_mod  # noqa: E402


@dataclass
class Finding:
    level: str   # ok | warn | fail
    what: str


def _placeholders(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def choices(sn, field: str, dependent: str = "", table: str = "incident") -> list[dict]:
    """Active English choices for table.field (task fields are inherited), in display order."""
    q = f"nameIN{table},task^element={field}^inactive=false^language=en"
    if dependent:
        q += f"^dependent_value={dependent}"
    rows = sn.query("sys_choice", q, "value,label,sequence,dependent_value", 500)
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: int(r.get("sequence") or 0)):
        if r["value"] not in seen:
            seen.add(r["value"])
            out.append(r)
    return out


def check(sn, p: profile_mod.Profile) -> list[Finding]:
    out: list[Finding] = []
    settings = p.servicenow

    cats = {c["value"] for c in choices(sn, "category")}
    out.append(Finding("ok" if settings.ticket_category in cats else "fail",
                       f"ticket_category {settings.ticket_category!r} " +
                       ("is an incident category" if settings.ticket_category in cats
                        else f"is not an incident category (have: {sorted(cats)})")))

    for table in settings.asset_tables:
        exists = bool(sn.query("sys_db_object", f"name={table}", "name", 1))
        out.append(Finding("ok" if exists else "fail", f"asset table {table!r} " + ("exists" if exists else "not found")))

    model_cats = {r["name"].lower() for r in sn.query("cmdb_model_category", "", "name", 2000)}
    missing = [c for c in p.devices.clinical_categories if c.lower() not in model_cats]
    if p.devices.clinical_categories:
        out.append(Finding("warn" if missing else "ok",
                           "clinical_categories: " + (f"not model categories here: {missing}" if missing
                                                      else "all are model categories")))

    for field, template in settings.ticket_fields.items():
        exists = bool(sn.query("sys_dictionary", f"nameINincident,task^element={field}", "element", 1))
        if not exists:
            out.append(Finding("fail", f"ticket_fields.{field}: no such incident field"))
            continue
        dependent = settings.ticket_category if field == "subcategory" else ""
        valid = {c["value"] for c in choices(sn, field, dependent)}
        if not valid:
            out.append(Finding("ok", f"ticket_fields.{field}: free-text field"))
            continue
        names = _placeholders(template)
        candidates: dict[str, str] = {}
        if not names:
            candidates[template] = "fixed value"
        if "device_value" in names:
            candidates.update({v: f"device_values.{k}" for k, v in settings.device_values.items()})
        if "issue_value" in names:
            candidates.update({(i.servicenow_value or i.key): f"issue {i.key}"
                               for i in p.issues.personal + p.issues.equipment})
        if names - {"device_value", "issue_value"}:
            out.append(Finding("warn", f"ticket_fields.{field}: {sorted(names)} can't be checked ahead of time"))
        bad = {v: src for v, src in candidates.items() if v not in valid}
        out.append(Finding("fail" if bad else "ok", f"ticket_fields.{field}: " + (
            "; ".join(f"{src} -> {v!r} is not a choice" for v, src in bad.items()) + f" (choices: {sorted(valid)})"
            if bad else f"{len(candidates)} value(s), all valid choices")))

    if settings.ship_to_field:
        exists = bool(sn.query("sys_dictionary", f"nameINincident,task^element={settings.ship_to_field}", "element", 1))
        out.append(Finding("ok" if exists else "fail", f"ship_to_field {settings.ship_to_field!r} " +
                           ("exists" if exists else "is not an incident field")))

    for field, values in (("close_code", {"close_codes.resolve": settings.close_codes.resolve,
                                           "close_codes.cancel": settings.close_codes.cancel}),
                          ("hold_reason", {"hold_reason": settings.hold_reason})):
        valid = {c["value"] for c in choices(sn, field)}
        bad = {k: v for k, v in values.items() if valid and v not in valid}
        out.append(Finding("fail" if bad else "ok", f"{field}: " + (
            "; ".join(f"{k} {v!r} is not a choice" for k, v in bad.items()) + f" (choices: {sorted(valid)})"
            if bad else "values valid")))

    for name in ("impact", "urgency"):
        valid = {c["value"] for c in choices(sn, name)}
        used = {getattr(v, name) for v in settings.urgency_matrix.values()}
        bad = sorted(used - valid) if valid else []
        out.append(Finding("fail" if bad else "ok", f"urgency_matrix {name}: " +
                           (f"{bad} not valid (choices: {sorted(valid)})" if bad else "values valid")))
    return out


def import_issues(sn, p: profile_mod.Profile, field: str, dependent: str, group: str) -> str:
    """YAML for one issues group built from a choice list, keeping existing rules."""
    existing = {(i.servicenow_value or i.key): i for i in getattr(p.issues, group)}
    entries = []
    for c in choices(sn, field, dependent):
        old = existing.get(c["value"])
        key = old.key if old else "".join(ch if ch.isalnum() else "_" for ch in c["value"].lower()).strip("_")
        if not key or not key[0].isalpha():
            key = f"choice_{key}"
        entry = old.model_dump(exclude_defaults=True) if old else {}
        entry.update({"key": key, "label": c["label"], "servicenow_value": c["value"]})
        if entry.get("servicenow_value") == key:
            entry.pop("servicenow_value")
        entries.append(entry)
    return yaml.safe_dump({"issues": {group: entries}}, sort_keys=False, allow_unicode=True, width=110)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check the profile against ServiceNow")
    c.add_argument("--profile", help="profile path (default: ORGANIZATION_PROFILE or config/organization.yaml)")
    c = sub.add_parser("choices", help="list a field's choices")
    c.add_argument("field")
    c.add_argument("--dependent", default="")
    c = sub.add_parser("import-issues", help="print an issues group built from a choice list")
    c.add_argument("field")
    c.add_argument("--dependent", default="")
    c.add_argument("--group", choices=["personal", "equipment"], default="personal")
    c.add_argument("--profile")
    args = ap.parse_args()

    import sn_seed  # noqa: E402  (OAuth sign-in; imported late so --help works without it)
    sn = sn_seed.SN(sn_seed.load_client())
    if args.cmd == "choices":
        for r in choices(sn, args.field, args.dependent):
            print(f"{r['value']:24} {r['label']}")
        return
    p = profile_mod.load(getattr(args, "profile", None))
    if args.cmd == "import-issues":
        print(import_issues(sn, p, args.field, args.dependent, args.group))
        return
    findings = check(sn, p)
    for f in findings:
        print(f"  {f.level.upper():4}  {f.what}")
    failed = sum(f.level == "fail" for f in findings)
    print(f"\n{failed} problem(s)." if failed else "\nProfile matches this ServiceNow instance.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
