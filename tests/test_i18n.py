"""Tests for the translations: complete, consistent, and easy to extend."""

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tonie_studio.i18n import DEFAULT, Translator, catalogs, languages, negotiate

TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "tonie_studio" / "templates"
SOURCES = Path(__file__).resolve().parent.parent / "src" / "tonie_studio"


# Families of keys whose last part is a value from the code, so no source file
# names them in full. Each has a test below that every value is covered.
DYNAMIC = ("job.state.", "ordering.", "checkup.severity.", "backup.part.", "restore.action.",
           "restore.what.", "job.step.")


def placeholders(text):
    if isinstance(text, dict):
        return set().union(*(placeholders(value) for value in text.values()))
    return set(re.findall(r"\{(\w+)\}", text))


class TestCatalogs:
    def test_starts_with_german_and_english(self):
        assert {"de", "en"} <= {code for code, _name in languages()}

    def test_german_is_the_default(self):
        assert DEFAULT == "de"

    @pytest.mark.parametrize("code", sorted(catalogs()))
    def test_every_language_has_every_key(self, code):
        # A language added later is only finished when nothing falls back.
        expected = set(catalogs()[DEFAULT])
        assert set(catalogs()[code]) == expected, (
            f"{code} is missing {sorted(expected - set(catalogs()[code]))[:10]} "
            f"and has extra {sorted(set(catalogs()[code]) - expected)[:10]}")

    @pytest.mark.parametrize("code", sorted(catalogs()))
    def test_placeholders_match_the_default(self, code):
        # A translation that drops {count} prints nonsense; one that adds an
        # unknown name crashes the page.
        for key, text in catalogs()[DEFAULT].items():
            if key.startswith("_"):
                continue
            assert placeholders(catalogs()[code][key]) == placeholders(text), key

    def test_every_key_the_code_uses_exists(self):
        used = set()
        pattern = re.compile(r"""\bt\(\s*["']([a-z0-9_.]+)["']""")
        for path in list(TEMPLATES.glob("*.html")) + list(SOURCES.rglob("*.py")):
            used |= set(pattern.findall(path.read_text(encoding="utf-8")))
        # "job.state." ~ value: the family is checked below, value by value.
        used -= set(DYNAMIC)
        missing = used - set(catalogs()[DEFAULT])
        assert not missing, sorted(missing)

    def test_no_key_is_left_unused(self):
        # A key nobody uses is a translation someone keeps up for nothing.
        text = "".join(path.read_text(encoding="utf-8")
                       for path in list(TEMPLATES.glob("*.html")) + list(SOURCES.rglob("*.py")))
        unused = [key for key in catalogs()[DEFAULT]
                  if not key.startswith("_") and not key.startswith(DYNAMIC)
                  and f'"{key}"' not in text and f"'{key}'" not in text]
        assert not unused, unused

    def test_keys_built_from_a_value_cover_every_value(self):
        from tonie_studio.jobs import JobState

        keys = set(catalogs()[DEFAULT])
        assert {f"job.state.{state.value}" for state in JobState} <= keys

        from tonie_studio.album import ORDERINGS

        assert {f"ordering.{ordering}" for ordering in ORDERINGS} <= keys

        from tonie_studio.checkup import SEVERITIES

        assert {f"checkup.severity.{severity}" for severity in SEVERITIES} <= keys

        from tonie_studio.backup import ACTIONS, KINDS, PARTS

        assert {f"backup.part.{part}" for part in PARTS} <= keys
        assert {f"restore.action.{action}" for action in ACTIONS} <= keys
        assert {f"restore.what.{kind}" for kind in KINDS} <= keys

        from tonie_studio.encode import STEPS

        assert {f"job.step.{step}" for step in STEPS} <= keys


class TestTranslator:
    def test_translates(self):
        assert Translator("en")("nav.albums") == "Albums"
        assert Translator("de")("nav.albums") == "Alben"

    def test_fills_in_values(self):
        assert "3" in Translator("en")("listening.placed", count=3)

    def test_picks_the_plural(self):
        assert Translator("en")("listening.placed", count=1) != Translator("en")("listening.placed", count=2)

    def test_an_unknown_language_falls_back_to_the_default(self):
        assert Translator("xx")("nav.albums") == "Alben"

    def test_a_missing_key_shows_the_key_rather_than_failing(self):
        assert Translator("en")("no.such.key") == "no.such.key"

    @pytest.mark.parametrize("size, shown", [
        (812, "812 Byte"), (340 * 1024, "340 KB"), (1_268_644, "1,2 MB"),
    ])
    def test_sizes_read_like_a_person_says_them(self, size, shown):
        assert Translator("de").size(size) == shown

    def test_formats_numbers_and_dates_per_language(self):
        moment = datetime(2026, 10, 3, 18, 2, tzinfo=timezone.utc)

        assert Translator("de").size(1_268_644) == "1,2 MB"
        assert Translator("en").size(1_268_644) == "1.2 MB"
        assert Translator("de").when(moment).startswith("03.10.2026")
        assert Translator("en").when(moment).startswith("2026-10-03")


class TestNegotiation:
    def test_a_chosen_language_wins(self):
        assert negotiate(cookie="en", accept_language="de-DE,de;q=0.9") == "en"

    def test_otherwise_the_browser_decides(self):
        assert negotiate(cookie=None, accept_language="en-GB,en;q=0.9,de;q=0.5") == "en"

    def test_quality_values_are_respected(self):
        assert negotiate(cookie=None, accept_language="en;q=0.3,de;q=0.8") == "de"

    def test_a_language_nobody_translated_falls_back(self):
        assert negotiate(cookie=None, accept_language="fr-FR,fr;q=0.9") == DEFAULT

    def test_a_forged_cookie_is_ignored(self):
        assert negotiate(cookie="../../etc", accept_language="") == DEFAULT
