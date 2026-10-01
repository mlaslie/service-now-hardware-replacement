"""The demo run sheet: built from the template and the demo users, nothing personal committed."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo"))
import build_demo  # noqa: E402


def test_example_build_is_complete_and_generic():
    html = build_demo.render(ROOT / "seed" / "users.example.json", "https://<instance>.service-now.com", "<repo>")
    assert "{{" not in html
    assert "Jane Doe" in html and "John Doe" in html and "jane.doe@example.com" in html
    assert not re.search(r"personal-domain|INSTANCE|/Users/", html)


def test_committed_demo_matches_the_template():
    built = build_demo.render(ROOT / "seed" / "users.example.json", "https://<instance>.service-now.com",
                              "<path to your checkout>")
    assert (ROOT / "demo" / "DEMO.html").read_text() == built, "run: uv run python demo/build_demo.py --example"


def test_other_users_fill_in(tmp_path):
    users = tmp_path / "users.json"
    users.write_text('[{"user_name": "a.b", "first_name": "Ann", "last_name": "B", "email": "a@x", "demo_role": "clinician"},'
                     ' {"user_name": "c.d", "first_name": "Carl", "last_name": "D", "email": "c@x", "demo_role": "it"}]')
    html = build_demo.render(users, "https://acme.service-now.com", "/r")
    assert "Ann B" in html and "Carl D" in html and "user_name%3Da.b" in html and "/r/demo/tag-pump.jpg" in html


def test_example_users_are_generic():
    text = (ROOT / "seed" / "users.example.json").read_text()
    assert "example.com" in text and not re.search(r"personal-patterns", text, re.I)
