# Fuzz harness

Tests the agent far beyond the hand-written tests, locally, without a real ServiceNow or Gemini
Enterprise, and publishes the result as one HTML report. The plan is `docs/FUZZING_PLAN.md`.

Two layers:

1. **Deterministic** (no model, a couple of minutes): property-based fuzzing with
   [Hypothesis](https://hypothesis.readthedocs.io/) of inbound parsing, cards, the tools against an
   in-memory ServiceNow, the organization profile and the ServiceNow client.
2. **Model** (real Gemini, a few dollars per full pass): generated messy and adversarial conversations
   run through the real agent against the same in-memory ServiceNow, scored by invariants over the
   tool-call trace and the ServiceNow audit log.

`fuzz/` is outside `testpaths`, so the normal `pytest` run is unchanged.

## Running it

```bash
# Deterministic layer (ci profile: derandomized, ~2 minutes), then the report
uv run --group dev pytest fuzz/deterministic -m fuzz -p no:cacheprovider
uv run python fuzz/report.py                       # -> fuzz/report/index.html (stdlib only)

# 10x the examples, new examples every run; failures are kept in fuzz/.hypothesis and replayed first
HYPOTHESIS_PROFILE=deep uv run --group dev pytest fuzz/deterministic -m fuzz -p no:cacheprovider

# One test, or one family
uv run --group dev pytest "fuzz/deterministic/test_tools_stateful.py::TestAccess" -m fuzz -p no:cacheprovider
uv run python fuzz/show_violations.py T5           # the last run's violations, in the terminal

# Model layer (needs gcloud application-default credentials and GOOGLE_CLOUD_PROJECT, env or .env)
uv run python fuzz/model/generate.py --seed 1234 --count 60       # -> fuzz/corpus/expanded.yaml (review it)
uv run python fuzz/model/run.py --workers 6 --budget-minutes 25   # seeds + expanded -> results/model.json
uv run python fuzz/model/run.py --count 3 --workers 2             # smoke run (cents)
uv run python fuzz/model/run.py --filter injection --repeat 3     # flaky check on a subset

# Everything: deterministic -> model (optional) -> report
uv run --group dev python fuzz/run_all.py [--profile deep] [--with-model --workers 6 --budget-minutes 25]
```

The model runner runs one conversation per worker process (the fakes patch module globals), at most
120 s and 20 model calls per conversation, and starts no new conversation once `--budget-minutes` is
used up. Token usage is summed into a cost estimate (`--price-in` / `--price-out`, USD per million
tokens; check current pricing for the configured model).

## Safety

- Nothing reaches a real ServiceNow: `fuzz/fakes.py` sets `SN_INSTANCE_URL` to `https://sn.fuzz.invalid`
  before the app is imported, replaces `servicenow._request` (and `servicenow._http`) with fakes, and
  installs a network guard that refuses any HTTP request (httpx and requests) to a host that isn't
  Google's (the model) or a `.invalid` mock host.
- No deploy, no Gemini Enterprise, no Agent Runtime. No secrets are read or logged.
- Users are Jane Doe, John Doe and Ana Ruiz at example.com (`tests/test_paths.py`).
- Model runs are never part of `pytest`.

## Layout

| Path | What it is |
|---|---|
| `conftest.py` | Hypothesis profiles (`ci`, `deep`, `replay`), the `fuzz` marker, the autouse ServiceNow guard, and the hook that writes `results/deterministic.json` and one `results/violations.jsonl` line per failing invariant (with the falsifying example and the `@reproduce_failure` line). |
| `fakes.py` | Network guard; `AuditedTableAPI` (the FakeTableAPI from `tests/test_paths.py` plus a request log, a `sysparm_query` audit against the queries the app may make, ServiceNow-like 404/400 errors, `^NQ` evaluated like ServiceNow, Ana's tickets with hostile notes, chaos mode); `World` (installs and removes the fakes). |
| `strategies.py` | Hostile strings, payloads, A2A parts and clicks, addresses, photo findings, ticket numbers, JSON values; `examples(ci, deep)`. |
| `invariants.py` | The invariant table (id, title, severity, code location), `InvariantViolation`, `record()`, and the trace checks T1-T8 shared by both layers. Stdlib only. |
| `known.yaml` | Invariant id -> backlog id of an open item tracking it ("known" in the report). Empty means a violation is new. |
| `deterministic/` | `test_inbound_fuzz`, `test_cards_fuzz`, `test_tools_stateful`, `test_profile_fuzz` (+ `profile_smoke.py`, run in a subprocess), `test_servicenow_fuzz`. |
| `model/` | `world.py` (fake world per conversation, M1-M5 checks), `generate.py` (seeded mutations), `run.py` (process pool, budget, cost). |
| `corpus/` | `seeds.yaml` (24 conversations, 12 categories), `payloads.yaml`, `translations.yaml`, `expanded.yaml` (generated). |
| `report.py` | `results/*` -> `report/index.html`. |
| `run_all.py` | Deterministic -> model -> report. |
| `results/`, `report/`, `.hypothesis/` | Output (gitignored). |

## How to read the report

`fuzz/report/index.html` is one self-contained file (light and dark follow the system; the button
switches). From the top:

- **Header**: when each layer ran, the commit (and whether `app/` had local changes), the Hypothesis
  profile and seed, the model, durations, tokens and the cost estimate.
- **Summary tiles**: pass/fail per category (deterministic categories and model categories);
  violations by severity, new vs known (P0 = security or data integrity, P1 = crash or broken UI,
  P2 = quality); the model pass rate (conversations with no hard violation), soft score (share of
  `cases.yaml`-style expectations met) and flaky count (`--repeat` runs that disagreed).
- **Findings**: every violation, grouped by invariant and test (the 24 parametrized cases of one test
  are one finding), most severe first, linked to the failing test.
- **Invariant matrix**: each invariant with its severity, the code it covers, how many tests check it,
  how many violations were recorded, and its backlog id (or what last hardened it).
- **Per-category tables**: every test, sortable by any column. A failing row expands to its
  reproducer: the violation and its data, the **falsifying example** Hypothesis shrank (for state
  machines, the exact sequence of tool calls as code), the **`@reproduce_failure(...)` line** (add it
  as a decorator to the test function to replay that exact example, then remove it), the **rerun
  command**, and the traceback.
- **Model layer**: every conversation; a row expands to the transcript (what the user said, what it
  was sent as in text mode, each tool call with its arguments and result, the reply), the violations,
  the ServiceNow audit log (every request, `!!` marks a query the audit refused) and a rerun command.

## Triage: app defect or harness bug?

A failing property is either a real defect in `app/` or a wrong assumption in the harness. Before
recording a failure as a finding, check the reproducer against what ServiceNow, Gemini Enterprise or
the model can really send. Assumptions the harness makes on purpose (fix the harness, not the app, if
one turns out wrong):

- ServiceNow returns `{"result": [...]}` for a table query and `{"result": {...}}` for one record, rows
  always carry `sys_id`, reference fields may be `{"value", "link"}`, dot-walked fields are text, and
  error bodies come with 4xx/5xx. Anything else is in the separate "odd JSON shapes" tests.
- Field-level ACLs drop fields silently, but never `comments` (chaos mode keeps comments; the app
  relies on it, see `_after_filing`).
- A click can't arrive while the agent is asking "desktop or mobile?": no card is shown before then.
- The model may send `null`, numbers, lists or objects for any tool argument (ADK passes arguments
  through unchecked), so every tool must answer with a status.

One known finding can hide others, because Hypothesis stops a test at its first failure. So each
finding has its own focused test that stays red until it is fixed, and the broad tests step around
exactly that trigger, with a comment naming the focused test:

| Focused test (stays red) | Where the broad tests step around it |
|---|---|
| `test_lone_carriage_return_cannot_add_an_option` | card builders turn a lone `\r` into `\n` |
| `test_rewrite_parts_never_raises_...` (junk context keys) | `test_rewrite_parts_with_string_context_keys` |
| `test_update_request_before_a_problem_is_chosen` | `KNOWN_CRASHES` in `test_tools_stateful.py` |
| `test_free_text_cannot_add_a_ship_to_line`, `test_ship_to_change_reaches_the_real_line` | `_defuse_ship_to` in the hostile-text machine |
| `test_persona_with_a_placeholder_works` | braces removed from the persona in `test_a_profile_that_loads_works` |
| `test_profile_saved_in_another_encoding` | the general profile test writes UTF-8 only |
| `test_query_operators_in_servicenow_settings` | the profile smoke run uses plain category/table names |
| `test_an_empty_json_answer` | the realistic client test never sends an empty JSON body |
| `test_client_with_odd_json_shapes`, `test_client_with_a_wrong_content_encoding` | the realistic client test sends ServiceNow's own shapes, no content-encoding |

When a finding is fixed, remove its guard so the broad tests cover that ground again. When a finding
goes into `docs/BACKLOG.md`, put its id in `known.yaml` so the report shows it as known.

## Adding to it

- A new tool needs a rule in `ToolsMachine` (`test_every_tool_has_a_rule` fails until it has one); its
  wrong-type arguments are covered automatically.
- A new query shape in `app/servicenow.py` must be added to `_query_patterns()` in `fakes.py`, or T3
  flags it: that list is the contract for what the app may ask ServiceNow.
- A new card builder goes into `builders()` in `test_cards_fuzz.py`.
- New seed conversations go into `corpus/seeds.yaml` (format in `model/world.py`).
