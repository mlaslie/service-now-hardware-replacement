"""A terminal stand-in for Gemini Enterprise: talks A2A `message/stream` to the agent.

Sends messages the way Gemini Enterprise does (one contextId per conversation,
photos as inline base64 wrapped in upload sentinels, button clicks as A2UI
A2UI v0.9 action DataParts) and prints cards as text.

    uv run python scripts/chat.py                       # local server, interactive
    CHAT_ID_TOKEN="$(gcloud auth print-identity-token)" uv run python scripts/chat.py --url https://...run.app

In the prompt:
    any text                      send a message
    /photo path/to/image.jpg      attach a photo (optionally followed by text)
    /click N                      click button N on the last card
    /quit
"""

import argparse
import base64
import json
import mimetypes
import os
import sys
import uuid

import httpx


def _walk(by_id: dict, cid: str, buttons: list[dict]) -> None:
    """Prints an A2UI v0.9 card in the order the renderer lays it out."""
    comp = by_id[cid]
    kind = comp.get("component")
    if kind == "Text":
        print(f"  | {comp['text']}")
    elif kind == "Button":
        buttons.append(comp["action"]["event"])
        print(f"  | [{len(buttons)}] {by_id[comp['child']]['text']}")
    elif kind == "Card":
        _walk(by_id, comp["child"], buttons)
    elif kind in ("Column", "Row"):
        kids = comp["children"]
        if kind == "Row" and all(by_id[k].get("component") == "Text" for k in kids):
            print("  | " + " ".join(by_id[k]["text"] for k in kids))
        else:
            for k in kids:
                _walk(by_id, k, buttons)


def render(parts: list[dict], buttons: list[dict]) -> None:
    for part in parts:
        if part.get("kind") == "text":
            print(f"  agent: {part['text']}")
        elif part.get("kind") == "data":
            update = (part.get("data") or {}).get("updateComponents")
            if not update:
                continue
            by_id = {c["id"]: c for c in update["components"]}
            print("  +-- card " + "-" * 50)
            _walk(by_id, "root", buttons)
            print("  +" + "-" * 59)


def stream(client: httpx.Client, url: str, headers: dict, message: dict) -> list[dict]:
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/stream",
            "params": {"message": message}}
    buttons: list[dict] = []
    with client.stream("POST", url, json=body, headers=headers | {"Accept": "text/event-stream"}) as r:
        if r.status_code != 200 or "text/event-stream" not in r.headers.get("content-type", ""):
            print(f"  ! HTTP {r.status_code} {r.headers.get('content-type')}: {r.read()[:500]!r}")
            return buttons
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:]).get("result", {})
            if event.get("kind") == "artifact-update":
                render(event["artifact"]["parts"], buttons)
            elif event.get("kind") == "status-update":
                status = event["status"]
                if status["state"] == "failed":
                    print(f"  ! failed: {status.get('message')}")
                elif status.get("message") and status["state"] != "working" and status["message"].get("role") == "agent":
                    render(status["message"]["parts"], buttons)
    return buttons


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    # Tokens come from the environment, not arguments: arguments end up in shell history and `ps`.
    ap.add_argument("--token", help="deprecated: set CHAT_ID_TOKEN instead")
    ap.add_argument("--user-token", help="deprecated: set CHAT_USER_TOKEN instead")
    args = ap.parse_args()
    id_token = os.environ.get("CHAT_ID_TOKEN") or args.token  # Cloud Run IAM (x-serverless-authorization)
    user_token = os.environ.get("CHAT_USER_TOKEN") or args.user_token  # ServiceNow OAuth token, as GE forwards it
    if args.token or args.user_token:
        print("note: pass tokens as CHAT_ID_TOKEN / CHAT_USER_TOKEN environment variables instead of arguments",
              file=sys.stderr)

    headers = {}
    if id_token:
        headers["X-Serverless-Authorization"] = f"Bearer {id_token}"
    if user_token:
        headers["Authorization"] = f"Bearer {user_token}"
    url = args.url.rstrip("/") + "/"
    context_id = str(uuid.uuid4())
    buttons: list[dict] = []
    print(f"conversation {context_id}\n")

    with httpx.Client(timeout=180) as client:
        while True:
            try:
                line = input("you> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line == "/quit":
                break
            parts: list[dict] = []
            if line.startswith("/click "):
                n = int(line.split()[1])
                if not 0 < n <= len(buttons):
                    print("  ! no such button")
                    continue
                action = buttons[n - 1]
                # As Gemini Enterprise sends an A2UI v0.9 click: the action plus an echo text part.
                parts.append({"kind": "text", "text": "User action triggered."})
                parts.append({"kind": "data", "data": {"action": {
                    "name": action["name"], "sourceComponentId": "button", "context": action.get("context", {})}},
                    "metadata": {"mimeType": "application/json+a2ui"}})
            elif line.startswith("/photo "):
                path, _, text = line[7:].partition(" ")
                mime = mimetypes.guess_type(path)[0] or "image/jpeg"
                name = path.rsplit("/", 1)[-1]
                if text:
                    parts.append({"kind": "text", "text": text})
                parts += [
                    {"kind": "text", "text": f"\n<start_of_user_uploaded_file: {name}>"},
                    {"kind": "file", "file": {"bytes": base64.b64encode(open(path, "rb").read()).decode(),
                                              "mimeType": mime, "name": name}},
                    {"kind": "text", "text": f"<end_of_user_uploaded_file: {name}>\n"},
                ]
            else:
                parts.append({"kind": "text", "text": line})
            message = {"role": "user", "messageId": str(uuid.uuid4()), "contextId": context_id, "parts": parts}
            buttons = stream(client, url, headers, message) or buttons


if __name__ == "__main__":
    sys.exit(main())
