"""The GTK4 window: today's artwork as a greeting, and a way back to the earlier ones."""

from __future__ import annotations

import html
import threading
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from ars_mediaevalis import artwork, state  # noqa: E402
from ars_mediaevalis.artwork import Artwork  # noqa: E402

# Becomes the window class in Hyprland: the window rule that floats and centres it matches on this.
APP_ID: str = "io.github.claudiormalvino.ArsMediaevalis"

SOURCE_LABEL: dict[str, str] = {
    "wikipedia:object": "From Wikipedia",
    "wikipedia:artist": "From Wikipedia, about the artist",
    "catalogue": "From The Met's catalogue",
}


def meta_line(art: Artwork) -> str:
    """
    Joins the catalogue facts that exist into one line.

    Args:
        art (Artwork): the artwork to describe.

    Returns:
        str: e.g. "Robert Campin · Netherlandish, ca. 1375-1444 · ca. 1427-32 · Oil on oak"
    """
    meta: list[str | None] = [art.artist or art.culture, art.artist_bio, art.date, art.place,
                              art.medium, art.dimensions]
    # The Met puts several measurements on separate lines.
    return " · ".join("; ".join(m.splitlines()) for m in meta if m)


def gallery(current: Artwork | None, cached: list[Artwork]) -> tuple[list[Artwork], int]:
    """
    Orders the artworks the window can page through.

    Args:
        current (Artwork | None): the artwork that is the wallpaper now, if any.
        cached (list[Artwork]): the earlier artworks still in the cache, newest first.

    Returns:
        tuple[list[Artwork], int]: the artworks, newest first, and the index to open on
        (the current artwork, which is added in front if the cache does not hold it).
    """
    works: list[Artwork] = list(cached)
    if current is None:
        return works, 0
    for i, art in enumerate(works):
        if art.object_id == current.object_id:
            return works, i
    return [current] + works, 0


def _label(text: str, markup: bool = False, css: str | None = None) -> Gtk.Label:
    lab = Gtk.Label()
    (lab.set_markup if markup else lab.set_text)(text)
    lab.set_wrap(True)
    lab.set_selectable(True)
    lab.set_can_focus(False)   # else the first label opens focused, with its text selected
    lab.set_xalign(0)
    if css:
        lab.add_css_class(css)
    return lab


class _Window:
    """The window and what it is showing: one artwork out of `works`."""

    def __init__(self, app: Gtk.Application, works: list[Artwork], index: int, current_id: int | None) -> None:
        self.works: list[Artwork] = works
        self.index: int = index
        self.current_id: int | None = current_id   # the artwork that is the wallpaper now
        self.message: str = ""

        self.win = Gtk.ApplicationWindow(application=app)
        self.win.set_default_size(900, 1000)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.win.add_controller(keys)

        self._render()
        self.win.present()

    def _on_key(self, _controller, keyval: int, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.win.close()
            return True
        return False

    def _go(self, step: int) -> None:
        self.index += step
        self.message = ""
        self._render()

    def _use(self, button: Gtk.Button) -> None:
        art: Artwork = self.works[self.index]
        button.set_sensitive(False)
        button.set_label("Setting wallpaper…")

        def work() -> None:
            from ars_mediaevalis import cli   # lazy: cli imports this module lazily too
            code: int = cli.use(art.object_id)
            GLib.idle_add(self._used, art, code)

        # Composing for a new resolution takes a moment: keep the window responsive.
        threading.Thread(target=work, daemon=True).start()

    def _used(self, art: Artwork, code: int) -> bool:
        if code == 0:
            self.current_id = art.object_id
            self.message = ""
        else:
            self.message = "Could not set the wallpaper. See the log for details."
        self._render()
        return False   # run once

    def _render(self) -> None:
        art: Artwork = self.works[self.index]
        self.win.set_title(art.title)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(20)

        # The original download, not the composed wallpaper.
        if art.image_path and Path(art.image_path).exists():
            pic = Gtk.Picture.new_for_filename(art.image_path)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_vexpand(True)
            box.append(pic)

        e = html.escape
        box.append(_label(f'<span size="x-large" weight="bold">{e(art.title)}</span>', markup=True))

        meta: str = meta_line(art)
        if meta:
            box.append(_label(meta, css="dim-label"))

        source: str = SOURCE_LABEL.get(art.history_source or "", "")
        if source:
            box.append(_label(f"<i>{e(source)}</i>", markup=True))

        if art.history:
            scroll = Gtk.ScrolledWindow()
            scroll.set_min_content_height(140)
            text = _label(art.history)
            text.set_valign(Gtk.Align.START)
            scroll.set_child(text)
            box.append(scroll)

        if art.credit:
            box.append(_label(art.credit, css="dim-label"))

        if self.message:
            box.append(_label(self.message, css="error"))

        bar = Gtk.Box(spacing=8)

        if len(self.works) > 1:
            # Newest first: "Earlier" moves towards the end of the list.
            earlier = Gtk.Button(label="‹ Earlier")
            earlier.set_sensitive(self.index < len(self.works) - 1)
            earlier.connect("clicked", lambda _b: self._go(+1))
            later = Gtk.Button(label="Later ›")
            later.set_sensitive(self.index > 0)
            later.connect("clicked", lambda _b: self._go(-1))
            bar.append(earlier)
            bar.append(Gtk.Label(label=f"{self.index + 1} of {len(self.works)}"))
            bar.append(later)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        bar.append(Gtk.LinkButton.new_with_label(art.object_url, "View at The Met"))
        if art.history_url:
            bar.append(Gtk.LinkButton.new_with_label(art.history_url, "Wikipedia"))

        if art.object_id == self.current_id:
            bar.append(_label("Current wallpaper", css="dim-label"))
        else:
            use = Gtk.Button(label="Use as wallpaper")
            use.add_css_class("suggested-action")
            use.connect("clicked", self._use)
            bar.append(use)

        box.append(bar)
        self.win.set_child(box)


def show_today() -> int:
    """
    Opens the window on the current artwork and blocks until it is closed.

    Returns:
        int: exit code; 1 if there is no artwork to show.
    """
    current: Artwork | None = artwork.from_dict(state.load().artwork)
    works, index = gallery(current, artwork.cached())
    if not works:
        print("No artwork yet. Run `ars_mediaevalis run`.")
        return 1

    def activate(app: Gtk.Application) -> None:
        if app.get_active_window():   # activated a second time: just raise the window
            app.get_active_window().present()
            return
        app.window = _Window(app, works, index, current.object_id if current else None)

    app = Gtk.Application(application_id=APP_ID)
    app.connect("activate", activate)
    return app.run(None)   # None: keep GTK from parsing sys.argv, which still holds "show"
