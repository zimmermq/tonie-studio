"""Tests for naming imported tonies after the catalogue or their dump's file name."""

from tonie_studio.models import Model
from tonie_studio.naming import (NEW, OFFICIAL, PROMOTE, RENAME, RETITLE, REUSE, Catalog, Name,
                                 fingerprint, norm, official_by_audio, plan, promotions)
from tonie_studio.tags import TonieTag

OFFICIAL_ENTRIES = [
    {"model": "10000001", "series": "Wilde Wölfe", "episodes": "Das große Rennen",
     "title": "Wilde Wölfe - Das große Rennen", "language": "de-de"},
    {"model": "11000002", "series": "100 Wörter für die Reise",
     "episodes": "100 Wörter für die Reise - Set", "language": "de-de",
     "category": "audio-book", "pic": "https://img/11000002.png"},
    {"model": "10000003", "series": "Forscherkids", "episodes": "Alles über Vulkane", "language": "de-de"},
    {"model": "20000003", "series": "Forscherkids", "episodes": "Alles über Vulkane", "language": "en-gb"},
    {"model": "10000004", "series": "Bobo", "episodes": "Folge 1", "language": "de-de",
     "category": "audio-play", "pic": "https://img/10000004.png"},
    {"model": "10000005", "series": "Bobo", "episodes": "Folge 1", "language": "de-de",
     "category": "audio-play", "pic": "https://img/10000005.png"},
    {"model": "10000006", "series": "Gemischt", "episodes": "A", "language": "de-de", "category": "music"},
    {"model": "10000007", "series": "Gemischt", "episodes": "B", "language": "de-de",
     "category": "audio-book"},
]


def test_norm_folds_umlauts_the_way_dump_folders_spell_them():
    assert norm("Alles über Vulkane!") == norm("Alles ueber Vulkane")
    assert norm("Die Glücksfüchse") == "diegluecksfuechse"


class TestDescribe:
    catalog = Catalog(OFFICIAL_ENTRIES)

    def test_an_exact_episode_match_uses_the_official_model(self):
        name = self.catalog.describe("Sammlung/Forscherkids/Alles ueber Vulkane.nfc")

        assert name.model == "10000003"

    def test_a_series_only_match_takes_the_official_spelling(self):
        name = self.catalog.describe("100 Woerter fuer die Reise/Englisch.nfc")

        assert name.model is None
        assert (name.series, name.episode) == ("100 Wörter für die Reise", "Englisch")

    def test_a_series_of_one_entry_lends_its_category_and_cover(self):
        name = self.catalog.describe("100 Woerter fuer die Reise/Englisch.nfc")

        assert (name.category, name.pic) == ("audio-book", "https://img/11000002.png")

    def test_a_series_of_several_entries_lends_its_category_but_no_cover(self):
        name = self.catalog.describe("Bobo/Folge 2.nfc")

        assert (name.category, name.pic) == ("audio-play", "")

    def test_a_series_of_mixed_categories_lends_none(self):
        name = self.catalog.describe("Gemischt/C.nfc")

        assert (name.category, name.pic) == ("", "")

    def test_an_unknown_series_keeps_the_folder_and_file_names(self):
        name = self.catalog.describe("Comics/Fledermaus.nfc")

        assert (name.model, name.series, name.episode) == (None, "Comics", "Fledermaus")

    def test_an_ambiguous_episode_is_not_guessed(self):
        name = self.catalog.describe("Bobo/Folge 1.nfc")

        assert name.model is None and name.series == "Bobo"


SETS = [
    {"model": "11000801", "series": "Tolle Berufe", "episodes": "Tolle Berufe - Set",
     "title": "Tolle Berufe - Tolle Berufe - Set", "language": "de-de", "category": "audio-book",
     "pic": "https://img/set.png",
     "tracks": ["Tierfotografie - Wilde Natur", "Pizzabacken - Genau gewogen"],
     "audio_id": ["1719490067", "1719490058"], "hash": ["AA", "BB"]},
    {"model": "11004634", "series": "Wuselwesen", "episodes": "Wuselwesen Set", "language": "de-de",
     "tracks": ["Insekten & Bienen", "Luft & Elektrizität"]},
    {"model": "10007777", "series": "Sunset", "episodes": "Sunset", "language": "de-de"},
]


