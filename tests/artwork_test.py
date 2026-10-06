import src.ars_mediaevalis.artwork as artwork
import dataclasses
import httpx
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

artwork.log.addHandler(logging.NullHandler())  # keeps expected warnings out of the test output


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
        "classification": "Paintings",
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

        art: artwork.Artwork | None = artwork.to_artwork(met_object(42))
        self.assertIsInstance(art, artwork.Artwork)
        self.assertEqual(art.object_id, 42)
        self.assertEqual(art.title, "The Annunciation")
        self.assertEqual(art.artist, "Robert Campin")
        self.assertEqual(art.date, "ca. 1427–32")
        self.assertTrue(art.image_url.endswith("/original/42.jpg"))
        self.assertTrue(art.thumb_url.endswith("/web-large/42.jpg"))
        self.assertEqual(art.wikidata_url, "https://www.wikidata.org/wiki/Q123")

    def test_not_public_domain_is_rejected(self) -> None:
        """Tests that a record with isPublicDomain False returns None."""

        self.assertIsNone(artwork.to_artwork(met_object(isPublicDomain=False)))

    def test_missing_public_domain_flag_is_rejected(self) -> None:
        """Tests that a record without an isPublicDomain field returns None."""

        obj: dict = met_object()
        del obj["isPublicDomain"]
        self.assertIsNone(artwork.to_artwork(obj))

    def test_empty_primary_image_is_rejected(self) -> None:
        """Tests that an empty, blank or missing primaryImage returns None."""

        for image in ("", "   ", None):
            with self.subTest(primaryImage=image):
                self.assertIsNone(artwork.to_artwork(met_object(primaryImage=image)))

    def test_paintings_are_accepted(self) -> None:
        """Tests that "Paintings" and its sub-kinds count as paintings."""

        for classification in ("Paintings", "Paintings-Panels", "Paintings-Fresco", " Paintings "):
            with self.subTest(classification=classification):
                self.assertIsNotNone(artwork.to_artwork(met_object(classification=classification)))

    def test_other_kinds_are_rejected(self) -> None:
        """Tests that manuscripts, textiles, sculptures, facsimiles and unclassified objects return None."""

        for classification in ("Manuscripts and Illuminations", "Textiles-Embroidered", "Sculpture-Miniature-Wood",
                               "Reproductions-Paintings", "Mosaics", "Drawings", "paintings", "", None):
            with self.subTest(classification=classification):
                self.assertIsNone(artwork.to_artwork(met_object(classification=classification)))

    def test_missing_classification_is_rejected(self) -> None:
        """Tests that a record without a classification field returns None."""

        obj: dict = met_object()
        del obj["classification"]
        self.assertIsNone(artwork.to_artwork(obj))

    def test_empty_strings_become_none(self) -> None:
        """Tests that empty or blank string fields are normalised to None."""

        art: artwork.Artwork | None = artwork.to_artwork(
            met_object(artistDisplayName="", artistWikidata_URL="  ", medium="")
        )
        self.assertIsNone(art.artist)
        self.assertIsNone(art.artist_wikidata_url)
        self.assertIsNone(art.medium)

    def test_strings_are_stripped(self) -> None:
        """Tests that surrounding whitespace is stripped from string fields."""

        art: artwork.Artwork | None = artwork.to_artwork(met_object(title="  Virgin and Child  "))
        self.assertEqual(art.title, "Virgin and Child")

    def test_missing_title_defaults_to_untitled(self) -> None:
        """Tests that an empty title becomes "Untitled"."""

        art: artwork.Artwork | None = artwork.to_artwork(met_object(title=""))
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
                art: artwork.Artwork | None = artwork.to_artwork(
                    met_object(city=city, region=region, country=country)
                )
                self.assertEqual(art.place, expected)

    def test_object_url_falls_back_when_missing(self) -> None:
        """Tests that an empty objectURL falls back to the constructed Met URL."""

        art: artwork.Artwork | None = artwork.to_artwork(met_object(7, objectURL=""))
        self.assertEqual(art.object_url, "https://www.metmuseum.org/art/collection/search/7")

    def test_filled_later_fields_start_empty(self) -> None:
        """Tests that history, history_source, history_url and image_path start as None."""

        art: artwork.Artwork | None = artwork.to_artwork(met_object())
        self.assertIsNone(art.history)
        self.assertIsNone(art.history_source)
        self.assertIsNone(art.history_url)
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

        patcher = mock.patch.object(artwork.met, "get_object", side_effect=fake_get_object)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_usable_artwork(self) -> None:
        """Tests that a single usable object in the pool is returned."""

        self.records[1] = met_object(1)
        art: artwork.Artwork = artwork.choose(None, [1], shown=set(), rejected=set())
        self.assertEqual(art.object_id, 1)

    def test_skips_and_records_unusable_objects(self) -> None:
        """Tests that unusable objects are skipped and added to rejected."""

        self.records[1] = met_object(1, isPublicDomain=False)
        self.records[2] = met_object(2, primaryImage="")
        self.records[3] = met_object(3)
        rejected: set[int] = set()

        art: artwork.Artwork = artwork.choose(None, [1, 2, 3], shown=set(), rejected=rejected)

        self.assertEqual(art.object_id, 3)
        # The pick order is random, so only some of 1 and 2 may have been tried.
        self.assertLessEqual(rejected, {1, 2})

    def test_never_picks_shown_ids(self) -> None:
        """Tests that IDs in shown are never requested."""

        self.records[1] = met_object(1)
        self.records[2] = met_object(2)
        for _ in range(20):
            self.calls.clear()
            art: artwork.Artwork = artwork.choose(None, [1, 2], shown={1}, rejected=set())
            self.assertEqual(art.object_id, 2)
            self.assertNotIn(1, self.calls)

    def test_never_picks_rejected_ids(self) -> None:
        """Tests that IDs in rejected are never requested."""

        self.records[2] = met_object(2)
        for _ in range(20):
            self.calls.clear()
            artwork.choose(None, [1, 2], shown=set(), rejected={1})
            self.assertEqual(self.calls, [2])

    def test_never_asks_for_the_same_id_twice(self) -> None:
        """Tests that each ID is requested at most once per call."""

        for i in range(1, 6):
            self.records[i] = met_object(i, isPublicDomain=False)
        with self.assertRaises(RuntimeError):
            artwork.choose(None, list(range(1, 6)), shown=set(), rejected=set(), max_attempts=5)
        self.assertEqual(sorted(self.calls), [1, 2, 3, 4, 5])

    def test_resets_history_when_pool_exhausted(self) -> None:
        """Tests that shown is cleared in place once every ID has been shown."""

        self.records[1] = met_object(1)
        shown: set[int] = {1}
        art: artwork.Artwork = artwork.choose(None, [1], shown=shown, rejected=set())
        self.assertEqual(art.object_id, 1)
        self.assertEqual(shown, set())

    def test_raises_after_max_attempts(self) -> None:
        """Tests that choose() gives up with RuntimeError after max_attempts."""

        for i in range(10):
            self.records[i] = met_object(i, isPublicDomain=False)
        with self.assertRaisesRegex(RuntimeError, "3 attempts"):
            artwork.choose(None, list(range(10)), shown=set(), rejected=set(), max_attempts=3)
        self.assertEqual(len(self.calls), 3)

    def test_empty_pool_raises(self) -> None:
        """Tests that an empty pool raises RuntimeError without any request."""

        with self.assertRaisesRegex(RuntimeError, "no usable artworks"):
            artwork.choose(None, [], shown=set(), rejected=set())
        self.assertEqual(self.calls, [])

    def test_fully_rejected_pool_raises(self) -> None:
        """Tests that a pool whose every ID is rejected raises RuntimeError without any request."""

        with self.assertRaisesRegex(RuntimeError, "no usable artworks"):
            artwork.choose(None, [1, 2], shown={1}, rejected={1, 2})
        self.assertEqual(self.calls, [])

    def test_small_pool_stops_when_exhausted(self) -> None:
        """Tests that a pool smaller than max_attempts is tried once and then given up on."""

        self.records[1] = met_object(1, isPublicDomain=False)
        rejected: set[int] = set()
        with self.assertRaises(RuntimeError):
            artwork.choose(None, [1], shown=set(), rejected=rejected, max_attempts=15)
        self.assertEqual(self.calls, [1])
        self.assertEqual(rejected, {1})


