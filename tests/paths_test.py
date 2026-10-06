import src.ars_mediaevalis.paths as p
import unittest
from pathlib import Path

class TestPathing(unittest.TestCase):
    """Tests that the pathing functions operate as intended."""

    def test_config_dir(self) -> None:
        """Tests that config_dir() returns .config/ars_mediaevalis/"""

        expected_path: Path = Path.home() / ".config" / "ars_mediaevalis"
        provided_path: Path = p.config_dir()
        self.assertEqual(expected_path, provided_path)
        self.assertTrue(provided_path.exists())

    def test_cache_dir(self) -> None:
        """Tests that cache_dir() returns .config/ars_mediaevalis/"""

        expected_path: Path = Path.home() / ".cache" / "ars_mediaevalis"
        provided_path: Path = p.cache_dir()
        self.assertEqual(expected_path, provided_path)
        self.assertTrue(provided_path.exists())

    def test_state_dir(self) -> None:
        """Tests that state_dir() returns .local/state/ars_mediaevalis/"""

        expected_path: Path = Path.home() / ".local" / "state" / "ars_mediaevalis"
        provided_path: Path = p.state_dir()
        self.assertEqual(expected_path, provided_path)
        self.assertTrue(provided_path.exists())

    def test_images_dir(self) -> None:
            """Tests that images_dir() returns .local/state/ars_mediaevalis/"""

            expected_path: Path = Path.home() / ".cache" / "ars_mediaevalis" / "images"
            provided_path: Path = p.images_dir()
            self.assertEqual(expected_path, provided_path)
            self.assertTrue(provided_path.exists())
