"""The organization profile: everything an organization adapts without editing code.

One YAML file (default `config/organization.yaml`, or the path in the
ORGANIZATION_PROFILE environment variable) holds the agent's name and examples,
the brand colour, the problem choices and their photo and urgency rules, device
rules, service texts and the ServiceNow ticket category. It is validated when
the agent starts; a mistake stops it with a message naming the field.

Deployment settings (project, region, ServiceNow instance...) are not here:
they are environment variables, see `app/config.py` and `.env.example`.

    uv run python -m app.profile                # check config/organization.yaml
    uv run python -m app.profile path/to.yaml   # check another profile
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "config" / "organization.yaml"

Urgency = Literal["low", "normal", "high", "critical"]
URGENCY_ORDER = ("low", "normal", "high", "critical")
PRIORITY_KEYS = ("1", "2", "3", "4")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a misspelled key is an error, not silently ignored


class Issue(_Strict):
    """One problem the user can pick, e.g. "Cracked or broken screen"."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="Stable id, stored on the request")
    label: str = Field(min_length=1, description="Button text")
    photo: Literal["required", "recommended", "optional", "none"] = "none"
    photo_of: str = Field("", description="What to photograph, shown on the photo step")
    min_urgency: Urgency | None = Field(None, description="Urgency is raised to at least this")
    replace: bool = Field(False, description="Personal devices: the problem itself justifies a replacement")
    safety: bool = Field(False, description="Shows the safety text; use with min_urgency: critical")
    recommendation: str = Field("", description="Fixed recommendation for this problem, e.g. remote diagnostics")

    @model_validator(mode="after")
    def _photo_needs_subject(self):
        if self.photo != "none" and not self.photo_of.strip():
            raise ValueError(f"issue {self.key!r}: photo is {self.photo!r} but photo_of is empty")
        return self


class Issues(_Strict):
    personal: list[Issue] = Field(min_length=1, description="For devices assigned to a person")
    equipment: list[Issue] = Field(min_length=1, description="For shared and clinical equipment")

    @model_validator(mode="after")
    def _keys(self):
        seen: dict[str, str] = {}
        for group in ("personal", "equipment"):
            keys = [i.key for i in getattr(self, group)]
            dupes = {k for k in keys if keys.count(k) > 1}
            if dupes:
                raise ValueError(f"issues.{group}: duplicate keys {sorted(dupes)}")
            for issue in getattr(self, group):
                if seen.setdefault(issue.key, issue.label) != issue.label:
                    raise ValueError(f"issue {issue.key!r} has different labels in personal and equipment")
        return self


class Agent(_Strict):
    name: str = "Hardware Replacement"
    persona: str = Field(
        "You are the Hardware Replacement assistant. You help employees report broken or failing work "
        "hardware in as few taps as possible, and follow up on it. Use plain, friendly language and "
        "never IT jargon.",
        description="The opening of the agent's instructions: who it helps and how it talks")
    description: str
    skill_name: str = "Hardware repair and replacement"
    skill_description: str
    examples: list[str] = Field(min_length=1)


class Branding(_Strict):
    primary_color: str = Field("#22873B", pattern=r"^#[0-9A-Fa-f]{6}$",
                               description="Colour of primary (forward) buttons, as #RRGGBB")


class ServiceNowSettings(_Strict):
    ticket_category: str = Field("hardware", min_length=1, description="incident.category for every ticket")
    asset_tables: list[str] = Field(["alm_hardware"], min_length=1, description="Tables searched for devices")


class Devices(_Strict):
    clinical_categories: list[str] = Field(default_factory=list,
                                           description="Model categories that are medical equipment")
    asset_tag_hint: str = Field(min_length=1, description="How asset tags look, for the photo model")
    refresh_years: dict[str, float] = Field(default_factory=dict, description="Replacement age by device type")
    default_refresh_years: float = 4


class Recommendations(_Strict):
    equipment_repair: str = "On-site repair"
    equipment_safety: str = "Remove from service, then on-site repair"
    refresh: str = "Replace with current standard model (refresh eligible, no cost to department)"
    warranty_replacement: str = "Warranty replacement"
    charged_replacement: str = "Replacement, charged to {cost_center}"
    warranty_repair: str = "Warranty repair with a loaner device"
    repair_assessment: str = "Repair assessment; replace if repair is uneconomical"


