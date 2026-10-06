import src.ars_mediaevalis.cli as cli
import contextlib
import dataclasses
import datetime
import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from tests.helpers import jpeg_bytes, met_record

cli.log.addHandler(logging.NullHandler())  # keeps expected errors out of the test output

TODAY: datetime.date = datetime.date(2026, 10, 6)
TOMORROW: datetime.date = datetime.date(2026, 10, 7)


class CliTestCase(unittest.TestCase):
    """
    Base class that runs the real daily logic against a fake Met API, a fake desktop,
    temporary XDG directories and a controllable calendar.
    """

    def setUp(self) -> None:
        """Serves three usable paintings, and replaces the desktop, the greeting and the date."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.images: Path = Path(tmp.name) / "cache" / "ars_mediaevalis" / "images"
        self.state_file: Path = Path(tmp.name) / "state" / "ars_mediaevalis" / "state.json"

        self.records: dict[int, dict] = {i: met_record(i) for i in (1, 2, 3)}
        self.image: bytes = jpeg_bytes()
        self.online: bool = True
        self.requests: list[httpx.Request] = []
        self.today: datetime.date = TODAY

        def handler(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            if not self.online:
                raise httpx.ConnectError("no network", request=req)
            if req.url.path.endswith("/search"):
                ids: list[int] = sorted(self.records) if req.url.params["offset"] == "0" else []
                return httpx.Response(200, json={"total": len(self.records), "objectIDs": ids})
            if "/objects/" in req.url.path:
                return httpx.Response(200, json=self.records[int(req.url.path.rsplit("/", 1)[-1])])
            if req.url.host == "images.metmuseum.org":
                return httpx.Response(200, content=self.image)
            return httpx.Response(404)

        fake_date = mock.Mock()
        fake_date.today.side_effect = lambda: self.today

        patchers: dict[str, mock._patch] = {
            "env": mock.patch.dict(os.environ, {
                "XDG_CACHE_HOME": str(Path(tmp.name) / "cache"),
                "XDG_STATE_HOME": str(Path(tmp.name) / "state"),
            }),
            "queries": mock.patch.object(cli.met, "QUERIES", [{"departmentId": 7}]),
            "sleep": mock.patch.object(cli.met.time, "sleep"),
            "make_client": mock.patch.object(
                cli.met, "make_client", side_effect=lambda: httpx.Client(transport=httpx.MockTransport(handler))
            ),
            "monitors": mock.patch.object(cli.picker, "monitors", return_value=[cli.picker.Monitor("DP-1", 800, 600)]),
            "apply": mock.patch.object(cli.picker, "apply"),
            "greet": mock.patch.object(cli, "greet"),
            "date": mock.patch.object(cli, "date", fake_date),
        }
        for name, patcher in patchers.items():
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def state(self) -> cli.state.State:
        """Returns the state as it is on disk."""

        return cli.state.load()

    def object_requests(self) -> list[int]:
        """Returns the IDs of the objects requested from the fake API so far."""

        return [int(r.url.path.rsplit("/", 1)[-1]) for r in self.requests if "/objects/" in r.url.path]


class TestRunFirstTime(CliTestCase):
    """Tests that the first run of a day picks and applies an artwork and opens the greeting as intended."""

    def test_returns_zero(self) -> None:
        """Tests that a successful run exits with 0."""

        self.assertEqual(cli.run(), 0)

    def test_applies_the_downloaded_image(self) -> None:
        """Tests that the wallpaper is set once, from the image downloaded into the cache."""

        cli.run()

        self.apply.assert_called_once()
        src: Path = self.apply.call_args.args[0]
        self.assertEqual(src.parent, self.images)
        self.assertEqual(src.read_bytes(), self.image)

    def test_saves_todays_pick(self) -> None:
        """Tests that the state records the date, the artwork, the shown ID and the greeting."""

        cli.run()

        st: cli.state.State = self.state()
        self.assertEqual(st.date, "2026-10-06")
        self.assertIn(st.artwork["object_id"], (1, 2, 3))
        self.assertEqual(st.shown, [st.artwork["object_id"]])
        self.assertEqual(st.artwork["image_path"], str(self.apply.call_args.args[0]))
        self.assertTrue(st.greeted)

    def test_history_is_filled(self) -> None:
        """Tests that the stored artwork carries a history, here from the catalogue."""

        cli.run()

        self.assertEqual(self.state().artwork["history_source"], "catalogue")
        self.assertTrue(self.state().artwork["history"])

    def test_greets_once(self) -> None:
        """Tests that the greeting window is opened once."""

        cli.run()
        self.greet.assert_called_once()

    def test_artwork_is_remembered_for_later(self) -> None:
        """Tests that the new artwork is stored in the cache, where `use` can find it again."""

        cli.run()
        self.assertEqual([art.object_id for art in cli.artwork.cached()], [self.state().artwork["object_id"]])
        self.assertEqual(dataclasses.asdict(cli.artwork.cached()[0]), self.state().artwork)

    def test_unusable_objects_are_rejected_and_remembered(self) -> None:
        """Tests that objects that are not public-domain paintings are skipped and stored as rejected."""

        self.records[1] = met_record(1, isPublicDomain=False)
        self.records[2] = met_record(2, classification="Textiles-Embroidered")

        self.assertEqual(cli.run(), 0)

        st: cli.state.State = self.state()
        self.assertEqual(st.artwork["object_id"], 3)
        self.assertLessEqual(set(st.rejected), {1, 2})
        self.assertEqual(sorted(set(self.object_requests())), sorted(st.rejected + [3]))


class TestRunSameDay(CliTestCase):
    """Tests that later runs on the same day only re-apply today's artwork."""

    def setUp(self) -> None:
        """Does the first run of the day, then forgets its requests and calls."""

        super().setUp()
        cli.run()
        self.first: dict = self.state().artwork
        self.requests.clear()
        self.apply.reset_mock()
        self.greet.reset_mock()

    def test_makes_no_request(self) -> None:
        """Tests that a second run does not touch the network."""

        self.assertEqual(cli.run(), 0)
        self.assertEqual(self.requests, [])

    def test_reapplies_the_same_image(self) -> None:
        """Tests that the same image is handed to the desktop again."""

        cli.run()
        self.apply.assert_called_once_with(Path(self.first["image_path"]))

    def test_does_not_greet_again(self) -> None:
        """Tests that the greeting window is not opened again on later logins that day."""

        cli.run()
        cli.run()
        self.greet.assert_not_called()

    def test_state_is_unchanged(self) -> None:
        """Tests that the artwork, date and shown list stay as they were."""

        cli.run()
        st: cli.state.State = self.state()
        self.assertEqual((st.date, st.artwork, st.shown), ("2026-10-06", self.first, [self.first["object_id"]]))

    def test_wiped_cache_redownloads_todays_image(self) -> None:
        """Tests that a deleted image is fetched again instead of a new artwork being picked."""

        Path(self.first["image_path"]).unlink()

        self.assertEqual(cli.run(), 0)

        self.assertEqual(self.state().artwork["object_id"], self.first["object_id"])
        self.assertEqual([r.url.host for r in self.requests], ["images.metmuseum.org"])
        self.assertTrue(Path(self.first["image_path"]).exists())
        self.greet.assert_not_called()

    def test_wiped_cache_while_offline_fails(self) -> None:
        """Tests that a deleted image with no network returns 1 and sets nothing."""

        Path(self.first["image_path"]).unlink()
        self.online = False

        self.assertEqual(cli.run(), 1)
        self.apply.assert_not_called()


