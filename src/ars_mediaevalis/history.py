"""Finding a short history of an artwork: Wikidata -> Wikipedia, else the catalogue facts."""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx

from ars_mediaevalis.artwork import Artwork

log = logging.getLogger(__name__)

QID: re.Pattern[str] = re.compile(r"^Q\d+$")
WIKIDATA_URL: str = "https://www.wikidata.org/wiki/Special:EntityData"
WIKIPEDIA_URL: str = "https://en.wikipedia.org/api/rest_v1/page/summary"


def qid_from_url(url: str | None) -> str | None:
    """
    Extracts the Q-ID from a Wikidata URL.

    Args:
        url (str | None): e.g. https://www.wikidata.org/wiki/Q29910832

    Returns:
        str | None: e.g. "Q29910832", or None if the URL does not end in a Q-ID.
    """
    if not url:
        return None
    qid: str = url.rstrip("/").rsplit("/", 1)[-1]
    return qid if QID.match(qid) else None


def enwiki_title(client: httpx.Client, qid: str) -> str | None:
    """
    Looks up the title of the English Wikipedia article linked to a Wikidata item.

    Args:
        client (httpx.Client): Client connection.
        qid (str): Wikidata Q-ID.

    Returns:
        str | None: the article title, or None if the item has no English article.
    """
    r = client.get(f"{WIKIDATA_URL}/{qid}.json")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    # Iterate rather than index by qid: a merged item is returned under its new Q-ID.
    for entity in r.json().get("entities", {}).values():
        link = entity.get("sitelinks", {}).get("enwiki")
        if link:
            return link["title"]
    return None


def wikipedia_summary(client: httpx.Client, title: str) -> tuple[str, str | None] | None:
    """
    Fetches the lead paragraph of an English Wikipedia article.

    Args:
        client (httpx.Client): Client connection.
        title (str): article title, as given by Wikidata.

    Returns:
        tuple[str, str | None] | None: (extract, page URL), or None for a missing,
        empty or disambiguation page.
    """
    slug: str = quote(title.replace(" ", "_"), safe="")
    r = client.get(f"{WIKIPEDIA_URL}/{slug}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data: dict = r.json()
    extract: str = (data.get("extract") or "").strip()
    if data.get("type") == "disambiguation" or not extract:
        return None
    url: str | None = data.get("content_urls", {}).get("desktop", {}).get("page") or None
    return extract, url


def catalogue_blurb(art: Artwork) -> str:
    """
    Composes a sentence or two from the catalogue fields, for works without an article.

    Args:
        art (Artwork): the artwork to describe.

    Returns:
        str: e.g. "Made in Tournai, Belgium, ca. 1427-32, in oil on oak. Now in The Cloisters at The Met."
    """
    parts: list[str] = []
    if art.place:
        parts.append(f"Made in {art.place}")
    elif art.culture:
        parts.append(art.culture)   # an adjective ("French"), so not "Made in French"
    if art.date:
        parts.append(art.date if parts else art.date[0].upper() + art.date[1:])
    if art.medium:
        medium: str = art.medium[0].lower() + art.medium[1:]
        parts.append(f"in {medium}" if parts else f"Made in {medium}")
    first: str = ", ".join(parts) + "." if parts else ""
    where: str = f"Now in {art.department} at The Met." if art.department else ""
    return " ".join(s for s in (first, where) if s) or "No further details are catalogued."


def _try_wiki(client: httpx.Client, wikidata_url: str | None) -> tuple[str, str | None] | None:
    qid: str | None = qid_from_url(wikidata_url)
    if not qid:
        return None
    title: str | None = enwiki_title(client, qid)
    return wikipedia_summary(client, title) if title else None


def enrich(client: httpx.Client, art: Artwork) -> None:
    """
    Fills art.history, art.history_source and art.history_url in place.

    Tries the work's own Wikipedia article, then the artist's, then falls back to the
    catalogue facts. Never raises: a missing history must not stop the wallpaper.

    Args:
        client (httpx.Client): Client connection.
        art (Artwork): the artwork to enrich.
    """
    for url, source in ((art.wikidata_url, "wikipedia:object"),
                        (art.artist_wikidata_url, "wikipedia:artist")):
        try:
            found = _try_wiki(client, url)
        except (httpx.HTTPError, ValueError, KeyError, AttributeError, TypeError):
            log.warning("history lookup failed for %s", url, exc_info=True)
            continue
        if found:
            art.history, art.history_url = found
            art.history_source = source
            return
    art.history = catalogue_blurb(art)
    art.history_source = "catalogue"
    art.history_url = None
