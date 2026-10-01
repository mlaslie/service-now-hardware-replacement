"""Deployment settings: where the agent runs and what it connects to.

Read from environment variables. For local runs and the helper scripts, a `.env`
file at the repository root is loaded first (variables already set win); Cloud
Run gets the same variables from `scripts/deploy.sh`. Every variable is
explained in `.env.example` and docs/CONFIGURATION.md.

What the agent says and does for an organization (names, colours, problem
choices...) is in the organization profile instead: see `app/profile.py`.

`_env` treats a set-but-empty variable as unset: an unfilled `.env` line would
otherwise yield "" and, for SERVICE_URL, an agent card whose url is "/".
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path = ROOT / ".env") -> None:
    """KEY=value lines, # comments. Never overrides a variable that is already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


# --- Google Cloud ------------------------------------------------------------------
PROJECT_ID = _env("GOOGLE_CLOUD_PROJECT")
REGION = _env("REGION", "us-central1")

# The chat model and the photo model. gemini-3.8-flash is served from `global`.
MODEL = _env("MODEL", "gemini-3.8-flash")
VISION_MODEL = _env("VISION_MODEL", MODEL)
MODEL_LOCATION = _env("GOOGLE_CLOUD_LOCATION", "global")

# Agent Runtime instance that runs no code: it only hosts managed Sessions and
# Memory Bank for this Cloud Run agent. (A2A on Agent Runtime cannot receive the
# Gemini Enterprise user's identity, so the agent itself stays on Cloud Run.)
AGENT_ENGINE_ID = _env("AGENT_ENGINE_ID")
AGENT_ENGINE_LOCATION = _env("AGENT_ENGINE_LOCATION", REGION)

# ADK artifact service bucket (photos).
ARTIFACT_BUCKET = _env("ARTIFACT_BUCKET", _env("PHOTO_BUCKET", f"{PROJECT_ID}-hardware-ticket-photos"))

# The public https URL Cloud Run assigned. The agent card advertises it, and
# Gemini Enterprise stores whatever the card says at registration time.
SERVICE_URL = _env("SERVICE_URL", "http://localhost:8080").rstrip("/")

# --- ServiceNow --------------------------------------------------------------------
# The instance, e.g. https://acme.service-now.com. The agent calls it with each
# signed-in user's own OAuth token; it holds no ServiceNow credentials itself.
SN_INSTANCE_URL = _env("SN_INSTANCE_URL").rstrip("/")

APP_NAME = "hardware_replacement"

# Required to serve requests; checked at start-up by `require()`.
REQUIRED = {
    "GOOGLE_CLOUD_PROJECT": PROJECT_ID,
    "AGENT_ENGINE_ID": AGENT_ENGINE_ID,
    "SN_INSTANCE_URL": SN_INSTANCE_URL,
}


class ConfigError(Exception):
    pass


def missing() -> list[str]:
    return [name for name, value in REQUIRED.items() if not value]


def require() -> None:
    """Stops start-up with a message naming every missing setting."""
    gaps = missing()
    problems = [f"  - {name} is not set" for name in gaps]
    if SN_INSTANCE_URL and not SN_INSTANCE_URL.startswith("https://"):
        problems.append(f"  - SN_INSTANCE_URL must start with https:// (got {SN_INSTANCE_URL!r})")
    if problems:
        raise ConfigError("The agent is missing settings:\n" + "\n".join(problems) +
                          "\nSet them in .env (copy .env.example) or pass them to scripts/deploy.sh. "
                          "See docs/CONFIGURATION.md.")
