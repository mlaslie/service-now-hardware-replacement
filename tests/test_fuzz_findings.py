"""The fuzzing findings file loads: the report's "Findings and fixes" section is built from it."""

from pathlib import Path

import yaml

FINDINGS = Path(__file__).resolve().parents[1] / "fuzz" / "findings.yaml"


def test_findings_file_loads_and_is_complete():
    data = yaml.safe_load(FINDINGS.read_text(encoding="utf-8"))
    assert data["baseline"]["deterministic"] and data["findings"]
    for f in data["findings"]:
        assert {"id", "invariant", "severity", "summary", "status"} <= set(f), f.get("id")
        assert f["status"] in ("fixed", "harness", "open"), f["id"]