class TestSets:
    catalog = Catalog(SETS)

    def test_a_set_is_an_entry_whose_episode_ends_in_the_word_set(self):
        assert self.catalog.is_set("11000801") and self.catalog.is_set("11004634")
        assert not self.catalog.is_set("10007777")
        assert not self.catalog.is_set("TS-0001")

    def test_a_figure_takes_the_track_it_matches_and_the_sets_cover(self):
        name = self.catalog.figure("Tolle Berufe/1 Tierfotografie - Wilde Natur.nfc", "11000801")

        assert name.model is None
        assert (name.series, name.episode) == ("Tolle Berufe", "Tierfotografie - Wilde Natur")
        assert (name.category, name.pic) == ("audio-book", "https://img/set.png")

    def test_a_figure_gets_its_umlauts_back_from_the_track(self):
        name = self.catalog.figure("Wuselwesen/Luft & Elektrizitaet.nfc", "11004634")

        assert name.episode == "Luft & Elektrizität"

    def test_a_figure_matches_a_track_named_after_its_series(self):
        # The catalogue writes "<series> - <figure>", dump files "<series> Set - <figure>".
        catalog = Catalog([{"model": "11003994", "series": "Eule", "episodes": "Eule Set",
                            "tracks": ["Eule - Mit Gefühl", "Eule - Auf Reisen"]}])

        name = catalog.figure("Eule/Eule Set - Mit Gefuehl.nfc", "11003994")

        assert (name.series, name.episode) == ("Eule", "Mit Gefühl")

    def test_a_figure_without_a_matching_track_keeps_the_file_name(self):
        name = self.catalog.figure("Tolle Berufe/5 Neu.nfc", "11000801")

        assert name.episode == "5 Neu"


STUBS = [
    {"model": "11003879", "series": None, "episodes": None, "title": "None - None",
     "audio_id": ["1775049960"], "hash": ["96BA"]},
    {"model": "10000001", "series": "Wilde Wölfe", "episodes": "Wilde Wölfe", "language": "de-de"},
]


class TestStubs:
    catalog = Catalog(STUBS)

    def test_an_entry_without_a_series_is_a_stub(self):
        assert self.catalog.is_stub("11003879")
        assert not self.catalog.is_stub("10000001") and not self.catalog.is_stub("TS-0001")


OFFICIAL_AUDIO = [
    {"model": "10000581", "audio_id": ["1642145177", "1642145178"], "hash": ["821CD801", "821CD802"]},
    {"model": "10000999", "audio_id": ["555"], "hash": ["abcdef"]},
]


def model(model_id, audio_id=None, sha1=None, series="Serie", episode="Folge"):
    return Model(id=model_id, series=series, episode=episode,
                 audio_ids=[str(audio_id)] if audio_id else [], hashes=[sha1] if sha1 else [])


class TestPromotions:
    by_audio = official_by_audio(OFFICIAL_AUDIO)

    def test_audio_is_matched_like_teddycloud_does_any_id_with_any_hash(self):
        assert self.by_audio[("1642145178", "821CD801")] == "10000581"
        assert self.by_audio[("555", "ABCDEF")] == "10000999"

    def test_audio_nobody_lists_is_no_match(self):
        assert self.by_audio.get(("555", "OTHER")) is None
        assert self.by_audio.get(("999", "ABCDEF")) is None

    def test_where_two_entries_list_the_same_audio_the_later_one_names_it(self):
        twice = [{"model": "10000001", "audio_id": ["7"], "hash": ["aa"]},
                 {"model": "10000002", "audio_id": ["7", "8"], "hash": ["bb", "aa"]}]

        assert official_by_audio(twice).get(("7", "AA")) == "10000002"

    def test_a_podcast_of_a_thousand_episodes_is_matched_by_each_of_them(self):
        # The catalogue lists such podcasts as one entry with an id and a hash
        # per episode; pairing every id with every hash made a million keys.
        podcast = [{"model": "11000782", "audio_id": [str(n) for n in range(1000)],
                    "hash": [f"{n:040x}" for n in range(1000)]}]

        index = official_by_audio(podcast)

        assert index.get(("0", f"{999:040X}")) == "11000782"
        assert index.get(("999", f"{0:040X}")) == "11000782"

    def test_a_custom_model_whose_audio_became_official_is_promoted(self):
        custom = [model("TS-0020", 555, "ABCDEF"), model("TS-0021", 555, "OTHER"),
                  model("NFC-0003", 555, "abcdef")]

        assert promotions(custom, self.by_audio, lambda m: False) == {
            "TS-0020": "10000999", "NFC-0003": "10000999"}

    def test_never_to_a_set_or_a_stub(self):
        custom = [model("TS-0020", 1719490067, "AA")]

        assert promotions(custom, official_by_audio(SETS), Catalog(SETS).unsuitable) == {}


