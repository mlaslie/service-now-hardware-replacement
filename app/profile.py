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
import string
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
    servicenow_value: str = Field("", description="Value written for {issue_value} (default: the key)")
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
        seen: dict[str, dict] = {}
        for group in ("personal", "equipment"):
            keys = [i.key for i in getattr(self, group)]
            dupes = {k for k in keys if keys.count(k) > 1}
            if dupes:
                raise ValueError(f"issues.{group}: duplicate keys {sorted(dupes)}")
            for issue in getattr(self, group):
                # One key is one problem: the agent looks rules up by key, so a key in both lists
                # must mean the same thing in both ("replace" only applies to personal devices).
                rules = issue.model_dump(exclude={"replace"})
                first = seen.setdefault(issue.key, rules)
                if first["label"] != rules["label"]:
                    raise ValueError(f"issue {issue.key!r} has different labels in personal and equipment")
                differ = sorted(k for k in rules if rules[k] != first[k])
                if differ:
                    raise ValueError(f"issue {issue.key!r} has different {differ} in personal and equipment; "
                                     "use the same settings, or a different key")
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


# Fields the agent always sets itself; a profile may not override them.
RESERVED_FIELDS = {"caller_id", "category", "short_description", "description", "impact", "urgency",
                   "correlation_id", "correlation_display", "cmdb_ci", "watch_list", "state"}
# Placeholders a ticket field template may use.
TEMPLATE_KEYS = {"issue_key", "issue_value", "issue_label", "device_value", "device_type", "device_kind",
                 "model_category", "department", "location"}


class ImpactUrgency(_Strict):
    impact: str = Field(pattern=r"^[1-3]$")
    urgency: str = Field(pattern=r"^[1-3]$")


def _default_matrix() -> dict:
    pairs = {"critical": ("1", "1"), "high": ("1", "2"), "normal": ("2", "2"), "low": ("2", "3")}
    return {k: ImpactUrgency(impact=i, urgency=u) for k, (i, u) in pairs.items()}


