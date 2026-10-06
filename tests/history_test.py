import src.ars_mediaevalis.history as history
import logging
import unittest

import httpx
from tests.helpers import artwork_fields

history.log.addHandler(logging.NullHandler())  # keeps expected warnings out of the test output


def mock_client(handler) -> httpx.Client:
    """Returns an httpx.Client whose requests are answered by handler instead of the network."""

    return httpx.Client(transport=httpx.MockTransport(handler))


def wiki_handler(sitelinks: dict[str, str], summaries: dict[str, dict], seen: list[httpx.Request]):
    """
    Returns a handler that imitates Wikidata and Wikipedia: sitelinks maps a Q-ID to an
    English article title, summaries maps an article's URL slug to its summary payload.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        last: str = req.url.raw_path.decode().rsplit("/", 1)[-1]
        if req.url.host == "www.wikidata.org":
            qid: str = last.removesuffix(".json")
            if qid not in sitelinks:
                return httpx.Response(200, json={"entities": {qid: {"sitelinks": {}}}})
            return httpx.Response(200, json={
                "entities": {qid: {"sitelinks": {"enwiki": {"title": sitelinks[qid]}}}}
            })
        if last not in summaries:
            return httpx.Response(404, json={"title": "Not found."})
        return httpx.Response(200, json=summaries[last])

    return handler


def summary(extract: str, page: str = "https://en.wikipedia.org/wiki/Page", **extra) -> dict:
    """Returns a Wikipedia /page/summary payload."""

    return {"type": "standard", "extract": extract, "content_urls": {"desktop": {"page": page}}, **extra}


class TestQidFromUrl(unittest.TestCase):
    """Tests that qid_from_url() extracts Wikidata Q-IDs as intended."""

    def test_valid_urls(self) -> None:
        """Tests that the Q-ID is taken from the end of a Wikidata URL."""

        cases: list[tuple[str, str]] = [
            ("https://www.wikidata.org/wiki/Q29910832", "Q29910832"),
            ("https://www.wikidata.org/wiki/Q1/", "Q1"),
            ("http://www.wikidata.org/entity/Q42", "Q42"),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(history.qid_from_url(url), expected)

    def test_invalid_urls(self) -> None:
        """Tests that anything not ending in a Q-ID gives None."""

        for url in (None, "", "https://www.wikidata.org/wiki/", "https://www.wikidata.org/wiki/P170",
                    "https://www.wikidata.org/wiki/Q12a", "https://en.wikipedia.org/wiki/Quince"):
            with self.subTest(url=url):
                self.assertIsNone(history.qid_from_url(url))


class TestEnwikiTitle(unittest.TestCase):
    """Tests that enwiki_title() finds the English article of a Wikidata item as intended."""

    def test_returns_title(self) -> None:
        """Tests that the enwiki sitelink title is returned from the entity endpoint."""

        seen: list[httpx.Request] = []
        client: httpx.Client = mock_client(wiki_handler({"Q123": "Mérode Altarpiece"}, {}, seen))

        self.assertEqual(history.enwiki_title(client, "Q123"), "Mérode Altarpiece")
        self.assertEqual(str(seen[0].url), "https://www.wikidata.org/wiki/Special:EntityData/Q123.json")

    def test_redirected_item(self) -> None:
        """Tests that a merged item, returned under another Q-ID, is still read."""

        payload: dict = {"entities": {"Q999": {"sitelinks": {"enwiki": {"title": "Unicorn Tapestries"}}}}}
        client: httpx.Client = mock_client(lambda req: httpx.Response(200, json=payload))
        self.assertEqual(history.enwiki_title(client, "Q123"), "Unicorn Tapestries")

    def test_no_english_article(self) -> None:
        """Tests that an item with other sitelinks only, or none at all, gives None."""

        for sitelinks in ({}, {"itwiki": {"title": "Trittico di Mérode"}}):
            with self.subTest(sitelinks=sitelinks):
                payload: dict = {"entities": {"Q123": {"sitelinks": sitelinks}}}
                client: httpx.Client = mock_client(lambda req: httpx.Response(200, json=payload))
                self.assertIsNone(history.enwiki_title(client, "Q123"))

    def test_unknown_item_is_none(self) -> None:
        """Tests that a 404 for a deleted item gives None."""

        client: httpx.Client = mock_client(lambda req: httpx.Response(404))
        self.assertIsNone(history.enwiki_title(client, "Q123"))

    def test_server_error_raises(self) -> None:
        """Tests that a 500 raises HTTPStatusError."""

        client: httpx.Client = mock_client(lambda req: httpx.Response(500))
        with self.assertRaises(httpx.HTTPStatusError):
            history.enwiki_title(client, "Q123")


class TestWikipediaSummary(unittest.TestCase):
    """Tests that wikipedia_summary() fetches an article's lead paragraph as intended."""

    def test_returns_extract_and_url(self) -> None:
        """Tests that the extract and the desktop page URL are returned."""

        page: str = "https://en.wikipedia.org/wiki/M%C3%A9rode_Altarpiece"
        client: httpx.Client = mock_client(lambda req: httpx.Response(200, json=summary(" A triptych. ", page)))
        self.assertEqual(history.wikipedia_summary(client, "Mérode Altarpiece"), ("A triptych.", page))

    def test_title_is_encoded_into_the_path(self) -> None:
        """Tests that spaces become underscores and other characters are percent-encoded."""

        cases: list[tuple[str, str]] = [
            ("Unicorn Tapestries", "Unicorn_Tapestries"),
            ("Mérode Altarpiece", "M%C3%A9rode_Altarpiece"),
            ("Virgin and Child (Duccio)", "Virgin_and_Child_%28Duccio%29"),
            ("AC/DC", "AC%2FDC"),
            ("What?", "What%3F"),
        ]
        for title, slug in cases:
            with self.subTest(title=title):
                seen: list[httpx.Request] = []
                client: httpx.Client = mock_client(wiki_handler({}, {slug: summary("Text.")}, seen))
                self.assertIsNotNone(history.wikipedia_summary(client, title))
                self.assertEqual(seen[0].url.raw_path.decode(), f"/api/rest_v1/page/summary/{slug}")

    def test_missing_page_is_none(self) -> None:
        """Tests that a 404 gives None rather than raising."""

        client: httpx.Client = mock_client(lambda req: httpx.Response(404))
        self.assertIsNone(history.wikipedia_summary(client, "No such page"))

    def test_disambiguation_page_is_none(self) -> None:
        """Tests that a disambiguation page is skipped."""

        payload: dict = summary("Annunciation may refer to:", type="disambiguation")
        client: httpx.Client = mock_client(lambda req: httpx.Response(200, json=payload))
        self.assertIsNone(history.wikipedia_summary(client, "Annunciation"))

    def test_empty_extract_is_none(self) -> None:
        """Tests that a page with an empty, blank or missing extract gives None."""

        for payload in (summary(""), summary("   "), {"type": "standard"}):
            with self.subTest(payload=payload):
                client: httpx.Client = mock_client(lambda req: httpx.Response(200, json=payload))
                self.assertIsNone(history.wikipedia_summary(client, "Page"))

    def test_missing_page_url_is_none(self) -> None:
        """Tests that a summary without content_urls still returns the extract."""

        payload: dict = {"type": "standard", "extract": "A triptych."}
        client: httpx.Client = mock_client(lambda req: httpx.Response(200, json=payload))
        self.assertEqual(history.wikipedia_summary(client, "Page"), ("A triptych.", None))

    def test_server_error_raises(self) -> None:
        """Tests that a 503 raises HTTPStatusError."""

        client: httpx.Client = mock_client(lambda req: httpx.Response(503))
        with self.assertRaises(httpx.HTTPStatusError):
            history.wikipedia_summary(client, "Page")


