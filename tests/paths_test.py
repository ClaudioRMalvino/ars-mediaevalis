import src.ars_mediaevalis.paths as p
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

XDG_VARS: tuple[str, ...] = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")


class PathsTestCase(unittest.TestCase):
    """Base class that keeps every pathing test away from the real home directory."""

    def setUp(self) -> None:
        """Points HOME at a temporary directory and clears the XDG variables."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home: Path = Path(tmp.name)

        patcher = mock.patch.dict(os.environ, {"HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in XDG_VARS:
            os.environ.pop(var, None)


class TestDefaultPaths(PathsTestCase):
    """Tests that the pathing functions fall back to the home directory as intended."""

    def test_config_dir(self) -> None:
        """Tests that config_dir() returns ~/.config/ars_mediaevalis/ and creates it."""

        provided_path: Path = p.config_dir()
        self.assertEqual(provided_path, self.home / ".config" / "ars_mediaevalis")
        self.assertTrue(provided_path.is_dir())

    def test_cache_dir(self) -> None:
        """Tests that cache_dir() returns ~/.cache/ars_mediaevalis/ and creates it."""

        provided_path: Path = p.cache_dir()
        self.assertEqual(provided_path, self.home / ".cache" / "ars_mediaevalis")
        self.assertTrue(provided_path.is_dir())

    def test_state_dir(self) -> None:
        """Tests that state_dir() returns ~/.local/state/ars_mediaevalis/ and creates it."""

        provided_path: Path = p.state_dir()
        self.assertEqual(provided_path, self.home / ".local" / "state" / "ars_mediaevalis")
        self.assertTrue(provided_path.is_dir())

    def test_images_dir(self) -> None:
        """Tests that images_dir() returns ~/.cache/ars_mediaevalis/images/ and creates it."""

        provided_path: Path = p.images_dir()
        self.assertEqual(provided_path, self.home / ".cache" / "ars_mediaevalis" / "images")
        self.assertTrue(provided_path.is_dir())

    def test_empty_xdg_variable_falls_back_to_default(self) -> None:
        """Tests that an XDG variable set to "" is treated as unset."""

        with mock.patch.dict(os.environ, {var: "" for var in XDG_VARS}):
            self.assertEqual(p.config_dir(), self.home / ".config" / "ars_mediaevalis")
            self.assertEqual(p.cache_dir(), self.home / ".cache" / "ars_mediaevalis")
            self.assertEqual(p.state_dir(), self.home / ".local" / "state" / "ars_mediaevalis")


class TestXdgOverrides(PathsTestCase):
    """Tests that the pathing functions honour the XDG environment variables."""

    def test_each_function_reads_its_own_variable(self) -> None:
        """Tests that each function uses its XDG variable and appends the app name."""

        cases: list[tuple[str, callable]] = [
            ("XDG_CONFIG_HOME", p.config_dir),
            ("XDG_CACHE_HOME", p.cache_dir),
            ("XDG_STATE_HOME", p.state_dir),
        ]
        for var, func in cases:
            with self.subTest(var=var):
                base: Path = self.home / "custom" / var.lower()
                with mock.patch.dict(os.environ, {var: str(base)}):
                    provided_path: Path = func()
                self.assertEqual(provided_path, base / "ars_mediaevalis")
                self.assertTrue(provided_path.is_dir())

    def test_variables_do_not_affect_each_other(self) -> None:
        """Tests that setting one XDG variable leaves the other directories at their defaults."""

        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.home / "elsewhere")}):
            self.assertEqual(p.cache_dir(), self.home / "elsewhere" / "ars_mediaevalis")
            self.assertEqual(p.config_dir(), self.home / ".config" / "ars_mediaevalis")
            self.assertEqual(p.state_dir(), self.home / ".local" / "state" / "ars_mediaevalis")

    def test_images_dir_follows_cache_home(self) -> None:
        """Tests that images_dir() lives inside the overridden cache directory."""

        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.home / "elsewhere")}):
            provided_path: Path = p.images_dir()
            self.assertEqual(provided_path, p.cache_dir() / "images")
        self.assertEqual(provided_path, self.home / "elsewhere" / "ars_mediaevalis" / "images")
        self.assertTrue(provided_path.is_dir())

    def test_environment_is_read_on_every_call(self) -> None:
        """Tests that a change to the variable between calls is picked up."""

        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.home / "first")}):
            first: Path = p.cache_dir()
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.home / "second")}):
            second: Path = p.cache_dir()
        self.assertEqual(first, self.home / "first" / "ars_mediaevalis")
        self.assertEqual(second, self.home / "second" / "ars_mediaevalis")


class TestDirectoryCreation(PathsTestCase):
    """Tests that the pathing functions create their directories as intended."""

    def test_missing_parents_are_created(self) -> None:
        """Tests that a base directory several levels deep is created along with its parents."""

        base: Path = self.home / "a" / "b" / "c"
        self.assertFalse(base.exists())
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(base)}):
            self.assertTrue(p.state_dir().is_dir())

    def test_functions_return_path_objects(self) -> None:
        """Tests that every function returns a Path."""

        for func in (p.config_dir, p.cache_dir, p.state_dir, p.images_dir):
            with self.subTest(func=func.__name__):
                self.assertIsInstance(func(), Path)

    def test_repeated_calls_are_idempotent(self) -> None:
        """Tests that calling a function twice returns the same path without raising."""

        for func in (p.config_dir, p.cache_dir, p.state_dir, p.images_dir):
            with self.subTest(func=func.__name__):
                self.assertEqual(func(), func())

    def test_existing_contents_are_preserved(self) -> None:
        """Tests that files already in a directory survive later calls."""

        for func in (p.config_dir, p.cache_dir, p.state_dir, p.images_dir):
            with self.subTest(func=func.__name__):
                file: Path = func() / "keep.txt"
                file.write_text("medieval")
                func()
                self.assertEqual(file.read_text(), "medieval")

    def test_file_in_the_way_raises(self) -> None:
        """Tests that a regular file where the directory should be raises FileExistsError."""

        (self.home / ".config").mkdir()
        (self.home / ".config" / "ars_mediaevalis").write_text("not a directory")
        with self.assertRaises(FileExistsError):
            p.config_dir()


if __name__ == "__main__":
    unittest.main()
