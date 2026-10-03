"""Builds the fuzz report: fuzz/results/*.json -> fuzz/report/index.html (one self-contained file).

    uv run python fuzz/report.py            # or: python fuzz/report.py (stdlib only)

Reads results/deterministic.json (pytest hook), results/model.json (model runner, optional),
results/violations.jsonl and known.yaml. Every string from the results is HTML-escaped.
"""

from __future__ import annotations

import datetime
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

FUZZ = Path(__file__).resolve().parent
sys.path.insert(0, str(FUZZ.parent))

from fuzz.invariants import INVARIANTS, RESULTS, known, known_notes, read_violations  # noqa: E402

OUT = FUZZ / "report" / "index.html"
SEVERITIES = ("P0", "P1", "P2")
DET_CATEGORIES = ("inbound", "cards", "tools", "profile", "servicenow")


def e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _load(name: str):
    path = RESULTS / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _pre(text, cls="") -> str:
    return f'<pre class="{cls}">{e(text)}</pre>' if text else ""


def _badge(text, kind) -> str:
    return f'<span class="badge {e(kind)}">{e(text)}</span>'


def _func(nodeid: str) -> str:
    """test function without parameters: the unit findings are grouped by."""
    return re.sub(r"\[.*\]$", "", nodeid.split("::", 1)[-1])


# --- sections ------------------------------------------------------------------------------


def header(det, model) -> str:
    rows = []
    if det:
        rows += [("Deterministic run", det.get("started")), ("Profile", det.get("profile")),
                 ("Commit", f"{det.get('commit')} ({det.get('branch')})" + (" + local app changes" if det.get("dirty") else "")),
                 ("Duration", f"{det.get('duration_s')} s"), ("Hypothesis", det.get("hypothesis")),
                 ("Seed", det.get("seed") or ("derandomized" if det.get("profile") == "ci" else "random"))]
    if model:
        rows += [("Model run", model.get("started")), ("Model", model.get("model")),
                 ("Generator seed", model.get("seed")), ("Model duration", f"{model.get('duration_s')} s"),
                 ("Tokens", f"{model['tokens']['prompt']:,} in / {model['tokens']['output']:,} out"),
                 ("Cost (estimate)", f"${model.get('cost_estimate_usd', 0):.2f}")]
    if not rows:
        rows = [("Results", "none yet: run the deterministic layer first")]
    cells = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in rows)
    built = datetime.datetime.now().isoformat(timespec="seconds")
    return f'<header><h1>Fuzz report</h1><p class="sub">Hardware Replacement agent · built {e(built)}</p><dl class="meta">{cells}</dl></header>'


