"""Strict runtime configuration parsing independent from AstrBot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    enabled: bool
    poll_interval_minutes: int
    push_targets: object
    github_token: str
    llm_provider_id: str
    enable_ai_summary: bool
    ai_timeout_seconds: int
    include_star_count: bool
    request_timeout_seconds: int
    max_items_per_push: int


def parse_runtime_config(config: Mapping[str, Any]) -> RuntimeConfig:
    return RuntimeConfig(
        enabled=_bool(config.get("enabled"), False),
        poll_interval_minutes=_int_range(
            config.get("poll_interval_minutes"), 30, 5, 1440
        ),
        push_targets=config.get("push_targets", []),
        github_token=_string(config.get("github_token")),
        llm_provider_id=_string(config.get("llm_provider_id")),
        enable_ai_summary=_bool(config.get("enable_ai_summary"), False),
        ai_timeout_seconds=_int_range(config.get("ai_timeout_seconds"), 60, 10, 120),
        include_star_count=_bool(config.get("include_star_count"), True),
        request_timeout_seconds=_int_range(
            config.get("request_timeout_seconds"), 15, 5, 60
        ),
        max_items_per_push=_int_range(config.get("max_items_per_push"), 10, 1, 50),
    )


def _bool(value: Any, default: bool) -> bool:
    return value if type(value) is bool else default


def _string(value: Any) -> str:
    return value.strip() if type(value) is str else ""


def _int_range(value: Any, default: int, minimum: int, maximum: int) -> int:
    return value if type(value) is int and minimum <= value <= maximum else default