class TestRunNewDay(CliTestCase):
    """Tests that a run on a later day picks a new artwork as intended."""

    def setUp(self) -> None:
        """Does a run today, then moves the calendar to tomorrow."""

        super().setUp()
        cli.run()
        self.first: dict = self.state().artwork
        self.today = TOMORROW
        self.requests.clear()
        self.apply.reset_mock()
        self.greet.reset_mock()

    def test_picks_a_different_artwork(self) -> None:
        """Tests that tomorrow's artwork is one that has not been shown."""

        self.assertEqual(cli.run(), 0)

        st: cli.state.State = self.state()
        self.assertEqual(st.date, "2026-10-07")
        self.assertNotEqual(st.artwork["object_id"], self.first["object_id"])
        self.assertEqual(st.shown, sorted([self.first["object_id"], st.artwork["object_id"]]))

    def test_shown_artworks_are_never_requested(self) -> None:
        """Tests that an already shown object is not even fetched."""

        cli.run()
        self.assertNotIn(self.first["object_id"], self.object_requests())

    def test_greets_again(self) -> None:
        """Tests that the new artwork gets its own greeting."""

        cli.run()
        self.greet.assert_called_once()

    def test_pool_is_served_from_cache(self) -> None:
        """Tests that the ID pool is not searched for again on the second day."""

        cli.run()
        self.assertEqual([r for r in self.requests if r.url.path.endswith("/search")], [])

    def test_history_restarts_when_everything_was_shown(self) -> None:
        """Tests that after the whole pool has been shown, the shown list starts over."""

        for day in range(7, 9):   # shows the two remaining artworks
            self.today = datetime.date(2026, 10, day)
            self.assertEqual(cli.run(), 0)
        self.assertEqual(self.state().shown, [1, 2, 3])

        self.today = datetime.date(2026, 10, 9)
        self.assertEqual(cli.run(), 0)

        st: cli.state.State = self.state()
        self.assertEqual(st.shown, [st.artwork["object_id"]])

    def test_offline_keeps_yesterdays_artwork(self) -> None:
        """Tests that without network yesterday's image is re-applied and 1 is returned."""

        self.online = False

        self.assertEqual(cli.run(), 1)

        self.apply.assert_called_once_with(Path(self.first["image_path"]))
        self.greet.assert_not_called()

    def test_offline_does_not_advance_the_date(self) -> None:
        """Tests that a failed day is retried: once the network is back, a new artwork is picked."""

        self.online = False
        cli.run()
        self.assertEqual(self.state().date, "2026-10-06")
        self.assertEqual(self.state().artwork, self.first)

        self.online = True
        self.assertEqual(cli.run(), 0)

        self.assertEqual(self.state().date, "2026-10-07")
        self.assertNotEqual(self.state().artwork["object_id"], self.first["object_id"])
        self.greet.assert_called_once()

    def test_unreadable_image_is_rejected(self) -> None:
        """Tests that a download that is not an image is deleted, rejected and reported as a failure."""

        self.image = b"<html>Access denied</html>"

        self.assertEqual(cli.run(), 1)

        st: cli.state.State = self.state()
        self.assertEqual(st.date, "2026-10-06")
        self.assertEqual(len(st.rejected), 1)
        self.assertNotIn(st.rejected[0], st.shown)
        self.assertFalse((self.images / f"met-{st.rejected[0]}.jpg").exists())


