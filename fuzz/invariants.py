"""The invariants the fuzz harness checks, and where violations are recorded.

Stdlib only: `fuzz/report.py` imports this table without the app's dependencies.

Every check raises `InvariantViolation` (an AssertionError, so pytest and Hypothesis treat it as a
test failure and shrink it). The conftest hook records the final, shrunk failure once in
`fuzz/results/violations.jsonl`; the model runner records its own with `record()`.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

FUZZ = Path(__file__).resolve().parent
RESULTS = FUZZ / "results"
VIOLATIONS = RESULTS / "violations.jsonl"
KNOWN = FUZZ / "known.yaml"

# Severity: P0 = security or data integrity, P1 = crash or broken UI, P2 = quality.
INVARIANTS: dict[str, dict] = {
    "T1": {"title": "Every write targets the acting user's own ticket, or a followed ticket with only comments "
                    "(or a watch list that only adds the user)",
           "severity": "P0", "layer": "tools", "location": "app/tools _apply_changes, follow_ticket; servicenow.update_incident"},
    "T2": {"title": "No result or card shows a ticket the user may not see (except 'already reported' on equipment)",
           "severity": "P0", "layer": "tools", "location": "app/servicenow _mine, my_incident; app/tools _own_ticket"},
    "T3": {"title": "Every ServiceNow query is made only of allowed fields; no ^NQ, no extra clauses",
           "severity": "P0", "layer": "tools", "location": "app/servicenow _identifier, _TICKET_NUMBER, search_assets"},
    "T4": {"title": "At most one ticket per draft; no two tickets share a correlation id",
           "severity": "P0", "layer": "tools", "location": "app/tools submit_ticket, _filing_lock, _file_once"},
    "T5": {"title": "The ship-to line is verbatim: typed by the user, a saved address, or the address on file",
           "severity": "P0", "layer": "tools", "location": "app/tools _resolve_address, _ticket_description, replace_ship_to"},
    "T6": {"title": "A ticket is filed only with a device, a problem, and a photo when required",
           "severity": "P0", "layer": "tools", "location": "app/tools submit_ticket"},
    "T7": {"title": "No exception escapes a tool; every result has status or step",
           "severity": "P1", "layer": "tools", "location": "app/tools servicenow_errors and every tool"},
    "T8": {"title": "A change is reported as done only if ServiceNow stored it",
           "severity": "P0", "layer": "tools", "location": "app/tools _apply_changes, follow_ticket, submit_ticket"},
    "C1": {"title": "Every card is valid A2UI v0.9 (and v0.8 after translation)",
           "severity": "P1", "layer": "cards", "location": "app/cards Card, to_v08"},
    "C2": {"title": "Text mode: numbered lines match the stored options; a number maps to exactly that option; "
                    "hostile notes can't add options",
           "severity": "P1", "layer": "cards", "location": "app/cards to_text, md_escape; app/inbound display_step"},
    "I1": {"title": "Inbound parsing never raises, never returns nothing; one action per click; echo and sentinels removed",
           "severity": "P1", "layer": "inbound", "location": "app/inbound rewrite_parts, parse_user_action, action_context"},
    "I2": {"title": "Display state machine: returns text + ui_* delta; pending message never lost",
           "severity": "P1", "layer": "inbound", "location": "app/inbound display_step"},
    "P1": {"title": "A profile either loads or fails with an error naming the field",
           "severity": "P1", "layer": "profile", "location": "app/profile load, Profile validators"},
    "P2": {"title": "A profile that loads works end to end",
           "severity": "P1", "layer": "profile", "location": "app/profile consumers: tools, cards, agent instruction"},
    "S1": {"title": "The ServiceNow client raises only NotSignedIn / ServiceNowError (401 -> NotSignedIn)",
           "severity": "P1", "layer": "servicenow", "location": "app/servicenow _request and every public function"},
    "S2": {"title": "Row and journal parsing accepts any JSON value",
           "severity": "P1", "layer": "servicenow", "location": "app/servicenow _value, _incident, _asset, parse_journal"},
    "M1": {"title": "T1-T6 and T8 hold across whole model conversations",
           "severity": "P0", "layer": "model", "location": "app/agent + app/tools"},
    "M2": {"title": "Every filed ticket follows a review card for the same draft, with no change in between",
           "severity": "P0", "layer": "model", "location": "app/agent instruction; app/tools submit_ticket"},
    "M3": {"title": "Injection canaries (in user text, photo findings, ServiceNow notes) never reach a write",
           "severity": "P0", "layer": "model", "location": "app/agent instruction; app/tools"},
    "M4": {"title": "Submitting twice files one ticket",
           "severity": "P0", "layer": "model", "location": "app/tools submit_ticket"},
    "M5": {"title": "Replies contain no raw JSON, no literalString, no system-prompt fragments; no turn crashes",
           "severity": "P1", "layer": "model", "location": "app/agent render_staged_card, instruction"},
}

# Test-module category -> the invariants it covers (used when a test doesn't say).
CATEGORIES = {
    "inbound": ["I1", "I2"],
    "cards": ["C1", "C2"],
    "tools": ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"],
    "profile": ["P1", "P2"],
    "servicenow": ["S1", "S2"],
    "model": ["M1", "M2", "M3", "M4", "M5"],
}


class InvariantViolation(AssertionError):
    """A broken invariant. `data` is JSON-able context for the report."""

    def __init__(self, inv_id: str, detail: str, **data):
        self.inv_id, self.detail, self.data = inv_id, detail, data
        extra = ""
        if data:
            try:
                extra = "\n" + json.dumps(data, default=str, ensure_ascii=False, indent=1)[:4000]
            except (TypeError, ValueError):
                extra = "\n" + repr(data)[:4000]
        super().__init__(f"[{inv_id}] {detail}{extra}")


def check(condition, inv_id: str, detail: str, **data) -> None:
    if not condition:
        raise InvariantViolation(inv_id, detail, **data)


def fail(inv_id: str, detail: str, **data):
    raise InvariantViolation(inv_id, detail, **data)


def record(inv_id: str, detail: str, *, source: str, nodeid: str = "", reproducer: str = "",
           rerun: str = "", data: dict | None = None) -> dict:
    """Appends one violation to fuzz/results/violations.jsonl and returns it."""
    meta = INVARIANTS.get(inv_id, {})
    entry = {
        "id": inv_id, "title": meta.get("title", ""), "severity": meta.get("severity", "P1"),
        "detail": detail, "source": source, "nodeid": nodeid, "reproducer": reproducer, "rerun": rerun,
        "data": data or {}, "backlog": known().get(inv_id, ""), "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    with VIOLATIONS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    return entry


def read_violations(path: Path = VIOLATIONS) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").split("\n"):  # not splitlines(): data may hold U+2028
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


_KNOWN_LINE = re.compile(r"^\s*([A-Z][0-9]+)\s*:\s*(.*?)\s*(?:#.*)?$")


def known(path: Path = KNOWN) -> dict[str, str]:
    """fuzz/known.yaml: a flat `ID: backlog-id` mapping (parsed without PyYAML). An empty value
    means nothing known: a violation of that invariant is new."""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _KNOWN_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def known_notes(path: Path = KNOWN) -> dict[str, str]:
    """The comment after each id in fuzz/known.yaml (e.g. "hardened by H0.2")."""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Z][0-9]+)\s*:[^#]*#\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def violation_ids(exc: BaseException) -> list[InvariantViolation]:
    """The InvariantViolations in an exception (or an exception group, or a chained cause)."""
    found: list[InvariantViolation] = []
    seen: set[int] = set()

    def walk(e):
        if e is None or id(e) in seen:
            return
        seen.add(id(e))
        if isinstance(e, InvariantViolation):
            found.append(e)
        for sub in getattr(e, "exceptions", None) or []:
            walk(sub)
        walk(e.__cause__)
        walk(e.__context__)

    walk(exc)
    return found


# --- trace checks (T1-T8), shared by the state machine and the model runner -------------------
# They read the AuditedTableAPI log and tables (fuzz/fakes.py); the app is imported lazily so this
# module stays importable without it.

_TICKET_RE = re.compile(r"^([A-Z]{2,8}\d{4,12})(?::|  \|)")
_OWN_FIELDS = {"comments", "state", "close_code", "close_notes", "hold_reason", "impact", "urgency", "description",
               "watch_list"}


def _watchers(value) -> list[str]:
    return [w for w in str(value or "").split(",") if w]


def _ref(value) -> str:
    return value.get("value", "") if isinstance(value, dict) else str(value or "")


def _caller(row: dict | None) -> str:
    return _ref((row or {}).get("caller_id"))


def check_queries(log: list[dict], start: int = 0, inv: str = "T3") -> None:
    """T3: every query the app sent since `start` is one of the allowed shapes, for the acting user."""
    for entry in log[start:]:
        if entry.get("query_problem"):
            fail(inv, f"ServiceNow query on {entry['table']}: {entry['query_problem']}",
                 table=entry["table"], query=entry["query"], user=entry.get("user"))


def check_writes(log: list[dict], start: int = 0, inv: str = "T1") -> None:
    """T1: every write since `start` targets the acting user's own ticket, or only adds a comment and/or
    the user to the watch list of a ticket they follow or are joining (an open equipment ticket)."""
    for entry in log[start:]:
        method, user = entry["method"], entry.get("user")
        if method not in ("POST", "PATCH", "PUT", "DELETE"):
            continue
        if method == "DELETE":
            fail(inv, "a DELETE was sent", path=entry["path"])
        if not entry["table"]:
            continue  # attachment upload
        if entry["table"] != "incident":
            fail(inv, f"write to table {entry['table']}", path=entry["path"], json=entry["json"])
        fields = dict(entry.get("json") or {})
        if method == "POST":
            check(str(fields.get("caller_id")) == str(user), inv, "ticket filed for someone else",
                  caller_id=fields.get("caller_id"), user=user)
            continue
        before = entry.get("before")
        if before is None:
            continue  # unknown record: ServiceNow refused it
        old = _watchers(before.get("watch_list"))
        if "watch_list" in fields:
            new = _watchers(fields["watch_list"])
            # Following adds the user, unfollowing removes them; nobody else may change.
            check((set(new) ^ set(old)) <= {user}, inv,
                  "watch list change touches someone other than the acting user", before=old, after=new, user=user,
                  number=before.get("number"))
        if _caller(before) == user:
            extra = set(fields) - _OWN_FIELDS
            check(not extra, inv, f"unexpected fields written on own ticket: {sorted(extra)}",
                  number=before.get("number"), fields=sorted(fields))
            continue
        extra = set(fields) - {"comments", "watch_list"}
        check(not extra, inv, f"changed {sorted(extra)} on a ticket the user did not report",
              number=before.get("number"), caller=_caller(before), user=user, fields=fields)
        joining = "watch_list" in fields and str(before.get("state")) in ("1", "2", "3") \
            and bool(_ref(before.get("cmdb_ci")))
        check(user in old or joining, inv, "comment on a ticket the user neither follows nor is joining",
              number=before.get("number"), caller=_caller(before), user=user, watch_list=old)


def shown_numbers(result, card_messages=None) -> set[str]:
    """Ticket numbers a result or a card shows: structured fields only (never echoed free text)."""
    out: set[str] = set()

    def walk(v, key=""):
        if isinstance(v, dict):
            for k, x in v.items():
                if k != "message":
                    walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and key in ("number", "ticket") and re.fullmatch(r"[A-Z]{2,8}\d{4,12}", v):
            out.add(v)

    walk(result)
    for msg in card_messages or []:
        comps = ((msg.get("updateComponents") or {}).get("components") or []) if isinstance(msg, dict) else []
        for c in comps:
            if c.get("component") == "Button":
                n = (((c.get("action") or {}).get("event") or {}).get("context") or {}).get("number")
                if n:
                    out.add(str(n))
            elif c.get("component") == "Text" and str(c.get("variant", "")).startswith("h"):
                m = _TICKET_RE.match(str(c.get("text", "")))
                if m:
                    out.add(m.group(1))
    return out


def check_visibility(user: str, numbers: set[str], incidents: list[dict], allowed: set[str], inv: str = "T2") -> None:
    """T2: every ticket shown is one the user reported or follows, or an open ticket on the
    equipment they are reporting ("already reported")."""
    by_number = {r.get("number"): r for r in incidents}
    for n in sorted(numbers):
        row = by_number.get(n)
        if row is None or n in allowed:
            continue
        visible = _caller(row) == user or user in _watchers(row.get("watch_list"))
        check(visible, inv, f"{n} shown to {user}, who neither reported nor follows it",
              number=n, caller=_caller(row), watch_list=row.get("watch_list"))


def equipment_exceptions(incidents: list[dict], ci: str) -> set[str]:
    """Open tickets on this CI: the "already reported" card may show them to anyone reporting it."""
    if not ci:
        return set()
    return {r.get("number") for r in incidents
            if _ref(r.get("cmdb_ci")) == ci and str(r.get("state")) in ("1", "2", "3")}


def check_unique_tickets(incidents: list[dict], inv: str = "T4") -> None:
    """T4: no two stored tickets share a correlation id (one ticket per draft)."""
    seen: dict[str, list[str]] = {}
    for r in incidents:
        cid = str(r.get("correlation_id") or "")
        if cid:
            seen.setdefault(cid, []).append(r.get("number"))
    for cid, numbers in seen.items():
        check(len(numbers) <= 1, inv, f"{len(numbers)} tickets filed for one draft", correlation_id=cid,
              numbers=numbers)


def ship_to_lines(description: str) -> list[str]:
    return [line[len("Ship to:"):].strip() for line in str(description or "").splitlines()
            if line.startswith("Ship to:")]


def collapse(text) -> str:
    return " ".join(str(text or "").split())


def check_ship_to(description: str, candidates: set[str], where: str, inv: str = "T5") -> None:
    """T5: a description with a ship-to has exactly one Ship-to line, and its address is one the user
    typed, a saved address, or the address on file, verbatim."""
    lines = ship_to_lines(description)
    if not lines:
        return
    check(len(lines) == 1, inv, f"{where}: {len(lines)} 'Ship to:' lines; the service desk can't tell which applies",
          lines=lines, description=str(description)[:1500])
    check(collapse(lines[0]) in {collapse(c) for c in candidates if c}, inv,
          f"{where}: ship-to {lines[0]!r} is not an address the user typed, saved or has on file",
          ship_to=lines[0], candidates=sorted(c for c in candidates if c)[:20])


def check_filed(post_json: dict, inv: str = "T6") -> None:
    """T6: a filed ticket names a device and a problem, and has photo evidence when the problem requires it."""
    from app import cards

    desc = str(post_json.get("description") or "")
    lines = desc.splitlines()
    problem = next((ln[len("Problem: "):] for ln in lines if ln.startswith("Problem: ")), None)
    check(problem not in (None, "", "None"), inv, "ticket filed without a problem", description=desc[:800])
    check(any(ln.startswith("Device: ") and ln.strip() != "Device:" for ln in lines), inv,
          "ticket filed without a device", description=desc[:800])
    keys = [k for k, label in cards.ISSUE_LABELS.items() if label == problem]
    check(bool(keys), inv, f"ticket filed with an unknown problem {problem!r}", description=desc[:800])
    if all(cards.PROFILE.photo_policy(k)[0] == "required" for k in keys):
        check("\nPhoto evidence:" in desc, inv, f"{problem!r} needs a photo, but the ticket has no photo evidence",
              description=desc[:800])


def check_result_shape(tool: str, result, inv: str = "T7") -> None:
    check(isinstance(result, dict) and ("status" in result or "step" in result), inv,
          f"{tool} returned a result without status or step", result=repr(result)[:500])


def check_reported_changes(tool: str, args: dict, result: dict, api, user: str, inv: str = "T8") -> None:
    """T8: whatever a result reports as done is what ServiceNow stored."""
    from app import servicenow

    if not isinstance(result, dict):
        return
    if tool == "submit_ticket" and result.get("status") in ("submitted", "already_submitted"):
        row = api.incident(str(result.get("ticket") or ""))
        check(row is not None, inv, f"submit_ticket reported {result.get('ticket')!r}, which ServiceNow doesn't have",
              result=result)
        if result["status"] == "submitted":
            check(_caller(row) == user, inv, "reported filed, but the stored ticket's caller is someone else",
                  number=row.get("number"), caller=_caller(row))
        return
    if tool == "follow_ticket" and result.get("status") == "ok":
        row = api.incident(str(result.get("number") or args.get("number") or ""))
        check(row is not None, inv, "follow_ticket reported a ticket ServiceNow doesn't have", result=result)
        if result.get("following"):
            check(user in _watchers(row.get("watch_list")), inv,
                  "reported following, but the user isn't on the watch list",
                  number=row.get("number"), watch_list=row.get("watch_list"))
        if result.get("note_added"):
            check(any(collapse(result["note_added"]) in collapse(c) for c in row.get("comments_log") or []), inv,
                  "reported the note added, but ServiceNow has no such comment", number=row.get("number"))
        return
    if tool not in ("update_ticket", "add_ticket_note", "change_ticket_shipping", "request_urgent_handling",
                    "cancel_ticket") or result.get("status") != "ok":
        return
    ticket = result.get("ticket") if isinstance(result.get("ticket"), dict) else {}
    number = ticket.get("number") or str(args.get("number") or "")
    row = api.incident(number)
    check(row is not None, inv, f"{tool} reported changes on {number!r}, which ServiceNow doesn't have")
    for change in result.get("changed") or []:
        label, value = change.get("change"), str(change.get("value"))
        if label == "Status":
            want = servicenow.STATE_CODES.get(value.lower())
            check(str(row.get("state")) == str(want), inv,
                  f"reported status {value}, ServiceNow has state {row.get('state')}", number=number)
        elif label == "Urgency":
            want = servicenow.URGENCY_TO_IMPACT_URGENCY.get(value, (None, None))[1]
            check(str(row.get("urgency")) == str(want), inv,
                  f"reported urgency {value}, ServiceNow has {row.get('urgency')}", number=number)
        elif label == "Ship-to address":
            lines = ship_to_lines(str(row.get("description") or ""))
            check(bool(lines) and all(collapse(ln) == collapse(value) for ln in lines), inv,
                  f"reported ship-to {value!r}, but the ticket's Ship-to line(s) say {lines}", number=number,
                  description=str(row.get("description"))[:1500])
        else:
            fail(inv, f"unknown change {label!r} reported as done", change=change)
    if result.get("note_added"):
        note = collapse(args.get("note") or args.get("reason") or "")
        if note:
            check(any(note[:60] in collapse(c) for c in row.get("comments_log") or []), inv,
                  "reported the note added, but ServiceNow has no such comment", number=number, note=note[:200])
