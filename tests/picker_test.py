import src.ars_mediaevalis.picker as picker
import json
import logging
import os
import random
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image, ImageCms
from src.ars_mediaevalis import artwork
from src.ars_mediaevalis.museums import met
from src.ars_mediaevalis.paths import images_dir

WHITE: int = 255
DARKENED: int = round(WHITE * picker.BRIGHTNESS)  # what a white source becomes in the background

picker.log.addHandler(logging.NullHandler())  # keeps expected warnings out of the test output


def hypr_monitor(name: str = "DP-1", width: int = 3440, height: int = 1440, **overrides) -> dict:
    """Returns a minimal but realistic `hyprctl monitors -j` entry with optional overrides."""

    mon: dict = {
        "id": 0,
        "name": name,
        "description": "LG Electronics LG ULTRAWIDE",
        "width": width,
        "height": height,
        "refreshRate": 60.0,
        "x": 0,
        "y": 0,
        "scale": 1,
        "transform": 0,
        "focused": True,
        "disabled": False,
    }
    mon.update(overrides)
    return mon


def completed(stdout: str = "") -> subprocess.CompletedProcess:
    """Returns the result of a command that exited with 0 and printed stdout."""

    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def srgb_profile() -> bytes:
    """Returns the bytes of an sRGB ICC profile, as a camera or scanner would embed them."""

    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


class TestMonitors(unittest.TestCase):
    """Tests that monitors() reads the active monitors from Hyprland as intended."""

    def setUp(self) -> None:
        """Pretends to be inside a Hyprland session and answers hyprctl from self.reported."""

        self.reported: list[dict] = [hypr_monitor()]

        env_patcher = mock.patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "abc_123"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        run_patcher = mock.patch.object(
            picker.subprocess, "run", side_effect=lambda *args, **kwargs: completed(json.dumps(self.reported))
        )
        self.run: mock.MagicMock = run_patcher.start()
        self.addCleanup(run_patcher.stop)

    def test_returns_name_and_resolution(self) -> None:
        """Tests that a monitor is returned with its connector name and resolution."""

        self.assertEqual(picker.monitors(), [picker.Monitor("DP-1", 3440, 1440)])

    def test_asks_hyprctl_for_json(self) -> None:
        """Tests that `hyprctl monitors -j` is run once, checked and with a timeout."""

        picker.monitors()

        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0], ["hyprctl", "monitors", "-j"])
        self.assertTrue(self.run.call_args.kwargs["check"])
        self.assertIsNotNone(self.run.call_args.kwargs["timeout"])

    def test_returns_every_monitor_in_order(self) -> None:
        """Tests that several monitors are returned in the order Hyprland reports them."""

        self.reported = [hypr_monitor("eDP-1", 1920, 1200), hypr_monitor("HDMI-A-1", 2560, 1440)]
        self.assertEqual(
            picker.monitors(),
            [picker.Monitor("eDP-1", 1920, 1200), picker.Monitor("HDMI-A-1", 2560, 1440)],
        )

    def test_disabled_monitors_are_skipped(self) -> None:
        """Tests that a monitor with disabled set is left out."""

        self.reported = [hypr_monitor("eDP-1", 1920, 1200, disabled=True), hypr_monitor("DP-1")]
        self.assertEqual([m.name for m in picker.monitors()], ["DP-1"])

    def test_rotated_monitors_swap_width_and_height(self) -> None:
        """Tests that 90° and 270° transforms swap the resolution and the others do not."""

        cases: list[tuple[int, tuple[int, int]]] = [
            (0, (2560, 1440)),  # normal
            (1, (1440, 2560)),  # 90°
            (2, (2560, 1440)),  # 180°
            (3, (1440, 2560)),  # 270°
            (4, (2560, 1440)),  # flipped
            (5, (1440, 2560)),  # flipped + 90°
            (6, (2560, 1440)),  # flipped + 180°
            (7, (1440, 2560)),  # flipped + 270°
        ]
        for transform, expected in cases:
            with self.subTest(transform=transform):
                self.reported = [hypr_monitor("DP-2", 2560, 1440, transform=transform)]
                mon: picker.Monitor = picker.monitors()[0]
                self.assertEqual((mon.width, mon.height), expected)

    def test_missing_optional_fields_use_defaults(self) -> None:
        """Tests that an entry without transform or disabled counts as upright and enabled."""

        self.reported = [{"name": "DP-1", "width": 1920, "height": 1080}]
        self.assertEqual(picker.monitors(), [picker.Monitor("DP-1", 1920, 1080)])

    def test_not_in_hyprland_session_raises(self) -> None:
        """Tests that an unset or empty HYPRLAND_INSTANCE_SIGNATURE raises without running hyprctl."""

        for value in (None, ""):
            with self.subTest(HYPRLAND_INSTANCE_SIGNATURE=value):
                with mock.patch.dict(os.environ):
                    os.environ.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
                    if value is not None:
                        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = value
                    with self.assertRaisesRegex(picker.DesktopError, "HYPRLAND_INSTANCE_SIGNATURE"):
                        picker.monitors()
                self.run.assert_not_called()

    def test_no_active_monitors_raises(self) -> None:
        """Tests that an empty list, or one with only disabled monitors, raises DesktopError."""

        for reported in ([], [hypr_monitor(disabled=True)]):
            with self.subTest(reported=len(reported)):
                self.reported = reported
                with self.assertRaisesRegex(picker.DesktopError, "no active monitors"):
                    picker.monitors()

    def test_hyprctl_missing_raises(self) -> None:
        """Tests that a missing hyprctl binary raises DesktopError."""

        self.run.side_effect = FileNotFoundError("hyprctl")
        with self.assertRaisesRegex(picker.DesktopError, "not found"):
            picker.monitors()

    def test_hyprctl_failure_raises_with_stderr(self) -> None:
        """Tests that a non-zero exit raises DesktopError carrying hyprctl's stderr."""

        self.run.side_effect = subprocess.CalledProcessError(
            1, ["hyprctl"], stderr="Couldn't connect to socket\n"
        )
        with self.assertRaisesRegex(picker.DesktopError, "Couldn't connect to socket$"):
            picker.monitors()

    def test_hyprctl_timeout_raises(self) -> None:
        """Tests that a hanging hyprctl raises DesktopError."""

        self.run.side_effect = subprocess.TimeoutExpired(["hyprctl"], 5)
        with self.assertRaisesRegex(picker.DesktopError, "timed out"):
            picker.monitors()

    def test_desktop_error_keeps_the_cause(self) -> None:
        """Tests that the original exception is chained onto the DesktopError."""

        cause: FileNotFoundError = FileNotFoundError("hyprctl")
        self.run.side_effect = cause
        with self.assertRaises(picker.DesktopError) as ctx:
            picker.monitors()
        self.assertIs(ctx.exception.__cause__, cause)


