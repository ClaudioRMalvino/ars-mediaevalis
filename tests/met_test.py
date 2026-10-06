import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx
from src.ars_mediaevalis.museums import met

met.log.addHandler(logging.NullHandler())  # keeps expected warnings out of the test output


def mock_client(handler) -> httpx.Client:
    """Returns an httpx.Client whose requests are answered by handler instead of the network."""

    return httpx.Client(transport=httpx.MockTransport(handler))


def status_error(code: int) -> httpx.HTTPStatusError:
    """Returns the HTTPStatusError that raise_for_status() would raise for the given code."""

    request: httpx.Request = httpx.Request("GET", met.BASE_URL)
    response: httpx.Response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


def search_handler(ids_by_department: dict[int, list[int]], seen: list[httpx.Request]):
    """
    Returns a handler that imitates the Met /search endpoint, paging through the IDs of the
    requested department and logging every request in seen.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        ids: list[int] = ids_by_department[int(req.url.params["departmentId"])]
        offset: int = int(req.url.params["offset"])
        limit: int = int(req.url.params["limit"])
        return httpx.Response(200, json={"total": len(ids), "objectIDs": ids[offset:offset + limit]})

    return handler


class MetTestCase(unittest.TestCase):
    """Base class that keeps every Met test away from the network, the real cache and the clock."""

    def setUp(self) -> None:
        """Points XDG_CACHE_HOME at a temporary directory and replaces time.sleep with a mock."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pool_file: Path = Path(tmp.name) / "ars_mediaevalis" / "pool.json"

        env_patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp.name})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        sleep_patcher = mock.patch.object(met.time, "sleep")
        self.sleep: mock.MagicMock = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)


class TestMakeClient(unittest.TestCase):
    """Tests that make_client() configures the client as intended."""

    def setUp(self) -> None:
        """Builds a client and closes it once the test is over."""

        self.client: httpx.Client = met.make_client()
        self.addCleanup(self.client.close)

    def test_sends_user_agent(self) -> None:
        """Tests that the client identifies itself with the project User-Agent."""

        self.assertEqual(self.client.headers["User-Agent"], met.USER_AGENT)

    def test_follows_redirects(self) -> None:
        """Tests that the client follows redirects."""

        self.assertTrue(self.client.follow_redirects)

    def test_timeouts(self) -> None:
        """Tests that the client uses a 15 s timeout with a 5 s connect timeout."""

        self.assertEqual(self.client.timeout.connect, 5.0)
        self.assertEqual(self.client.timeout.read, 15.0)
        self.assertEqual(self.client.timeout.write, 15.0)
        self.assertEqual(self.client.timeout.pool, 15.0)


