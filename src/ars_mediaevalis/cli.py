"""Argument parsing and the daily rule: a new artwork each day, not each login."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import asdict
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from PIL import Image

from ars_mediaevalis import artwork, history, picker, state
from ars_mediaevalis.artwork import Artwork
from ars_mediaevalis.museums import met
from ars_mediaevalis.paths import state_dir

log = logging.getLogger("ars_mediaevalis")

# What a missing network, a misbehaving API or a broken download can raise.
FETCH_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError, OSError, RuntimeError, ValueError, KeyError)


def _fetch_new(client: httpx.Client, st: state.State) -> Artwork:
    """
    Picks a new artwork, looks up its history and downloads its image.

    Updates st.shown and st.rejected, but not st.date or st.artwork.

    Args:
        client (httpx.Client): Client connection.
        st (state.State): the current state.

    Returns:
        Artwork: the new artwork, with history and image_path filled in.
    """
    pool: list[int] = met.load_pool(client)
    shown, rejected = set(st.shown), set(st.rejected)
    try:
        art: Artwork = artwork.choose(client, pool, shown, rejected)
    finally:
        st.rejected = sorted(rejected)   # worth keeping even when the pick failed

    src: Path = artwork.download(client, art)
    try:
        with Image.open(src) as im:
            im.verify()
    except Exception as e:
        src.unlink(missing_ok=True)
        st.rejected = sorted(rejected | {art.object_id})
        raise RuntimeError(f"image of object {art.object_id} is unreadable: {e}") from e

    art.image_path = str(src)
    history.enrich(client, art)
    artwork.remember(art)
    st.shown = sorted(shown | {art.object_id})
    return art


def greet() -> None:
    """
    Opens the detail window as the day's greeting, in a detached process.

    The window stays open as long as the user likes, so it must not run inside the
    daily run (which holds the state lock).
    """
    subprocess.Popen(
        [sys.executable, "-m", "ars_mediaevalis", "show"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run() -> int:
    """
    The daily rule. Safe to call as often as you like.

    Same day: re-applies today's artwork without touching the network. New day: picks a new
    painting, sets it and opens the greeting window. If that fails, yesterday's artwork stays
    and the date is not advanced, so the next run tries again.

    Returns:
        int: exit code; 0 if today's artwork is on the screens.
    """
    today: str = date.today().isoformat()
    with state.locked():   # login autostart and a timer may fire in the same second
        st: state.State = state.load()

        try:
            picker.monitors()
        except picker.DesktopError as e:
            # E.g. a timer fired before the Hyprland session exists. Nothing is lost.
            log.error("desktop not reachable, nothing done: %s", e)
            return 1

        current: Artwork | None = artwork.from_dict(st.artwork)
        status: int = 0

        if st.date != today or current is None:
            try:
                with met.make_client() as client:
                    art: Artwork = _fetch_new(client, st)
            except FETCH_ERRORS as e:
                log.error("could not get a new artwork: %s", e)
                state.save(st)
                if current is None or not current.image_path or not Path(current.image_path).exists():
                    return 1
                log.info("keeping the previous artwork: %s", current.title)
                art, status = current, 1
            else:
                log.info("new artwork: %s (%s)", art.title, art.object_url)
                st.date = today
                st.greeted = False
                st.artwork = asdict(art)
                state.save(st)   # before touching the desktop: the pick survives a failure there
        else:
            art = current
            if not art.image_path or not Path(art.image_path).exists():
                # The cache was wiped: fetch today's image again rather than picking anew.
                try:
                    with met.make_client() as client:
                        art.image_path = str(artwork.download(client, art))
                    artwork.remember(art)
                except FETCH_ERRORS as e:
                    log.error("could not download today's image again: %s", e)
                    return 1
                st.artwork = asdict(art)
                state.save(st)

        try:
            picker.apply(Path(art.image_path))
        except (picker.DesktopError, OSError) as e:
            log.error("could not set the wallpaper: %s", e)
            return 1

        if status == 0 and not st.greeted:
            greet()
            st.greeted = True
            state.save(st)
        return status


def use(object_id: int) -> int:
    """
    Sets an earlier artwork as today's, from the cache only: nothing is fetched.

    Args:
        object_id (int): objectID of an artwork listed by cached_list().

    Returns:
        int: exit code; 1 if the artwork is not in the cache or the wallpaper could not be set.
    """
    with state.locked():
        art: Artwork | None = artwork.find_cached(object_id)
        if art is None:
            log.error("artwork %s is not in the cache; `ars_mediaevalis list` shows what is", object_id)
            return 1
        try:
            picker.apply(Path(art.image_path))
        except (picker.DesktopError, OSError) as e:
            log.error("could not set the wallpaper: %s", e)
            return 1

        st: state.State = state.load()
        st.date = date.today().isoformat()   # it is today's artwork now; tomorrow brings a new one
        st.artwork = asdict(art)
        st.greeted = True
        state.save(st)
        log.info("wallpaper set to an earlier artwork: %s", art.title)
    return 0


def _one_line(art: Artwork) -> str:
    return " · ".join(p for p in (art.title, art.artist or art.culture, art.date) if p)


def cached_list() -> int:
    """
    Prints the earlier artworks that `use` can bring back, newest first.

    Returns:
        int: exit code; 1 if the cache holds none.
    """
    works: list[Artwork] = artwork.cached()
    if not works:
        print("No artworks in the cache yet.")
        return 1
    current: Artwork | None = artwork.from_dict(state.load().artwork)
    for art in works:
        mark: str = "*" if current and art.object_id == current.object_id else " "
        print(f"{mark} {art.object_id:>7}  {_one_line(art)}")
    print("\n* current wallpaper · bring one back with `ars_mediaevalis use <id>`")
    return 0


def info() -> int:
    """
    Prints today's artwork to the terminal.

    Returns:
        int: exit code; 1 if there is no artwork yet.
    """
    st: state.State = state.load()
    art: Artwork | None = artwork.from_dict(st.artwork)
    if art is None:
        print("No artwork yet. Run `ars_mediaevalis run`.")
        return 1
    print(art.title)
    print(" · ".join(p for p in (art.artist or art.culture or "Unknown maker", art.date, art.place) if p))
    for line in (art.medium, art.dimensions):
        if line:
            print(line)
    if art.history:
        print(f"\n{art.history}")
    print(f"\n{art.object_url}")
    if art.history_url:
        print(art.history_url)
    print(f"\nSet on {st.date} · {len(st.shown)} shown so far")
    return 0


def _setup_logging(verbose: bool) -> None:
    fmt: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt)
    for noisy in ("httpx", "httpcore", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        # A run started by Hyprland at login has no terminal and no journal to log to.
        handler = RotatingFileHandler(state_dir() / "ars_mediaevalis.log", maxBytes=256_000, backupCount=2)
    except OSError:
        return
    handler.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    """
    Entry point of the `ars_mediaevalis` command.

    Args:
        argv (list[str] | None): arguments; None means sys.argv[1:].

    Returns:
        int: exit code.
    """
    ap = argparse.ArgumentParser(
        prog="ars_mediaevalis",
        description="A medieval painting a day as your wallpaper (Hyprland + Noctalia).",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="log debug messages")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="the daily rule: new painting on a new day, else re-apply today's")
    sub.add_parser("show", help="open the window with today's artwork and the earlier ones")
    sub.add_parser("info", help="print today's artwork")
    sub.add_parser("list", help="list the earlier artworks still in the cache")
    use_parser = sub.add_parser("use", help="bring back an earlier artwork from the cache (no download)")
    use_parser.add_argument("object_id", type=int, help="ID as printed by `list`")
    args = ap.parse_args(argv)

    _setup_logging(args.verbose)

    match args.cmd:
        case "run":
            return run()
        case "info":
            return info()
        case "list":
            return cached_list()
        case "use":
            return use(args.object_id)
        case "show":
            from ars_mediaevalis import viewer   # lazy: only import GTK when needed
            return viewer.show_today()
    return 2
