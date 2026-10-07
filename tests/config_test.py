import src.ars_mediaevalis.config as config
import logging
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

config.log.addHandler(logging.NullHandler())  # keeps expected warnings out of the test output

EXAMPLE: Path = Path(__file__).resolve().parent.parent / "config.example.toml"


class TestDefaults(unittest.TestCase):
    """Tests the settings used when the user configures nothing."""

    def test_default_values(self) -> None:
        """Tests each default."""

        cfg: config.Config = config.Config()
        self.assertEqual(cfg.margin, 0.06)
        self.assertEqual(cfg.background_brightness, 0.45)
        self.assertEqual(cfg.max_upscale, 1.5)
        self.assertIs(cfg.greeting, True)
        self.assertEqual((cfg.date_begin, cfg.date_end), (1200, 1500))
        self.assertEqual(cfg.pool_max_age_days, 30)

    def test_defaults_are_within_their_limits(self) -> None:
        """Tests that every default would be accepted if written in the file."""

        self.assertEqual(config.from_dict(vars(config.Config())), config.Config())

    def test_config_is_immutable(self) -> None:
        """Tests that a loaded Config cannot be changed by accident."""

        with self.assertRaises(AttributeError):
            config.Config().margin = 0.5

    def test_every_number_has_limits(self) -> None:
        """Tests that each numeric setting has a range, and each float a precision."""

        for name, default in vars(config.Config()).items():
            if isinstance(default, bool):
                continue
            with self.subTest(setting=name):
                self.assertIn(name, config.LIMITS)
                if isinstance(default, float):
                    self.assertIn(name, config.DECIMALS)


