from __future__ import annotations

import unittest
from copy import deepcopy
from unittest import mock

from market_watcher.github import classify_github_response
from market_watcher.github_diagnostic import (
    GITHUB_RATE_LIMIT_URL,
    GitHubDiagnosticResult,
    format_github_diagnostic,
    run_github_diagnostic,
)
from market_watcher.http import HttpResponse
from tests.test_test_push import Event, collect, load_main_module


class Client:
    def __init__(self, outcome: HttpResponse | BaseException):
        self.outcome = outcome
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.closed = False

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        self.calls.append((url, dict(headers or {})))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def close(self):
        self.closed = True


class Factory:
    def __init__(self, outcome: HttpResponse | BaseException):
        self.outcome = outcome
        self.calls: list[dict] = []
        self.client: Client | None = None

    def __call__(self, **kwargs) -> Client:
        self.calls.append(kwargs)
        self.client = Client(self.outcome)
        return self.client

    def created_client(self) -> Client:
        if self.client is None:
            raise AssertionError("client factory was not called")
        return self.client


def response(status=200, *, headers=None, body=b""):
    return HttpResponse(
        status, body=body, headers=headers or {}, url=GITHUB_RATE_LIMIT_URL
    )


class GitHubClassificationTests(unittest.TestCase):
    def test_status_classification_contract(self):
        cases = (
            (response(200), ("ok", None)),
            (response(304), ("ok", None)),
            (response(401), ("auth_failed", "github_auth_failed")),
            (
                response(403, headers={"X-RateLimit-Remaining": "0"}),
                ("rate_limited", "github_rate_limited"),
            ),
            (
                response(403, headers={"Retry-After": "30"}),
                ("rate_limited", "github_rate_limited"),
            ),
            (
                response(403, body=b"secondary rate limit"),
                ("rate_limited", "github_rate_limited"),
            ),
            (
                response(403, body=b"permission denied"),
                ("permission_denied", "github_permission_denied"),
            ),
            (response(429), ("rate_limited", "github_rate_limited")),
            (response(500), ("http_error", "github_http_error")),
        )
        for item, expected in cases:
            with self.subTest(status=item.status, expected=expected):
                result = classify_github_response(item)
                self.assertEqual((result.status, result.error_code), expected)


class GitHubDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_anonymous_200_headers_and_reset(self):
        factory = Factory(
            response(
                headers={
                    "X-RateLimit-Limit": "60",
                    "X-RateLimit-Remaining": "59",
                    "X-RateLimit-Reset": "0",
                }
            )
        )
        result = await run_github_diagnostic(
            token="", timeout_seconds=17, client_factory=factory
        )
        self.assertEqual(result.auth_mode, "匿名")
        self.assertEqual(result.classification, "ok")
        self.assertEqual((result.limit, result.remaining), (60, 59))
        self.assertEqual(result.reset_at, "1970-01-01T00:00:00Z")
        self.assertEqual(factory.calls[0]["timeout_seconds"], 17)
        self.assertEqual(factory.created_client().calls, [(GITHUB_RATE_LIMIT_URL, {})])
        self.assertTrue(factory.created_client().closed)

    async def test_configured_token_is_sent_only_as_auth_header(self):
        token = "github_pat_TEST_SECRET"
        factory = Factory(response())
        result = await run_github_diagnostic(
            token=token, timeout_seconds=15, client_factory=factory
        )
        headers = factory.created_client().calls[0][1]
        self.assertEqual(headers["Authorization"], f"Bearer {token}")
        text = format_github_diagnostic(result)
        self.assertIn("认证模式=已配置 Token", text)
        self.assertNotIn(token, text)
        self.assertNotIn("Authorization", text)

    async def test_invalid_token_401_does_not_expose_body(self):
        secret = "github_pat_INVALID_SECRET"
        factory = Factory(response(401, body=f"bad {secret}".encode()))
        result = await run_github_diagnostic(
            token=secret, timeout_seconds=15, client_factory=factory
        )
        text = format_github_diagnostic(result)
        self.assertEqual(
            (result.classification, result.error_code),
            ("auth_failed", "github_auth_failed"),
        )
        self.assertNotIn(secret, text)
        self.assertNotIn("bad", text)
        self.assertTrue(factory.created_client().closed)

    async def test_missing_or_invalid_headers_display_unknown(self):
        factory = Factory(
            response(headers={"X-RateLimit-Limit": "bad", "X-RateLimit-Reset": "bad"})
        )
        result = await run_github_diagnostic(
            token="", timeout_seconds=15, client_factory=factory
        )
        self.assertIsNone(result.limit)
        self.assertIsNone(result.remaining)
        self.assertIsNone(result.reset_at)
        self.assertIn("Limit=unknown", format_github_diagnostic(result))
        self.assertIn("Remaining=unknown", format_github_diagnostic(result))
        self.assertIn("Reset=unknown", format_github_diagnostic(result))

    async def test_timeout_and_network_errors_are_sanitized_and_closed(self):
        for error, code in (
            (TimeoutError("secret timeout"), "timeout"),
            (OSError("secret network"), "request_failed"),
        ):
            with self.subTest(code=code):
                factory = Factory(error)
                result = await run_github_diagnostic(
                    token="secret-token", timeout_seconds=15, client_factory=factory
                )
                text = format_github_diagnostic(result)
                self.assertEqual(
                    (result.classification, result.error_code), (code, code)
                )
                self.assertNotIn("secret", text)
                self.assertTrue(factory.created_client().closed)

    async def test_command_is_group_only_and_does_not_touch_production_state(self):
        main = load_main_module()
        plugin = object.__new__(main.MarketWatcherPlugin)
        plugin.config = {"github_token": "configured-secret"}
        plugin._runtime_config = None
        production = {"budget": 5, "state": {"status": "unchanged"}, "outbox": []}
        plugin._service = production
        before = deepcopy(production)
        diagnostic = GitHubDiagnosticResult(
            "已配置 Token", 200, "ok", None, 5000, 4999, "2030-01-01T00:00:00Z"
        )
        with mock.patch.object(
            main, "run_github_diagnostic", mock.AsyncMock(return_value=diagnostic)
        ) as runner:
            replies = await collect(plugin.test_github(Event()))
        self.assertEqual(production, before)
        runner.assert_awaited_once_with(token="configured-secret", timeout_seconds=15)
        self.assertNotIn("configured-secret", replies[0])

        with mock.patch.object(main, "run_github_diagnostic") as private_runner:
            private_replies = await collect(plugin.test_github(Event(private=True)))
        private_runner.assert_not_called()
        self.assertEqual(private_replies, ["GitHub API 测试仅可在群聊中使用。"])


if __name__ == "__main__":
    unittest.main()
