from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import aiohttp

from market_watcher.http import (
    AioHttpClient,
    GitHubAuthHttpClient,
    HttpResponse,
    ResponseTooLargeError,
    RetryingHttpClient,
)


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, size: int):
        del size
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        content_length: int | None = None,
    ) -> None:
        self.status = 200
        self.headers = {"ETag": '"x"'}
        self.url = "https://example.invalid/data"
        self.content_length = content_length
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSession:
    def __init__(self, response: FakeResponse | None = None, *, timeout=False) -> None:
        self.response = response
        self.raise_timeout = timeout
        self.closed = False
        self.calls = []

    def get(self, url, *, headers, timeout):
        self.calls.append((url, headers, timeout))
        if self.raise_timeout:
            raise asyncio.TimeoutError
        return self.response

    async def close(self) -> None:
        self.closed = True


class HttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_github_token_is_scoped_to_exact_official_api_host(self) -> None:
        class Inner:
            def __init__(self):
                self.calls = []

            async def get(self, url, *, headers=None):
                self.calls.append((url, dict(headers or {})))
                return HttpResponse(200)

            async def close(self):
                return None

        inner = Inner()
        client = GitHubAuthHttpClient(inner, "top-secret")
        urls = [
            "https://api.github.com/repos/a/b",
            "https://raw.githubusercontent.com/a/b/main/x",
            "https://market.example.invalid/api",
            "https://api.github.com.evil.invalid/x",
            "http://api.github.com/x",
        ]
        for url in urls:
            await client.get(url, headers={"X-Test": "1", "Authorization": "leak"})
        self.assertEqual(inner.calls[0][1]["Authorization"], "Bearer top-secret")
        for _, headers in inner.calls[1:]:
            self.assertNotIn("Authorization", headers)
            self.assertEqual(headers["X-Test"], "1")

    async def test_each_request_receives_explicit_total_timeout(self) -> None:
        session = FakeSession(FakeResponse([b"ok"], content_length=2))
        client = AioHttpClient(timeout_seconds=7, session=session)
        result = await client.get("https://example.invalid/data", headers={"X": "1"})
        self.assertEqual(result.body, b"ok")
        timeout = session.calls[0][2]
        self.assertIsInstance(timeout, aiohttp.ClientTimeout)
        self.assertEqual(timeout.total, 7)

    async def test_content_length_and_chunked_limits(self) -> None:
        declared = AioHttpClient(
            max_response_bytes=3,
            session=FakeSession(FakeResponse([], content_length=4)),
        )
        chunked = AioHttpClient(
            max_response_bytes=3,
            session=FakeSession(FakeResponse([b"ab", b"cd"])),
        )
        with self.assertRaises(ResponseTooLargeError):
            await declared.get("https://example.invalid/declared")
        with self.assertRaises(ResponseTooLargeError):
            await chunked.get("https://example.invalid/chunked")

    async def test_timeout_propagates(self) -> None:
        client = AioHttpClient(session=FakeSession(timeout=True))
        with self.assertRaises(asyncio.TimeoutError):
            await client.get("https://example.invalid/timeout")

    async def test_external_session_is_not_closed(self) -> None:
        session = FakeSession(FakeResponse([b"ok"]))
        client = AioHttpClient(session=session)
        await client.close()
        self.assertFalse(session.closed)

    async def test_owned_session_is_closed(self) -> None:
        session = FakeSession(FakeResponse([b"ok"]))
        with mock.patch(
            "market_watcher.http.aiohttp.ClientSession", return_value=session
        ):
            client = AioHttpClient()
            await client.get("https://example.invalid/data")
            await client.close()
        self.assertTrue(session.closed)

    async def test_retry_wrapper_handles_transient_status_and_retry_after(self) -> None:
        class Inner:
            def __init__(self):
                self.responses = [
                    HttpResponse(429, headers={"Retry-After": "3"}),
                    HttpResponse(503),
                    HttpResponse(200, body=b"ok"),
                ]

            async def get(self, url, *, headers=None):
                del url, headers
                return self.responses.pop(0)

            async def close(self):
                return None

        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)

        result = await RetryingHttpClient(
            Inner(), sleep=sleep, jitter=lambda delay: 0
        ).get("https://x")
        self.assertEqual(result.status, 200)
        self.assertEqual(sleeps, [3.0, 2])

    async def test_retry_wrapper_retries_network_error_twice(self) -> None:
        class Inner:
            def __init__(self):
                self.calls = 0

            async def get(self, url, *, headers=None):
                del url, headers
                self.calls += 1
                raise aiohttp.ClientConnectionError("offline")

            async def close(self):
                return None

        inner = Inner()

        async def sleep(delay):
            del delay

        with self.assertRaises(aiohttp.ClientConnectionError):
            await RetryingHttpClient(inner, sleep=sleep, jitter=lambda delay: 0).get(
                "https://x"
            )
        self.assertEqual(inner.calls, 3)

    async def test_retry_status_contract_and_retry_after_cap(self) -> None:
        class Inner:
            def __init__(self, status, retry_after=None):
                self.calls = 0
                self.status = status
                self.retry_after = retry_after

            async def get(self, url, *, headers=None):
                del url, headers
                self.calls += 1
                if self.calls == 1:
                    headers = (
                        {"Retry-After": self.retry_after}
                        if self.retry_after is not None
                        else {}
                    )
                    return HttpResponse(self.status, headers=headers)
                return HttpResponse(200)

            async def close(self):
                return None

        async def exercise(status, retry_after=None):
            sleeps = []

            async def sleep(delay):
                sleeps.append(delay)

            inner = Inner(status, retry_after)
            result = await RetryingHttpClient(
                inner, sleep=sleep, jitter=lambda delay: 0
            ).get("https://x")
            return result, inner.calls, sleeps

        for status in (408, 429, 500, 501, 599):
            with self.subTest(status=status):
                result, calls, _ = await exercise(status)
                self.assertEqual((result.status, calls), (200, 2))
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                result, calls, sleeps = await exercise(status)
                self.assertEqual((result.status, calls, sleeps), (status, 1, []))
        _, _, sleeps = await exercise(429, "999999")
        self.assertEqual(sleeps, [60.0])
        _, _, sleeps = await exercise(429, "invalid")
        self.assertEqual(sleeps, [1])


if __name__ == "__main__":
    unittest.main()
