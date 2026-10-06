from __future__ import annotations
from typing import Any, Final, Literal, Required, TypedDict

import json
import logging
import time
from datetime import datetime, timedelta, timezone
import httpx
from pathlib import Path

from ars_mediaevalis.paths import cache_dir

log = logging.getLogger(__name__)

BASE_URL: str = "https://collectionapi.metmuseum.org/public/collection"
USER_AGENT: str = "ars_mediaevalis/0.1 (https://github.com/ClaudioRMalvino/ars-mediaevalis)"

class SearchQuery(TypedDict, total=False):
    departmentId: Required[int]
    hasImages: Literal["true", "false"]
    medium: str
    dateBegin: int
    dateEnd: int
    q: str
    isOnView: Literal["true", "false"]
    geoLocation: str

# Paintings only. The search's medium filter is loose (it also returns manuscripts,
# sculptures and facsimiles), so artwork.is_painting() checks each object again.
QUERIES: Final[list[SearchQuery]] = [
    {"departmentId": 7,  "hasImages": "true", "medium": "Paintings"},                    # The Cloisters
    {"departmentId": 17, "hasImages": "true", "medium": "Paintings"},                    # Medieval Art
    {"departmentId": 11, "hasImages": "true", "dateBegin": 1200, "dateEnd": 1500},       # European Paintings
]

PAGE: int = 500
MAX_REACH: int = 10_000

def make_client() -> httpx.Client:
    """
    Constructs a client class with time out defaulted to 15.0 and a User-Agent header.

    Returns:
        httpx.Client: Client agent
    """
    return httpx.Client(
        timeout=httpx.Timeout(15.0, connect=5.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )

def _retryable(exc: Exception) -> bool:
    """
    Checks connection status and returns if a status code allows for a retryable connection.

    Args:
        exc (Exception): the exception passed by the connection request.

    Returns:
        bool: If so, returns true, if not, returns fale.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
       code = exc.response.status_code
       return code == 429 or code >= 500
    return False

def get_json(client: httpx.Client, url: str, params=None, attempts:int=4) -> dict:
    """
    Tries N attempts to get data from the url, given user params. If successful, provides JSON payload.
    If all attempts fail, raises an AssertionError.

    Args:
        client (httpx.Client): Client connection
        url (str): url to the MET API.
        params (type): parameters to pass to the API. optional (default: None).
        attempts (int): the number of connection attempts before throwing AssertionError, optional (default: 4).

    Returns:
        dict: JSON payload from the get request.
    """
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt == attempts or not _retryable(exc):
                raise
            log.warning("request failed (%s), retrying in %.0fs", exc, delay)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")

def search_all(client: httpx.Client, query: SearchQuery) -> list[int]:
    """
    Searches through all queries passed into query, page by page.

    Args:
        client (httpx.Client): Client to the MET API.
        query (SearchQuery): Dict of queries for items within the MET database.

    Returns:
        list[int]: list of objectIDs of items pulled from the query.
    """

    ids: list[int] = []
    offset: int = 0

    while True:
        params = {**query, "offset": offset, "limit": PAGE}
        data: dict[Any, Any]= get_json(client, f"{BASE_URL}/v1.1/search", params)
        page: list[int] = data.get("objectIDs") or []
        ids.extend(page)
        total: int = data.get("total", 0)
        offset += PAGE
        if not page or offset >= total or offset + PAGE > MAX_REACH:
            return ids

def _pool_file() -> Path:
    return cache_dir() / "pool.json"

def _build_pool(client: httpx.Client) -> list[int]:
    """
    Construct the pool of objectIDs
    Args:
        client (type): Description.

    Returns:
        list[int]: Description.
    """
    seen: set[int] = set()
    for q in QUERIES:
        found: list[int] = search_all(client, q)
        log.info("query %s -> %d ids", q, len(found))
        seen.update(found)
    return sorted(seen)

def load_pool(client: httpx.Client, max_age_days: int=30) -> list[int]:
    """
    Provides the pool of objectIDs, from the cache if it is recent and was built with
    the current QUERIES, else by searching again.

    Args:
        client (httpx.Client): Client to the MET API.
        max_age_days (int): age at which the cached pool is rebuilt, optional (default: 30).

    Returns:
        list[int]: sorted objectIDs. A stale pool is returned if the rebuild fails.
    """
    f: Path = _pool_file()
    cached = None

    if f.exists():
        try:
            cached = json.loads(f.read_text())
            built: datetime = datetime.fromisoformat(cached["built_at"])
            fresh: bool = datetime.now(timezone.utc) - built < timedelta(days=max_age_days)
            if fresh and cached.get("queries") == QUERIES:
                return cached["ids"]
        except (ValueError, KeyError, TypeError):
            log.warning("unreadable pool cache; rebuilding")
            cached = None
    try:
        ids = _build_pool(client)
    except Exception:
        # A pool built with other queries would bring back what the queries now exclude.
        if cached and cached.get("queries") == QUERIES:
            log.warning("pool rebuild failed; using stale pool", exc_info=True)
            return cached["ids"]
        raise
    f.write_text(json.dumps({
        "built_at": datetime.now(timezone.utc).isoformat(),
        "queries": QUERIES,
        "ids": ids,
    }))
    return ids

def get_object(client, object_id: int) -> dict:
    return get_json(client, f"{BASE_URL}/v1/objects/{object_id}")
