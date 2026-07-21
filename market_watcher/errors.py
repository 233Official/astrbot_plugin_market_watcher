"""Stable, secret-safe error classification for persisted fetch results."""

from __future__ import annotations

import asyncio


def classify_exception(exc: BaseException) -> tuple[str, str]:
    if type(exc).__name__ == "GitHubBudgetExceeded":
        return "github_budget_exhausted", "GitHub request budget exhausted"
    if type(exc).__name__ == "GitHubRateLimited":
        return "github_rate_limited", "GitHub requests rate limited"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout", "request timed out"
    return "request_failed", f"request failed ({type(exc).__name__})"
