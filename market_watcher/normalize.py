"""Pure normalization and bounded external-data helpers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlsplit

from .models import RAW_EXCERPT_MAX_BYTES, SourceKind

_GITHUB_PART = re.compile(r"^[A-Za-z0-9_-](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?$")
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.I | re.S)
_GITHUB_URL = re.compile(
    r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
    re.I,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sanitize_text(value: Any, max_length: int = 4096) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = "".join(
        char
        for char in text
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:max_length] or None


def normalize_github_repo(value: Any) -> tuple[str, str] | None:
    """Normalize exact GitHub repository web, API, and common SSH URLs."""
    text = sanitize_text(value, 2048)
    if not text:
        return None
    if text.lower().startswith("git@github.com:"):
        path = text.split(":", 1)[1]
    else:
        candidate = (
            f"https://{text}" if text.lower().startswith("github.com/") else text
        )
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if scheme in {"http", "https"} and host in {"github.com", "www.github.com"}:
            if parsed.username is not None or parsed.password is not None:
                return None
            path = parsed.path
        elif scheme == "https" and host == "api.github.com":
            if parsed.username is not None or parsed.password is not None:
                return None
            api_parts = [part for part in parsed.path.split("/") if part]
            if len(api_parts) != 3 or api_parts[0].lower() != "repos":
                return None
            path = "/".join(api_parts[1:])
        elif (
            scheme == "ssh"
            and host == "github.com"
            and parsed.username in {None, "git"}
        ):
            path = parsed.path
        else:
            return None
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if (
        owner in {".", ".."}
        or repo in {".", ".."}
        or not _GITHUB_PART.fullmatch(owner)
        or not _GITHUB_PART.fullmatch(repo)
    ):
        return None
    owner, repo = owner.lower(), repo.lower()
    return f"github:{owner}/{repo}", f"https://github.com/{owner}/{repo}"


def repo_parts(normalized_repo_url: str | None) -> tuple[str | None, str | None]:
    normalized = normalize_github_repo(normalized_repo_url)
    if not normalized:
        return None, None
    owner, name = normalized[0].removeprefix("github:").split("/", 1)
    return owner, name


def fallback_canonical_id(source_kind: SourceKind, source_record_id: Any) -> str:
    """Use a full stable digest so lossy character replacement cannot collide."""
    value = sanitize_text(source_record_id, 4096)
    if not value:
        raise ValueError("source_record_id must not be empty")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"source:{source_kind.value}:sha256:{digest}"


def extract_json_code_block(body: Any) -> dict[str, Any] | None:
    text = sanitize_text(body, 65536)
    if not text:
        return None
    for match in _JSON_BLOCK.finditer(text):
        try:
            value = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        if type(value) is dict:
            return value
    return None


def extract_github_url(text: Any) -> str | None:
    value = sanitize_text(text, 65536)
    if not value:
        return None
    for match in _GITHUB_URL.finditer(value):
        normalized = normalize_github_repo(match.group(0))
        if normalized:
            return normalized[1]
    return None


def bounded_excerpt(fields: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic JSON object no larger than the FSD 8 KiB limit."""
    result: dict[str, Any] = {}
    for key in sorted(fields):
        value = fields[key]
        candidate = {**result, key: value}
        try:
            encoded = json.dumps(
                candidate,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            continue
        if len(encoded) <= RAW_EXCERPT_MAX_BYTES:
            result[key] = value
    return result


def stable_content_hash(fields: dict[str, Any]) -> str:
    encoded = json.dumps(
        fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def observation_hash(**fields: Any) -> str:
    """Hash normalized source facts for snapshot-level comparison."""
    return stable_content_hash(fields)
