"""Runtime configuration, read once from the environment.

`_env` treats a set-but-empty variable as unset: an unfilled `.env` line would
otherwise yield "" and, for SERVICE_URL, an agent card whose url is "/".
"""

import os


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _flag(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes")


PROJECT_ID = _env("GOOGLE_CLOUD_PROJECT", "PROJECT_ID")
REGION = _env("REGION", "us-central1")

# The chat model and the photo model. gemini-3.8-flash is served from `global`.
MODEL = _env("MODEL", "gemini-3.8-flash")
VISION_MODEL = _env("VISION_MODEL", MODEL)
MODEL_LOCATION = _env("GOOGLE_CLOUD_LOCATION", "global")

# Agent Runtime instance that runs no code: it only hosts managed Sessions and
# Memory Bank for this Cloud Run agent. (A2A on Agent Runtime cannot receive the
# Gemini Enterprise user's identity, so the agent itself stays on Cloud Run.)
AGENT_ENGINE_ID = _env("AGENT_ENGINE_ID", "ENGINE_ID")
AGENT_ENGINE_LOCATION = _env("AGENT_ENGINE_LOCATION", "us-central1")

# ADK artifact service bucket (photos).
ARTIFACT_BUCKET = _env("ARTIFACT_BUCKET", _env("PHOTO_BUCKET", f"{PROJECT_ID}-hardware-ticket-photos"))

# The public https URL Cloud Run assigned. The agent card advertises it, and
# Gemini Enterprise stores whatever the card says at registration time.
SERVICE_URL = _env("SERVICE_URL", "http://localhost:8080").rstrip("/")

# ServiceNow instance. The agent calls it with each signed-in user's own token.
SN_INSTANCE_URL = _env("SN_INSTANCE_URL", "https://INSTANCE.service-now.com")

# Tables searched for devices and equipment. Hospitals that keep medical devices
# in their own table (e.g. from a clinical device app) add it here.
ASSET_TABLES = [t.strip() for t in _env("ASSET_TABLES", "alm_hardware").split(",") if t.strip()]

# Model categories that mark medical equipment: repaired on site by its support
# group rather than replaced and shipped.
CLINICAL_CATEGORIES = [c.strip() for c in _env(
    "CLINICAL_CATEGORIES",
    "Imaging Equipment,Patient Care Equipment,Medical Equipment,Medical Device,Clinical Device,Laboratory Equipment",
).split(",") if c.strip()]

# How this company's asset tags look, for the photo model. Plain words.
ASSET_TAG_HINT = _env("ASSET_TAG_HINT", "a sticker with a barcode and a short number: e.g. 123456 on IT "
                      "devices, or a Clinical Engineering control number like CE-10421 on medical equipment")

APP_NAME = "hardware_replacement"