class TestNoctaliaSet(unittest.TestCase):
    """Tests that _noctalia_set() hands a wallpaper to Noctalia as intended."""

    def setUp(self) -> None:
        """Pretends noctalia is installed and replaces subprocess.run and time.sleep with mocks."""

        which_patcher = mock.patch.object(picker.shutil, "which", return_value="/usr/bin/noctalia")
        self.which: mock.MagicMock = which_patcher.start()
        self.addCleanup(which_patcher.stop)

        run_patcher = mock.patch.object(picker.subprocess, "run", return_value=completed())
        self.run: mock.MagicMock = run_patcher.start()
        self.addCleanup(run_patcher.stop)

        sleep_patcher = mock.patch.object(picker.time, "sleep")
        self.sleep: mock.MagicMock = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

        self.wall: Path = Path("/tmp/met-1-wall.jpg")

    def test_sends_wallpaper_set_command(self) -> None:
        """Tests that `noctalia msg wallpaper-set <monitor> <path>` is run once."""

        picker._noctalia_set("DP-1", self.wall)

        self.run.assert_called_once()
        self.assertEqual(
            self.run.call_args.args[0],
            ["noctalia", "msg", "wallpaper-set", "DP-1", "/tmp/met-1-wall.jpg"],
        )
        self.assertTrue(self.run.call_args.kwargs["check"])
        self.sleep.assert_not_called()

    def test_noctalia_missing_raises(self) -> None:
        """Tests that a missing noctalia binary raises DesktopError without running anything."""

        self.which.return_value = None
        with self.assertRaisesRegex(picker.DesktopError, "noctalia not found"):
            picker._noctalia_set("DP-1", self.wall)
        self.run.assert_not_called()

    def test_retries_until_noctalia_answers(self) -> None:
        """Tests that failed and timed-out attempts are retried until one succeeds."""

        self.run.side_effect = [
            subprocess.CalledProcessError(1, ["noctalia"], stderr="no running instance\n"),
            subprocess.TimeoutExpired(["noctalia"], 10),
            completed(),
        ]

        picker._noctalia_set("DP-1", self.wall)

        self.assertEqual(self.run.call_count, 3)

    def test_waits_delay_between_attempts(self) -> None:
        """Tests that each retry is preceded by a sleep of delay seconds."""

        self.run.side_effect = [
            subprocess.CalledProcessError(1, ["noctalia"], stderr=""),
            subprocess.CalledProcessError(1, ["noctalia"], stderr=""),
            completed(),
        ]

        picker._noctalia_set("DP-1", self.wall, delay=0.5)

        self.assertEqual(self.sleep.call_args_list, [mock.call(0.5), mock.call(0.5)])

    def test_gives_up_after_attempts(self) -> None:
        """Tests that DesktopError naming the monitor is raised once every attempt has failed."""

        for exc in (
            subprocess.CalledProcessError(1, ["noctalia"], stderr="no running instance"),
            subprocess.TimeoutExpired(["noctalia"], 10),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.run.reset_mock()
                self.sleep.reset_mock()
                self.run.side_effect = exc

                with self.assertRaisesRegex(picker.DesktopError, "DP-1") as ctx:
                    picker._noctalia_set("DP-1", self.wall, attempts=3)

                self.assertIs(ctx.exception.__cause__, exc)
                self.assertEqual(self.run.call_count, 3)
                self.assertEqual(self.sleep.call_count, 2)  # no sleep after the last attempt

    def test_failures_are_logged(self) -> None:
        """Tests that each failed attempt logs a warning with the monitor and the stderr."""

        self.run.side_effect = [
            subprocess.CalledProcessError(1, ["noctalia"], stderr="no running instance\n"),
            completed(),
        ]

        with self.assertLogs(picker.log, level="WARNING") as logs:
            picker._noctalia_set("DP-1", self.wall)

        self.assertEqual(len(logs.output), 1)
        self.assertIn("DP-1", logs.output[0])
        self.assertIn("no running instance", logs.output[0])


class TestNoctaliaGet(unittest.TestCase):
    """Tests that _noctalia_get() reads the current wallpaper of a monitor as intended."""

    def test_returns_stripped_path(self) -> None:
        """Tests that the path printed by `noctalia msg wallpaper-get <monitor>` is returned."""

        with mock.patch.object(picker.subprocess, "run", return_value=completed("/tmp/wall.jpg\n")) as run:
            self.assertEqual(picker._noctalia_get("DP-1"), "/tmp/wall.jpg")
        self.assertEqual(run.call_args.args[0], ["noctalia", "msg", "wallpaper-get", "DP-1"])

    def test_empty_answer_is_none(self) -> None:
        """Tests that a monitor without a wallpaper gives None."""

        with mock.patch.object(picker.subprocess, "run", return_value=completed("\n")):
            self.assertIsNone(picker._noctalia_get("DP-1"))

    def test_failure_is_none(self) -> None:
        """Tests that a missing, failing or hanging noctalia gives None instead of raising."""

        for exc in (
            FileNotFoundError("noctalia"),
            subprocess.CalledProcessError(1, ["noctalia"], stderr='unknown output "eDP-1"'),
            subprocess.TimeoutExpired(["noctalia"], 5),
        ):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(picker.subprocess, "run", side_effect=exc):
                    self.assertIsNone(picker._noctalia_get("eDP-1"))


class TestToSrgb(unittest.TestCase):
    """Tests that _to_srgb() converts images to RGB as intended."""

    def test_modes_without_profile_become_rgb(self) -> None:
        """Tests that every common mode without an ICC profile is converted to RGB."""

        for mode in ("RGB", "L", "RGBA", "CMYK", "P", "1", "I;16"):
            with self.subTest(mode=mode):
                out: Image.Image = picker._to_srgb(Image.new(mode, (4, 3)))
                self.assertEqual(out.mode, "RGB")
                self.assertEqual(out.size, (4, 3))

    def test_plain_conversion_keeps_colours(self) -> None:
        """Tests that an RGB image without a profile keeps its pixels."""

        out: Image.Image = picker._to_srgb(Image.new("RGB", (2, 2), (200, 100, 50)))
        self.assertEqual(out.getpixel((0, 0)), (200, 100, 50))

    def test_embedded_profile_is_applied(self) -> None:
        """Tests that an image with an sRGB profile comes out as RGB with the same colours."""

        im: Image.Image = Image.new("RGB", (2, 2), (200, 100, 50))
        im.info["icc_profile"] = srgb_profile()

        out: Image.Image = picker._to_srgb(im)

        self.assertEqual(out.mode, "RGB")
        for got, expected in zip(out.getpixel((0, 0)), (200, 100, 50)):
            self.assertAlmostEqual(got, expected, delta=2)

    def test_bad_profile_falls_back_with_warning(self) -> None:
        """Tests that a corrupt ICC profile is logged and the image converted without it."""

        im: Image.Image = Image.new("RGB", (2, 2), (200, 100, 50))
        im.info["icc_profile"] = b"not an icc profile"

        with self.assertLogs(picker.log, level="WARNING"):
            out: Image.Image = picker._to_srgb(im)

        self.assertEqual(out.mode, "RGB")
        self.assertEqual(out.getpixel((0, 0)), (200, 100, 50))

    def test_profile_that_does_not_match_the_mode_falls_back(self) -> None:
        """Tests that an RGB profile on a CMYK image still gives an RGB image."""

        im: Image.Image = Image.new("CMYK", (2, 2))
        im.info["icc_profile"] = srgb_profile()

        with self.assertLogs(picker.log, level="WARNING"):
            out: Image.Image = picker._to_srgb(im)

        self.assertEqual(out.mode, "RGB")


class TestComposeWallpaper(unittest.TestCase):
    """Tests that compose_wallpaper() builds the wallpaper image as intended."""

    def setUp(self) -> None:
        """Creates a temporary directory to hold the source images and the wallpapers."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir: Path = Path(tmp.name)

    def source(self, size: tuple[int, int], name: str = "met-1.png", mode: str = "RGB", **save) -> Path:
        """Saves a plain white image of the given size and returns its path."""

        path: Path = self.dir / name
        Image.new(mode, size, "white").save(path, **save)
        return path

    def luma(self, wall: Path, xy: tuple[int, int]) -> int:
        """Returns the brightness (0-255) of one pixel of the wallpaper."""

        with Image.open(wall) as im:
            return im.convert("L").getpixel(xy)

    def assert_painting(self, wall: Path, xy: tuple[int, int]) -> None:
        """Asserts that the pixel belongs to the (white) painting."""

        self.assertGreater(self.luma(wall, xy), 235, f"{xy} should be painting")

    def assert_background(self, wall: Path, xy: tuple[int, int]) -> None:
        """Asserts that the pixel belongs to the (darkened) background."""

        self.assertAlmostEqual(self.luma(wall, xy), DARKENED, delta=12, msg=f"{xy} should be background")

    def test_output_matches_screen_size(self) -> None:
        """Tests that the wallpaper has exactly the requested resolution, whatever the source."""

        screens: list[tuple[int, int]] = [(800, 600), (1440, 600), (600, 800), (1, 1)]
        sources: list[tuple[int, int]] = [(400, 200), (200, 400), (2000, 1500), (1, 1)]
        for screen in screens:
            for size in sources:
                with self.subTest(screen=screen, source=size):
                    src: Path = self.source(size, f"met-{size[0]}x{size[1]}.png")
                    with Image.open(picker.compose_wallpaper(src, screen)) as im:
                        self.assertEqual(im.size, screen)

    def test_output_is_rgb_jpeg(self) -> None:
        """Tests that the wallpaper is saved as an RGB JPEG."""

        with Image.open(picker.compose_wallpaper(self.source((400, 200)), (800, 600))) as im:
            self.assertEqual(im.format, "JPEG")
            self.assertEqual(im.mode, "RGB")

    def test_output_is_named_after_source_and_settings(self) -> None:
        """Tests that the wallpaper sits next to the source, named after it, the size and the settings."""

        src: Path = self.source((400, 200), "met-437.png")
        wall: Path = picker.compose_wallpaper(src, (800, 600))
        self.assertEqual(wall, self.dir / "met-437-wall-800x600-m6-b45-u15.jpg")

    def test_each_resolution_gets_its_own_file(self) -> None:
        """Tests that two resolutions of the same source do not overwrite each other."""

        src: Path = self.source((400, 200))
        first: Path = picker.compose_wallpaper(src, (800, 600))
        second: Path = picker.compose_wallpaper(src, (600, 800))
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())

    def test_changed_settings_get_their_own_file(self) -> None:
        """Tests that changing MARGIN, BRIGHTNESS or MAX_UPSCALE changes the file name."""

        src: Path = self.source((400, 200))
        default: Path = picker.compose_wallpaper(src, (800, 600))
        for setting, value in (("MARGIN", 0.1), ("BRIGHTNESS", 0.3), ("MAX_UPSCALE", 2.0)):
            with self.subTest(setting=setting):
                with mock.patch.object(picker, setting, value):
                    self.assertNotEqual(picker.compose_wallpaper(src, (800, 600)), default)

    def test_style_arguments_get_their_own_file(self) -> None:
        """Tests that margin, brightness and max_upscale passed as arguments end up in the file name."""

        src: Path = self.source((400, 200), "met-437.png")
        wall: Path = picker.compose_wallpaper(src, (800, 600), margin=0.1, brightness=0.3, max_upscale=2.0)
        self.assertEqual(wall, self.dir / "met-437-wall-800x600-m10-b30-u20.jpg")

    def test_arguments_override_the_constants(self) -> None:
        """Tests that an argument wins over the module constant, and None falls back to it."""

        src: Path = self.source((400, 200), "met-437.png")
        with mock.patch.object(picker, "MARGIN", 0.2):
            self.assertIn("-m20-", picker.compose_wallpaper(src, (800, 600)).name)
            self.assertIn("-m20-", picker.compose_wallpaper(src, (800, 600), margin=None).name)
            self.assertIn("-m5-", picker.compose_wallpaper(src, (800, 600), margin=0.05).name)

    def test_margin_argument_shrinks_the_painting(self) -> None:
        """Tests that a larger margin leaves more background around a large painting."""

        # 2000x1000 on 800x600 with a 0.2 margin: the box is 480x360, so 480x240 at (160, 180).
        wall: Path = picker.compose_wallpaper(self.source((2000, 1000)), (800, 600), margin=0.2)

        self.assert_background(wall, (152, 300))
        self.assert_painting(wall, (168, 300))
        self.assert_background(wall, (400, 172))
        self.assert_painting(wall, (400, 188))

    def test_brightness_argument_darkens_the_background(self) -> None:
        """Tests that the corners show the source darkened by the given brightness."""

        wall: Path = picker.compose_wallpaper(self.source((400, 200)), (800, 600), brightness=0.2)
        self.assertAlmostEqual(self.luma(wall, (2, 2)), round(WHITE * 0.2), delta=12)
        self.assert_painting(wall, (400, 300))

    def test_black_and_full_brightness(self) -> None:
        """Tests the ends of the range: 0 gives a black backdrop, 1 leaves it as bright as the painting."""

        src: Path = self.source((400, 200))
        self.assertLess(self.luma(picker.compose_wallpaper(src, (800, 600), brightness=0.0), (2, 2)), 12)
        self.assertGreater(self.luma(picker.compose_wallpaper(src, (800, 600), brightness=1.0), (2, 2)), 243)

    def test_max_upscale_argument(self) -> None:
        """Tests that max_upscale = 1 leaves a small painting at its own size."""

        # 400x200 on 800x600 at 1x: 400x200 at (200, 200).
        wall: Path = picker.compose_wallpaper(self.source((400, 200)), (800, 600), max_upscale=1.0)

        self.assert_background(wall, (192, 300))
        self.assert_painting(wall, (208, 300))
        self.assert_background(wall, (400, 192))
        self.assert_painting(wall, (400, 208))

    def test_zero_margin_fills_the_tight_side(self) -> None:
        """Tests that margin = 0 lets a large painting reach the screen edges on its tight side."""

        wall: Path = picker.compose_wallpaper(self.source((2000, 1000)), (800, 600), margin=0.0)
        self.assert_painting(wall, (4, 300))
        self.assert_painting(wall, (795, 300))

    def test_existing_wallpaper_is_reused(self) -> None:
        """Tests that a wallpaper already on disk is returned without being composed again."""

        src: Path = self.source((400, 200))
        first: Path = picker.compose_wallpaper(src, (800, 600))
        first.write_bytes(b"old")

        second: Path = picker.compose_wallpaper(src, (800, 600))

        self.assertEqual(second, first)
        self.assertEqual(second.read_bytes(), b"old")

    def test_leaves_no_part_file(self) -> None:
        """Tests that the temporary .part file is gone once the wallpaper is written."""

        picker.compose_wallpaper(self.source((400, 200)), (800, 600))
        self.assertEqual(list(self.dir.glob("*.part")), [])

    def test_missing_source_raises_and_writes_nothing(self) -> None:
        """Tests that a missing source raises FileNotFoundError and leaves no file behind."""

        with self.assertRaises(FileNotFoundError):
            picker.compose_wallpaper(self.dir / "met-404.jpg", (800, 600))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_background_is_darkened(self) -> None:
        """Tests that the corners show the source darkened by BRIGHTNESS."""

        wall: Path = picker.compose_wallpaper(self.source((400, 200)), (800, 600))
        for xy in ((2, 2), (797, 2), (2, 597), (797, 597)):
            with self.subTest(xy=xy):
                self.assert_background(wall, xy)

    def test_painting_is_centred(self) -> None:
        """Tests that the middle of the wallpaper shows the painting at full brightness."""

        wall: Path = picker.compose_wallpaper(self.source((400, 200)), (800, 600))
        self.assert_painting(wall, (400, 300))

    def test_large_painting_is_shrunk_into_the_margin_box(self) -> None:
        """Tests that a painting larger than the screen is scaled down to leave MARGIN on its tight side."""

        # 2000x1000 on 800x600: the box is 704x528, so the painting becomes 704x352 at (48, 124).
        wall: Path = picker.compose_wallpaper(self.source((2000, 1000)), (800, 600))

        self.assert_background(wall, (40, 300))
        self.assert_painting(wall, (56, 300))
        self.assert_painting(wall, (743, 300))
        self.assert_background(wall, (759, 300))
        self.assert_background(wall, (400, 116))
        self.assert_painting(wall, (400, 132))
        self.assert_painting(wall, (400, 467))
        self.assert_background(wall, (400, 483))

    def test_portrait_painting_is_limited_by_height(self) -> None:
        """Tests that a tall painting is scaled to the height of the margin box."""

        # 1000x2000 on 800x600: the box is 704x528, so the painting becomes 264x528 at (268, 36).
        wall: Path = picker.compose_wallpaper(self.source((1000, 2000)), (800, 600))

        self.assert_background(wall, (400, 28))
        self.assert_painting(wall, (400, 44))
        self.assert_background(wall, (260, 300))
        self.assert_painting(wall, (276, 300))

    def test_small_painting_is_not_enlarged_beyond_max_upscale(self) -> None:
        """Tests that a small painting grows by MAX_UPSCALE at most, not to the full margin box."""

        # 400x200 on 800x600 would fit at 1.76x, but is capped at 1.5x: 600x300 at (100, 150).
        wall: Path = picker.compose_wallpaper(self.source((400, 200)), (800, 600))

        self.assert_background(wall, (92, 300))
        self.assert_painting(wall, (108, 300))
        self.assert_background(wall, (400, 142))
        self.assert_painting(wall, (400, 158))

    def test_exif_rotation_is_applied(self) -> None:
        """Tests that a JPEG stored sideways is turned upright before it is composed."""

        exif: Image.Exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: rotate 90° clockwise to display
        src: Path = self.source((100, 300), "met-1.jpg", exif=exif)

        # Upright the painting is 300x100, capped at 1.5x: 450x150 at (175, 225).
        wall: Path = picker.compose_wallpaper(src, (800, 600))

        self.assert_painting(wall, (200, 300))
        self.assert_background(wall, (400, 200))

    def test_large_jpeg_source(self) -> None:
        """Tests that a JPEG far larger than the screen is still placed correctly."""

        # 4000x2000 on 800x600: same geometry as the 2000x1000 case, 704x352 at (48, 124).
        wall: Path = picker.compose_wallpaper(self.source((4000, 2000), "met-1.jpg"), (800, 600))

        self.assert_background(wall, (40, 300))
        self.assert_painting(wall, (56, 300))
        self.assert_background(wall, (400, 116))
        self.assert_painting(wall, (400, 132))

    def test_non_rgb_sources(self) -> None:
        """Tests that greyscale, palette, transparent and CMYK sources are composed without error."""

        cases: list[tuple[str, str]] = [("L", "png"), ("P", "png"), ("RGBA", "png"), ("L", "jpg"), ("CMYK", "jpg")]
        for mode, ext in cases:
            with self.subTest(mode=mode, ext=ext):
                src: Path = self.source((400, 200), f"met-{mode}.{ext}", mode=mode)
                with Image.open(picker.compose_wallpaper(src, (800, 600))) as im:
                    self.assertEqual(im.mode, "RGB")
                    self.assertEqual(im.size, (800, 600))

    def test_source_with_icc_profile(self) -> None:
        """Tests that a source with an embedded ICC profile keeps its colours."""

        src: Path = self.source((400, 200), "met-1.jpg", icc_profile=srgb_profile())
        wall: Path = picker.compose_wallpaper(src, (800, 600))
        self.assert_painting(wall, (400, 300))

    def test_source_is_left_untouched(self) -> None:
        """Tests that composing does not modify the source image."""

        src: Path = self.source((400, 200))
        before: bytes = src.read_bytes()
        picker.compose_wallpaper(src, (800, 600))
        self.assertEqual(src.read_bytes(), before)


class TestApply(unittest.TestCase):
    """Tests that apply() composes and sets a wallpaper for every monitor as intended."""

    def setUp(self) -> None:
        """Creates a source image and fakes a Hyprland session with a running Noctalia."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src: Path = Path(tmp.name) / "met-1.png"
        Image.new("RGB", (400, 200), "white").save(self.src)

        self.reported: list[dict] = [hypr_monitor("eDP-1", 800, 600), hypr_monitor("DP-1", 1440, 600)]
        self.current: dict[str, str] = {}          # what Noctalia shows on each monitor
        self.noctalia_calls: list[list[str]] = []  # every wallpaper-set command

        def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
            if cmd[0] == "hyprctl":
                return completed(json.dumps(self.reported))
            if cmd[2] == "wallpaper-get":
                return completed(self.current.get(cmd[3], "") + "\n")
            self.noctalia_calls.append(cmd)
            self.current[cmd[3]] = cmd[4]
            return completed()

        for patcher in (
            mock.patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "abc_123"}),
            mock.patch.object(picker.subprocess, "run", side_effect=fake_run),
            mock.patch.object(picker.shutil, "which", return_value="/usr/bin/noctalia"),
            mock.patch.object(picker.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_sets_wallpaper_on_every_monitor(self) -> None:
        """Tests that Noctalia is called once per monitor, in order."""

        picker.apply(self.src)

        self.assertEqual([cmd[:3] for cmd in self.noctalia_calls], [["noctalia", "msg", "wallpaper-set"]] * 2)
        self.assertEqual([cmd[3] for cmd in self.noctalia_calls], ["eDP-1", "DP-1"])

    def test_each_monitor_gets_its_own_resolution(self) -> None:
        """Tests that the file handed to Noctalia exists and matches that monitor's resolution."""

        picker.apply(self.src)

        sizes: list[tuple[int, int]] = []
        for cmd in self.noctalia_calls:
            with Image.open(cmd[4]) as im:
                sizes.append(im.size)
        self.assertEqual(sizes, [(800, 600), (1440, 600)])

    def test_rotated_monitor_gets_portrait_wallpaper(self) -> None:
        """Tests that a monitor rotated by 90° receives a portrait wallpaper."""

        self.reported = [hypr_monitor("DP-2", 800, 600, transform=1)]

        picker.apply(self.src)

        with Image.open(self.noctalia_calls[0][4]) as im:
            self.assertEqual(im.size, (600, 800))

    def test_monitors_with_same_resolution_share_a_file(self) -> None:
        """Tests that two monitors with the same resolution are given the same wallpaper file."""

        self.reported = [hypr_monitor("DP-1", 800, 600), hypr_monitor("DP-2", 800, 600)]

        picker.apply(self.src)

        self.assertEqual(self.noctalia_calls[0][4], self.noctalia_calls[1][4])

    def test_second_apply_sets_nothing(self) -> None:
        """Tests that applying the same image again does not call wallpaper-set a second time."""

        picker.apply(self.src)
        self.noctalia_calls.clear()

        picker.apply(self.src)

        self.assertEqual(self.noctalia_calls, [])

    def test_only_changed_monitors_are_set(self) -> None:
        """Tests that a monitor showing something else is set while an up-to-date one is left alone."""

        picker.apply(self.src)
        self.current["DP-1"] = "/home/user/Pictures/other.png"
        self.noctalia_calls.clear()

        picker.apply(self.src)

        self.assertEqual([cmd[3] for cmd in self.noctalia_calls], ["DP-1"])

    def test_style_is_passed_to_every_monitor(self) -> None:
        """Tests that the style arguments shape the wallpaper of each monitor."""

        picker.apply(self.src, margin=0.1, brightness=0.3, max_upscale=2.0)

        self.assertEqual(
            [Path(cmd[4]).name for cmd in self.noctalia_calls],
            ["met-1-wall-800x600-m10-b30-u20.jpg", "met-1-wall-1440x600-m10-b30-u20.jpg"],
        )

    def test_changed_style_replaces_the_wallpaper(self) -> None:
        """Tests that the same image in another style is set again, as it is a different file."""

        picker.apply(self.src)
        self.noctalia_calls.clear()

        picker.apply(self.src, margin=0.1)

        self.assertEqual([cmd[3] for cmd in self.noctalia_calls], ["eDP-1", "DP-1"])

    def test_disabled_monitor_is_ignored(self) -> None:
        """Tests that no wallpaper is set on a disabled monitor."""

        self.reported[0]["disabled"] = True

        picker.apply(self.src)

        self.assertEqual([cmd[3] for cmd in self.noctalia_calls], ["DP-1"])

    def test_desktop_error_stops_before_composing(self) -> None:
        """Tests that outside Hyprland apply() raises and neither composes nor calls Noctalia."""

        with mock.patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": ""}):
            with self.assertRaises(picker.DesktopError):
                picker.apply(self.src)

        self.assertEqual(self.noctalia_calls, [])
        self.assertEqual(list(self.src.parent.glob("*-wall-*")), [])

    def test_noctalia_failure_raises(self) -> None:
        """Tests that a Noctalia that never answers raises DesktopError naming the first monitor."""

        with mock.patch.object(
            picker.subprocess, "run",
            side_effect=lambda cmd, **kwargs: completed(json.dumps(self.reported)) if cmd[0] == "hyprctl"
            else (_ for _ in ()).throw(subprocess.CalledProcessError(1, cmd, stderr="no running instance")),
        ):
            with self.assertRaisesRegex(picker.DesktopError, "eDP-1"):
                picker.apply(self.src)


@unittest.skipUnless(os.environ.get("ARS_LIVE_WALLPAPER"), "set ARS_LIVE_WALLPAPER=1 to change the real wallpaper")
class TestLiveWallpaper(unittest.TestCase):
    """Tests the whole chain on the real desktop: Met API -> download -> compose -> Noctalia."""

    def pick(self, client: httpx.Client, pool: list[int], tries: int = 25) -> dict:
        """Returns a random public-domain painting with an image, or ARS_LIVE_OBJECT_ID if set."""

        if os.environ.get("ARS_LIVE_OBJECT_ID"):
            return met.get_object(client, int(os.environ["ARS_LIVE_OBJECT_ID"]))
        for object_id in random.sample(pool, min(tries, len(pool))):
            obj: dict = met.get_object(client, object_id)
            if obj.get("isPublicDomain") and obj.get("primaryImage") and artwork.is_painting(obj):
                return obj
        self.fail(f"no usable object in {tries} tries")

    def download(self, client: httpx.Client, obj: dict) -> Path:
        """Saves the object's image into the real image cache, where Noctalia can keep reading it."""

        path: Path = images_dir() / f"met-{obj['objectID']}.jpg"
        if not path.exists():
            with client.stream("GET", obj["primaryImage"]) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_bytes(65536):
                        f.write(chunk)
        return path

    def test_applies_a_random_artwork(self) -> None:
        """Tests that a random artwork from the Met ends up as the wallpaper of every monitor."""

        previous: dict[str, str] = {
            mon.name: subprocess.run(
                ["noctalia", "msg", "wallpaper-get", mon.name], capture_output=True, text=True
            ).stdout.strip()
            for mon in picker.monitors()
        }

        client: httpx.Client = met.make_client()
        self.addCleanup(client.close)
        obj: dict = self.pick(client, met.load_pool(client))
        src: Path = self.download(client, obj)

        picker.apply(src)

        print(f"\n{obj['title']} ({obj.get('objectDate') or 'undated'}) - {obj['objectURL']}")
        for name, old in previous.items():
            wall: Path = picker.compose_wallpaper(src, next(
                (m.width, m.height) for m in picker.monitors() if m.name == name
            ))
            self.assertTrue(wall.exists())
            print(f"{name}: {wall}")
            print(f"  restore with: noctalia msg wallpaper-set {name} '{old}'")


if __name__ == "__main__":
    unittest.main()