class TestRunFailures(CliTestCase):
    """Tests that run() fails politely when the network or the desktop is missing."""

    def test_offline_first_run(self) -> None:
        """Tests that a first run without network returns 1 and sets nothing."""

        self.online = False

        self.assertEqual(cli.run(), 1)

        self.apply.assert_not_called()
        self.greet.assert_not_called()
        self.assertIsNone(self.state().artwork)

    def test_desktop_unreachable_does_nothing(self) -> None:
        """Tests that outside a Hyprland session nothing is fetched, saved or set."""

        self.monitors.side_effect = cli.picker.DesktopError("HYPRLAND_INSTANCE_SIGNATURE is not set")

        self.assertEqual(cli.run(), 1)

        self.assertEqual(self.requests, [])
        self.assertFalse(self.state_file.exists())
        self.apply.assert_not_called()

    def test_wallpaper_failure_keeps_the_pick(self) -> None:
        """Tests that when Noctalia refuses the wallpaper, the pick is saved but no greeting is opened."""

        self.apply.side_effect = cli.picker.DesktopError("Noctalia did not accept the wallpaper for DP-1")

        self.assertEqual(cli.run(), 1)

        st: cli.state.State = self.state()
        self.assertEqual(st.date, "2026-10-06")
        self.assertIsNotNone(st.artwork)
        self.assertFalse(st.greeted)
        self.greet.assert_not_called()

    def test_next_run_after_wallpaper_failure_finishes_the_job(self) -> None:
        """Tests that the following run sets the same artwork and opens the greeting, without the network."""

        self.apply.side_effect = cli.picker.DesktopError("Noctalia is not running")
        cli.run()
        picked: dict = self.state().artwork
        self.apply.side_effect = None
        self.requests.clear()

        self.assertEqual(cli.run(), 0)

        self.assertEqual(self.state().artwork, picked)
        self.assertEqual(self.requests, [])
        self.greet.assert_called_once()
        self.assertTrue(self.state().greeted)

    def test_corrupt_state_starts_fresh(self) -> None:
        """Tests that an unreadable state.json is moved aside and a new artwork picked."""

        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text('{"date": "2026-10')

        self.assertEqual(cli.run(), 0)

        self.assertEqual(self.state().date, "2026-10-06")
        self.assertTrue(self.state_file.with_suffix(".json.bad").exists())

    def test_incomplete_stored_artwork_is_replaced(self) -> None:
        """Tests that a state whose artwork lacks fields counts as having no artwork."""

        cli.state.save(cli.state.State(date="2026-10-06", artwork={"title": "Only a title"}))

        self.assertEqual(cli.run(), 0)
        self.assertIn(self.state().artwork["object_id"], (1, 2, 3))


