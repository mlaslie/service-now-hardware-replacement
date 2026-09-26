"""Reads a hardware photo: what the device is, its label data, and any damage.

One call answers both questions a photo can settle, because users rarely know
which kind of picture we wanted: a shot of a cracked screen often shows the
asset sticker too, and a label close-up can still reveal a swollen chassis.
"""

import logging
from enum import Enum
from functools import lru_cache

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app import config

logger = logging.getLogger(__name__)


class ImageKind(str, Enum):
    label = "label"
    damage = "damage"
    both = "both"
    device = "device"
    unrelated = "unrelated"


class Severity(str, Enum):
    none = "none"
    minor = "minor"
    moderate = "moderate"
    severe = "severe"


class PhotoFindings(BaseModel):
    image_kind: ImageKind = Field(description="label = asset/serial sticker close-up; damage = shows a fault; both; device = device with neither; unrelated = not hardware")
    device_type: str = Field(default="", description="laptop, desktop, monitor, phone, tablet, keyboard, mouse, dock, headset, printer, medical equipment (imaging systems, infusion pumps, patient monitors, hospital beds, ventilators...), other")
    manufacturer: str = ""
    model: str = Field(default="", description="Marketing model name, e.g. 'ThinkPad X1 Carbon Gen 11'")
    part_number: str = Field(default="", description="Part/model/MTM number exactly as printed")
    serial_number: str = Field(default="", description="Serial exactly as printed, no spaces")
    asset_tag: str = Field(default="", description="Company asset tag exactly as printed next to its barcode")
    damage_present: bool = False
    damage_description: str = Field(default="", description="One plain-language sentence a non-technical person understands")
    damage_severity: Severity = Severity.none
    issue_category: str = Field(default="", description="For personal devices one of: cracked_screen, physical_damage, liquid_damage, battery, keyboard_trackpad, wont_power_on, other. For medical or shared equipment one of: error_alarm (an error or alarm on screen), damaged, safety_concern, not_working, other. Empty if no fault is visible")
    supports_replacement: bool = Field(default=False, description="True when the visible damage alone justifies replacing rather than repairing")
    confidence: float = Field(default=0.0, ge=0, le=1, description="Confidence in the identification and label reading")
    notes: str = Field(default="", description="Anything unreadable or uncertain, e.g. 'serial partially obscured by glare'")


_PROMPT = """You are an IT hardware technician looking at a photo an employee took for a
replacement request. Identify the device, transcribe any label text, and assess visible damage.

Rules:
- Transcribe serial numbers, asset tags and part numbers exactly as printed. Never guess
  characters you cannot read; leave the field empty and say so in notes.
- Common label cues: "S/N" or "Serial" = serial number; "P/N", "MTM", "Model No." = part number.
  Apple prints "Serial (S)" or "(S) Serial No." with a leading S that is not part of the serial:
  leave that S out. The asset tag is {asset_tag_hint}.
- You cannot decode barcodes or QR codes. Read only the characters printed next to them.
- Damage: describe only what is visible. Cracks, spider-webbing, dead pixels, dents, bent
  hinges, missing keys, swollen battery (lifted trackpad or bulging case), corrosion.
- Medical equipment: read error codes or alarm text on its screen into damage_description, and
  use safety_concern for anything that could harm a patient (exposed wiring, broken bed rails,
  cracked pump housing, fluid inside).
- supports_replacement is true for cracked or shattered screens, swollen batteries, broken
  hinges, liquid corrosion, or a cracked chassis.
"""


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    return genai.Client(
        vertexai=True, project=config.PROJECT_ID, location=config.MODEL_LOCATION,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=5, initial_delay=1, max_delay=16)),
    )


async def analyze_photo(gcs_uri: str, mime_type: str, context_hint: str = "") -> PhotoFindings:
    prompt = _PROMPT.replace("{asset_tag_hint}", config.ASSET_TAG_HINT)
    if context_hint:
        prompt += f"\nWhat the employee said about the problem: {context_hint}\n"
    response = await _client().aio.models.generate_content(
        model=config.VISION_MODEL,
        contents=[
            types.Part.from_uri(file_uri=gcs_uri, mime_type=mime_type),
            types.Part.from_text(text=prompt),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PhotoFindings,
            temperature=0,
        ),
    )
    if response.parsed is not None:
        return response.parsed
    return PhotoFindings.model_validate_json(response.text or "{}")