def tiles(det, model, violations, known_ids) -> str:
    out = []
    tests = (det or {}).get("tests", [])
    by_cat = defaultdict(Counter)
    for t in tests:
        by_cat[t["category"]][t["outcome"]] += 1
    cat_rows = "".join(
        f'<tr><td><a href="#cat-{e(c)}">{e(c)}</a></td><td class="num ok">{by_cat[c]["passed"]}</td>'
        f'<td class="num {"bad" if by_cat[c]["failed"] else ""}">{by_cat[c]["failed"]}</td></tr>'
        for c in DET_CATEGORIES if c in by_cat)
    if model:
        mc = defaultdict(Counter)
        for conv in model.get("conversations", []):
            mc[conv.get("category") or "?"]["passed" if conv.get("passed") else "failed"] += 1
        cat_rows += "".join(
            f'<tr><td><a href="#model">model: {e(c)}</a></td><td class="num ok">{n["passed"]}</td>'
            f'<td class="num {"bad" if n["failed"] else ""}">{n["failed"]}</td></tr>' for c, n in sorted(mc.items()))
    out.append(f'<section class="tile"><h3>Pass / fail per category</h3><table class="mini"><thead><tr><th>Category</th>'
               f'<th>Pass</th><th>Fail</th></tr></thead><tbody>{cat_rows or "<tr><td colspan=3>no results</td></tr>"}'
               f'</tbody></table></section>')
    sev = Counter()
    for v in violations:
        sev[(v.get("severity", "P1"), "known" if known_ids.get(v.get("id")) else "new")] += 1
    sev_rows = "".join(f'<tr><td>{_badge(s, s)}</td><td class="num {"bad" if sev[(s, "new")] else ""}">{sev[(s, "new")]}</td>'
                       f'<td class="num">{sev[(s, "known")]}</td></tr>' for s in SEVERITIES)
    out.append(f'<section class="tile"><h3>Violations by severity</h3><table class="mini"><thead><tr><th>Severity</th>'
               f'<th>New</th><th>Known</th></tr></thead><tbody>{sev_rows}</tbody></table>'
               f'<p class="note">{len(violations)} recorded; {len({(v["id"], _func(v.get("nodeid", ""))) for v in violations})} '
               f'distinct findings (invariant × test).</p></section>')
    if model and model.get("summary"):
        s = model["summary"]
        rate = f"{s['pass_rate'] * 100:.0f}%" if s.get("pass_rate") is not None else "–"
        soft = f"{s['soft_score'] * 100:.0f}%" if s.get("soft_score") is not None else "–"
        body = (f'<div class="big">{e(rate)}</div><p>{e(s["passed"])} of {e(s["conversations"])} conversations passed '
                f'(no hard violation)</p><p>Soft score {e(soft)} · flaky {len(s.get("flaky") or [])} · '
                f'skipped for budget {len(s.get("skipped_for_budget") or [])}</p>')
    else:
        body = '<div class="big">–</div><p>No model run in results/ (uv run python fuzz/model/run.py).</p>'
    out.append(f'<section class="tile"><h3>Model layer</h3>{body}</section>')
    return f'<div class="tiles">{"".join(out)}</div>'


FINDINGS = FUZZ / "findings.yaml"