class TestUse(CliTestCase):
    """Tests that use() brings back an earlier artwork from the cache as intended."""

    def setUp(self) -> None:
        """Shows one artwork today and another tomorrow, then forgets the requests and calls."""

        super().setUp()
        cli.run()
        self.earlier: dict = self.state().artwork
        self.today = TOMORROW
        cli.run()
        self.latest: dict = self.state().artwork
        self.requests.clear()
        self.apply.reset_mock()
        self.greet.reset_mock()

    def test_sets_the_earlier_artwork(self) -> None:
        """Tests that the earlier image is applied and becomes the current artwork."""

        self.assertEqual(cli.use(self.earlier["object_id"]), 0)

        self.apply.assert_called_once_with(Path(self.earlier["image_path"]))
        self.assertEqual(self.state().artwork, self.earlier)

    def test_makes_no_request(self) -> None:
        """Tests that nothing is fetched: not the pool, not the object, not the image."""

        cli.use(self.earlier["object_id"])
        self.assertEqual(self.requests, [])

    def test_opens_no_greeting(self) -> None:
        """Tests that choosing an artwork yourself does not open the greeting window."""

        cli.use(self.earlier["object_id"])
        self.greet.assert_not_called()
        self.assertTrue(self.state().greeted)

    def test_later_runs_today_keep_the_choice(self) -> None:
        """Tests that the daily run re-applies the chosen artwork instead of picking a new one."""

        cli.use(self.earlier["object_id"])
        self.apply.reset_mock()

        self.assertEqual(cli.run(), 0)

        self.apply.assert_called_once_with(Path(self.earlier["image_path"]))
        self.assertEqual(self.requests, [])
        self.greet.assert_not_called()

    def test_next_day_still_brings_a_new_artwork(self) -> None:
        """Tests that the day after a choice, a new artwork is picked as usual."""

        cli.use(self.earlier["object_id"])
        self.today = datetime.date(2026, 10, 8)

        self.assertEqual(cli.run(), 0)

        self.assertNotIn(self.state().artwork["object_id"], (self.earlier["object_id"], self.latest["object_id"]))

    def test_shown_list_is_unchanged(self) -> None:
        """Tests that bringing an artwork back does not alter the list of shown IDs."""

        before: list[int] = self.state().shown
        cli.use(self.earlier["object_id"])
        self.assertEqual(self.state().shown, before)

    def test_unknown_artwork_fails(self) -> None:
        """Tests that an ID that was never shown returns 1, without a request and without a change."""

        unseen: int = ({1, 2, 3} - {self.earlier["object_id"], self.latest["object_id"]}).pop()

        self.assertEqual(cli.use(unseen), 1)

        self.assertEqual(self.requests, [])
        self.apply.assert_not_called()
        self.assertEqual(self.state().artwork, self.latest)

    def test_artwork_with_deleted_image_fails(self) -> None:
        """Tests that an earlier artwork whose image left the cache is not downloaded again."""

        Path(self.earlier["image_path"]).unlink()

        self.assertEqual(cli.use(self.earlier["object_id"]), 1)

        self.assertEqual(self.requests, [])
        self.assertEqual(self.state().artwork, self.latest)

    def test_wallpaper_failure_keeps_the_current_artwork(self) -> None:
        """Tests that when Noctalia refuses the wallpaper, the state is left as it was."""

        self.apply.side_effect = cli.picker.DesktopError("Noctalia is not running")

        self.assertEqual(cli.use(self.earlier["object_id"]), 1)
        self.assertEqual(self.state().artwork, self.latest)

    def test_works_offline(self) -> None:
        """Tests that an earlier artwork can be brought back without any network."""

        self.online = False
        self.assertEqual(cli.use(self.earlier["object_id"]), 0)