class Service(_Strict):
    personal_response_targets: dict[str, str] = Field(description="Priority 1-4 -> text, personal devices")
    equipment_response_targets: dict[str, str] = Field(description="Priority 1-4 -> text, equipment")
    safety_text: str = Field(min_length=1)
    recommendations: Recommendations = Field(default_factory=Recommendations)

    @field_validator("personal_response_targets", "equipment_response_targets")
    @classmethod
    def _all_priorities(cls, value: dict[str, str]) -> dict[str, str]:
        value = {str(k): v for k, v in value.items()}
        missing = [p for p in PRIORITY_KEYS if not value.get(p)]
        if missing:
            raise ValueError(f"needs a text for priorities {missing}")
        return value


class Profile(_Strict):
    organization: str = Field(min_length=1)
    agent: Agent
    branding: Branding = Field(default_factory=Branding)
    servicenow: ServiceNowSettings = Field(default_factory=ServiceNowSettings)
    devices: Devices
    issues: Issues
    service: Service

    # --- derived views the code uses -----------------------------------------------

    @property
    def issue_labels(self) -> dict[str, str]:
        return {i.key: i.label for i in self.issues.personal + self.issues.equipment}

    def issue(self, key: str) -> Issue | None:
        return next((i for i in self.issues.personal + self.issues.equipment if i.key == key), None)

    def photo_policy(self, key: str) -> tuple[str | None, str]:
        """(None | "required" | "recommended" | "optional", what to photograph)."""
        issue = self.issue(key)
        if not issue:
            return "optional", "the problem"
        return (None if issue.photo == "none" else issue.photo), issue.photo_of

    def is_safety(self, key: str) -> bool:
        issue = self.issue(key)
        return bool(issue and issue.safety)

    def min_urgency(self, key: str) -> str | None:
        issue = self.issue(key)
        return issue.min_urgency if issue else None

    def keys(self, group: str) -> list[str]:
        return [i.key for i in getattr(self.issues, group)]

    @property
    def safety_keys(self) -> list[str]:
        return list(dict.fromkeys(i.key for i in self.issues.equipment + self.issues.personal if i.safety))


class ProfileError(Exception):
    pass


def _format(err: ValidationError, path: Path) -> str:
    lines = [f"Organization profile {path} is not valid:"]
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"]) or "(top level)"
        lines.append(f"  - {where}: {e['msg']}")
    lines.append("See docs/CONFIGURATION.md for every field.")
    return "\n".join(lines)


def load(path: str | Path | None = None) -> Profile:
    path = Path(path or os.environ.get("ORGANIZATION_PROFILE") or DEFAULT_PATH)
    if not path.is_absolute():
        path = ROOT / path
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise ProfileError(f"Organization profile not found: {path}. Copy config/examples/office.yaml "
                           "to config/organization.yaml, or set ORGANIZATION_PROFILE.") from None
    except yaml.YAMLError as exc:
        raise ProfileError(f"Organization profile {path} is not valid YAML: {exc}") from None
    try:
        return Profile.model_validate(data or {})
    except ValidationError as exc:
        raise ProfileError(_format(exc, path)) from None


@functools.lru_cache(maxsize=1)
def current() -> Profile:
    """The profile the agent runs with, loaded once."""
    return load()


def summary(p: Profile) -> str:
    return "\n".join([
        f"organization: {p.organization}",
        f"agent: {p.agent.name}  |  brand colour {p.branding.primary_color}",
        f"ServiceNow: category '{p.servicenow.ticket_category}', asset tables {p.servicenow.asset_tables}",
        f"personal problems: {', '.join(p.keys('personal'))}",
        f"equipment problems: {', '.join(p.keys('equipment'))}",
        f"medical equipment categories: {', '.join(p.devices.clinical_categories) or 'none'}",
    ])


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else None
    try:
        p = load(path)
    except ProfileError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("OK\n" + summary(p))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