def tag(ruid, source="", model_id=""):
    return TonieTag(ruid=ruid, uid="", source=source, model=model_id, series="", episode="",
                    picture="", claimed=False)


def lib(audio_id):
    return f"lib://by/audioID/{audio_id}.taf"


def header(audio_id, sha1):
    return {"audioId": audio_id, "sha1Hash": sha1, "valid": True}


def ruid(tail):
    return f"{tail}500304e0"


class TestPlan:
    def test_names_promotes_and_links_by_audio(self):
        tags = [tag(ruid("00000001"), lib(1)),                      # unknown -> new model
                tag(ruid("00000002"), lib(555)),                    # audio official -> link
                tag(ruid("00000003"), lib(3), "TS-0007"),           # now official -> promote
                tag(ruid("00000004"), lib(4), "10000581"),          # named already
                tag(ruid("00000005"))]                              # not imported
        items = {ruid(f"0000000{n}"): f"Comics/{name}.nfc"
                 for n, name in enumerate(["Fledermaus", "Supermann", "Blitz", "Rotkehlchen",
                                           "Spassvogel"], 1)}
        headers = {"1.taf": header(1, "11"), "555.taf": header(555, "abcdef"),
                   "3.taf": header(3, "33"), "4.taf": header(4, "44")}
        by_audio = official_by_audio(OFFICIAL_AUDIO + [
            {"model": "10003333", "audio_id": ["3"], "hash": ["33"]}])

        result = plan(tags, items, Catalog([]), by_audio, [model("TS-0007", 3, "33")], headers)

        got = {step.ruid: (step.kind, step.model_id, step.replaces) for step in result.steps}
        assert got == {
            ruid("00000001"): (NEW, "TS-0008", ""),
            ruid("00000002"): (OFFICIAL, "10000999", ""),
            ruid("00000003"): (PROMOTE, "10003333", "TS-0007"),
        }
        new = result.steps[[s.ruid for s in result.steps].index(ruid("00000001"))].new_model
        assert (new.series, new.episode, new.audio_ids, new.hashes) == (
            "Comics", "Fledermaus", ["1"], ["11"])
        assert result.retired == ["TS-0007"] and result.skipped == []

    def test_the_same_audio_reuses_the_model_that_carries_it(self):
        tags = [tag(ruid("00000001"), lib(1))]
        result = plan(tags, {ruid("00000001"): "Comics/Fledermaus.nfc"}, Catalog([]), {},
                      [model("TS-0002", 1, "11")], {"1.taf": header(1, "11")})

        (step,) = result.steps
        assert (step.kind, step.model_id, step.new_model) == (REUSE, "TS-0002", None)

    def test_two_tonies_with_new_audio_get_one_model_each(self):
        tags = [tag(ruid("00000001"), lib(1)), tag(ruid("00000002"), lib(2))]
        items = {ruid("00000001"): "A/Eins.nfc", ruid("00000002"): "A/Zwei.nfc"}

        result = plan(tags, items, Catalog([]), {}, [], {"1.taf": header(1, "11"),
                                                         "2.taf": header(2, "22")})

        assert [step.model_id for step in result.steps] == ["TS-0001", "TS-0002"]

    def test_a_tonie_whose_audio_is_not_in_the_library_is_skipped_with_a_reason(self):
        result = plan([tag(ruid("00000001"), "content/00000001/500304E0")],
                      {ruid("00000001"): "A/Eins.nfc"}, Catalog([]), {}, [], {})

        assert result.steps == [] and len(result.skipped) == 1

    def test_set_figures_get_a_model_each(self):
        catalog = Catalog(SETS)
        tags = [tag(ruid("00000001"), lib(1719490067), "11000801"),
                tag(ruid("00000002"), lib(1719490058))]
        items = {ruid("00000001"): "Tolle Berufe/1 Tierfotografie - Wilde Natur.nfc",
                 ruid("00000002"): "Tolle Berufe/2 Pizzabacken - Genau gewogen.nfc"}
        headers = {"1719490067.taf": header(1719490067, "aa"),
                   "1719490058.taf": header(1719490058, "bb")}

        result = plan(tags, items, catalog, official_by_audio(SETS), [], headers)

        got = {s.name.episode: (s.kind, s.model_id, s.replaces) for s in result.steps}
        assert got == {"Tierfotografie - Wilde Natur": (NEW, "TS-0001", "11000801"),
                       "Pizzabacken - Genau gewogen": (NEW, "TS-0002", "")}

    def test_stub_tonies_are_named_from_their_dump(self):
        tags = [tag(ruid("00000001"), lib(1775049960), "11003879")]
        result = plan(tags, {ruid("00000001"): "Filme/Kuschel Stitch.nfc"}, Catalog(STUBS),
                      official_by_audio(STUBS), [], {"1775049960.taf": header(1775049960, "96ba")})

        (step,) = result.steps
        assert (step.kind, step.name.series, step.name.episode, step.replaces) == (
            NEW, "Filme", "Kuschel Stitch", "11003879")

    def test_album_tonies_of_this_app_are_left_alone(self):
        # Their audio was encoded here and is in no catalogue.
        tags = [tag(ruid("00000001"), lib(9), "TS-0001")]
        result = plan(tags, {}, Catalog([]), official_by_audio(OFFICIAL_AUDIO),
                      [model("TS-0001", 9, "99")], {"9.taf": header(9, "99")})

        assert result.steps == [] and result.retired == []


