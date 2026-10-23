"""Tests for grouping the languages tonies speak the way a person picks them."""

from collections import Counter

from tonie_studio.languages import NAMED, NONE, OTHER, chosen, from_path, offered, spoken


class TestSpoken:
    def test_a_regional_variant_counts_as_its_language(self):
        assert spoken("de-ch") == "de" and spoken("de-de") == "de"
        assert spoken("EN-us") == "en" and spoken("en-gb") == "en"

    def test_nothing_known_is_its_own_group(self):
        assert spoken("") == NONE and spoken(None) == NONE

    def test_a_language_without_a_name_here_counts_as_another(self):
        # Its chip would otherwise read as a bare translation key.
        assert "fi" not in NAMED
        assert spoken("fi-fi") == OTHER


class TestOffered:
    def test_german_english_and_french_first_the_rest_by_name_and_unknown_last(self):
        counts = Counter({"sv": 1, NONE: 2, "en": 3, "da": 1, "de": 9})

        assert offered(counts) == ["de", "en", "da", "sv", NONE]

    def test_other_languages_come_before_the_unknown(self):
        assert offered(Counter({"de": 3, NONE: 1, OTHER: 2})) == ["de", OTHER, NONE]

    def test_a_single_language_is_nothing_to_choose_from(self):
        assert offered(Counter({"de": 12})) == []


class TestChosen:
    def test_a_choice_is_stored_the_way_the_catalogue_writes_it(self):
        assert chosen("en") == "en-us" and chosen("fr") == "fr-fr" and chosen("de") == "de-de"

    def test_the_same_language_as_before_keeps_its_region(self):
        assert chosen("en", "en-gb") == "en-gb"

    def test_nonsense_keeps_what_was_there_or_is_german(self):
        assert chosen("xx", "en-gb") == "en-gb"
        assert chosen("xx") == "de-de"


class TestFromPath:
    def test_a_folder_named_after_a_language_gives_it(self):
        assert from_path("English/Amelia Bedelia and Friends/Beat the Clock.nfc") == "en-us"
        assert from_path("French/Pat Patrouille/Chase.nfc") == "fr-fr"
        assert from_path("German/Bobo/Folge 1.nfc") == "de-de"

    def test_its_german_and_its_own_name_count_too(self):
        assert from_path("Englisch/Peppa/Peppa.nfc") == "en-us"
        assert from_path("Polski/Bajki/Bajka.nfc") == "pl-pl"
        assert from_path("Francais/Chase/Chase.nfc") == "fr-fr"
        assert from_path("Deutsch/Bobo/Folge 1.nfc") == "de-de"

    def test_a_folder_named_by_its_code_gives_it(self):
        assert from_path("dumps/en/Peppa/Peppa.nfc") == "en-us"
        assert from_path("pl/Bajki/Bajka.nfc") == "pl-pl"
        assert from_path("tonies/FR/Chase/Chase.nfc") == "fr-fr"

    def test_a_code_with_a_region_keeps_the_region(self):
        assert from_path("en-gb/Peppa/Peppa.nfc") == "en-gb"
        assert from_path("de_CH/Globi/Globi.nfc") == "de-ch"

    def test_the_language_folder_nearest_the_top_decides(self):
        assert from_path("English/German/Folge 1.nfc") == "en-us"

    def test_only_a_whole_folder_name_counts_never_the_file_name(self):
        assert from_path("Comics/English.nfc") is None
        assert from_path("Englisch lernen/Tiere/Hund.nfc") is None
        assert from_path("Sammlung/Bobo/Folge 1.nfc") is None

    def test_a_language_without_a_name_here_is_not_guessed(self):
        assert from_path("fi/Muumi/Muumi.nfc") is None
