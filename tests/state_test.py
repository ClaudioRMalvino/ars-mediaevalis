import src.ars_mediaevalis.state as state
import fcntl
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

state.log.addHandler(logging.NullHandler())  # keeps expected errors out of the test output


class StateTestCase(unittest.TestCase):
    """Base class that keeps every state test away from the real state directory."""

    def setUp(self) -> None:
        """Points XDG_STATE_HOME at a temporary directory."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir: Path = Path(tmp.name) / "ars_mediaevalis"
        self.file: Path = self.dir / "state.json"

        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestLoad(StateTestCase):
    """Tests that load() reads state.json defensively as intended."""

    def write(self, text: str) -> None:
        """Writes text to state.json."""

        self.dir.mkdir(parents=True, exist_ok=True)
        self.file.write_text(text)

    def test_missing_file_gives_empty_state(self) -> None:
        """Tests that a first run starts from the defaults."""

        st: state.State = state.load()
        self.assertEqual(st, state.State())
        self.assertIsNone(st.date)
        self.assertIsNone(st.artwork)
        self.assertFalse(st.greeted)
        self.assertEqual((st.shown, st.rejected), ([], []))

    def test_reads_stored_fields(self) -> None:
        """Tests that every stored field is read back."""

        self.write(json.dumps({
            "date": "2026-10-06", "artwork": {"object_id": 7}, "greeted": True,
            "shown": [7, 8], "rejected": [9],
        }))
        self.assertEqual(
            state.load(),
            state.State(date="2026-10-06", artwork={"object_id": 7}, greeted=True, shown=[7, 8], rejected=[9]),
        )

    def test_missing_fields_use_defaults(self) -> None:
        """Tests that a file holding only some fields is completed with defaults."""

        self.write(json.dumps({"date": "2026-10-06"}))
        self.assertEqual(state.load(), state.State(date="2026-10-06"))

    def test_unknown_fields_are_ignored(self) -> None:
        """Tests that a field written by another version does not discard the state."""

        self.write(json.dumps({"date": "2026-10-06", "wallpaper_path": "/tmp/old.jpg"}))
        self.assertEqual(state.load().date, "2026-10-06")
        self.assertTrue(self.file.exists())

    def test_corrupt_file_is_moved_aside(self) -> None:
        """Tests that truncated, non-JSON, non-object or binary content is moved to state.json.bad."""

        for content in (b'{"date": "2026-10', b"not json", b"[1, 2, 3]", b"null", b"", b"\xff\xfe\x00"):
            with self.subTest(content=content):
                self.dir.mkdir(parents=True, exist_ok=True)
                self.file.write_bytes(content)

                self.assertEqual(state.load(), state.State())

                self.assertFalse(self.file.exists())
                self.assertEqual((self.dir / "state.json.bad").read_bytes(), content)

    def test_default_lists_are_not_shared(self) -> None:
        """Tests that two empty states do not share their shown and rejected lists."""

        first, second = state.load(), state.load()
        first.shown.append(1)
        self.assertEqual(second.shown, [])


class TestSave(StateTestCase):
    """Tests that save() writes state.json atomically as intended."""

    def test_round_trip(self) -> None:
        """Tests that a saved state is loaded back unchanged."""

        st: state.State = state.State(
            date="2026-10-06", artwork={"object_id": 7, "title": "Vierge à l'Enfant"},
            greeted=True, shown=[7, 8], rejected=[9],
        )
        state.save(st)
        self.assertEqual(state.load(), st)

    def test_writes_json_object(self) -> None:
        """Tests that the file holds a JSON object with the five fields."""

        state.save(state.State(date="2026-10-06"))
        self.assertEqual(
            json.loads(self.file.read_text()),
            {"date": "2026-10-06", "artwork": None, "greeted": False, "shown": [], "rejected": []},
        )

    def test_overwrites_previous_state(self) -> None:
        """Tests that a second save replaces the first."""

        state.save(state.State(date="2026-10-05"))
        state.save(state.State(date="2026-10-06"))
        self.assertEqual(state.load().date, "2026-10-06")

    def test_leaves_no_temporary_file(self) -> None:
        """Tests that only state.json is left in the directory."""

        state.save(state.State(date="2026-10-06"))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["state.json"])

    def test_failed_save_keeps_previous_state(self) -> None:
        """Tests that a crash while writing leaves the old file intact and no temporary file."""

        state.save(state.State(date="2026-10-05"))

        with mock.patch.object(state.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                state.save(state.State(date="2026-10-06"))

        self.assertEqual(state.load().date, "2026-10-05")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["state.json"])


class TestLocked(StateTestCase):
    """Tests that locked() serialises runs as intended."""

    def try_lock(self) -> bool:
        """Returns whether another holder could take the lock right now."""

        with open(self.dir / "lock", "w") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True

    def test_lock_is_held_inside_the_block(self) -> None:
        """Tests that a second holder is refused while the block runs."""

        with state.locked():
            self.assertFalse(self.try_lock())

    def test_lock_is_released_after_the_block(self) -> None:
        """Tests that the lock is free again once the block has ended."""

        with state.locked():
            pass
        self.assertTrue(self.try_lock())

    def test_lock_is_released_after_an_exception(self) -> None:
        """Tests that an exception inside the block still releases the lock."""

        with self.assertRaises(ValueError):
            with state.locked():
                raise ValueError("boom")
        self.assertTrue(self.try_lock())

    def test_lock_can_be_taken_again(self) -> None:
        """Tests that two runs after each other both get the lock."""

        for _ in range(2):
            with state.locked():
                self.assertFalse(self.try_lock())


if __name__ == "__main__":
    unittest.main()
