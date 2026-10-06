"""Everything about getting an image onto the screens: Hyprland + Noctalia only."""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms, ImageEnhance, ImageFilter, ImageOps

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 500_000_000  # trusted source, but keep *a* limit

MARGIN = 0.06           # fraction of the screen left around the painting
BRIGHTNESS = 0.45       # background darkening factor (< 1 darkens)
MAX_UPSCALE = 1.5       # never enlarge the foreground more than this
BLUR_DOWNSCALE = 8      # blur a 1/8-size copy: same look, far faster


class DesktopError(RuntimeError):
    """Hyprland or Noctalia is missing or unreachable."""


@dataclass(frozen=True)
class Monitor:
    name: str      # connector, e.g. "eDP-1": what Noctalia's wallpaper-set expects
    width: int     # physical pixels, already corrected for rotation
    height: int


# --- Hyprland ---------------------------------------------------------------

def monitors() -> list[Monitor]:
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        raise DesktopError(
            "HYPRLAND_INSTANCE_SIGNATURE is not set: not inside a Hyprland session, "
            "or the session environment was not imported into systemd "
            "(dbus-update-activation-environment --systemd ...)."
        )
    try:
        out = subprocess.run(
            ["hyprctl", "monitors", "-j"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout
    except FileNotFoundError as e:
        raise DesktopError("hyprctl not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise DesktopError(f"hyprctl failed: {e.stderr.strip()}") from e
    except subprocess.TimeoutExpired as e:
        raise DesktopError("hyprctl timed out") from e

    result: list[Monitor] = []
    for m in json.loads(out):
        if m.get("disabled"):
            continue
        w, h = m["width"], m["height"]
        if m.get("transform", 0) % 2 == 1:   # rotated 90° / 270°
            w, h = h, w
        result.append(Monitor(m["name"], w, h))

    if not result:
        raise DesktopError("Hyprland reports no active monitors")
    return result


# --- Noctalia ---------------------------------------------------------------

def _noctalia_set(monitor: str, path: Path, attempts: int = 5, delay: float = 2.0) -> None:
    if not shutil.which("noctalia"):
        raise DesktopError("noctalia not found on PATH (Noctalia v5+ required)")
    cmd = ["noctalia", "msg", "wallpaper-set", monitor, str(path)]
    for i in range(1, attempts + 1):
        try:
            subprocess.run(cmd, check=True, timeout=10, capture_output=True, text=True)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            # At login Noctalia may not be listening yet: retry a few times.
            stderr = (getattr(e, "stderr", "") or "").strip()
            log.warning("wallpaper-set %s failed (attempt %d): %s", monitor, i, stderr or e)
            if i == attempts:
                raise DesktopError(f"Noctalia did not accept the wallpaper for {monitor}") from e
            time.sleep(delay)


# --- Composition ------------------------------------------------------------

def _to_srgb(im: Image.Image) -> Image.Image:
    """Convert using the embedded ICC profile if there is one, else a plain convert."""
    icc = im.info.get("icc_profile")
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            dst = ImageCms.createProfile("sRGB")
            return ImageCms.profileToProfile(im, src, dst, outputMode="RGB")
        except ImageCms.PyCMSError:
            log.warning("bad ICC profile; converting without colour management")
    return im.convert("RGB")


def compose_wallpaper(src: Path, size: tuple[int, int]) -> Path:
    w, h = size
    tag = f"{w}x{h}-m{round(MARGIN * 100)}-b{round(BRIGHTNESS * 100)}-u{round(MAX_UPSCALE * 10)}"
    out = src.with_name(f"{src.stem}-wall-{tag}.jpg")
    if out.exists():
        return out

    with Image.open(src) as raw:
        raw.draft(raw.mode, size)   # JPEG only: decode at reduced scale, still >= size
        im = _to_srgb(ImageOps.exif_transpose(raw))

    # Background: cover a small copy, blur it, scale back up, darken.
    small = (max(1, w // BLUR_DOWNSCALE), max(1, h // BLUR_DOWNSCALE))
    bg = ImageOps.fit(im, small, Image.Resampling.BILINEAR)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=max(small) / 40))
    bg = bg.resize(size, Image.Resampling.BICUBIC)
    bg = ImageEnhance.Brightness(bg).enhance(BRIGHTNESS)

    # Foreground: fit inside the margin box, but don't blow up small images.
    box_w, box_h = int(w * (1 - 2 * MARGIN)), int(h * (1 - 2 * MARGIN))
    scale = min(box_w / im.width, box_h / im.height, MAX_UPSCALE)
    fg = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                   Image.Resampling.LANCZOS)
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))

    # Atomic write: a crash never leaves a truncated file at `out`.
    tmp = out.with_suffix(".part")
    bg.save(tmp, "JPEG", quality=92)
    os.replace(tmp, out)
    return out


# --- Public entry point -----------------------------------------------------

def apply(src: Path) -> None:
    """Compose for each monitor at its own resolution and set it via Noctalia."""
    for mon in monitors():
        wall = compose_wallpaper(src, (mon.width, mon.height))
        _noctalia_set(mon.name, wall)
        log.info("wallpaper set on %s (%dx%d)", mon.name, mon.width, mon.height)