class TestRetitling:
    eule = [{"model": "11003994", "series": "Eule", "episodes": "Eule Set", "language": "de-de",
             "tracks": ["Eule - Mit Gefühl", "Eule - Auf Reisen"],
             "audio_id": ["11", "12"], "hash": ["AA", "BB"]}]

    def test_a_model_of_ours_gets_the_better_name_the_rules_now_find(self):
        ours = model("TS-0039", 11, "AA", series="Eule", episode="Eule Set - Mit Gefuehl")
        tags = [tag(ruid("00000001"), lib(11), "TS-0039")]

        result = plan(tags, {ruid("00000001"): "Eule/Eule Set - Mit Gefuehl.nfc"},
                      Catalog(self.eule), official_by_audio(self.eule), [ours],
                      {"11.taf": header(11, "aa")}, retitle=True)

        (step,) = result.steps
        assert (step.kind, step.model_id, step.new_model.episode) == (RETITLE, "TS-0039", "Mit Gefühl")
        assert step.new_model.audio_ids == ["11"] and result.retired == []

    def test_keeps_the_cover_and_shop_link_a_person_took_from_tonies_com(self):
        ours = model("TS-0039", 11, "AA", series="Eule", episode="Eule Set - Mit Gefuehl")
        ours.picture = "/custom_img/TS-0039.png"
        ours.other["shop_sales_id"] = "11003994"
        tags = [tag(ruid("00000001"), lib(11), "TS-0039")]

        result = plan(tags, {ruid("00000001"): "Eule/Eule Set - Mit Gefuehl.nfc"},
                      Catalog(self.eule), official_by_audio(self.eule), [ours],
                      {"11.taf": header(11, "aa")}, retitle=True)

        written = result.steps[0].new_model.to_json()
        assert written["pic"] == "/custom_img/TS-0039.png"
        assert written["shop_sales_id"] == "11003994"
        assert written["episodes"] == "Mit Gefühl"

    def test_only_when_asked(self):
        ours = model("TS-0039", 11, "AA", series="Eule", episode="Eule Set - Mit Gefuehl")
        tags = [tag(ruid("00000001"), lib(11), "TS-0039")]

        result = plan(tags, {ruid("00000001"): "Eule/Eule Set - Mit Gefuehl.nfc"},
                      Catalog(self.eule), official_by_audio(self.eule), [ours],
                      {"11.taf": header(11, "aa")})

        assert result.steps == []

    def test_a_name_that_is_already_right_is_left(self):
        ours = model("TS-0039", 11, "AA", series="Eule", episode="Mit Gefühl")
        tags = [tag(ruid("00000001"), lib(11), "TS-0039")]

        result = plan(tags, {ruid("00000001"): "Eule/Eule Set - Mit Gefuehl.nfc"},
                      Catalog(self.eule), official_by_audio(self.eule), [ours],
                      {"11.taf": header(11, "aa")}, retitle=True)

        assert result.steps == []

    def test_album_tonies_have_no_dump_and_are_never_retitled(self):
        ours = model("TS-0001", 9, "99", series="Mein Album", episode="Teil 1")
        result = plan([tag(ruid("00000001"), lib(9), "TS-0001")], {}, Catalog([]), {}, [ours],
                      {"9.taf": header(9, "99")}, retitle=True)

        assert result.steps == []