class TestRetryable(unittest.TestCase):
    """Tests that _retryable() tells temporary failures from permanent ones."""

    def test_transport_errors_are_retryable(self) -> None:
        """Tests that connection-level failures are retryable."""

        for exc in (httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(met._retryable(exc))

    def test_rate_limit_and_server_errors_are_retryable(self) -> None:
        """Tests that 429 and 5xx responses are retryable."""

        for code in (429, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.assertTrue(met._retryable(status_error(code)))

    def test_client_errors_are_not_retryable(self) -> None:
        """Tests that 4xx responses other than 429 are not retryable."""

        for code in (400, 403, 404):
            with self.subTest(code=code):
                self.assertFalse(met._retryable(status_error(code)))

    def test_other_exceptions_are_not_retryable(self) -> None:
        """Tests that exceptions unrelated to the connection are not retryable."""

        for exc in (ValueError("bad json"), KeyError("objectIDs"), RuntimeError("boom")):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(met._retryable(exc))


class TestGetJson(MetTestCase):
    """Tests that get_json() fetches, retries and gives up as intended."""

    def test_returns_json_payload(self) -> None:
        """Tests that a 200 response is returned as a decoded dict."""

        client: httpx.Client = mock_client(lambda req: httpx.Response(200, json={"total": 3}))
        self.assertEqual(met.get_json(client, met.BASE_URL), {"total": 3})
        self.sleep.assert_not_called()

    def test_sends_url_and_params(self) -> None:
        """Tests that the URL and the query parameters reach the server."""

        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json={})

        met.get_json(mock_client(handler), f"{met.BASE_URL}/v1.1/search", {"departmentId": 7, "q": "angel"})

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.path, "/public/collection/v1.1/search")
        self.assertEqual(dict(seen[0].url.params), {"departmentId": "7", "q": "angel"})

    def test_retries_transport_error_then_succeeds(self) -> None:
        """Tests that a dropped connection is retried and the later payload returned."""

        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            if len(calls) < 3:
                raise httpx.ConnectError("refused", request=req)
            return httpx.Response(200, json={"ok": True})

        self.assertEqual(met.get_json(mock_client(handler), met.BASE_URL), {"ok": True})
        self.assertEqual(len(calls), 3)

    def test_retries_rate_limit_and_server_errors(self) -> None:
        """Tests that a 429 or 5xx response is retried and the later payload returned."""

        for code in (429, 500, 503):
            with self.subTest(code=code):
                responses: list[httpx.Response] = [
                    httpx.Response(code),
                    httpx.Response(200, json={"ok": True}),
                ]
                client: httpx.Client = mock_client(lambda req: responses.pop(0))
                self.assertEqual(met.get_json(client, met.BASE_URL), {"ok": True})
                self.assertEqual(responses, [])

    def test_backoff_doubles_between_attempts(self) -> None:
        """Tests that the waits between attempts are 1 s, 2 s and 4 s."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=req)

        with self.assertRaises(httpx.ConnectError):
            met.get_json(mock_client(handler), met.BASE_URL)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1.0), mock.call(2.0), mock.call(4.0)])

    def test_gives_up_after_attempts(self) -> None:
        """Tests that the last error is raised once attempts requests have failed."""

        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            raise httpx.ReadTimeout("slow", request=req)

        with self.assertRaises(httpx.ReadTimeout):
            met.get_json(mock_client(handler), met.BASE_URL, attempts=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_client_error_is_not_retried(self) -> None:
        """Tests that a 404 raises HTTPStatusError after a single request."""

        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(404)

        with self.assertRaises(httpx.HTTPStatusError):
            met.get_json(mock_client(handler), met.BASE_URL)
        self.assertEqual(len(calls), 1)
        self.sleep.assert_not_called()

    def test_invalid_json_is_not_retried(self) -> None:
        """Tests that a 200 response with a non-JSON body raises ValueError after a single request."""

        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(200, content=b"<html>maintenance</html>")

        with self.assertRaises(ValueError):
            met.get_json(mock_client(handler), met.BASE_URL)
        self.assertEqual(len(calls), 1)


class TestSearchAll(MetTestCase):
    """Tests that search_all() pages through the search results as intended."""

    def test_single_page(self) -> None:
        """Tests that a result smaller than one page needs a single request."""

        seen: list[httpx.Request] = []
        client: httpx.Client = mock_client(search_handler({7: [3, 1, 2]}, seen))

        self.assertEqual(met.search_all(client, {"departmentId": 7}), [3, 1, 2])
        self.assertEqual(len(seen), 1)

    def test_sends_query_offset_and_limit(self) -> None:
        """Tests that the query is forwarded together with offset and limit."""

        seen: list[httpx.Request] = []
        client: httpx.Client = mock_client(search_handler({7: [1]}, seen))

        met.search_all(client, {"departmentId": 7, "hasImages": "true", "medium": "Paintings"})

        self.assertEqual(seen[0].url.path, "/public/collection/v1.1/search")
        self.assertEqual(dict(seen[0].url.params), {
            "departmentId": "7",
            "hasImages": "true",
            "medium": "Paintings",
            "offset": "0",
            "limit": str(met.PAGE),
        })

    def test_follows_pages_until_total(self) -> None:
        """Tests that every page is requested in order until total is reached."""

        seen: list[httpx.Request] = []
        client: httpx.Client = mock_client(search_handler({7: [1, 2, 3, 4, 5]}, seen))

        with mock.patch.object(met, "PAGE", 2):
            ids: list[int] = met.search_all(client, {"departmentId": 7})

        self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertEqual([req.url.params["offset"] for req in seen], ["0", "2", "4"])

    def test_total_on_page_boundary_makes_no_extra_request(self) -> None:
        """Tests that a total that is an exact multiple of PAGE does not trigger an empty request."""

        seen: list[httpx.Request] = []
        client: httpx.Client = mock_client(search_handler({7: [1, 2, 3, 4]}, seen))

        with mock.patch.object(met, "PAGE", 2):
            ids: list[int] = met.search_all(client, {"departmentId": 7})

        self.assertEqual(ids, [1, 2, 3, 4])
        self.assertEqual(len(seen), 2)

    def test_stops_at_max_reach(self) -> None:
        """Tests that paging never asks beyond MAX_REACH, however large total is."""

        seen: list[httpx.Request] = []
        client: httpx.Client = mock_client(search_handler({7: list(range(100))}, seen))

        with mock.patch.object(met, "PAGE", 2), mock.patch.object(met, "MAX_REACH", 6):
            ids: list[int] = met.search_all(client, {"departmentId": 7})

        self.assertEqual(ids, [0, 1, 2, 3, 4, 5])
        self.assertEqual([req.url.params["offset"] for req in seen], ["0", "2", "4"])

    def test_no_results(self) -> None:
        """Tests that null, empty or missing objectIDs give an empty list."""

        for payload in ({"total": 0, "objectIDs": None}, {"total": 0, "objectIDs": []}, {}):
            with self.subTest(payload=payload):
                client: httpx.Client = mock_client(lambda req: httpx.Response(200, json=payload))
                self.assertEqual(met.search_all(client, {"departmentId": 7}), [])

    def test_empty_page_stops_paging(self) -> None:
        """Tests that an empty page ends the search even when total promises more."""

        pages: list[list[int]] = [[1, 2], []]
        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(200, json={"total": 50, "objectIDs": pages.pop(0)})

        with mock.patch.object(met, "PAGE", 2):
            ids: list[int] = met.search_all(mock_client(handler), {"departmentId": 7})

        self.assertEqual(ids, [1, 2])
        self.assertEqual(len(calls), 2)


class TestBuildPool(MetTestCase):
    """Tests that _build_pool() merges the results of every query as intended."""

    def test_merges_sorts_and_deduplicates(self) -> None:
        """Tests that IDs shared between queries appear once and the pool is sorted."""

        seen: list[httpx.Request] = []
        client: httpx.Client = mock_client(search_handler({7: [30, 10, 20], 17: [20, 5, 40]}, seen))
        queries: list[met.SearchQuery] = [{"departmentId": 7}, {"departmentId": 17}]

        with mock.patch.object(met, "QUERIES", queries):
            self.assertEqual(met._build_pool(client), [5, 10, 20, 30, 40])

    def test_runs_every_query(self) -> None:
        """Tests that each entry in QUERIES is searched exactly once."""

        with mock.patch.object(met, "search_all", return_value=[1]) as search_all:
            met._build_pool(None)
        self.assertEqual(
            [call.args[1] for call in search_all.call_args_list],
            met.QUERIES,
        )

    def test_no_results_gives_empty_pool(self) -> None:
        """Tests that queries without results give an empty pool."""

        with mock.patch.object(met, "search_all", return_value=[]):
            self.assertEqual(met._build_pool(None), [])


class TestLoadPool(MetTestCase):
    """Tests that load_pool() builds, caches and refreshes the pool as intended."""

    def setUp(self) -> None:
        """Serves a two-query pool from a mock transport and logs every request."""

        super().setUp()
        self.requests: list[httpx.Request] = []
        self.client: httpx.Client = mock_client(search_handler({7: [3, 1], 17: [2]}, self.requests))

        patcher = mock.patch.object(met, "QUERIES", [{"departmentId": 7}, {"departmentId": 17}])
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_cache(self, ids: list[int], age_days: float) -> None:
        """Writes a pool.json that was built age_days ago."""

        built: datetime = datetime.now(timezone.utc) - timedelta(days=age_days)
        self.pool_file.parent.mkdir(parents=True, exist_ok=True)
        self.pool_file.write_text(json.dumps({"built_at": built.isoformat(), "queries": met.QUERIES, "ids": ids}))

    def failing_client(self) -> httpx.Client:
        """Returns a client whose every request is answered with a 404."""

        return mock_client(lambda req: httpx.Response(404))

    def test_builds_pool_when_no_cache(self) -> None:
        """Tests that the pool is built from the API when there is no cache file."""

        self.assertFalse(self.pool_file.exists())
        self.assertEqual(met.load_pool(self.client), [1, 2, 3])
        self.assertEqual(len(self.requests), 2)

    def test_writes_cache_file(self) -> None:
        """Tests that a build writes the IDs and a current UTC timestamp to pool.json."""

        before: datetime = datetime.now(timezone.utc)
        met.load_pool(self.client)
        after: datetime = datetime.now(timezone.utc)

        cached: dict = json.loads(self.pool_file.read_text())
        self.assertEqual(cached["ids"], [1, 2, 3])
        built: datetime = datetime.fromisoformat(cached["built_at"])
        self.assertIsNotNone(built.tzinfo)
        self.assertTrue(before <= built <= after)

    def test_second_call_uses_cache(self) -> None:
        """Tests that a second call returns the same pool without any request."""

        first: list[int] = met.load_pool(self.client)
        self.requests.clear()

        second: list[int] = met.load_pool(self.client)

        self.assertEqual(second, first)
        self.assertEqual(self.requests, [])

    def test_fresh_cache_is_used_without_request(self) -> None:
        """Tests that a cache younger than max_age_days is returned as it is."""

        self.write_cache([7, 8, 9], age_days=29)
        self.assertEqual(met.load_pool(self.client), [7, 8, 9])
        self.assertEqual(self.requests, [])

    def test_stale_cache_is_rebuilt(self) -> None:
        """Tests that a cache older than max_age_days is rebuilt and overwritten."""

        self.write_cache([7, 8, 9], age_days=31)
        self.assertEqual(met.load_pool(self.client), [1, 2, 3])
        self.assertEqual(json.loads(self.pool_file.read_text())["ids"], [1, 2, 3])

    def test_max_age_days_is_respected(self) -> None:
        """Tests that max_age_days decides whether a cache counts as stale."""

        self.write_cache([7, 8, 9], age_days=5)
        self.assertEqual(met.load_pool(self.client, max_age_days=10), [7, 8, 9])
        self.assertEqual(met.load_pool(self.client, max_age_days=3), [1, 2, 3])

    def test_cache_records_its_queries(self) -> None:
        """Tests that pool.json stores the queries it was built with."""

        met.load_pool(self.client)
        self.assertEqual(json.loads(self.pool_file.read_text())["queries"], met.QUERIES)

    def test_changed_queries_rebuild_a_fresh_cache(self) -> None:
        """Tests that a recent pool built with other queries is rebuilt rather than reused."""

        self.write_cache([7, 8, 9], age_days=1)
        with mock.patch.object(met, "QUERIES", [{"departmentId": 17}]):
            self.assertEqual(met.load_pool(self.client), [2])
            self.assertEqual(met.load_pool(self.client), [2])
        self.assertEqual(len(self.requests), 1)

    def test_cache_without_queries_is_rebuilt(self) -> None:
        """Tests that a pool.json from before queries were recorded is rebuilt."""

        built: str = datetime.now(timezone.utc).isoformat()
        self.pool_file.parent.mkdir(parents=True, exist_ok=True)
        self.pool_file.write_text(json.dumps({"built_at": built, "ids": [7, 8, 9]}))
        self.assertEqual(met.load_pool(self.client), [1, 2, 3])

    def test_pool_from_other_queries_is_not_a_fallback(self) -> None:
        """Tests that a failed rebuild does not fall back to a pool built with other queries."""

        self.write_cache([7, 8, 9], age_days=1)
        with mock.patch.object(met, "QUERIES", [{"departmentId": 17}]):
            with self.assertRaises(httpx.HTTPStatusError):
                met.load_pool(self.failing_client())

    def test_corrupt_cache_is_rebuilt(self) -> None:
        """Tests that an unreadable pool.json is rebuilt instead of raising."""

        self.pool_file.parent.mkdir(parents=True, exist_ok=True)
        for content in ("{not json", "[1, 2, 3]", '{"ids": [1]}', '{"built_at": "yesterday", "ids": [1]}'):
            with self.subTest(content=content):
                self.pool_file.write_text(content)
                self.assertEqual(met.load_pool(self.client), [1, 2, 3])

    def test_stale_cache_is_used_when_rebuild_fails(self) -> None:
        """Tests that a failed rebuild falls back to the stale pool and leaves the file alone."""

        self.write_cache([7, 8, 9], age_days=31)
        before: str = self.pool_file.read_text()

        self.assertEqual(met.load_pool(self.failing_client()), [7, 8, 9])
        self.assertEqual(self.pool_file.read_text(), before)

    def test_stale_empty_pool_is_used_when_rebuild_fails(self) -> None:
        """Tests that a stale cache holding no IDs still counts as a fallback."""

        self.write_cache([], age_days=31)
        self.assertEqual(met.load_pool(self.failing_client()), [])

    def test_build_failure_without_cache_raises(self) -> None:
        """Tests that a failed build with no cache raises and writes no file."""

        with self.assertRaises(httpx.HTTPStatusError):
            met.load_pool(self.failing_client())
        self.assertFalse(self.pool_file.exists())


class TestGetObject(MetTestCase):
    """Tests that get_object() fetches a single object record as intended."""

    def test_requests_object_endpoint(self) -> None:
        """Tests that /v1/objects/<id> is requested and its payload returned."""

        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json={"objectID": 42, "title": "The Annunciation"})

        obj: dict = met.get_object(mock_client(handler), 42)

        self.assertEqual(obj, {"objectID": 42, "title": "The Annunciation"})
        self.assertEqual([str(req.url) for req in seen], [f"{met.BASE_URL}/v1/objects/42"])

    def test_unknown_object_raises(self) -> None:
        """Tests that a 404 for an unknown ID raises HTTPStatusError."""

        client: httpx.Client = mock_client(lambda req: httpx.Response(404))
        with self.assertRaises(httpx.HTTPStatusError):
            met.get_object(client, 0)


@unittest.skipUnless(os.environ.get("ARS_LIVE_TESTS"), "set ARS_LIVE_TESTS=1 to query the real Met API")
class TestLiveApi(MetTestCase):
    """Tests the real Met API end to end, with the pool cached in a temporary directory."""

    def test_pool_and_object(self) -> None:
        """Tests that a pool can be built, is cached, and that its first object has a title."""

        client: httpx.Client = met.make_client()
        self.addCleanup(client.close)

        pool: list[int] = met.load_pool(client)

        self.assertGreater(len(pool), 0)
        self.assertEqual(pool, sorted(set(pool)))
        self.assertTrue(self.pool_file.exists())
        self.assertIn("title", met.get_object(client, pool[0]))


if __name__ == "__main__":
    unittest.main()
