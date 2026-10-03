# Costs

What drives the cost of running the agent, how to estimate it per ticket, and which knobs lower
it. No prices are quoted here: they change and differ by region. Use the official pages:

- Gemini models on Vertex AI: https://cloud.google.com/vertex-ai/generative-ai/pricing
- Cloud Run: https://cloud.google.com/run/pricing
- Agent Runtime Sessions and Memory Bank: https://cloud.google.com/products/gemini-enterprise-agent-platform/pricing
- Cloud Storage: https://cloud.google.com/storage/pricing

Gemini Enterprise and ServiceNow licences are separate and not covered here.

## Cost drivers

| Driver | When | Notes |
|---|---|---|
| **Chat model calls** | Every turn, usually several | One call per model step. Each tool the model calls adds one more call (model → tool → model). Each call sends the instruction, the conversation so far and the tool results. Earlier cards are compacted to one sentence (`compact_card_history`), which keeps input size down. |
| **Vision call** | Once per photo, each time photos are analysed | `app/vision.py`, one structured-output call per photo, on `VISION_MODEL` (default: same as `MODEL`). |
| **Cloud Run** | Always | `deploy.sh` sets `--min-instances=1`, 1 vCPU, 1 GiB. One instance stays warm all month, even with no traffic. |
| **Agent Runtime Sessions** | Every turn | Session reads and event writes for the conversation. |
| **Memory Bank** | Start of a request, and after filing | A memory search on `start_request`, a saved-address lookup on review, memory generation from the conversation after each filed ticket, and a write when a new address is saved. |
| **Cloud Storage** | Per photo | Storage of the photos (a few MB each) and operations. Small. |
| **Cloud Logging** | Every turn | A few lines per turn; usually within the free allotment. |
| **Cloud Build / Artifact Registry** | Per deploy | `gcloud run deploy --source` builds a container image each time. |

What costs nothing extra: the first-turn "Desktop or Mobile App?" question (answered without a
model call), and card rendering (Python).

## Estimating per ticket

Count model calls for a typical conversation, then apply your prices. Placeholders:

| Symbol | Meaning |
|---|---|
| `C` | Chat model calls per ticket (all turns, from first message to filed) |
| `Tin`, `Tout` | Average input and output tokens per chat call |
| `P` | Photos per ticket |
| `Vin`, `Vout` | Input tokens (image + prompt) and output tokens per vision call |
| `$in`, `$out` | Model price per input and output token (from the pricing page; image input may be priced per image or per token) |
| `M` | Memory Bank charges per ticket (one generation, a few searches) |
| `S` | Session charges per ticket (one session, its events) |

```
model cost per ticket   = C × (Tin × $in + Tout × $out) + P × (Vin × $in + Vout × $out)
variable cost per ticket = model cost per ticket + M + S + photo storage
fixed cost per month     = Cloud Run min instance (vCPU-seconds + GiB-seconds at the idle rate,
                           × seconds in a month) + log and image storage
cost per ticket          = variable cost per ticket + fixed cost per month ÷ tickets per month
```

**Rough counts to start from** (from the code, not measured billing):

- "My laptop screen is cracked" with one laptop: `start_request`, `select_device`, `set_issue`,
  then the reply: about 4 chat calls. The photo turn: `analyze_photos` plus the reply, about 2
  calls and 1 vision call. "Yes" on the confirm card, then Submit: about 2 calls each. A
  straightforward ticket is roughly `C = 8–12`, `P = 1`.
- A follow-up question ("any update?"): about 2 chat calls.
- `Tin` grows during a conversation (history), and is dominated by the instruction and tool
  results at the start. Output is short: the instruction asks for one sentence.

**Measure instead of guessing.** File a known number of test tickets (for example 20) in a quiet
project, wait for billing data, and divide each SKU's cost in the Billing report by the number of
tickets. `evals/run.py` drives the real model and gives a feel for call counts without ServiceNow.

## Knobs

| Knob | Saves | Trade-off |
|---|---|---|
| **`--min-instances=0`** in `scripts/deploy.sh` | The always-on instance | Cold starts on the first message after idle. The first reply is slower; test whether Gemini Enterprise waits long enough. |
| **Smaller or cheaper model**: `MODEL` in `.env` | Chat cost per call | Routing quality. Rerun `uv run python evals/run.py --repeat 2` after changing it. |
| **Separate vision model**: `VISION_MODEL` | Vision cost | Label reading accuracy (serials, tags). Set it in `.env`; `deploy.sh` passes it to Cloud Run. |
| **Skip duplicate photos** | Repeated vision calls when the same photo is sent twice | **Not built.** The idea: hash the photo bytes in `inbound.py` and reuse earlier findings. |
| **Fewer steps** | Chat calls | Already done where safe: steps are skipped when known, one sentence can reach review. |
| **Memory Bank off**: `features: {memory: false}` | Memory generation and searches | No recall of preferences or history across conversations. |
| **Photo analysis off**: `features: {photo_analysis: false}` | Vision calls | Photos are attached for the desk unread; no device from the label, no damage check. |
| **Photo lifecycle rule** | Storage | Photos are copied to ServiceNow on submit; see [PRIVACY.md](PRIVACY.md). |
| **Log retention / exclusions** | Logging | Less history for troubleshooting. |

`--concurrency=4` is set because each request may carry a photo of up to ~25 MB in memory.
Raising it packs more users per instance but needs more memory.
