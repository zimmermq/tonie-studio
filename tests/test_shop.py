"""Tests for what the tonies.com shop says about a tonie: looking it up, searching, keeping it."""

from tonie_studio.shop import API, BASE, SEARCH_REQUESTS, Shop, clean_description, search_words


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
            found = [p for p in self.products if p["salesId"] == params["salesId"]]
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

    def test_only_the_german_shop_is_asked(self, tmp_path):
        api = Api([MADAGASCAR])

        assert Shop(tmp_path, fetch=api).lookup("10001924", language="en-gb") is None
        assert api.asked == []


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