class TestFingerprint:
    def test_the_same_plan_gives_the_same_print_and_a_change_another(self):
        tags = [tag(ruid("00000001"), lib(1))]
        dumps = {ruid("00000001"): "A/Eins.nfc"}
        headers = {"1.taf": header(1, "11")}

        first = plan(tags, dumps, Catalog([]), {}, [], headers)
        again = plan(tags, dumps, Catalog([]), {}, [], headers)
        other = plan(tags, {ruid("00000001"): "A/Zwei.nfc"}, Catalog([]), {}, [], headers)

        assert fingerprint(first) == fingerprint(again) != fingerprint(other)


class TestRenaming:
    def test_models_from_the_old_script_move_into_this_apps_namespace(self):
        old = model("NFC-0004", 7, "77", series="Comics", episode="Fledermaus")
        tags = [tag(ruid("00000001"), lib(7), "NFC-0004"), tag(ruid("00000002"), lib(7), "NFC-0004")]

        result = plan(tags, {}, Catalog([]), {}, [model("TS-0015"), old], {}, migrate=True)

        assert [(s.kind, s.model_id, s.replaces) for s in result.steps] == [
            (RENAME, "TS-0016", "NFC-0004"), (RENAME, "TS-0016", "NFC-0004")]
        new = result.steps[0].new_model
        assert (new.id, new.series, new.episode, new.audio_ids) == ("TS-0016", "Comics",
                                                                    "Fledermaus", ["7"])
        assert result.steps[1].new_model is None  # written once
        assert result.retired == ["NFC-0004"]

    def test_one_that_became_official_is_promoted_instead(self):
        tags = [tag(ruid("00000001"), lib(555), "NFC-0004")]

        result = plan(tags, {}, Catalog([]), official_by_audio(OFFICIAL_AUDIO),
                      [model("NFC-0004", 555, "ABCDEF")], {}, migrate=True)

        assert [(s.kind, s.model_id) for s in result.steps] == [(PROMOTE, "10000999")]

    def test_one_no_tonie_uses_is_only_deleted(self):
        result = plan([], {}, Catalog([]), {}, [model("NFC-0004", 7, "77")], {}, migrate=True)

        assert result.steps == [] and result.retired == ["NFC-0004"]

    def test_without_migrate_they_stay(self):
        tags = [tag(ruid("00000001"), lib(7), "NFC-0004")]

        result = plan(tags, {}, Catalog([]), {}, [model("NFC-0004", 7, "77")], {})

        assert result.steps == [] and result.retired == []


def test_a_name_is_what_the_model_will_say():
    assert Name(None, "Serie", "Folge").episode == "Folge"


