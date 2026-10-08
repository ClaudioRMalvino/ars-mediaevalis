"""Choosing an artwork from the pool and getting its image onto the disk."""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import httpx

from ars_mediaevalis.museums import met
from ars_mediaevalis.paths import images_dir

log = logging.getLogger(__name__)


@dataclass
class Artwork:
    object_id: int
    title: str
    artist: str | None
    artist_bio: str | None
    date: str | None
    culture: str | None
    place: str | None          # built from city / region / country
    medium: str | None
    dimensions: str | None
    department: str | None
    image_url: str
    thumb_url: str | None
    object_url: str
    wikidata_url: str | None
    artist_wikidata_url: str | None
    credit: str | None
    history: str | None = None          # filled by history.enrich()
    history_source: str | None = None   # "wikipedia:object" | "wikipedia:artist" | "catalogue"
    history_url: str | None = None      # the Wikipedia article, if there is one
    image_path: str | None = None       # filled after download


def _s(value) -> str | None:
    """
    Normalises a Met field: the API uses "" for missing values.

    Args:
        value: any value from a Met record.

    Returns:
        str | None: the stripped string, or None if it is empty or not a string.
    """
    return value.strip() if isinstance(value, str) and value.strip() else None


def is_painting(obj: dict) -> bool:
    """
    Tells paintings from the manuscripts, textiles, sculptures and facsimiles that the
    Met's search also returns for medium=Paintings.

    Args:
        obj (dict): JSON payload of a Met object.

    Returns:
        bool: True if the classification is "Paintings" or one of its kinds ("Paintings-Panels").
    """
    classification: str = _s(obj.get("classification")) or ""
    return classification == "Paintings" or classification.startswith("Paintings-")


def to_artwork(obj: dict) -> Artwork | None:
    """
    Converts a Met /objects/{id} record into an Artwork.

    Args:
        obj (dict): JSON payload of a Met object.

    Returns:
        Artwork | None: None unless the object is a public-domain painting with a primary image.
    """
    if not obj.get("isPublicDomain") or not _s(obj.get("primaryImage")) or not is_painting(obj):
        return None
    place: str = ", ".join(p for p in (_s(obj.get(k)) for k in ("city", "region", "country")) if p)
    return Artwork(
        object_id=obj["objectID"],
        title=_s(obj.get("title")) or "Untitled",
        artist=_s(obj.get("artistDisplayName")),
        artist_bio=_s(obj.get("artistDisplayBio")),
        date=_s(obj.get("objectDate")),
        culture=_s(obj.get("culture")),
        place=place or None,
        medium=_s(obj.get("medium")),
        dimensions=_s(obj.get("dimensions")),
        department=_s(obj.get("department")),
        image_url=obj["primaryImage"].strip(),
        thumb_url=_s(obj.get("primaryImageSmall")),
        object_url=_s(obj.get("objectURL")) or f"https://www.metmuseum.org/art/collection/search/{obj['objectID']}",
        wikidata_url=_s(obj.get("objectWikidata_URL")),
        artist_wikidata_url=_s(obj.get("artistWikidata_URL")),
        credit=_s(obj.get("creditLine")),
    )


def from_dict(data: dict | None) -> Artwork | None:
    """
    Rebuilds an Artwork that was stored with dataclasses.asdict(), e.g. in state.json.

    Args:
        data (dict | None): the stored fields; unknown keys are ignored.

    Returns:
        Artwork | None: None if data is empty or lacks a required field.
    """
    if not isinstance(data, dict):
        return None
    known: set[str] = {f.name for f in fields(Artwork)}
    try:
        return Artwork(**{k: v for k, v in data.items() if k in known})
    except TypeError:
        log.warning("stored artwork is incomplete; ignoring it")
        return None


def choose(client: httpx.Client, pool: list[int], shown: set[int], rejected: set[int],
           max_attempts: int = 15) -> Artwork:
    """
    Picks a random usable artwork that has not been shown yet.

    Mutates its arguments: unusable IDs are added to rejected, and shown is cleared
    once the whole pool has been seen.

    Args:
        client (httpx.Client): Client to the MET API.
        pool (list[int]): objectIDs to choose from.
        shown (set[int]): objectIDs already used as a wallpaper.
        rejected (set[int]): objectIDs known to be unusable.
        max_attempts (int): objects to try before giving up, optional (default: 15).

    Returns:
        Artwork: the chosen artwork.
    """
    candidates: set[int] = set(pool) - shown - rejected
    if not candidates:
        log.info("seen the whole pool; starting over")
        shown.clear()
        candidates = set(pool) - rejected
    if not candidates:
        raise RuntimeError("the pool has no usable artworks")
    for _ in range(max_attempts):
        if not candidates:
            break
        object_id: int = random.choice(tuple(candidates))
        candidates.discard(object_id)
        try:
            art: Artwork | None = to_artwork(met.get_object(client, object_id))
        except httpx.HTTPStatusError as e:
            # The search still lists objects the Met has withdrawn: their record is a 404.
            if e.response.status_code != 404:
                raise
            log.info("object %d no longer exists at the Met; skipping", object_id)
            art = None
        if art:
            return art
        rejected.add(object_id)
    raise RuntimeError(f"no usable artwork after {max_attempts} attempts")


def download(client: httpx.Client, art: Artwork) -> Path:
    """
    Streams the artwork's image into the cache, unless it is already there.

    Args:
        client (httpx.Client): Client used for the download.
        art (Artwork): the artwork whose image_url is fetched.

    Returns:
        Path: cache/images/met-<id>.jpg
    """
    dest: Path = images_dir() / f"met-{art.object_id}.jpg"
    if dest.exists():
        return dest
    tmp: Path = dest.with_suffix(".part")
    try:
        with client.stream("GET", art.image_url) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        os.replace(tmp, dest)   # atomic: a half-downloaded file never sits at dest
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def _metadata_file(object_id: int) -> Path:
    return images_dir() / f"met-{object_id}.json"


def remember(art: Artwork) -> None:
    """
    Stores the artwork's details next to its image, so it can be picked again later
    without the network. Image and details share the cache: wiping it forgets both.

    Args:
        art (Artwork): the artwork, with image_path filled in.
    """
    dest: Path = _metadata_file(art.object_id)
    tmp: Path = dest.with_suffix(".json.part")
    tmp.write_text(json.dumps(asdict(art), indent=2))
    os.replace(tmp, dest)


def cached() -> list[Artwork]:
    """
    Lists the artworks that can be shown again without the network.

    Returns:
        list[Artwork]: every remembered artwork whose image is still in the cache, newest first.
    """
    found: list[tuple[float, Artwork]] = []
    for meta in images_dir().glob("met-*.json"):
        image: Path = meta.with_suffix(".jpg")
        if not image.exists():
            continue
        try:
            art: Artwork | None = from_dict(json.loads(meta.read_text()))
        except (OSError, ValueError):
            log.warning("unreadable details in %s; skipping", meta)
            continue
        if art is None:
            continue
        art.image_path = str(image)
        found.append((meta.stat().st_mtime, art))
    return [art for _, art in sorted(found, key=lambda pair: pair[0], reverse=True)]


def find_cached(object_id: int) -> Artwork | None:
    """
    Looks up one remembered artwork.

    Args:
        object_id (int): the Met objectID.

    Returns:
        Artwork | None: the artwork, or None if its details or its image are not in the cache.
    """
    return next((art for art in cached() if art.object_id == object_id), None)
