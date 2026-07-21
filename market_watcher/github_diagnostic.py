"""Isolated GitHub authentication and primary rate-limit diagnostic."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .errors import classify_exception
from .github import classify_github_response
from .http import AioHttpClient, GitHubAuthHttpClient, HttpClient, HttpResponse

GITHUB_RATE_LIMIT_URL = "https://api.github.com/rate_limit"


@dataclass(frozen=True, slots=True)
class GitHubDiagnosticResult:
    auth_mode: str
    http_status: int | None
    classification: str
    error_code: str | None
    limit: int | None = None
    remaining: int | None = None
    reset_at: str | None = None


async def run_github_diagnostic(
    *,
    token: str,
    timeout_seconds: float,
    client_factory: Callable[..., HttpClient] = AioHttpClient,
) -> GitHubDiagnosticResult:
    """Perform one independent request without production gateway state or budget."""
    auth_mode = "已配置 Token" if token.strip() else "匿名"
    client = client_factory(
        timeout_seconds=timeout_seconds,
        default_headers={"User-Agent": "astrbot-plugin-market-watcher"},
    )
    auth = GitHubAuthHttpClient(client, token)
    try:
        response = await auth.get(GITHUB_RATE_LIMIT_URL)
        classification = classify_github_response(response)
        return GitHubDiagnosticResult(
            auth_mode=auth_mode,
            http_status=response.status,
            classification=classification.status,
            error_code=classification.error_code,
            limit=_header_int(response, "X-RateLimit-Limit"),
            remaining=_header_int(response, "X-RateLimit-Remaining"),
            reset_at=_epoch_to_iso(response.header("X-RateLimit-Reset")),
        )
    except Exception as exc:
        error_code, _ = classify_exception(exc)
        return GitHubDiagnosticResult(
            auth_mode=auth_mode,
            http_status=None,
            classification=error_code,
            error_code=error_code,
        )
    finally:
        await auth.close()


def format_github_diagnostic(result: GitHubDiagnosticResult) -> str:
    """Format only bounded diagnostic fields, never response bodies or credentials."""
    return (
        "GitHub API 诊断："
        f"认证模式={result.auth_mode}；"
        f"HTTP={_display(result.http_status)}；"
        f"分类={result.classification}；"
        f"错误类别={result.error_code or 'none'}；"
        f"Limit={_display(result.limit)}；"
        f"Remaining={_display(result.remaining)}；"
        f"Reset={result.reset_at or 'unknown'}"
    )


def _header_int(response: HttpResponse, name: str) -> int | None:
    value = response.header(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _epoch_to_iso(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return (
            datetime.fromtimestamp(int(value), UTC).isoformat().replace("+00:00", "Z")
        )
    except (ValueError, OverflowError, OSError):
        return None


def _display(value: object | None) -> str:
    return "unknown" if value is None else str(value)
