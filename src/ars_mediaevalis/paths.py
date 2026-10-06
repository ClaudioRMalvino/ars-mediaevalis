import os
from pathlib import Path

APP = "ars_mediaevalis"

def _xdg(var: str, default: Path) -> Path:
    """
    Provides either specified variable, such as XDG_CONFIG_HOME, or for the system default path.

    Args:
        var (str): path as a string variable.
        default (Path): path as a Path type.

    Returns:
        Path: Either a path via system variable or path construction from pathlib Path.
    """
    base: Path = Path(os.environ.get(var) or default)
    path: Path= base / APP
    path.mkdir(parents=True, exist_ok=True)
    return path

def config_dir() -> Path:
   """
   Constructs .config/ars_mediaevalis directory.

   Returns:
       Path: user .config directory.
   """
   return _xdg("XDG_CONFIG_HOME", Path.home() / ".config")

def cache_dir() -> Path:
    """
    Constructs .cache/ars_mediaevalis directory.

    Returns:
        Path: user .cache directory.
    """
    return _xdg("XDG_CACHE_HOME", Path.home() / ".cache")

def state_dir() -> Path:
    """
    Constructs .local/state/ars_mediaevalis directory.

    Returns:
        Path: user state directory.
    """
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state")

def images_dir() -> Path:
    """
    Constructs .cache/ars_mediaevalis/images directory to hold retrieved images.

    Returns:
        Path: path to .cache
    """
    path: Path = cache_dir() / "images"
    path.mkdir(exist_ok=True)
    return path
