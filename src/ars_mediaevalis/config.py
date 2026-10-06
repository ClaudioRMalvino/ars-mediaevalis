"""config.toml: the settings a user may change, merged over the defaults in code."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

from ars_mediaevalis.paths import config_dir

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    margin: float = 0.06                 # fraction of the screen left around the painting
    background_brightness: float = 0.45  # 0 = black backdrop, 1 = as bright as the painting
    max_upscale: float = 1.5             # never enlarge a small painting more than this
    greeting: bool = True                # open the window when a new day's painting arrives
    date_begin: int = 1200               # European Paintings are searched from this year...
    date_end: int = 1500                 # ...to this one
    pool_max_age_days: int = 30          # how often the list of candidates is rebuilt


# (lowest, highest) accepted value, both included.
LIMITS: dict[str, tuple[float, float]] = {
    "margin": (0.0, 0.45),
    "background_brightness": (0.0, 1.0),
    "max_upscale": (1.0, 10.0),
    "date_begin": (1, 2100),
    "date_end": (1, 2100),
    "pool_max_age_days": (1, 3650),
}

# Decimals kept, so that every accepted value gets its own wallpaper file name.
DECIMALS: dict[str, int] = {"margin": 2, "background_brightness": 2, "max_upscale": 1}


def path() -> Path:
    """
    Provides the location of the configuration file, which need not exist.

    Returns:
        Path: config_dir()/config.toml
    """
    return config_dir() / "config.toml"


def _check(name: str, value: object, default: object) -> object | None:
    """
    Validates one setting.

    Args:
        name (str): name of the setting.
        value (object): the value from the file.
        default (object): the default, whose type the value must match.

    Returns:
        object | None: the value to use, or None (after a warning) to keep the default.
    """
    if isinstance(default, bool):
        if not isinstance(value, bool):
            log.warning("config: %s must be true or false, not %r; using %r", name, value, default)
            return None
        return value

    # bool is a subclass of int, so `margin = true` would otherwise pass as 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        log.warning("config: %s must be a number, not %r; using %r", name, value, default)
        return None
    if isinstance(default, int) and not float(value).is_integer():
        log.warning("config: %s must be a whole number, not %r; using %r", name, value, default)
        return None

    low, high = LIMITS[name]
    if not low <= value <= high:
        log.warning("config: %s must be between %s and %s, not %r; using %r", name, low, high, value, default)
        return None
    return int(value) if isinstance(default, int) else round(float(value), DECIMALS[name])


def from_dict(data: dict) -> Config:
    """
    Merges settings over the defaults. Unknown or invalid settings are reported and skipped.

    Args:
        data (dict): the parsed configuration file.

    Returns:
        Config: the defaults, overridden by every valid setting in data.
    """
    defaults: Config = Config()
    known: dict[str, object] = {f.name: getattr(defaults, f.name) for f in fields(Config)}
    accepted: dict[str, object] = {}
    for name, value in data.items():
        if name not in known:
            log.warning("config: unknown setting %r ignored (known: %s)", name, ", ".join(known))
            continue
        checked = _check(name, value, known[name])
        if checked is not None:
            accepted[name] = checked

    cfg: Config = replace(defaults, **accepted)
    if cfg.date_begin >= cfg.date_end:
        log.warning("config: date_begin (%d) must be before date_end (%d); using %d-%d",
                    cfg.date_begin, cfg.date_end, defaults.date_begin, defaults.date_end)
        cfg = replace(cfg, date_begin=defaults.date_begin, date_end=defaults.date_end)
    return cfg


def load() -> Config:
    """
    Reads config.toml. A missing file means the defaults; a broken one is reported and
    ignored, so a typo can never stop the wallpaper from changing.

    Returns:
        Config: the settings to use.
    """
    file: Path = path()
    if not file.exists():
        return Config()
    try:
        with open(file, "rb") as f:
            data: dict = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        log.warning("config: %s is unreadable (%s); using the defaults", file, e)
        return Config()
    return from_dict(data)
