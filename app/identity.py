"""Resolves the signed-in person from the ServiceNow token Gemini Enterprise forwards.

Gemini Enterprise sends two credentials to Cloud Run. `x-serverless-authorization`
is the Discovery Engine service agent, consumed by Cloud Run IAM; it names the
same account for every user and must never be read as an identity. The plain
`Authorization` header carries the user's ServiceNow access token, obtained
when they signed in through the agent's authorization resource.

The token is proven by using it: ServiceNow's current-user endpoint only
answers for a valid token, and says whose it is. No audience check is needed,
because a token from any other system simply fails against ServiceNow.
"""

import base64
import json
import logging
from dataclasses import asdict, dataclass, field

from app import servicenow

logger = logging.getLogger(__name__)

# httpx logs request URLs at INFO. Keep it quiet so nothing credential-bearing
# can reach Cloud Logging.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass
class EndUser:
    email: str
    name: str = ""
    sys_id: str = ""
    # "servicenow" when resolved from a valid ServiceNow token, "anonymous" otherwise.
    source: str = "anonymous"
    # Set when resolution failed for a reason the user should hear about.
    problem: str = ""
    profile: dict = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.source == "servicenow"

    def to_state(self) -> dict:
        return {**asdict(self), "verified": self.verified}


def mask_email(email: str | None) -> str:
    """For logs: enough to tell people apart, not the address ("j***@example.com")."""
    if not email:
        return "-"
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}" if domain else f"{name[:1]}***"


def bearer(headers: dict) -> str | None:
    value = headers.get("authorization") or headers.get("Authorization")
    if not value or not value.lower().startswith("bearer "):
        return None
    token = value.split(" ", 1)[1].strip()
    # A Google-issued JWT here is a service identity (Cloud Run invoker), not the user's ServiceNow
    # token. ServiceNow can itself issue JWT access tokens, so check the issuer, not the shape.
    return token if token and not _google_jwt(token) else None


_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com", "googleapis.com")


def _google_jwt(token: str) -> bool:
    parts = token.split(".")
    if len(parts) != 3:
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return True  # three segments but not a readable JWT: not a ServiceNow token either
    issuer = str(payload.get("iss", "")) if isinstance(payload, dict) else ""
    return any(issuer == g or issuer.endswith(g) for g in _GOOGLE_ISSUERS)


async def resolve_end_user(token: str | None) -> EndUser:
    if not token:
        return EndUser(email="", problem="no_token")
    try:
        profile = await servicenow.current_user(token)
    except servicenow.NotSignedIn:
        logger.warning("ServiceNow rejected the forwarded user token")
        return EndUser(email="", problem="token_rejected")
    except servicenow.Hibernating:
        return EndUser(email="", problem="servicenow_hibernating")
    except Exception as exc:  # noqa: BLE001 - never log the token or the exception text
        logger.warning("ServiceNow identity lookup failed: %s", type(exc).__name__)
        return EndUser(email="", problem="servicenow_unavailable")
    logger.info("resolved end user %s (%s)", mask_email(profile["email"] or profile["user_name"]), profile["sys_id"])
    return EndUser(email=profile["email"], name=profile["name"], sys_id=profile["sys_id"],
                   source="servicenow", profile=profile)