def check_template(template: str, allowed: set[str], where: str) -> None:
    """A text with {placeholders}: only named ones from `allowed`, no positional `{}` or `{0}`, no
    `{a.b}` / `{a[0]}`, balanced braces. Raises ValueError naming `where`, so a profile that loads
    can always be filled in."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"{where}: {exc} (write a literal brace as {{{{ or }}}})") from None
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if name == "" or name.isdigit():
            raise ValueError(f"{where}: positional placeholder {{{name}}}; use a name: {sorted(allowed) or 'none here'}")
        if not name.isidentifier():
            raise ValueError(f"{where}: placeholder {{{name}}} must be a plain name: {sorted(allowed) or 'none here'}")
        if name not in allowed:
            raise ValueError(f"{where}: unknown placeholder {{{name}}}; use {sorted(allowed) or 'none here'}")
        if spec or conversion:
            raise ValueError(f"{where}: placeholder {{{name}}} can't have a format (':' or '!')")


class ServiceNowSettings(_Strict):
    ticket_category: str = Field("hardware", min_length=1, description="incident.category for every ticket")
    asset_tables: list[str] = Field(["alm_hardware"], min_length=1, description="Tables searched for devices")
    ticket_fields: dict[str, str] = Field(
        default_factory=lambda: {"contact_type": "self-service"},
        description="Extra incident fields on every new ticket: fixed text or {placeholders}")
    device_values: dict[str, str] = Field(
        default_factory=dict, description="Device type or model category -> value for {device_value}")
    urgency_matrix: dict[Urgency, ImpactUrgency] = Field(
        default_factory=_default_matrix, description="Agent urgency -> incident impact and urgency")

    @field_validator("ticket_fields")
    @classmethod
    def _fields(cls, value: dict[str, str]) -> dict[str, str]:
        clash = sorted(set(value) & RESERVED_FIELDS)
        if clash:
            raise ValueError(f"{clash} are set by the agent itself and can't be configured")
        for field_name, template in value.items():
            check_template(template, TEMPLATE_KEYS, field_name)
        return value

    @field_validator("urgency_matrix")
    @classmethod
    def _every_urgency(cls, value):
        missing = [u for u in URGENCY_ORDER if u not in value]
        if missing:
            raise ValueError(f"needs impact/urgency for {missing}")
        return value

    def device_value(self, device_type: str, model_category: str = "") -> str:
        lookup = {k.lower(): v for k, v in self.device_values.items()}
        return lookup.get((device_type or "").lower()) or lookup.get((model_category or "").lower(), "")

    def render_fields(self, values: dict[str, str]) -> dict[str, str]:
        """The extra ticket fields with placeholders filled in; a field that renders empty is left out."""
        out = {}
        for field_name, template in self.ticket_fields.items():
            text = template.format_map({k: values.get(k, "") or "" for k in TEMPLATE_KEYS}).strip()
            if text:
                out[field_name] = text
        return out


class Devices(_Strict):
    clinical_categories: list[str] = Field(default_factory=list,
                                           description="Model categories that are medical equipment")
    asset_tag_hint: str = Field(min_length=1, description="How asset tags look, for the photo model")
    refresh_years: dict[str, float] = Field(default_factory=dict, description="Replacement age by device type")
    default_refresh_years: float = 4
    category_types: dict[str, Literal["laptop", "desktop", "monitor", "phone", "tablet", "other"]] = Field(
        default_factory=dict, description="Model category -> device type, when the category says it "
        "(otherwise the type is guessed from the category and model name)")


class Recommendations(_Strict):
    equipment_repair: str = "On-site repair"
    equipment_safety: str = "Remove from service, then on-site repair"
    refresh: str = "Replace with current standard model (refresh eligible, no cost to department)"
    warranty_replacement: str = "Warranty replacement"
    charged_replacement: str = "Replacement, charged to {cost_center}"
    warranty_repair: str = "Warranty repair with a loaner device"
    repair_assessment: str = "Repair assessment; replace if repair is uneconomical"

    @model_validator(mode="after")
    def _placeholders(self):
        for name, text in self:
            check_template(text, {"cost_center"} if name == "charged_replacement" else set(), name)
        return self


class Service(_Strict):
    personal_response_targets: dict[str, str] = Field(description="Priority 1-4 -> text, personal devices")
    equipment_response_targets: dict[str, str] = Field(description="Priority 1-4 -> text, equipment")
    safety_text: str = Field(min_length=1)
    safety_event_url: str = Field("", description="Where staff report a safety event (patient or staff harm); "
                                  "shown with safety concerns. Empty = not shown")

    @field_validator("safety_event_url")
    @classmethod
    def _https(cls, value: str) -> str:
        if value and not value.startswith("https://"):
            raise ValueError("must start with https://")
        return value
    recommendations: Recommendations = Field(default_factory=Recommendations)

    @field_validator("personal_response_targets", "equipment_response_targets")
    @classmethod
    def _all_priorities(cls, value: dict[str, str]) -> dict[str, str]:
        value = {str(k): v for k, v in value.items()}
        missing = [p for p in PRIORITY_KEYS if not value.get(p)]
        if missing:
            raise ValueError(f"needs a text for priorities {missing}")
        return value


class Features(_Strict):
    """Switch parts of the agent off. Everything is on by default (today's behaviour)."""
    equipment_reporting: bool = Field(True, description="Report shared and clinical equipment, not just own devices")
    photo_analysis: bool = Field(True, description="Read photos with the model (label, damage); off = attached as-is")
    follow_open_tickets: bool = Field(True, description="Offer to follow an open ticket on the same equipment")
    saved_addresses: bool = Field(True, description="Remember and offer permanent delivery addresses")
    memory: bool = Field(True, description="Recall preferences and history across conversations (Memory Bank)")
    ask_display_mode: bool = Field(True, description="Ask desktop or mobile on the first turn; off = cards only")


class RequesterChanges(_Strict):
    """What requesters may change on their own tickets themselves. A change that is off is added to
    the ticket as a note asking the service desk to make it."""
    urgency: bool = True
    status: bool = True
    ship_to: bool = True
    cancel: bool = True


class Profile(_Strict):
    organization: str = Field(min_length=1)
    agent: Agent
    branding: Branding = Field(default_factory=Branding)
    servicenow: ServiceNowSettings = Field(default_factory=ServiceNowSettings)
    devices: Devices
    issues: Issues
    service: Service
    features: Features = Field(default_factory=Features)
    requester_changes: RequesterChanges = Field(default_factory=RequesterChanges)

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