class TestFromDict(unittest.TestCase):
    """Tests that from_dict() rebuilds a stored Artwork as intended."""

    def test_round_trip(self) -> None:
        """Tests that asdict() followed by from_dict() gives an equal Artwork."""

        art: artwork.Artwork | None = artwork.to_artwork(met_object(42))
        art.history = "A triptych."
        art.image_path = "/tmp/met-42.jpg"
        self.assertEqual(artwork.from_dict(dataclasses.asdict(art)), art)

    def test_unknown_keys_are_ignored(self) -> None:
        """Tests that fields written by another version do not break loading."""

        data: dict = dataclasses.asdict(artwork.to_artwork(met_object(42)))
        data["wallpaper_path"] = "/tmp/old.jpg"
        self.assertEqual(artwork.from_dict(data).object_id, 42)

    def test_missing_optional_fields_use_defaults(self) -> None:
        """Tests that a record stored without the filled-later fields still loads."""

        data: dict = dataclasses.asdict(artwork.to_artwork(met_object(42)))
        for key in ("history", "history_source", "history_url", "image_path"):
            del data[key]
        self.assertIsNone(artwork.from_dict(data).history_url)

    def test_unusable_data_is_none(self) -> None:
        """Tests that empty, incomplete or non-dict data gives None."""

        for data in (None, {}, {"title": "Only a title"}, [1, 2], "met-42"):
            with self.subTest(data=data):
                self.assertIsNone(artwork.from_dict(data))


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

        provided_path: Path = artwork.download(client, artwork.to_artwork(met_object(99)))

        self.assertEqual(provided_path, self.images / "met-99.jpg")
        self.assertEqual(provided_path.read_bytes(), payload)

    def test_requests_the_primary_image_url(self) -> None:
        """Tests that download() requests the artwork's image_url."""

        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            return httpx.Response(200, content=b"img")

        art: artwork.Artwork | None = artwork.to_artwork(met_object(5))
        artwork.download(mock_client(handler), art)
        self.assertEqual(seen, [art.image_url])

    def test_leaves_no_part_file(self) -> None:
        """Tests that the temporary .part file is gone after a successful download."""

        client: httpx.Client = mock_client(lambda req: httpx.Response(200, content=b"img"))
        provided_path: Path = artwork.download(client, artwork.to_artwork(met_object(1)))
        self.assertFalse(provided_path.with_suffix(".part").exists())

    def test_existing_file_is_not_redownloaded(self) -> None:
        """Tests that an image already in the cache is reused without a request."""

        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(200, content=b"new")

        client: httpx.Client = mock_client(handler)
        art: artwork.Artwork | None = artwork.to_artwork(met_object(3))
        first: Path = artwork.download(client, art)
        first.write_bytes(b"old")

        second: Path = artwork.download(client, art)

        self.assertEqual(second, first)
        self.assertEqual(second.read_bytes(), b"old")
        self.assertEqual(len(calls), 1)

    def test_http_error_raises_and_writes_nothing(self) -> None:
        """Tests that a 404 raises HTTPStatusError and leaves no file behind."""

        client: httpx.Client = mock_client(lambda req: httpx.Response(404))
        with self.assertRaises(httpx.HTTPStatusError):
            artwork.download(client, artwork.to_artwork(met_object(8)))
        self.assertFalse((self.images / "met-8.jpg").exists())
        self.assertFalse((self.images / "met-8.part").exists())


    def test_interrupted_download_leaves_nothing(self) -> None:
        """Tests that a connection dropped mid-download leaves neither the image nor a .part file."""

        def body():
            yield b"\xff\xd8\xff"
            raise httpx.ReadError("connection reset")

        client: httpx.Client = mock_client(lambda req: httpx.Response(200, content=body()))
        with self.assertRaises(httpx.ReadError):
            artwork.download(client, artwork.to_artwork(met_object(9)))
        self.assertEqual(list(self.images.iterdir()), [])