def fixes(det, model) -> str:
    """fuzz/findings.yaml: what earlier runs found and what was done, with the baseline next to now."""
    try:
        import yaml  # optional: the report still builds without PyYAML, just without this section
        data = yaml.safe_load(FINDINGS.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return ""
    base = data.get("baseline") or {}
    tests = (det or {}).get("tests", [])
    now_det = Counter(t["outcome"] for t in tests)
    s = (model or {}).get("summary") or {}
    bd, bm = base.get("deterministic") or {}, base.get("model") or {}
    compare = (
        f'<table class="mini"><thead><tr><th></th><th>Baseline ({e(base.get("date", ""))}, {e(base.get("commit", ""))})</th>'
        f'<th>Now</th></tr></thead><tbody>'
        f'<tr><td>Deterministic tests failing</td><td class="num bad">{e(bd.get("failed", "–"))} of '
        f'{e((bd.get("failed") or 0) + (bd.get("passed") or 0))}</td>'
        f'<td class="num {"bad" if now_det["failed"] else "ok"}">{now_det["failed"]} of {sum(now_det.values())}</td></tr>'
        f'<tr><td>Model conversations passing</td><td class="num">{e(bm.get("passed", "–"))} of {e(bm.get("total", "–"))}</td>'
        f'<td class="num {"ok" if s and s.get("passed") == s.get("conversations") else "bad"}">'
        f'{e(s.get("passed", "–"))} of {e(s.get("conversations", "–"))}</td></tr></tbody></table>')
    kind = {"fixed": "ok", "harness": "muted", "open": "bad"}
    rows = "".join(
        f'<tr><td>{e(x.get("id"))}</td><td>{_badge(x.get("severity", ""), x.get("severity", ""))}</td>'
        f'<td><code>{e(x.get("invariant"))}</code></td><td>{e(x.get("summary"))}<br><span class="note">Found by '
        f'{e(x.get("found_by"))}</span></td><td>{e(x.get("fix"))}'
        + (f'<br><code>{e(x.get("where"))}</code>' if x.get("where") else "")
        + f'</td><td>{_badge(x.get("status", ""), kind.get(x.get("status"), ""))}</td></tr>'
        for x in data.get("findings") or [])
    counts = Counter(x.get("status") for x in data.get("findings") or [])
    return (f'<section id="fixes"><h2>Findings and fixes</h2><p>{counts["fixed"]} defects found by fuzzing and '
            f'fixed (each with a regression test), {counts["harness"]} harness corrections, {counts["open"]} open.</p>'
            f'{compare}<table class="sortable"><thead><tr><th>ID</th><th>Severity</th><th>Invariant</th>'
            f'<th>What was wrong</th><th>Fix</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></section>')


def matrix(violations, det, model, known_ids, notes) -> str:
    counts = Counter(v["id"] for v in violations)
    tested = Counter()
    for t in (det or {}).get("tests", []):
        for i in t.get("invariants", []):
            tested[i] += 1
    rows = []
    for inv, meta in INVARIANTS.items():
        n = counts.get(inv, 0)
        status = _badge("violated", "bad") if n else (_badge("held", "ok") if tested[inv] or (inv.startswith("M") and model) else
                                                      _badge("not run", "muted"))
        rows.append(f'<tr><td><b>{e(inv)}</b></td><td>{e(meta["title"])}</td><td>{_badge(meta["severity"], meta["severity"])}</td>'
                    f'<td><code>{e(meta["location"])}</code></td><td class="num">{tested[inv]}</td>'
                    f'<td class="num {"bad" if n else ""}">{n}</td><td>{status}</td>'
                    f'<td>{e(known_ids.get(inv) or "")}{"<br>" if known_ids.get(inv) and notes.get(inv) else ""}'
                    f'<span class="note">{e(notes.get(inv, ""))}</span></td></tr>')
    return (f'<section id="matrix"><h2>Invariant matrix</h2><div class="scroll"><table class="sortable"><thead><tr>'
            f'<th>ID</th><th>Invariant</th><th>Sev</th><th>Code</th><th data-type="num">Tests</th>'
            f'<th data-type="num">Violations</th><th>Status</th><th>Backlog</th></tr></thead><tbody>{"".join(rows)}'
            f'</tbody></table></div></section>')


def findings(violations, known_ids) -> str:
    groups: dict[tuple, list] = defaultdict(list)
    for v in violations:
        groups[(v["id"], v.get("source"), _func(v.get("nodeid") or ""))].append(v)
    if not groups:
        return '<section id="findings"><h2>Findings</h2><p>No violations recorded.</p></section>'
    order = sorted(groups.items(), key=lambda kv: (SEVERITIES.index(kv[1][0].get("severity", "P1")), kv[0]))
    rows = []
    for (inv, source, func), items in order:
        first = items[0]
        rows.append(f'<tr><td>{_badge(first.get("severity"), first.get("severity"))}</td><td><b>{e(inv)}</b></td>'
                    f'<td>{_badge("known " + known_ids[inv], "muted") if known_ids.get(inv) else _badge("new", "bad")}</td>'
                    f'<td>{e(source)}</td><td><a href="#{e(_anchor(first.get("nodeid", "")))}">{e(func)}</a></td>'
                    f'<td class="num">{len(items)}</td><td>{e(str(first.get("detail", ""))[:400])}</td></tr>')
    return (f'<section id="findings"><h2>Findings</h2><p class="note">Violations grouped by invariant and test '
            f'(parametrized cases of one test are one finding). Each links to its reproducer.</p><div class="scroll">'
            f'<table class="sortable"><thead><tr><th>Sev</th><th>Inv</th><th>Status</th><th>Layer</th><th>Test</th>'
            f'<th data-type="num">Cases</th><th>First violation</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
            f'</div></section>')


def _anchor(nodeid: str) -> str:
    return "t-" + re.sub(r"[^A-Za-z0-9_-]+", "-", nodeid)[:120]


def category_tables(det, violations) -> str:
    if not det:
        return ""
    by_node = defaultdict(list)
    for v in violations:
        by_node[v.get("nodeid")].append(v)
    out = []
    for cat in DET_CATEGORIES:
        tests = [t for t in det["tests"] if t["category"] == cat]
        if not tests:
            continue
        rows = []
        for t in tests:
            failed = t["outcome"] == "failed"
            vs = by_node.get(t["nodeid"], [])
            inv = ", ".join(t.get("invariants", []))
            name = t["nodeid"].split("::", 1)[-1]
            details = ""
            if failed:
                rep = t.get("reproducer") or {}
                vtext = "".join(f'<p>{_badge(v["id"], v.get("severity", "P1"))} {e(v["detail"])}</p>'
                                + (_pre(json.dumps(v.get("data"), indent=1, ensure_ascii=False, default=str)[:4000], "data")
                                   if v.get("data") else "") for v in vs)
                details = (f'<tr class="details"><td colspan="5"><details><summary>Reproducer</summary>{vtext}'
                           f'<h4>Falsifying example</h4>{_pre(rep.get("falsifying") or "(none printed)")}'
                           + (f'<h4>Replay the exact failure</h4>{_pre(rep["reproduce_failure"])}'
                              '<p class="note">Add this decorator to the test function, run it, then remove it.</p>'
                              if rep.get("reproduce_failure") else "")
                           + f'<h4>Rerun</h4>{_pre(rep.get("rerun"))}'
                           f'<details><summary>Traceback</summary>{_pre(rep.get("traceback"), "tb")}</details>'
                           f'</details></td></tr>')
            badge = _badge(t["outcome"], "ok" if t["outcome"] == "passed" else "bad" if failed else "muted")
            rows.append(f'<tr id="{e(_anchor(t["nodeid"]))}" class="{"fail" if failed else ""}"><td>{e(name)}</td>'
                        f'<td data-v="{e(t["outcome"])}">{badge}</td><td class="num" data-v="{t["duration"]}">{t["duration"]:.2f}</td>'
                        f'<td>{e(inv)}</td><td>{e(" ".join(sorted({v["id"] for v in vs})))}</td></tr>{details}')
        failed_n = sum(t["outcome"] == "failed" for t in tests)
        out.append(f'<section id="cat-{e(cat)}"><h2>{e(cat)} <span class="count">{len(tests) - failed_n} passed · '
                   f'{failed_n} failed</span></h2><div class="scroll"><table class="sortable grouped"><thead><tr>'
                   f'<th>Test</th><th>Outcome</th><th data-type="num">Seconds</th><th>Invariants</th><th>Violated</th>'
                   f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>')
    return "".join(out)


def model_table(model) -> str:
    if not model:
        return ('<section id="model"><h2>Model layer</h2><p>No model run yet. '
                '<code>uv run python fuzz/model/run.py --workers 6 --budget-minutes 25</code></p></section>')
    rows = []
    for c in model.get("conversations", []):
        vs = c.get("violations") or []
        transcript = []
        for t in c.get("turns", []):
            calls = "".join(f'<li><code>{e(x["name"])}</code> {e(json.dumps(x.get("args"), ensure_ascii=False)[:400])}'
                            + (f'<br><span class="note">→ {e(json.dumps(x.get("response"), ensure_ascii=False)[:500])}</span>'
                               if x.get("response") is not None else "") + "</li>" for x in t.get("calls", []))
            problems = "".join(f"<li>{e(p)}</li>" for p in t.get("problems", []))
            transcript.append(
                f'<div class="turn"><p><b>User:</b> {e(t["say"])}'
                + (f' <span class="note">(sent as {e(t["sent"])})</span>' if t.get("sent") else "") + "</p>"
                + (f"<ul>{calls}</ul>" if calls else "<p class='note'>no tool calls</p>")
                + f'{_pre(t.get("reply"), "reply")}'
                + (f'<p class="bad">{e(t["error"])}</p>' if t.get("error") else "")
                + (f'<p class="note">Expectations not met:</p><ul class="note">{problems}</ul>' if problems else "")
                + "</div>")
        vtext = "".join(f'<p>{_badge(v["id"], "bad")} {e(v["detail"])}</p>' for v in vs)
        audit = "\n".join(f'{a.get("method")} {a.get("table")}{"/" + a["sys_id"] if a.get("sys_id") else ""} '
                          f'{a.get("query") or ""} {json.dumps(a.get("json"), ensure_ascii=False) if a.get("json") else ""}'
                          f'{"  !! " + a["query_problem"] if a.get("query_problem") else ""}'
                          f'{"  error: " + a["error"] if a.get("error") else ""}'
                          for a in (c.get("audit") or []) if isinstance(a, dict))
        rerun = f'uv run python fuzz/model/run.py --filter "{c["id"]}" --workers 1'
        tokens = c.get("tokens") or {}
        rows.append(
            f'<tr class="{"" if c.get("passed") else "fail"}"><td>{e(c["id"])}</td><td>{e(c.get("category"))}</td>'
            f'<td>{e(", ".join(c.get("mutations") or []))}</td>'
            f'<td data-v="{int(bool(c.get("passed")))}">{_badge("pass", "ok") if c.get("passed") else _badge("fail", "bad")}</td>'
            f'<td>{e(" ".join(sorted({v["id"] for v in vs})))}</td>'
            f'<td class="num">{(c.get("soft") or {}).get("unmet", 0)}/{(c.get("soft") or {}).get("total", 0)}</td>'
            f'<td class="num" data-v="{c.get("duration_s", 0)}">{c.get("duration_s", 0)}</td>'
            f'<td class="num">{tokens.get("prompt", 0) + tokens.get("output", 0)}</td></tr>'
            f'<tr class="details"><td colspan="8"><details><summary>Transcript, tool calls and audit log</summary>{vtext}'
            f'{"".join(transcript)}<h4>ServiceNow audit log</h4>{_pre(audit or "(no requests)")}'
            f'<h4>Rerun</h4>{_pre(rerun)}{_pre(c.get("traceback"), "tb")}</details></td></tr>')
    s = model.get("summary", {})
    flaky = ", ".join(s.get("flaky") or []) or "none"
    return (f'<section id="model"><h2>Model layer <span class="count">{s.get("passed")} of {s.get("conversations")} passed'
            f'</span></h2><p class="note">Flaky: {e(flaky)}. Soft score = share of cases.yaml-style expectations met; '
            f'hard violations are M1-M5.</p><div class="scroll"><table class="sortable grouped"><thead><tr>'
            f'<th>Conversation</th><th>Category</th><th>Mutations</th><th>Result</th><th>Violated</th>'
            f'<th>Unmet</th><th data-type="num">Seconds</th><th data-type="num">Tokens</th></tr></thead><tbody>'
            f'{"".join(rows)}</tbody></table></div></section>')


CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e3e3e0;--ok:#1f7a3d;--okbg:#e5f4ea;
--bad:#b42318;--badbg:#fdecea;--p0:#b42318;--p1:#b54708;--p2:#475467;--code:#f2f2ef;--accent:#2557d6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141416;--fg:#ececee;--muted:#9a9aa2;
--card:#1d1d20;--line:#2e2e33;--ok:#5cc98a;--okbg:#17301f;--bad:#ff8a80;--badbg:#3a1a18;--p0:#ff8a80;--p1:#f5b041;
--p2:#a9b4c2;--code:#26262a;--accent:#8ab4ff}}
:root[data-theme=dark]{--bg:#141416;--fg:#ececee;--muted:#9a9aa2;--card:#1d1d20;--line:#2e2e33;--ok:#5cc98a;
--okbg:#17301f;--bad:#ff8a80;--badbg:#3a1a18;--p0:#ff8a80;--p1:#f5b041;--p2:#a9b4c2;--code:#26262a;--accent:#8ab4ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,
"Segoe UI",sans-serif}main{max-width:1200px;margin:0 auto;padding:16px}h1{margin:0;font-size:24px}h2{font-size:18px;
margin:32px 0 8px}h3{font-size:14px;margin:0 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
h4{margin:12px 0 4px;font-size:13px}a{color:var(--accent)}.sub,.note{color:var(--muted)}.note{font-size:12px}
header{padding:8px 0 16px;border-bottom:1px solid var(--line)}.meta{display:grid;grid-template-columns:
repeat(auto-fill,minmax(200px,1fr));gap:8px 16px;margin:12px 0 0}.meta dt{font-size:11px;color:var(--muted);
text-transform:uppercase}.meta dd{margin:0;word-break:break-word}.tiles{display:grid;grid-template-columns:
repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:16px}.tile{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px}.big{font-size:36px;font-weight:600}.scroll{overflow-x:auto}table{border-collapse:
collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:8px}th,td{text-align:left;
padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);
cursor:pointer;user-select:none;white-space:nowrap}th.asc::after{content:" ▲"}th.desc::after{content:" ▼"}
table.mini{border:0}table.mini td,table.mini th{padding:3px 6px}.num{text-align:right;font-variant-numeric:tabular-nums}
.ok{color:var(--ok)}.bad{color:var(--bad);font-weight:600}tr.fail td:first-child{border-left:3px solid var(--bad)}
.badge{display:inline-block;padding:0 7px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid
currentColor}.badge.ok{color:var(--ok);background:var(--okbg)}.badge.bad{color:var(--bad);background:var(--badbg)}
.badge.muted{color:var(--muted)}.badge.P0{color:var(--p0)}.badge.P1{color:var(--p1)}.badge.P2{color:var(--p2)}
pre{background:var(--code);padding:8px 10px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;
word-break:break-word;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:420px}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}tr.details>td{background:var(--bg);padding:0 8px}
tr.details details{padding:6px 0}summary{cursor:pointer;color:var(--accent)}.turn{border-left:2px solid var(--line);
padding-left:10px;margin:8px 0}.count{font-size:13px;color:var(--muted);font-weight:400}
.theme{float:right;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;
padding:3px 8px;cursor:pointer}@media (max-width:600px){main{padding:12px 16px}.big{font-size:28px}}
"""

JS = """
document.querySelectorAll('table.sortable').forEach(function(table){
  table.querySelectorAll('th').forEach(function(th, col){
    th.addEventListener('click', function(){
      var body = table.tBodies[0], asc = !th.classList.contains('asc');
      table.querySelectorAll('th').forEach(function(h){h.classList.remove('asc','desc');});
      th.classList.add(asc ? 'asc' : 'desc');
      var grouped = table.classList.contains('grouped'), rows = [], all = Array.from(body.rows);
      for (var i = 0; i < all.length; i++) {
        if (all[i].classList.contains('details')) continue;
        var next = all[i + 1] && all[i + 1].classList.contains('details') ? all[i + 1] : null;
        rows.push([all[i], next]);
      }
      var num = th.dataset.type === 'num';
      function key(r){ var c = r[0].cells[col]; var v = c ? (c.dataset.v || c.textContent) : '';
        return num ? parseFloat(v) || 0 : v.toLowerCase(); }
      rows.sort(function(a, b){ var x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1); });
      rows.forEach(function(r){ body.appendChild(r[0]); if (r[1]) body.appendChild(r[1]); });
    });
  });
});
document.getElementById('theme').addEventListener('click', function(){
  var root = document.documentElement, dark = root.dataset.theme ? root.dataset.theme === 'dark'
    : matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = dark ? 'light' : 'dark';
});
"""


def build() -> str:
    det, model = _load("deterministic.json"), _load("model.json")
    violations = read_violations()
    known_ids, notes = known(), known_notes()
    body = (header(det, model) + tiles(det, model, violations, known_ids) + fixes(det, model)
            + findings(violations, known_ids)
            + matrix(violations, det, model, known_ids, notes) + category_tables(det, violations) + model_table(model))
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Fuzz report</title>"
            f"<style>{CSS}</style></head><body><main><button id=\"theme\" class=\"theme\" type=\"button\">Light / dark"
            f"</button>{body}<p class=\"note\">Rerun: <code>uv run --group dev pytest fuzz/deterministic -m fuzz -p "
            "no:cacheprovider</code> then <code>python fuzz/report.py</code>. How to read this: fuzz/README.md.</p>"
            f"</main><script>{JS}</script></body></html>")


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(), encoding="utf-8")
    print(f"report -> {OUT}")


if __name__ == "__main__":
    main()