class TestNamer:
    """Against the fake teddyCloud: what ends up in tonies.custom.json and on the tags."""

    @staticmethod
    def teddycloud_with(audio_id, sha1, ruid_, model_id=""):
        from fake_teddycloud import FakeTeddyCloud

        fake = FakeTeddyCloud()
        fake.unique_audio = True
        fake.dirs.update({"/by", "/by/audioID"})
        fake.files[f"/by/audioID/{audio_id}.taf"] = b"taf"
        fake.taf_headers[f"/by/audioID/{audio_id}.taf"] = header(audio_id, sha1)
        fake.add_tag(ruid_, source=lib(audio_id), tonie_model=model_id)
        return fake

    def test_names_a_tonie_right_after_its_import(self):
        from tonie_studio.naming import Namer

        fake = self.teddycloud_with(1, "11", ruid("00000001"))
        namer = Namer(lambda: fake, dict)

        namer.name_one(ruid("00000001"), "Comics/Fledermaus.nfc")

        assert fake.tag_entries[ruid("00000001")]["tonie_model"] == "TS-0001"
        (made,) = fake.custom_models
        assert (made["series"], made["episodes"], made["audio_id"]) == ("Comics", "Fledermaus", ["1"])

    def test_naming_waits_for_other_writes(self):
        # Naming picks the next free model id, as a publish does; two at once
        # would take the same one and the later would overwrite the other.
        from web_helpers import waits_for_writing

        from tonie_studio.naming import Namer

        fake = self.teddycloud_with(1, "11", ruid("00000001"))
        namer = Namer(lambda: fake, dict)

        assert waits_for_writing(lambda: namer.name_one(ruid("00000001"), "Comics/Fledermaus.nfc"))
        assert waits_for_writing(lambda: namer.upkeep(migrate=True))

    def test_the_catalogue_is_loaded_before_other_writes_are_held_up(self):
        # tonies.json is several megabytes; while it loads, a person saving a
        # title must not wait for it.
        from tonie_studio.models import WRITING
        from tonie_studio.naming import Namer

        fake = self.teddycloud_with(1, "11", ruid("00000001"))
        held = []
        original = fake.tonies_json
        fake.tonies_json = lambda: held.append(WRITING._is_owned()) or original()
        namer = Namer(lambda: fake, dict)

        namer.name_one(ruid("00000001"), "Comics/Fledermaus.nfc")

        assert held == [False]

    def test_new_models_take_ids_never_handed_out_before(self, tmp_path):
        from tonie_studio.models import note_id_used
        from tonie_studio.naming import Namer

        note_id_used(tmp_path, "TS-0007")
        fake = self.teddycloud_with(1, "11", ruid("00000001"))
        namer = Namer(lambda: fake, dict, data_dir=tmp_path)

        namer.name_one(ruid("00000001"), "Comics/Fledermaus.nfc")

        assert fake.tag_entries[ruid("00000001")]["tonie_model"] == "TS-0008"

    def test_leaves_a_tonie_teddycloud_names_by_itself(self):
        from tonie_studio.naming import Namer

        fake = self.teddycloud_with(1, "11", ruid("00000001"), model_id="10000001")
        namer = Namer(lambda: fake, dict)

        namer.name_one(ruid("00000001"), "Comics/Fledermaus.nfc")

        assert fake.custom_models == []

    def test_the_upkeep_renames_old_models_only_when_asked(self):
        from tonie_studio.naming import Namer

        fake = self.teddycloud_with(7, "77", ruid("00000001"), model_id="NFC-0004")
        fake.custom_models.append({"model": "NFC-0004", "series": "Comics", "episodes": "Fledermaus",
                                   "audio_id": ["7"], "hash": ["77"]})
        namer = Namer(lambda: fake, dict)

        assert namer.upkeep().empty
        namer.upkeep(migrate=True)

        assert [m["model"] for m in fake.custom_models] == ["TS-0001"]
        assert fake.tag_entries[ruid("00000001")]["tonie_model"] == "TS-0001"
        # Written without its audio while the old model still had it, then with it.
        assert (fake.custom_models[0]["audio_id"], fake.custom_models[0]["hash"]) == (["7"], ["77"])

    def test_the_upkeep_counts_each_call_to_teddycloud(self):
        from tonie_studio.naming import Namer

        fake = self.teddycloud_with(7, "77", ruid("00000001"), model_id="NFC-0004")
        fake.custom_models.append({"model": "NFC-0004", "series": "Comics", "episodes": "Fledermaus",
                                   "audio_id": ["7"], "hash": ["77"]})
        heard = []

        namer = Namer(lambda: fake, dict)
        namer.upkeep(migrate=True, progress=lambda step, done, total: heard.append((done, total)))

        # The new model without audio, the sticker, the old model gone, the audio.
        assert heard == [(0, 4), (1, 4), (2, 4), (3, 4)]

    def test_the_upkeep_writes_nothing_when_the_plan_is_not_the_one_shown(self):
        import pytest

        from tonie_studio.naming import Namer, PlanChanged

        fake = self.teddycloud_with(7, "77", ruid("00000001"), model_id="NFC-0004")
        fake.custom_models.append({"model": "NFC-0004", "series": "Comics", "episodes": "Fledermaus",
                                   "audio_id": ["7"], "hash": ["77"]})
        namer = Namer(lambda: fake, dict)

        with pytest.raises(PlanChanged):
            namer.upkeep(migrate=True, expect="not what is planned now")

        assert [m["model"] for m in fake.custom_models] == ["NFC-0004"]

    def test_the_upkeep_promotes_a_model_the_catalogue_now_covers(self):
        from tonie_studio.naming import Namer

        fake = self.teddycloud_with(7, "77", ruid("00000001"), model_id="TS-0002")
        fake.custom_models.append({"model": "TS-0002", "series": "Comics", "episodes": "Fledermaus",
                                   "audio_id": ["7"], "hash": ["77"]})
        fake.catalogue = [{"model": "10000077", "series": "Comics", "episodes": "Fledermaus",
                           "audio_id": ["7"], "hash": ["77"], "language": "de-de"}]
        namer = Namer(lambda: fake, dict)

        namer.upkeep()

        assert fake.custom_models == []
        assert fake.tag_entries[ruid("00000001")]["tonie_model"] == "10000077"