class TestCached(unittest.TestCase):
    """Tests that remember(), cached() and find_cached() keep earlier artworks available offline."""

    def setUp(self) -> None:
        """Points XDG_CACHE_HOME at a temporary directory."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.images: Path = Path(tmp.name) / "ars_mediaevalis" / "images"

        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def remember(self, object_id: int, age: int = 0, image: bool = True, **overrides) -> artwork.Artwork:
        """Remembers an artwork whose details were written `age` seconds ago, with or without its image."""

        art: artwork.Artwork = artwork.to_artwork(met_object(object_id, **overrides))
        self.images.mkdir(parents=True, exist_ok=True)
        if image:
            (self.images / f"met-{object_id}.jpg").write_bytes(b"img")
            art.image_path = str(self.images / f"met-{object_id}.jpg")
        artwork.remember(art)
        stamp: float = 1_800_000_000 - age
        os.utime(self.images / f"met-{object_id}.json", (stamp, stamp))
        return art

    def test_empty_cache(self) -> None:
        """Tests that nothing is listed, or found, before anything was remembered."""

        self.assertEqual(artwork.cached(), [])
        self.assertIsNone(artwork.find_cached(1))

    def test_remembered_artwork_is_listed_unchanged(self) -> None:
        """Tests that a remembered artwork comes back with every field intact."""

        art: artwork.Artwork = self.remember(42, title="Vierge à l'Enfant")
        art.history = "A panel."
        artwork.remember(art)
        self.assertEqual(artwork.cached(), [art])

    def test_details_are_stored_next_to_the_image(self) -> None:
        """Tests that the details land in images/met-<id>.json and no temporary file is left."""

        self.remember(42)
        self.assertEqual(sorted(p.name for p in self.images.iterdir()), ["met-42.jpg", "met-42.json"])

    def test_newest_first(self) -> None:
        """Tests that the artworks are ordered from the most recently remembered to the oldest."""

        self.remember(1, age=300)
        self.remember(2, age=100)
        self.remember(3, age=200)
        self.assertEqual([art.object_id for art in artwork.cached()], [2, 3, 1])

    def test_artwork_without_image_is_not_listed(self) -> None:
        """Tests that details whose image is gone from the cache are left out."""

        self.remember(1)
        self.remember(2, image=False)
        self.assertEqual([art.object_id for art in artwork.cached()], [1])
        self.assertIsNone(artwork.find_cached(2))

    def test_image_without_details_is_not_listed(self) -> None:
        """Tests that a stray image, wallpaper or thumbnail without details is ignored."""

        self.remember(1)
        for name in ("met-2.jpg", "met-1-wall-800x600-m6-b45-u15.jpg", "met-1-thumb.png"):
            (self.images / name).write_bytes(b"img")
        self.assertEqual([art.object_id for art in artwork.cached()], [1])

    def test_unreadable_details_are_skipped(self) -> None:
        """Tests that corrupt or incomplete details do not hide the other artworks."""

        self.remember(1)
        for object_id, content in ((2, "{not json"), (3, '{"title": "Only a title"}'), (4, "[1, 2]")):
            (self.images / f"met-{object_id}.jpg").write_bytes(b"img")
            (self.images / f"met-{object_id}.json").write_text(content)
        self.assertEqual([art.object_id for art in artwork.cached()], [1])

    def test_image_path_follows_the_cache(self) -> None:
        """Tests that image_path points into the current cache even if the stored one is outdated."""

        art: artwork.Artwork = self.remember(1)
        art.image_path = "/somewhere/else/met-1.jpg"
        artwork.remember(art)
        self.assertEqual(artwork.cached()[0].image_path, str(self.images / "met-1.jpg"))

    def test_find_cached(self) -> None:
        """Tests that one artwork is found by its ID among several."""

        self.remember(1)
        self.remember(2, title="Saint Jerome")
        self.assertEqual(artwork.find_cached(2).title, "Saint Jerome")
        self.assertIsNone(artwork.find_cached(3))


if __name__ == "__main__":
    unittest.main()