class TestCatalogueBlurb(unittest.TestCase):
    """Tests that catalogue_blurb() composes readable sentences from whatever fields exist."""

    def blurb(self, **overrides) -> str:
        """Returns the blurb of an artwork that has only the given fields."""

        empty: dict = dict.fromkeys(("place", "culture", "date", "medium", "department"))
        return history.catalogue_blurb(history.Artwork(**artwork_fields(**{**empty, **overrides})))

    def test_all_fields(self) -> None:
        """Tests the full sentence, with the medium lower-cased."""

        self.assertEqual(
            self.blurb(place="Tournai, Belgium", date="ca. 1427–32", medium="Oil on oak", department="The Cloisters"),
            "Made in Tournai, Belgium, ca. 1427–32, in oil on oak. Now in The Cloisters at The Met.",
        )

    def test_missing_fields(self) -> None:
        """Tests that every combination of missing fields still reads as a sentence."""

        cases: list[tuple[dict, str]] = [
            ({"place": "Siena"}, "Made in Siena."),
            ({"culture": "French"}, "French."),
            ({"culture": "French", "date": "ca. 1300", "medium": "Ivory"}, "French, ca. 1300, in ivory."),
            ({"date": "ca. 1300"}, "Ca. 1300."),
            ({"medium": "Tempera on wood"}, "Made in tempera on wood."),
            ({"department": "Medieval Art"}, "Now in Medieval Art at The Met."),
            ({"place": "Siena", "medium": "Tempera on wood"}, "Made in Siena, in tempera on wood."),
            ({"date": "1420", "medium": "Tempera on wood"}, "1420, in tempera on wood."),
            ({"date": "1420", "department": "Medieval Art"}, "1420. Now in Medieval Art at The Met."),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(self.blurb(**fields), expected)

    def test_place_is_preferred_over_culture(self) -> None:
        """Tests that culture is only used when there is no place."""

        self.assertEqual(self.blurb(place="Tournai", culture="South Netherlandish"), "Made in Tournai.")

    def test_medium_keeps_inner_capitals(self) -> None:
        """Tests that only the first letter of the medium is lower-cased."""

        self.assertEqual(self.blurb(medium="Champlevé enamel, Limoges"), "Made in champlevé enamel, Limoges.")

    def test_nothing_catalogued(self) -> None:
        """Tests the placeholder for an artwork without any of the fields."""

        self.assertEqual(self.blurb(), "No further details are catalogued.")


class TestEnrich(unittest.TestCase):
    """Tests that enrich() fills in the history with the right fallback as intended."""

    def setUp(self) -> None:
        """Creates an artwork linked to Q123 (the work) and Q456 (the artist)."""

        self.art: history.Artwork = history.Artwork(**artwork_fields())
        self.seen: list[httpx.Request] = []

    def client(self, sitelinks: dict[str, str], summaries: dict[str, dict]) -> httpx.Client:
        """Returns a client backed by the fake Wikidata and Wikipedia."""

        return mock_client(wiki_handler(sitelinks, summaries, self.seen))

    def test_object_article_is_preferred(self) -> None:
        """Tests that the work's own article wins and the artist is not looked up."""

        page: str = "https://en.wikipedia.org/wiki/M%C3%A9rode_Altarpiece"
        client: httpx.Client = self.client(
            {"Q123": "Mérode Altarpiece", "Q456": "Robert Campin"},
            {"M%C3%A9rode_Altarpiece": summary("A triptych.", page), "Robert_Campin": summary("A painter.")},
        )

        history.enrich(client, self.art)

        self.assertEqual(self.art.history, "A triptych.")
        self.assertEqual(self.art.history_source, "wikipedia:object")
        self.assertEqual(self.art.history_url, page)
        self.assertEqual(len(self.seen), 2)

    def test_falls_back_to_artist_article(self) -> None:
        """Tests that the artist's article is used, and labelled as such, when the work has none."""

        client: httpx.Client = self.client({"Q456": "Robert Campin"}, {"Robert_Campin": summary("A painter.")})

        history.enrich(client, self.art)

        self.assertEqual(self.art.history, "A painter.")
        self.assertEqual(self.art.history_source, "wikipedia:artist")

    def test_falls_back_to_catalogue(self) -> None:
        """Tests that the catalogue blurb is used when neither article exists."""

        history.enrich(self.client({}, {}), self.art)

        self.assertEqual(self.art.history, history.catalogue_blurb(self.art))
        self.assertEqual(self.art.history_source, "catalogue")
        self.assertIsNone(self.art.history_url)

    def test_no_wikidata_links_makes_no_request(self) -> None:
        """Tests that an artwork without Wikidata URLs goes straight to the catalogue."""

        self.art.wikidata_url = None
        self.art.artist_wikidata_url = None

        history.enrich(self.client({}, {}), self.art)

        self.assertEqual(self.art.history_source, "catalogue")
        self.assertEqual(self.seen, [])

    def test_object_lookup_failure_falls_through_to_artist(self) -> None:
        """Tests that an error while looking up the work does not prevent the artist lookup."""

        def handler(req: httpx.Request) -> httpx.Response:
            if "Q123" in str(req.url):
                return httpx.Response(500)
            if req.url.host == "www.wikidata.org":
                return httpx.Response(200, json={
                    "entities": {"Q456": {"sitelinks": {"enwiki": {"title": "Robert Campin"}}}}
                })
            return httpx.Response(200, json=summary("A painter."))

        history.enrich(mock_client(handler), self.art)

        self.assertEqual(self.art.history_source, "wikipedia:artist")

    def test_never_raises(self) -> None:
        """Tests that network errors, bad JSON and odd payloads all end in the catalogue blurb."""

        def offline(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no network", request=req)

        handlers: dict = {
            "offline": offline,
            "server error": lambda req: httpx.Response(503),
            "not json": lambda req: httpx.Response(200, content=b"<html>"),
            "json list": lambda req: httpx.Response(200, json=[1, 2, 3]),
            "sitelink without title": lambda req: httpx.Response(200, json={
                "entities": {"Q1": {"sitelinks": {"enwiki": {}}}}
            }),
        }
        for name, handler in handlers.items():
            with self.subTest(case=name):
                art: history.Artwork = history.Artwork(**artwork_fields())
                history.enrich(mock_client(handler), art)
                self.assertEqual(art.history_source, "catalogue")
                self.assertTrue(art.history)


if __name__ == "__main__":
    unittest.main()