class TestCachedList(CliTestCase):
    """Tests that cached_list() prints the earlier artworks as intended."""

    def output(self) -> tuple[int, str]:
        """Returns the exit code and the printed text of cached_list()."""

        out: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(out):
            code: int = cli.cached_list()
        return code, out.getvalue()

    def test_empty_cache(self) -> None:
        """Tests that an empty cache returns 1 with a message."""

        code, text = self.output()
        self.assertEqual(code, 1)
        self.assertIn("No artworks in the cache", text)

    def test_lists_every_artwork_and_marks_the_current_one(self) -> None:
        """Tests that each shown artwork has a line with its ID, and only the current one a star."""

        cli.run()
        earlier: int = self.state().artwork["object_id"]
        self.today = TOMORROW
        cli.run()
        latest: int = self.state().artwork["object_id"]

        code, text = self.output()

        self.assertEqual(code, 0)
        lines: dict[int, str] = {int(line[1:].split()[0]): line for line in text.splitlines()[:2]}
        self.assertEqual(set(lines), {earlier, latest})
        self.assertTrue(lines[latest].startswith("*"))
        self.assertTrue(lines[earlier].startswith(" "))
        self.assertIn(f"Panel {earlier} · Robert Campin · ca. 1427–32", lines[earlier])

    def test_makes_no_request(self) -> None:
        """Tests that listing does not touch the network or the desktop."""

        cli.run()
        self.requests.clear()
        self.apply.reset_mock()

        self.output()

        self.assertEqual(self.requests, [])
        self.apply.assert_not_called()


class TestGreet(unittest.TestCase):
    """Tests that greet() opens the detail window in a detached process."""

    def test_spawns_detached_show_process(self) -> None:
        """Tests that `python -m ars_mediaevalis show` is started in its own session, without waiting."""

        with mock.patch.object(cli.subprocess, "Popen") as popen:
            cli.greet()

        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "ars_mediaevalis", "show"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        popen.return_value.wait.assert_not_called()


