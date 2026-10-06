import src.ars_mediaevalis.viewer as viewer
import unittest

from tests.helpers import artwork_fields


class TestMetaLine(unittest.TestCase):
    """Tests that meta_line() joins the catalogue facts as intended."""

    def line(self, **overrides) -> str:
        """Returns the meta line of an artwork with the given overrides."""

        return viewer.meta_line(viewer.Artwork(**artwork_fields(**overrides)))

    def test_all_fields(self) -> None:
        """Tests the order: maker, bio, date, place, medium, dimensions."""

        self.assertEqual(
            self.line(),
            "Robert Campin · Netherlandish, ca. 1375–1444 · ca. 1427–32 · Tournai, Belgium"
            " · Oil on oak · 25 3/8 x 46 3/8 in.",
        )

    def test_culture_replaces_missing_artist(self) -> None:
        """Tests that the culture stands in for an unknown artist."""

        self.assertTrue(self.line(artist=None, artist_bio=None).startswith("South Netherlandish · ca. 1427–32"))

    def test_missing_fields_are_skipped(self) -> None:
        """Tests that missing fields leave no stray separators."""

        self.assertEqual(
            self.line(artist_bio=None, place=None, dimensions=None),
            "Robert Campin · ca. 1427–32 · Oil on oak",
        )

    def test_multi_line_field_stays_on_one_line(self) -> None:
        """Tests that the Met's multi-line dimensions are joined with semicolons."""

        self.assertTrue(self.line(dimensions="Overall: 69 x 45 in.\r\nPainted surface: 63 x 45 in.").endswith(
            "Oil on oak · Overall: 69 x 45 in.; Painted surface: 63 x 45 in."
        ))

    def test_nothing_known(self) -> None:
        """Tests that an artwork without any fact gives an empty line."""

        empty: dict = dict.fromkeys(("artist", "culture", "artist_bio", "date", "place", "medium", "dimensions"))
        self.assertEqual(self.line(**empty), "")


class TestGallery(unittest.TestCase):
    """Tests that gallery() orders the artworks the window pages through as intended."""

    def art(self, object_id: int) -> viewer.Artwork:
        """Returns an artwork with the given ID."""

        return viewer.Artwork(**artwork_fields(object_id))

    def test_opens_on_the_current_artwork(self) -> None:
        """Tests that the cached order is kept and the index points at the current wallpaper."""

        cached: list[viewer.Artwork] = [self.art(3), self.art(2), self.art(1)]
        works, index = viewer.gallery(self.art(2), cached)
        self.assertEqual([a.object_id for a in works], [3, 2, 1])
        self.assertEqual(index, 1)

    def test_current_artwork_missing_from_cache_is_put_first(self) -> None:
        """Tests that a current artwork the cache does not hold is still shown, in front."""

        works, index = viewer.gallery(self.art(9), [self.art(2), self.art(1)])
        self.assertEqual([a.object_id for a in works], [9, 2, 1])
        self.assertEqual(index, 0)

    def test_no_current_artwork_opens_on_the_newest(self) -> None:
        """Tests that without a current artwork the newest cached one is shown."""

        works, index = viewer.gallery(None, [self.art(2), self.art(1)])
        self.assertEqual(([a.object_id for a in works], index), ([2, 1], 0))

    def test_nothing_to_show(self) -> None:
        """Tests that no current artwork and an empty cache give an empty gallery."""

        self.assertEqual(viewer.gallery(None, []), ([], 0))

    def test_only_the_current_artwork(self) -> None:
        """Tests that a first day, with an empty cache, shows just today's artwork."""

        works, index = viewer.gallery(self.art(1), [])
        self.assertEqual(([a.object_id for a in works], index), ([1], 0))


class TestAppId(unittest.TestCase):
    """Tests the application ID that Hyprland window rules match on."""

    def test_app_id(self) -> None:
        """Tests that the ID is the documented reverse-DNS name."""

        self.assertEqual(viewer.APP_ID, "io.github.claudiormalvino.ArsMediaevalis")


if __name__ == "__main__":
    unittest.main()
