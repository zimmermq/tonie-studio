"""Tests for what the tonies.com shop says about a tonie: looking it up, searching, keeping it."""

from tonie_studio.shop import (API, BASE, BATCH, SEARCH_REQUESTS, Shop, clean_description,
                               search_words)


def product(sales_id, name, series="Madagascar", description="<b>Das Original</b><p></p>Zoo.",
            tracks="Kapitel eins\nKapitel zwei", path="/de-de/tonies/x/y/", end_of_life=False):
    return {"salesId": sales_id, "name": name, "description": description, "ageMin": 3,
            "runTime": 55, "genre": {"name": "Hörspiel", "key": "audio-play"},
            "theme": [{"name": "Abenteuer", "key": "adventure"}],
            "series": {"name": series}, "tracks": tracks, "productUrl": path,
            "firstImage": {"url": f"https://images.example.com/{sales_id}.png"},
            "language": {"key": "de-de"}, "endOfLife": end_of_life}


class Api:
    """The shop's product search: answers by salesId or by a word in the name."""

    def __init__(self, products):
        self.products = products
        self.asked = []

    def __call__(self, url, params):
        assert url == API
        self.asked.append(dict(params))
        if "salesId" in params:
            wanted = params["salesId"].split(",")
            found = [p for p in self.products if p["salesId"] in wanted]
        else:
            word = params["search"].lower()
            found = [p for p in self.products
                     if word in (p["name"] + " " + p["series"]["name"]).lower()]
        return {"documents": found[:int(params["limit"])], "total": len(found)}


SIMBA = product("11003868", "Cuddle Tonie Simba", series="Der König der Löwen",
                tracks="Simba sagt Hallo!\nDschungelwitze\nGeschichte: Eine wilde Nacht",
                path="/de-de/cuddle-tonies/disney-der-koenig-der-loewen/disney-simba/")
LION_KING = product("01-0190", "Der König der Löwen", series="Der König der Löwen")
MADAGASCAR = product("10001924", "Madagascar", path="/de-de/tonies/madagascar/madagascar/")


class TestLookup:
    def test_reads_everything_one_request_gives(self, tmp_path):
        info = Shop(tmp_path, fetch=Api([MADAGASCAR])).lookup("10001924")

        assert info.url == f"{BASE}/de-de/tonies/madagascar/madagascar/"
        assert (info.name, info.age_min, info.minutes, info.genre, info.genre_key) == (
            "Madagascar", 3, 55, "Hörspiel", "audio-play")
        assert info.themes == ["Abenteuer"] and info.tracks == ["Kapitel eins", "Kapitel zwei"]
        assert info.picture == "https://images.example.com/10001924.png"
        assert info.description == "<b>Das Original</b>Zoo."

    def test_takes_the_picture_of_the_figure_alone(self, tmp_path):
        # The shop shows a product in four pictures: the figure with the CD
        # cover first, the figure alone, the cover alone, the figure on a box.
        # A tonie's picture is the figure, as most catalogue pictures are.
        base = "https://images.example.com/BOX10002002"
        alone = product("10002002", "Ein Fest f\u00fcr die Waldfreunde")
        alone["images"] = [{"url": f"{base}_3_f-e_H.png"}, {"url": f"{base}_1_a-PkB.png"},
                           {"url": f"{base}_4_r-zpb.png"}, {"url": f"{base}_2_a-hTx.png"}]
        alone["firstImage"] = alone["images"][0]

        info = Shop(tmp_path, fetch=Api([alone])).lookup("10002002")

        assert info.picture == f"{base}_1_a-PkB.png"

    def test_of_two_pictures_takes_the_first_whatever_its_name(self, tmp_path):
        # A product shown twice shows the figure alone, then on a box. The
        # names are not to be trusted here: some products call the box "_1_a".
        base = "https://images.example.com/BOX11001072"
        two = product("11001072", "Der kleine Drache Kokosnuss feiert Weihnachten")
        two["images"] = [{"url": f"{base}_2_a-8hi.png"}, {"url": f"{base}_1_a-fTa.png"}]
        two["firstImage"] = two["images"][0]

        info = Shop(tmp_path, fetch=Api([two])).lookup("11001072")

        assert info.picture == f"{base}_2_a-8hi.png"

    def test_an_answer_kept_before_the_figure_was_chosen_is_asked_again(self, tmp_path):
        # Kept answers otherwise last for good; one stored while the first
        # picture was taken would keep the figure beside its CD cover.
        import json

        (tmp_path / "shop.json").write_text(json.dumps({"10001924": {
            "sales_id": "10001924", "name": "Madagascar", "picture": "old.png"}}))
        api = Api([MADAGASCAR])

        info = Shop(tmp_path, fetch=api).lookup("10001924")
        Shop(tmp_path, fetch=api).lookup("10001924")

        assert info.picture == "https://images.example.com/10001924.png"
        assert len(api.asked) == 1

    def test_without_a_picture_of_the_figure_alone_takes_the_first(self, tmp_path):
        other = product("10000001", "Madagascar")
        other["images"] = [{"url": "https://images.example.com/packshot.png"}]

        info = Shop(tmp_path, fetch=Api([other])).lookup("10000001")

        assert info.picture == "https://images.example.com/10000001.png"

    def test_asks_by_article_number_in_the_german_shop(self, tmp_path):
        api = Api([MADAGASCAR])

        Shop(tmp_path, fetch=api).lookup("10001924")

        assert api.asked == [{"shopLocale": "de-DE", "salesId": "10001924", "limit": "1"}]

    def test_asks_once_per_tonie(self, tmp_path):
        api = Api([MADAGASCAR])
        Shop(tmp_path, fetch=api).lookup("10001924")

        info = Shop(tmp_path, fetch=api).lookup("10001924")

        assert info.name == "Madagascar" and len(api.asked) == 1

    def test_a_miss_is_remembered_and_tried_again_after_a_while(self, tmp_path):
        clock = [1_000_000.0]
        api = Api([])
        shop = Shop(tmp_path, fetch=api, clock=lambda: clock[0])
        shop.lookup("10001924")

        shop.lookup("10001924")
        assert len(api.asked) == 1

        clock[0] += 31 * 86400
        shop.lookup("10001924")
        assert len(api.asked) == 2

    def test_the_shop_being_away_is_no_answer_and_not_remembered(self, tmp_path):
        def broken(url, params):
            raise OSError("no route")
        api = Api([MADAGASCAR])

        assert Shop(tmp_path, fetch=broken).lookup("10001924") is None
        assert Shop(tmp_path, fetch=api).lookup("10001924") is not None

    def test_while_away_it_is_not_asked_again_for_a_while(self, tmp_path):
        # Every view of the tonie list would otherwise wait for the shop's
        # timeout, batch after batch.
        calls, clock = [], [1000.0]

        def broken(url, params):
            calls.append(params)
            raise OSError("no route")

        shop = Shop(tmp_path, fetch=broken, clock=lambda: clock[0])
        shop.lookup_many(["10001924", "11003868"])
        shop.lookup_many(["10001924"])
        assert len(calls) == 1

        clock[0] += 11 * 60
        shop.lookup_many(["10001924"])
        assert len(calls) == 2

    def test_only_the_german_shop_is_asked(self, tmp_path):
        api = Api([MADAGASCAR])

        assert Shop(tmp_path, fetch=api).lookup("10001924", language="en-gb") is None
        assert api.asked == []