class TestFromDict(unittest.TestCase):
    """Tests that from_dict() merges and validates settings as intended."""

    def test_empty_gives_defaults(self) -> None:
        """Tests that no settings give the defaults."""

        self.assertEqual(config.from_dict({}), config.Config())

    def test_every_setting_can_be_overridden(self) -> None:
        """Tests that a full set of valid settings is taken over."""

        data: dict = {
            "margin": 0.1, "background_brightness": 0.3, "max_upscale": 2.0, "greeting": False,
            "date_begin": 1300, "date_end": 1600, "pool_max_age_days": 7,
        }
        self.assertEqual(config.from_dict(data), config.Config(**data))

    def test_partial_override_keeps_other_defaults(self) -> None:
        """Tests that one setting leaves the others at their defaults."""

        cfg: config.Config = config.from_dict({"margin": 0.1})
        self.assertEqual(cfg, config.Config(margin=0.1))

    def test_limits_are_inclusive(self) -> None:
        """Tests that the lowest and highest value of every range are accepted."""

        for name, (low, high) in config.LIMITS.items():
            if name in ("date_begin", "date_end"):
                continue   # they also have to be in order: covered below
            for value in (low, high):
                with self.subTest(setting=name, value=value):
                    self.assertEqual(getattr(config.from_dict({name: value}), name), value)

    def test_out_of_range_values_keep_the_default(self) -> None:
        """Tests that a value outside its range is replaced by the default."""

        cases: list[tuple[str, float]] = [
            ("margin", -0.01), ("margin", 0.5), ("background_brightness", -0.1), ("background_brightness", 1.5),
            ("max_upscale", 0.5), ("max_upscale", 11), ("date_begin", 0), ("date_end", 3000),
            ("pool_max_age_days", 0), ("pool_max_age_days", -3),
        ]
        for name, value in cases:
            with self.subTest(setting=name, value=value):
                self.assertEqual(config.from_dict({name: value}), config.Config())

    def test_wrong_types_keep_the_default(self) -> None:
        """Tests that strings, lists, tables and booleans are not accepted as numbers, nor numbers as booleans."""

        cases: list[tuple[str, object]] = [
            ("margin", "0.1"), ("margin", True), ("margin", [0.1]), ("margin", {"x": 0.1}),
            ("max_upscale", False), ("date_begin", "1300"), ("date_begin", True),
            ("greeting", 1), ("greeting", 0), ("greeting", "false"), ("greeting", "yes"),
        ]
        for name, value in cases:
            with self.subTest(setting=name, value=value):
                self.assertEqual(config.from_dict({name: value}), config.Config())

    def test_whole_numbers_only_where_required(self) -> None:
        """Tests that years and days reject fractions but accept a float that is whole."""

        self.assertEqual(config.from_dict({"date_begin": 1300.5}), config.Config())
        self.assertEqual(config.from_dict({"pool_max_age_days": 7.5}), config.Config())
        cfg: config.Config = config.from_dict({"pool_max_age_days": 7.0})
        self.assertEqual(cfg.pool_max_age_days, 7)
        self.assertIsInstance(cfg.pool_max_age_days, int)

    def test_integers_are_accepted_for_floats(self) -> None:
        """Tests that `max_upscale = 2` works as well as `2.0`, and is stored as a float."""

        cfg: config.Config = config.from_dict({"max_upscale": 2, "background_brightness": 1, "margin": 0})
        self.assertEqual((cfg.max_upscale, cfg.background_brightness, cfg.margin), (2.0, 1.0, 0.0))
        self.assertIsInstance(cfg.max_upscale, float)

    def test_floats_are_rounded_to_file_name_precision(self) -> None:
        """Tests that the style settings keep exactly the precision the wallpaper file name records."""

        cfg: config.Config = config.from_dict({"margin": 0.0649, "background_brightness": 0.333, "max_upscale": 1.26})
        self.assertEqual((cfg.margin, cfg.background_brightness, cfg.max_upscale), (0.06, 0.33, 1.3))

    def test_invalid_setting_does_not_discard_the_valid_ones(self) -> None:
        """Tests that one bad value leaves the other settings from the file in effect."""

        cfg: config.Config = config.from_dict({"margin": 9, "background_brightness": 0.3, "greeting": False})
        self.assertEqual(cfg, config.Config(background_brightness=0.3, greeting=False))

    def test_unknown_settings_are_ignored_with_a_warning(self) -> None:
        """Tests that a typo is reported by name and does not affect the rest."""

        with self.assertLogs(config.log, level="WARNING") as logs:
            cfg: config.Config = config.from_dict({"marign": 0.1, "greeting": False})
        self.assertEqual(cfg, config.Config(greeting=False))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("'marign'", logs.output[0])

    def test_invalid_value_is_reported_with_setting_and_default(self) -> None:
        """Tests that the warning names the setting, the bad value and the default used instead."""

        with self.assertLogs(config.log, level="WARNING") as logs:
            config.from_dict({"margin": 0.9})
        for expected in ("margin", "0.9", "0.06"):
            self.assertIn(expected, logs.output[0])

    def test_valid_settings_log_nothing(self) -> None:
        """Tests that a correct configuration produces no warnings."""

        with self.assertNoLogs(config.log, level="WARNING"):
            config.from_dict({"margin": 0.1, "greeting": False, "date_end": 1600})

    def test_date_range_must_be_in_order(self) -> None:
        """Tests that a reversed or empty range puts both years back to their defaults."""

        for begin, end in ((1500, 1200), (1400, 1400)):
            with self.subTest(begin=begin, end=end):
                cfg: config.Config = config.from_dict({"date_begin": begin, "date_end": end})
                self.assertEqual((cfg.date_begin, cfg.date_end), (1200, 1500))

    def test_one_year_can_clash_with_the_other_default(self) -> None:
        """Tests that a single year that leaves the range empty is rejected too."""

        self.assertEqual(config.from_dict({"date_begin": 1550}), config.Config())
        self.assertEqual(config.from_dict({"date_end": 1100}), config.Config())

    def test_reversed_range_is_reported(self) -> None:
        """Tests that the warning for a reversed range names both years."""

        with self.assertLogs(config.log, level="WARNING") as logs:
            config.from_dict({"date_begin": 1500, "date_end": 1200})
        self.assertEqual(len(logs.output), 1)
        for expected in ("date_begin (1500)", "date_end (1200)", "1200-1500"):
            self.assertIn(expected, logs.output[0])

    def test_reversed_range_keeps_the_other_settings(self) -> None:
        """Tests that only the two years fall back; the rest of the file stays in effect."""

        cfg: config.Config = config.from_dict({"date_begin": 1500, "date_end": 1200, "margin": 0.1})
        self.assertEqual(cfg, config.Config(margin=0.1))

    def test_years_span_the_medieval_and_renaissance_period(self) -> None:
        """Tests that years from 700 to 1600 are accepted, both ends included, and nothing outside."""

        self.assertEqual(config.LIMITS["date_begin"], (700, 1600))
        self.assertEqual(config.LIMITS["date_end"], (700, 1600))
        widest: config.Config = config.from_dict({"date_begin": 700, "date_end": 1600})
        self.assertEqual((widest.date_begin, widest.date_end), (700, 1600))
        for name, year in (("date_begin", 699), ("date_begin", 1601), ("date_end", 699), ("date_end", 1601)):
            with self.subTest(setting=name, year=year):
                self.assertEqual(config.from_dict({name: year}), config.Config())

    def test_narrowest_range_is_one_year(self) -> None:
        """Tests that consecutive years are accepted."""

        cfg: config.Config = config.from_dict({"date_begin": 1400, "date_end": 1401})
        self.assertEqual((cfg.date_begin, cfg.date_end), (1400, 1401))

    def test_one_year_alone_is_fine_when_in_order(self) -> None:
        """Tests that only date_end, or only date_begin, can be set."""

        self.assertEqual(config.from_dict({"date_end": 1600}), config.Config(date_end=1600))
        self.assertEqual(config.from_dict({"date_begin": 1400}), config.Config(date_begin=1400))


