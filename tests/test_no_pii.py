"""Nothing personal in what is committed (and so shown on GitHub).

The patterns live in the uncommitted `.pii-patterns` (one regex per line), because listing someone's
name or instance in a committed test would itself publish it. Without that file (a fresh clone) the
test checks only generic things: no home-folder paths and no personal email domains.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERIC = [r"/Users/[A-Za-z]", r"/home/[a-z]+/", r"@gmail\.com", r"@yahoo\.com", r"@outlook\.com", r"@hotmail\.com"]


def _patterns() -> list[re.Pattern]:
    local = ROOT / ".pii-patterns"
    lines = local.read_text().splitlines() if local.exists() else []
    return [re.compile(p, re.I) for p in GENERIC + [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]]


def _tracked() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / f for f in out.splitlines()]


def test_no_personal_data_in_tracked_files():
    patterns, hits = _patterns(), []
    for path in _tracked():
        if path.name in (".pii-patterns", "test_no_pii.py") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary (images): check those by hand
        for n, line in enumerate(text.splitlines(), 1):
            hits += [f"{path.relative_to(ROOT)}:{n}: {p.pattern}" for p in patterns if p.search(line)]
    assert not hits, "personal data in committed files:\n" + "\n".join(hits[:40])
