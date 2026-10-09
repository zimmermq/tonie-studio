"""Tests for how the pages carry their scripts and styles."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src" / "tonie_studio"
TEMPLATES = sorted((ROOT / "templates").glob("*.html"))


class TestScriptsLiveInFiles:
    def test_no_template_holds_code_of_its_own(self):
        # Code in a file can be checked before a release and is kept by the
        # browser; what a template may still carry is data for that code.
        for template in TEMPLATES:
            for tag in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", template.read_text()):
                assert 'type="application/json"' in tag, (template.name, tag)

    def test_every_file_a_template_names_is_there(self):
        for template in TEMPLATES:
            for name in re.findall(r"asset\('([^']+)'\)", template.read_text()):
                assert (ROOT / "static" / name).is_file(), (template.name, name)

    def test_the_scripts_carry_no_template_syntax(self):
        # The server never renders them; a "{{" left in one reaches the browser as is.
        for script in (ROOT / "static").glob("*.js"):
            assert "{{" not in script.read_text() and "{%" not in script.read_text(), script.name
