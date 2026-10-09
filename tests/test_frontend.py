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


class TestCoverZoom:
    def test_a_pinch_reaches_what_the_slider_does(self):
        # The limits live on the slider only; a second copy in the script fell
        # out of step once already, and the phone could not zoom out.
        script = (ROOT / "static" / "album.js").read_text()

        assert "+zoom.min" in script and "+zoom.max" in script
        assert "2.5" not in script


class TestFilePickersTakeTheKeyboard:
    """A file input behind a styled label must still be reachable with Tab."""

    def test_no_file_input_is_taken_out_of_the_page(self):
        for template in TEMPLATES:
            for tag in re.findall(r"<input[^>]*type=\"file\"[^>]*>", template.read_text()):
                assert not re.search(r"\shidden[\s>]", tag), (template.name, tag)

    def test_the_label_shows_where_the_keyboard_is(self):
        css = (ROOT / "static" / "app.css").read_text()

        assert "label.button:focus-within" in css
        assert "label.button > input[type=file]" in css


def _node():
    import os
    import shutil

    found = os.environ.get("NODE") or shutil.which("node")
    if found:
        return found
    candidates = sorted(Path.home().glob(".nvm/versions/node/*/bin/node"))
    return str(candidates[-1]) if candidates else None


class TestPageScriptsRun:
    def test_every_page_script_test_passes(self):
        # CI runs these directly; here they run whenever a node is at hand.
        import subprocess

        import pytest

        node = _node()
        if node is None:
            pytest.skip("no node to run the page script tests with")
        for test in sorted((Path(__file__).resolve().parent / "js").glob("*.test.mjs")):
            result = subprocess.run([node, str(test)], capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, (test.name, result.stderr[-2000:])