class TestLookupMany:
    """The tonie list asks about hundreds of tonies; the shop answers many per request."""

    def test_asks_for_many_in_one_request(self, tmp_path):
        api = Api([SIMBA, MADAGASCAR])

        found = Shop(tmp_path, fetch=api).lookup_many(["11003868", "10001924", "99"])

        assert set(found) == {"11003868", "10001924"}
        assert api.asked == [{"shopLocale": "de-DE", "salesId": "11003868,10001924,99",
                              "limit": "3"}]

    def test_asks_in_batches(self, tmp_path):
        api = Api([])

        Shop(tmp_path, fetch=api).lookup_many([str(n) for n in range(BATCH + 1)])

        assert [len(params["salesId"].split(",")) for params in api.asked] == [BATCH, 1]

    def test_what_it_knows_or_missed_lately_is_not_asked_again(self, tmp_path):
        api = Api([MADAGASCAR])
        shop = Shop(tmp_path, fetch=api)
        shop.lookup_many(["10001924", "99"])

        found = shop.lookup_many(["10001924", "99"])

        assert list(found) == ["10001924"] and len(api.asked) == 1

    def test_a_batch_the_shop_did_not_answer_is_asked_again(self, tmp_path):
        def broken(url, params):
            raise OSError("no route")
        api = Api([MADAGASCAR])

        assert Shop(tmp_path, fetch=broken).lookup_many(["10001924"]) == {}
        assert list(Shop(tmp_path, fetch=api).lookup_many(["10001924"])) == ["10001924"]


class TestSearch:
    def test_finds_a_tonie_by_the_strongest_words_of_its_name(self, tmp_path):
        found = Shop(tmp_path, fetch=Api([LION_KING, SIMBA, MADAGASCAR])).search(
            "Disney", "Cuddle Tonie Simba")

        assert found[0].sales_id == "11003868"
        assert "10001924" not in [info.sales_id for info in found]

    def test_ranks_what_shares_most_words_first(self, tmp_path):
        found = Shop(tmp_path, fetch=Api([LION_KING, SIMBA])).search(
            "Der König der Löwen", "Simba")

        assert [info.sales_id for info in found] == ["11003868", "01-0190"]

    def test_asks_a_few_words_at_most(self, tmp_path):
        api = Api([])

        Shop(tmp_path, fetch=api).search("Lichterkinder",
                                         "Lernen mit LIKI und LUMI - Wir bleiben gesund")

        assert 1 <= len(api.asked) <= SEARCH_REQUESTS

    def test_also_asks_for_the_umlaut_spelling_of_a_word_written_out(self, tmp_path):
        api = Api([])

        Shop(tmp_path, fetch=api).search("Checker Tobi", "Gefuehle")

        assert "gef\u00fchle" in [params["search"] for params in api.asked]

    def test_the_shop_being_away_finds_nothing(self, tmp_path):
        def broken(url, params):
            raise OSError("no route")

        assert Shop(tmp_path, fetch=broken).search("Disney", "Simba") == []


class TestSearchWords:
    def test_leaves_out_short_and_common_words_and_prefers_long_ones(self):
        words = search_words("Lichterkinder", "Lernen mit LIKI und LUMI - Wir bleiben gesund")

        assert "mit" not in words and "und" not in words
        assert words[0] == "lichterkinder"

    def test_spells_umlauts_out_like_the_dumps_do_not(self):
        assert "gefuehle" in search_words("Checker Tobi", "Der grosse Gefuehle-Check")


class TestCleanDescription:
    def test_keeps_simple_formatting(self):
        assert clean_description("<b>Fett</b> und <i>schräg</i><br>neu") == \
            "<b>Fett</b> und <i>schräg</i><br>neu"

    def test_drops_everything_that_could_act(self):
        cleaned = clean_description('<p onclick="x()">a</p><script>alert(1)</script>'
                                    '<a href="javascript:y()">b</a><img src=z>')

        assert cleaned == "<p>a</p>b"

    def test_text_is_escaped(self):
        assert clean_description("1 < 2 & 3") == "1 &lt; 2 &amp; 3"