class TestInfo(CliTestCase):
    """Tests that info() prints today's artwork as intended."""

    def output(self) -> tuple[int, str]:
        """Returns the exit code and the printed text of info()."""

        out: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(out):
            code: int = cli.info()
        return code, out.getvalue()

    def test_no_artwork_yet(self) -> None:
        """Tests that an empty state returns 1 with a hint."""

        code, text = self.output()
        self.assertEqual(code, 1)
        self.assertIn("No artwork yet", text)

    def test_prints_artwork(self) -> None:
        """Tests that the title, maker, history, link and pick date are printed."""

        cli.run()
        art: dict = self.state().artwork

        code, text = self.output()

        self.assertEqual(code, 0)
        self.assertEqual(text.splitlines()[0], art["title"])
        for expected in ("Robert Campin · ca. 1427–32 · Belgium", art["history"], art["object_url"], "2026-10-06"):
            self.assertIn(expected, text)

    def test_makes_no_request(self) -> None:
        """Tests that printing does not touch the network or the desktop."""

        cli.run()
        self.requests.clear()
        self.apply.reset_mock()

        self.output()

        self.assertEqual(self.requests, [])
        self.apply.assert_not_called()


class TestMain(unittest.TestCase):
    """Tests that main() maps the subcommands onto the right functions."""

    def setUp(self) -> None:
        """Replaces logging setup and every command body with a mock."""

        for name, attr in (("logging", "_setup_logging"), ("run", "run"), ("info", "info"),
                           ("cached_list", "cached_list"), ("use", "use")):
            patcher = mock.patch.object(cli, attr, return_value=0)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_run(self) -> None:
        """Tests that `run` applies the daily rule."""

        self.assertEqual(cli.main(["run"]), 0)
        self.run.assert_called_once_with()

    def test_info(self) -> None:
        """Tests that `info` prints today's artwork."""

        cli.main(["info"])
        self.info.assert_called_once_with()

    def test_list(self) -> None:
        """Tests that `list` prints the earlier artworks."""

        cli.main(["list"])
        self.cached_list.assert_called_once_with()

    def test_use(self) -> None:
        """Tests that `use <id>` brings back that artwork, with the ID as an int."""

        cli.main(["use", "437"])
        self.use.assert_called_once_with(437)

    def test_use_needs_a_numeric_id(self) -> None:
        """Tests that `use` without an ID, or with a non-numeric one, is rejected by argparse."""

        for argv in (["use"], ["use", "latest"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                    cli.main(argv)
                self.assertEqual(ctx.exception.code, 2)
        self.use.assert_not_called()

    def test_show(self) -> None:
        """Tests that `show` opens the viewer."""

        from ars_mediaevalis import viewer
        with mock.patch.object(viewer, "show_today", return_value=0) as show_today:
            self.assertEqual(cli.main(["show"]), 0)
        show_today.assert_called_once_with()

    def test_exit_code_is_passed_on(self) -> None:
        """Tests that a failing run is reported through the exit code."""

        self.run.return_value = 1
        self.assertEqual(cli.main(["run"]), 1)

    def test_verbose_flag(self) -> None:
        """Tests that -v turns on debug logging."""

        cli.main(["-v", "run"])
        self.logging.assert_called_once_with(True)
        self.logging.reset_mock()
        cli.main(["run"])
        self.logging.assert_called_once_with(False)

    def test_there_is_no_way_to_force_a_new_artwork(self) -> None:
        """Tests that `next` is gone: a new artwork only ever comes with a new day."""

        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["next"])
        self.assertEqual(ctx.exception.code, 2)
        self.run.assert_not_called()

    def test_missing_or_unknown_command_exits(self) -> None:
        """Tests that argparse rejects no command and unknown commands with exit code 2."""

        for argv in ([], ["yesterday"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                    cli.main(argv)
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
