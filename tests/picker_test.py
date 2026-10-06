import src.ars_mediaevalis.picker as picker
import httpx
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def met_object(object_id: int = 1, **overrides) -> dict:
    """Returns a minimal but realistic Met /objects/{id} record with optional overrides."""

    obj: dict = {
        "objectID": object_id,
        "isPublicDomain": True,
        "primaryImage": f"https://images.metmuseum.org/CRDImages/cl/original/{object_id}.jpg",
        "primaryImageSmall": f"https://images.metmuseum.org/CRDImages/cl/web-large/{object_id}.jpg",
        "title": "The Annunciation",
        "artistDisplayName": "Robert Campin",
        "artistDisplayBio": "Netherlandish, ca. 1375–1444",
        "objectDate": "ca. 1427–32",
        "culture": "South Netherlandish",
        "city": "Tournai",
        "region": "",
        "country": "Belgium",
        "medium": "Oil on oak",
        "dimensions": "25 3/8 x 46 3/8 in.",
        "department": "The Cloisters",
        "objectURL": f"https://www.metmuseum.org/art/collection/search/{object_id}",
        "objectWikidata_URL": "https://www.wikidata.org/wiki/Q123",
        "artistWikidata_URL": "",
        "creditLine": "The Cloisters Collection, 1956",
    }
    obj.update(overrides)
    return obj


def mock_client(handler) -> httpx.Client:
    """Returns an httpx.Client whose requests are answered by handler instead of the network."""

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestToArtwork(unittest.TestCase):
    """Tests that to_artwork() converts Met records into Artwork objects as intended."""

    def test_valid_record_maps_fields(self) -> None:
        """Tests that a valid record maps onto the expected Artwork fields."""

        art: picker.Artwork | None = picker.to_artwork(met_object(42))
        self.assertIsInstance(art, picker.Artwork)
        self.assertEqual(art.object_id, 42)
        self.assertEqual(art.title, "The Annunciation")
        self.assertEqual(art.artist, "Robert Campin")
        self.assertEqual(art.date, "ca. 1427–32")
        self.assertTrue(art.image_url.endswith("/original/42.jpg"))
        self.assertTrue(art.thumb_url.endswith("/web-large/42.jpg"))
        self.assertEqual(art.wikidata_url, "https://www.wikidata.org/wiki/Q123")

    def test_not_public_domain_is_rejected(self) -> None:
        """Tests that a record with isPublicDomain False returns None."""

        self.assertIsNone(picker.to_artwork(met_object(isPublicDomain=False)))

    def test_missing_public_domain_flag_is_rejected(self) -> None:
        """Tests that a record without an isPublicDomain field returns None."""

        obj: dict = met_object()
        del obj["isPublicDomain"]
        self.assertIsNone(picker.to_artwork(obj))

    def test_empty_primary_image_is_rejected(self) -> None:
        """Tests that an empty, blank or missing primaryImage returns None."""

        for image in ("", "   ", None):
            with self.subTest(primaryImage=image):
                self.assertIsNone(picker.to_artwork(met_object(primaryImage=image)))

    def test_empty_strings_become_none(self) -> None:
        """Tests that empty or blank string fields are normalised to None."""

        art: picker.Artwork | None = picker.to_artwork(
            met_object(artistDisplayName="", artistWikidata_URL="  ", medium="")
        )
        self.assertIsNone(art.artist)
        self.assertIsNone(art.artist_wikidata_url)
        self.assertIsNone(art.medium)

    def test_strings_are_stripped(self) -> None:
        """Tests that surrounding whitespace is stripped from string fields."""

        art: picker.Artwork | None = picker.to_artwork(met_object(title="  Virgin and Child  "))
        self.assertEqual(art.title, "Virgin and Child")

    def test_missing_title_defaults_to_untitled(self) -> None:
        """Tests that an empty title becomes "Untitled"."""

        art: picker.Artwork | None = picker.to_artwork(met_object(title=""))
        self.assertEqual(art.title, "Untitled")

    def test_place_joins_non_empty_parts(self) -> None:
        """Tests that place joins the non-empty city, region and country fields."""

        cases: list[tuple[str, str, str, str | None]] = [
            ("Tournai", "", "Belgium", "Tournai, Belgium"),
            ("", "Burgundy", "France", "Burgundy, France"),
            ("Siena", "Tuscany", "Italy", "Siena, Tuscany, Italy"),
            ("", "", "", None),
        ]
        for city, region, country, expected in cases:
            with self.subTest(city=city, region=region, country=country):
                art: picker.Artwork | None = picker.to_artwork(
                    met_object(city=city, region=region, country=country)
                )
                self.assertEqual(art.place, expected)

    def test_object_url_falls_back_when_missing(self) -> None:
        """Tests that an empty objectURL falls back to the constructed Met URL."""

        art: picker.Artwork | None = picker.to_artwork(met_object(7, objectURL=""))
        self.assertEqual(art.object_url, "https://www.metmuseum.org/art/collection/search/7")

    def test_filled_later_fields_start_empty(self) -> None:
        """Tests that history, history_source and image_path start as None."""

        art: picker.Artwork | None = picker.to_artwork(met_object())
        self.assertIsNone(art.history)
        self.assertIsNone(art.history_source)
        self.assertIsNone(art.image_path)


