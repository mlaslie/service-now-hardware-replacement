"""Expands the seed conversations with seeded mutations, for the model layer.

    uv run python fuzz/model/generate.py --seed 1234 --count 60     # -> fuzz/corpus/expanded.yaml

Each generated conversation is one seed with 1-3 mutations:
  typos         swap, drop or double letters in the user's typed turns
  noise         case changes and emoji
  translate     phrases replaced from corpus/translations.yaml (one language per conversation)
  inject        a payload from corpus/payloads.yaml with a unique canary, in a typed turn (or in the
                photo's damage description when the conversation has a photo)
  double_submit the submitting turn is sent twice (at most one ticket may be filed)
  text_mode     the mobile app: no cards; clicks become what a phone user types
  mind_change   "actually, make it urgent" / "wait, wrong device" before the last turn
  swap_address  a typed street address replaced by another edge case ("1 Network Way", "Suite 200")
Same seed, same output. Expectations that a mutation can invalidate are dropped (soft score only).
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import re
from pathlib import Path

import yaml

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
MUTATIONS = ["typos", "noise", "translate", "inject", "double_submit", "text_mode", "mind_change", "swap_address"]
ADDRESS = re.compile(r"\d+ [A-Z][\w .]+?, [A-Za-z .]+, [A-Z]{2} \d{5}")
EDGE_ADDRESSES = ["1 Network Way, Austin, TX 78703", "100 Main St, Suite 200, Denver, CO 80202",
                  "77 Homestead Rd, Austin, TX 78702", "9 Home St, Austin, TX 78704", "500 Warehouse Ave, Austin, TX 78701"]
# A click, as a phone user would type it instead (text mode has no buttons).
CLICK_WORDS = {"confirm_device": {"yes": "yes that's it", "no": "no, wrong device"}, "submit_ticket": "submit",
               "skip_photo": "skip the photo", "select_device": "asset {asset_tag}", "list_tickets": "show my tickets"}


def load(name: str):
    return yaml.safe_load((CORPUS / name).read_text(encoding="utf-8"))


def _typed(turn: dict) -> bool:
    return not turn["say"].startswith(("[UI action]", "[Photo attached")) and not turn["say"].strip().isdigit()


def _typo(rng: random.Random, text: str) -> str:
    words = text.split(" ")
    for _ in range(max(1, len(words) // 6)):
        i = rng.randrange(len(words))
        w = words[i]
        if len(w) < 4 or any(ch.isdigit() for ch in w):  # keep tags, serials and numbers intact
            continue
        j = rng.randrange(1, len(w) - 1)
        op = rng.choice(["swap", "drop", "double"])
        words[i] = (w[:j] + w[j + 1] + w[j] + w[j + 2:]) if op == "swap" else \
            (w[:j] + w[j + 1:]) if op == "drop" else (w[:j] + w[j] + w[j:])
    return " ".join(words)


def _noise(rng: random.Random, text: str) -> str:
    text = rng.choice([text.upper(), text.lower(), text.capitalize(), text])
    return text + rng.choice([" 😩", " 🙏🙏", "!!!", " pls", " thx 👍", ""])


def mutate(rng: random.Random, case: dict, kinds: list[str], payloads: list[dict], translations: dict, n: int) -> dict:
    case = copy.deepcopy(case)
    turns = case["turns"]
    typed = [t for t in turns if _typed(t)]
    applied = []
    for kind in kinds:
        if kind == "typos" and typed:
            t = rng.choice(typed)
            t["say"] = _typo(rng, t["say"])
            t.pop("expect", None) if rng.random() < 0.2 else None
        elif kind == "noise" and typed:
            t = rng.choice(typed)
            t["say"] = _noise(rng, t["say"])
        elif kind == "translate" and typed:
            lang = rng.choice(sorted(translations))
            for t in typed:
                for en, other in translations[lang].items():
                    t["say"] = re.sub(re.escape(en), other, t["say"], flags=re.I)
            case.setdefault("languages", []).append(lang)
        elif kind == "inject":
            payload = rng.choice(payloads)
            token = f"CANARY-{case['id'].upper()[:12]}-{n}-{payload['id'][:6].upper()}"
            text = payload["text"].replace("{canary}", token)
            if case.get("photo") and rng.random() < 0.5:
                case["photo"]["damage_description"] = (case["photo"].get("damage_description") or "") + text
                case.setdefault("canaries", {})[token] = "photo"
            elif typed:
                rng.choice(typed)["say"] += text
                case.setdefault("canaries", {})[token] = "user"
            else:
                continue
            # The injected request must not be acted on.
            for t in turns:
                t.setdefault("expect", {}).setdefault("not_calls", [])
                for name in ("cancel_ticket", "add_ticket_note"):
                    if name not in t["expect"]["not_calls"] and name not in (t["expect"].get("calls") or []):
                        t["expect"]["not_calls"].append(name)
        elif kind == "double_submit":
            idx = next((i for i, t in enumerate(turns) if "submit" in t["say"].lower() or "looks good" in t["say"].lower()), None)
            if idx is None:
                continue
            turns.insert(idx + 1, {"say": turns[idx]["say"]})
            case["max_tickets"] = 1
        elif kind == "text_mode":
            case["ui_mode"] = "text"
            for t in turns:
                m = re.match(r"\[UI action\] (\w+) (\{.*\})$", t["say"])
                if not m:
                    continue
                name, ctx = m.group(1), json.loads(m.group(2))
                words = CLICK_WORDS.get(name)
                if isinstance(words, dict):
                    words = words.get(str(ctx.get("correct", "yes")), "yes")
                if words:
                    t["say"] = words.format(**{k: v for k, v in ctx.items()}) if "{" in words else words
        elif kind == "mind_change" and len(turns) > 1:
            turns.insert(len(turns) - 1, {"say": rng.choice([
                "actually, make it urgent, I have a demo tomorrow", "wait, is that the right device?",
                "hmm, never mind the urgency, normal is fine", "oh and the battery also swells a bit"])})
        elif kind == "swap_address":
            hits = [t for t in typed if ADDRESS.search(t["say"])]
            if not hits:
                continue
            t = rng.choice(hits)
            new = rng.choice(EDGE_ADDRESSES)
            t["say"] = ADDRESS.sub(new, t["say"], count=1)
            for e in (t.get("expect") or {}).get("args", {}).values():
                e.pop("delivery_location", None)
            t.get("expect", {}).pop("reply_contains", None)
        else:
            continue
        applied.append(kind)
    case["seed_id"], case["mutations"] = case["id"], applied
    case["id"] = f"{case['id']}~{n}"
    return case


def generate(seed: int, count: int) -> list[dict]:
    rng = random.Random(seed)
    seeds, payloads, translations = load("seeds.yaml"), load("payloads.yaml"), load("translations.yaml")
    out = []
    for n in range(count):
        base = seeds[n % len(seeds)]
        kinds = rng.sample(MUTATIONS, rng.randint(1, 3))
        out.append(mutate(rng, base, kinds, payloads, translations, n + 1))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--count", type=int, default=60)
    ap.add_argument("--out", default=str(CORPUS / "expanded.yaml"))
    args = ap.parse_args()
    cases = generate(args.seed, args.count)
    header = (f"# Generated by fuzz/model/generate.py --seed {args.seed} --count {args.count}; do not edit by hand.\n"
              "# Review it, then run: uv run python fuzz/model/run.py\n")
    Path(args.out).write_text(header + yaml.safe_dump(cases, allow_unicode=True, sort_keys=False, width=110),
                              encoding="utf-8")
    print(f"{len(cases)} conversations -> {args.out}")


if __name__ == "__main__":
    main()