class TestLoad(unittest.TestCase):
    """Tests that load() reads config.toml defensively as intended."""

    def setUp(self) -> None:
        """Points XDG_CONFIG_HOME at a temporary directory."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file: Path = Path(tmp.name) / "ars_mediaevalis" / "config.toml"

        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, content: str | bytes) -> None:
        """Writes the user's config.toml."""

        self.file.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            self.file.write_bytes(content)
        else:
            self.file.write_text(content)

    def test_path_is_in_the_config_directory(self) -> None:
        """Tests that the file is looked for at $XDG_CONFIG_HOME/ars_mediaevalis/config.toml."""

        self.assertEqual(config.path(), self.file)

    def test_missing_file_gives_defaults(self) -> None:
        """Tests that no file means the defaults, without creating one."""

        self.assertEqual(config.load(), config.Config())
        self.assertFalse(self.file.exists())

    def test_reads_settings(self) -> None:
        """Tests that settings in the file are loaded."""

        self.write("margin = 0.1\ngreeting = false\ndate_end = 1600\n")
        self.assertEqual(config.load(), config.Config(margin=0.1, greeting=False, date_end=1600))

    def test_empty_and_comment_only_files_give_defaults(self) -> None:
        """Tests that a file without settings gives the defaults."""

        for content in ("", "\n\n", "# margin = 0.3\n"):
            with self.subTest(content=content):
                self.write(content)
                self.assertEqual(config.load(), config.Config())

    def test_broken_file_gives_defaults_with_a_warning(self) -> None:
        """Tests that invalid TOML or a non-text file is reported and ignored, not raised."""

        for content in ("margin = = 0.1", "margin 0.1", "[unclosed", "greeting = yes", b"\xff\xfe\x00\x01"):
            with self.subTest(content=content):
                self.write(content)
                with self.assertLogs(config.log, level="WARNING") as logs:
                    self.assertEqual(config.load(), config.Config())
                self.assertIn(str(self.file), logs.output[0])

    def test_directory_in_place_of_the_file(self) -> None:
        """Tests that a directory named config.toml gives the defaults instead of raising."""

        self.file.mkdir(parents=True)
        with self.assertLogs(config.log, level="WARNING"):
            self.assertEqual(config.load(), config.Config())

    def test_tables_are_reported_as_unknown(self) -> None:
        """Tests that a [section] is not silently accepted."""

        self.write("[wallpaper]\nmargin = 0.1\n")
        with self.assertLogs(config.log, level="WARNING") as logs:
            self.assertEqual(config.load(), config.Config())
        self.assertIn("'wallpaper'", logs.output[0])

    def test_file_is_read_on_every_load(self) -> None:
        """Tests that an edit is picked up by the next load."""

        self.write("margin = 0.1\n")
        self.assertEqual(config.load().margin, 0.1)
        self.write("margin = 0.2\n")
        self.assertEqual(config.load().margin, 0.2)


class TestExampleFile(unittest.TestCase):
    """Tests that config.example.toml stays in step with the code."""

    def setUp(self) -> None:
        """Parses the example file."""

        with open(EXAMPLE, "rb") as f:
            self.example: dict = tomllib.load(f)

    def test_example_lists_every_setting(self) -> None:
        """Tests that the example names exactly the settings that exist."""

        self.assertEqual(set(self.example), set(vars(config.Config())))

    def test_example_shows_the_defaults(self) -> None:
        """Tests that copying the example unchanged gives the default configuration, without warnings."""

        with self.assertNoLogs(config.log, level="WARNING"):
            self.assertEqual(config.from_dict(self.example), config.Config())


if __name__ == "__main__":
    unittest.main()