class TestChoose(unittest.TestCase):
    """Tests that choose() selects artworks from the pool as intended."""

    def setUp(self) -> None:
        """Replaces met.get_object with a lookup in self.records and logs each requested ID."""

        self.records: dict[int, dict] = {}
        self.calls: list[int] = []

        def fake_get_object(client, object_id: int) -> dict:
            self.calls.append(object_id)
            return self.records[object_id]

        patcher = mock.patch.object(picker.met, "get_object", side_effect=fake_get_object)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_usable_artwork(self) -> None:
        """Tests that a single usable object in the pool is returned."""

        self.records[1] = met_object(1)
        art: picker.Artwork = picker.choose(None, [1], shown=set(), rejected=set())
        self.assertEqual(art.object_id, 1)

    def test_skips_and_records_unusable_objects(self) -> None:
        """Tests that unusable objects are skipped and added to rejected."""

        self.records[1] = met_object(1, isPublicDomain=False)
        self.records[2] = met_object(2, primaryImage="")
        self.records[3] = met_object(3)
        rejected: set[int] = set()

        art: picker.Artwork = picker.choose(None, [1, 2, 3], shown=set(), rejected=rejected)

        self.assertEqual(art.object_id, 3)
        # The pick order is random, so only some of 1 and 2 may have been tried.
        self.assertLessEqual(rejected, {1, 2})

    def test_never_picks_shown_ids(self) -> None:
        """Tests that IDs in shown are never requested."""

        self.records[1] = met_object(1)
        self.records[2] = met_object(2)
        for _ in range(20):
            self.calls.clear()
            art: picker.Artwork = picker.choose(None, [1, 2], shown={1}, rejected=set())
            self.assertEqual(art.object_id, 2)
            self.assertNotIn(1, self.calls)

    def test_never_picks_rejected_ids(self) -> None:
        """Tests that IDs in rejected are never requested."""

        self.records[2] = met_object(2)
        for _ in range(20):
            self.calls.clear()
            picker.choose(None, [1, 2], shown=set(), rejected={1})
            self.assertEqual(self.calls, [2])

    def test_never_asks_for_the_same_id_twice(self) -> None:
        """Tests that each ID is requested at most once per call."""

        for i in range(1, 6):
            self.records[i] = met_object(i, isPublicDomain=False)
        with self.assertRaises(RuntimeError):
            picker.choose(None, list(range(1, 6)), shown=set(), rejected=set(), max_attempts=5)
        self.assertEqual(sorted(self.calls), [1, 2, 3, 4, 5])

    def test_resets_history_when_pool_exhausted(self) -> None:
        """Tests that shown is cleared in place once every ID has been shown."""

        self.records[1] = met_object(1)
        shown: set[int] = {1}
        art: picker.Artwork = picker.choose(None, [1], shown=shown, rejected=set())
        self.assertEqual(art.object_id, 1)
        self.assertEqual(shown, set())

    def test_raises_after_max_attempts(self) -> None:
        """Tests that choose() gives up with RuntimeError after max_attempts."""

        for i in range(10):
            self.records[i] = met_object(i, isPublicDomain=False)
        with self.assertRaisesRegex(RuntimeError, "3 attempts"):
            picker.choose(None, list(range(10)), shown=set(), rejected=set(), max_attempts=3)
        self.assertEqual(len(self.calls), 3)

    def test_empty_pool_raises(self) -> None:
        """Tests current behaviour: an empty pool raises IndexError from random.choice."""

        with self.assertRaises(IndexError):
            picker.choose(None, [], shown=set(), rejected=set())


class TestDownload(unittest.TestCase):
    """Tests that download() saves images into the cache as intended."""

    def setUp(self) -> None:
        """Points XDG_CACHE_HOME at a temporary directory so ~/.cache is never touched."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_home: Path = Path(tmp.name)
        self.images: Path = self.cache_home / "ars_mediaevalis" / "images"

        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_writes_image_to_cache(self) -> None:
        """Tests that the image is written to cache/images/met-<id>.jpg with the right bytes."""

        payload: bytes = b"\xff\xd8\xff" + b"x" * 200_000  # larger than one 64 KiB chunk
        client: httpx.Client = mock_client(lambda req: httpx.Response(200, content=payload))

        provided_path: Path = picker.download(client, picker.to_artwork(met_object(99)))

        self.assertEqual(provided_path, self.images / "met-99.jpg")
        self.assertEqual(provided_path.read_bytes(), payload)

    def test_requests_the_primary_image_url(self) -> None:
        """Tests that download() requests the artwork's image_url."""

        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            return httpx.Response(200, content=b"img")

        art: picker.Artwork | None = picker.to_artwork(met_object(5))
        picker.download(mock_client(handler), art)
        self.assertEqual(seen, [art.image_url])

    def test_leaves_no_part_file(self) -> None:
        """Tests that the temporary .part file is gone after a successful download."""

        client: httpx.Client = mock_client(lambda req: httpx.Response(200, content=b"img"))
        provided_path: Path = picker.download(client, picker.to_artwork(met_object(1)))
        self.assertFalse(provided_path.with_suffix(".part").exists())

    def test_existing_file_is_not_redownloaded(self) -> None:
        """Tests that an image already in the cache is reused without a request."""

        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(200, content=b"new")

        client: httpx.Client = mock_client(handler)
        art: picker.Artwork | None = picker.to_artwork(met_object(3))
        first: Path = picker.download(client, art)
        first.write_bytes(b"old")

        second: Path = picker.download(client, art)

        self.assertEqual(second, first)
        self.assertEqual(second.read_bytes(), b"old")
        self.assertEqual(len(calls), 1)

    def test_http_error_raises_and_writes_nothing(self) -> None:
        """Tests that a 404 raises HTTPStatusError and leaves no file behind."""

        client: httpx.Client = mock_client(lambda req: httpx.Response(404))
        with self.assertRaises(httpx.HTTPStatusError):
            picker.download(client, picker.to_artwork(met_object(8)))
        self.assertFalse((self.images / "met-8.jpg").exists())
        self.assertFalse((self.images / "met-8.part").exists())


if __name__ == "__main__":
    unittest.main()
