"""Injectable HTTP boundary and an aiohttp implementation."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp


@dataclass(slots=True)
class HttpResponse:
    """Small response object independent from aiohttp."""

    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text())

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        return next(
            (value for key, value in self.headers.items() if key.lower() == lowered),
            None,
        )


class HttpClient(Protocol):
    """Only the GET operation required by M1 source adapters."""

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse: ...

    async def close(self) -> None: ...


class ResponseTooLargeError(RuntimeError):
    """Raised before buffering a response beyond the configured limit."""


class GitHubAuthHttpClient:
    """Inject GitHub credentials only for the official HTTPS API host."""

    def __init__(self, inner: HttpClient, token: str) -> None:
        self.inner = inner
        self.token = token.strip()
        self.enabled = bool(self.token)

    @property
    def has_token(self) -> bool:
        return bool(self.token) and self.enabled

    def disable(self) -> None:
        self.enabled = False

    def reset(self) -> None:
        self.enabled = bool(self.token)

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        request_headers = dict(headers or {})
        parsed = urlsplit(url)
        if (
            self.has_token
            and parsed.scheme == "https"
            and parsed.hostname == "api.github.com"
            and parsed.username is None
            and parsed.password is None
        ):
            request_headers["Authorization"] = f"Bearer {self.token}"
        else:
            request_headers.pop("Authorization", None)
        return await self.inner.get(url, headers=request_headers)

    async def close(self) -> None:
        await self.inner.close()


class AioHttpClient:
    """Bounded HTTP client.

    Sessions created internally are closed by ``close``. Injected sessions remain
    owned by their caller and are never closed here.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        max_response_bytes: int = 5 * 1024 * 1024,
        session: aiohttp.ClientSession | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._session = session
        self._owns_session = session is None
        self._default_headers = dict(default_headers or {})

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        request_headers = {**self._default_headers, **(headers or {})}
        async with self._session.get(
            url,
            headers=request_headers,
            timeout=self._timeout,
        ) as response:
            declared = response.content_length
            if declared is not None and declared > self._max_response_bytes:
                raise ResponseTooLargeError(
                    f"response exceeds {self._max_response_bytes} bytes"
                )
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > self._max_response_bytes:
                    raise ResponseTooLargeError(
                        f"response exceeds {self._max_response_bytes} bytes"
                    )
            return HttpResponse(
                status=response.status,
                body=bytes(body),
                headers=dict(response.headers),
                url=str(response.url),
            )

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None


class RetryingHttpClient:
    """M2-local bounded retry wrapper without global budgets or cache policy."""

    def __init__(
        self,
        inner: HttpClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 2,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.inner = inner
        self.sleep = sleep
        self.max_retries = max_retries
        self.jitter = jitter or (lambda delay: random.uniform(0, min(1.0, delay * 0.1)))

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.inner.get(url, headers=headers)
            except (aiohttp.ClientError, OSError, TimeoutError, asyncio.TimeoutError):
                if attempt >= self.max_retries:
                    raise
                delay = 2**attempt
                await self.sleep(delay + max(0, self.jitter(delay)))
                continue
            retryable = response.status in {408, 429} or 500 <= response.status <= 599
            if not retryable or attempt >= self.max_retries:
                return response
            retry_after = response.header("Retry-After")
            delay = _retry_delay(retry_after, 2**attempt)
            await self.sleep(max(0, delay) + max(0, self.jitter(delay)))
        raise RuntimeError("retry loop exhausted")

    async def close(self) -> None:
        await self.inner.close()


def _retry_delay(value: str | None, fallback: float) -> float:
    maximum = 60.0
    if value is None:
        return min(maximum, max(0, fallback))
    try:
        delay = float(value)
        return min(maximum, delay) if delay >= 0 else min(maximum, fallback)
    except ValueError:
        from datetime import UTC, datetime
        from email.utils import parsedate_to_datetime

        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return min(maximum, max(0, fallback))
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return min(
            maximum,
            max(0, (retry_at - datetime.now(UTC)).total_seconds()),
        )
