"""AstrBot notification boundary kept import-safe for offline tests."""

from __future__ import annotations

import asyncio
import importlib
import json as _json
import os
import time
from collections.abc import Callable

from .ai import (
    AI_TIMEOUT_SECONDS,
    AiCallFailed,
    AiProviderMissing,
    AiRoleError,
    AiTimeout,
)
from .card_renderer import build_render_request
from .models import DeliveryBatch

# ---------------------------------------------------------------------------
# Image validation — no new dependencies
# ---------------------------------------------------------------------------

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_RIFF_SIG = b"RIFF"
_WEBP_SIG = b"WEBP"
_GIF87A_SIG = b"GIF87a"
_GIF89A_SIG = b"GIF89a"
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MiB


def _classify_image(data: bytes) -> str | None:
    """Return 'jpeg' / 'png' / 'webp' / 'gif' or None for invalid content."""
    if not data or len(data) < _MIN_HEADER_BYTES or len(data) > _MAX_IMAGE_BYTES:
        return None

    if data.startswith(_JPEG_SOI):
        if len(data) < 50 or data[-2:] != _JPEG_EOI:
            return None
        return "jpeg"

    if data.startswith(_PNG_SIG):
        if len(data) < 60:
            return None
        return "png"

    if data[:4] == _RIFF_SIG and len(data) >= 12 and data[8:12] == _WEBP_SIG:
        if len(data) < 30:
            return None
        return "webp"

    if data.startswith((_GIF87A_SIG, _GIF89A_SIG)):
        if len(data) < 30 or data[-1:] != b"\x3b":
            return None
        return "gif"

    return None


_MIN_HEADER_BYTES = 4  # minimum to check any magic


def _detect_text_signature(data: bytes) -> str:
    """Classify non-image content for safe diagnostic logging.

    Returns a stable identifier: ``internal_server_error`` / ``html`` /
    ``json`` / ``unknown``.
    """
    if not data:
        return "unknown"
    stripped = data.lstrip()
    if data.startswith(b"Internal Server Error"):
        return "internal_server_error"
    if stripped.startswith((b"<html", b"<!DOCTYPE", b"<!doctype")):
        return "html"
    if stripped.startswith((b"{", b"[")):
        try:
            _json.loads(data)
            return "json"
        except (_json.JSONDecodeError, ValueError):
            pass
    return "unknown"


def _read_file_safe(path: str) -> bytes:
    """Read an image file from a local path.

    Raises
    ------
    OSError / PermissionError / ValueError
        On read failure or exceeding ``_MAX_IMAGE_BYTES``.
    """
    with open(path, "rb") as f:
        data = f.read()
    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError("image file exceeds size limit")
    return data


def _safe_logger():
    """Lazy AstrBot logger – falls back to noop when unavailable."""
    try:
        from astrbot.api import logger as _log

        return _log
    except Exception:
        import logging as _logging

        return _logging.getLogger(__name__)


def load_message_chain():
    from astrbot.api.event import MessageChain

    return MessageChain


def load_image_component():
    from astrbot.api.message_components import Image

    return Image


class AstrBotNotifier:
    def __init__(
        self,
        context,
        *,
        html_render: Callable | None = None,
        image_render_timeout: int = 8,
        message_chain_loader: Callable | None = None,
        image_loader: Callable | None = None,
    ) -> None:
        self.context = context
        self.html_render = html_render
        self.image_render_timeout = image_render_timeout
        self.message_chain_loader = message_chain_loader or load_message_chain
        self.image_loader = image_loader or load_image_component
        self._pending_image_bytes: bytes | None = None
        # Non-persistent flag: True when prepare() actually invoked the renderer
        # with image card enabled (even if the result was invalid). Drives
        # text_fallback vs text distinction in send().
        self._prepare_attempted: bool = False
        # Exposes actual delivery mode after send() call: "image" / "text_fallback" / "text"
        self.last_delivery_mode: str | None = None

    def clear_prepared(self) -> None:
        """Clear internally stored image bytes, attempt flag, and delivery mode."""
        self._pending_image_bytes = None
        self._prepare_attempted = False
        self.last_delivery_mode = None

    async def prepare(self, batch: DeliveryBatch) -> bytes | None:
        """Render card once per batch. Stores valid image bytes for send().

        Supports renderer returning:
        - Non-empty ``bytes``: validated for known image format and size.
        - ``str`` that is an existing local file: read via ``asyncio.to_thread``
          and validated identically.
        - ``str`` URL or other value: safe fallback (no fetch).
        """
        _start = time.monotonic()
        self._pending_image_bytes = None
        self._prepare_attempted = False
        _log = _safe_logger()

        # Pre-condition check — outcome=skipped, no attempt
        if self.html_render is None or batch.card_payload is None:
            _log.info(
                "[MarketWatcher] prepare diagnostic"
                " renderer_callable=%s payload_present=%s request_built=%s"
                " outcome=%s duration_ms=%s",
                callable(self.html_render),
                batch.card_payload is not None,
                False,
                "skipped",
                0,
            )
            return None

        # Mark attempt *before* calling renderer so that exception and
        # CancelledError paths also indicate an image delivery was requested.
        # CancelledError still propagates (send() never called).
        self._prepare_attempted = True

        try:
            template, data, options = build_render_request(batch.card_payload)
            result = await asyncio.wait_for(
                self.html_render(template, data, return_url=False, options=options),
                timeout=self.image_render_timeout,
            )
        except asyncio.TimeoutError:
            _duration = max(0, int(round((time.monotonic() - _start) * 1000)))
            _log.info(
                "[MarketWatcher] prepare diagnostic"
                " renderer_callable=%s payload_present=%s request_built=%s"
                " outcome=%s result_type=%s duration_ms=%s",
                callable(self.html_render),
                batch.card_payload is not None,
                True,
                "timeout",
                "none",
                _duration,
            )
            return None
        except asyncio.CancelledError:
            _duration = max(0, int(round((time.monotonic() - _start) * 1000)))
            _log.info(
                "[MarketWatcher] prepare diagnostic"
                " renderer_callable=%s payload_present=%s request_built=%s"
                " outcome=%s result_type=%s duration_ms=%s",
                callable(self.html_render),
                batch.card_payload is not None,
                True,
                "cancelled",
                "none",
                _duration,
            )
            raise
        except Exception as _exc:
            _duration = max(0, int(round((time.monotonic() - _start) * 1000)))
            _log.info(
                "[MarketWatcher] prepare diagnostic"
                " renderer_callable=%s payload_present=%s request_built=%s"
                " outcome=%s error_type=%s duration_ms=%s",
                callable(self.html_render),
                batch.card_payload is not None,
                True,
                "exception",
                type(_exc).__name__,
                _duration,
            )
            return None

        # ---- Outcome classification with path support and validation -----
        _duration = max(0, int(round((time.monotonic() - _start) * 1000)))
        _result_type = type(result).__name__

        if isinstance(result, bytes):
            if len(result) == 0:
                _log.info(
                    "[MarketWatcher] prepare diagnostic"
                    " renderer_callable=%s payload_present=%s request_built=%s"
                    " outcome=%s result_type=%s duration_ms=%s",
                    callable(self.html_render),
                    batch.card_payload is not None,
                    True,
                    "none",
                    _result_type,
                    _duration,
                )
                return None
            img_kind = _classify_image(result)
            if img_kind is not None:
                self._pending_image_bytes = result
                _log.info(
                    "[MarketWatcher] prepare diagnostic"
                    " renderer_callable=%s payload_present=%s request_built=%s"
                    " outcome=%s source=%s image_kind=%s"
                    " size=%s duration_ms=%s",
                    callable(self.html_render),
                    batch.card_payload is not None,
                    True,
                    "image_ready",
                    "bytes",
                    img_kind,
                    len(result),
                    _duration,
                )
                return result
            # Invalid image content — safe signature only
            sig = _detect_text_signature(result)
            _log.info(
                "[MarketWatcher] prepare diagnostic"
                " renderer_callable=%s payload_present=%s request_built=%s"
                " outcome=%s result_type=%s"
                " invalid_signature=%s size=%s duration_ms=%s",
                callable(self.html_render),
                batch.card_payload is not None,
                True,
                "invalid_image",
                _result_type,
                sig,
                len(result),
                _duration,
            )
            return None

        if isinstance(result, str):
            if os.path.isfile(result):
                try:
                    file_bytes = await asyncio.to_thread(_read_file_safe, result)
                except Exception as _exc:
                    # File read error — log only error type, not the path
                    _log.info(
                        "[MarketWatcher] prepare diagnostic"
                        " renderer_callable=%s payload_present=%s"
                        " request_built=%s outcome=%s"
                        " result_type=%s string_kind=%s"
                        " error_type=%s duration_ms=%s",
                        callable(self.html_render),
                        batch.card_payload is not None,
                        True,
                        "invalid_image",
                        _result_type,
                        "existing_file",
                        type(_exc).__name__,
                        _duration,
                    )
                    return None
                img_kind = _classify_image(file_bytes)
                if img_kind is not None:
                    self._pending_image_bytes = file_bytes
                    _log.info(
                        "[MarketWatcher] prepare diagnostic"
                        " renderer_callable=%s payload_present=%s"
                        " request_built=%s outcome=%s"
                        " source=%s image_kind=%s"
                        " size=%s duration_ms=%s",
                        callable(self.html_render),
                        batch.card_payload is not None,
                        True,
                        "image_ready",
                        "file",
                        img_kind,
                        len(file_bytes),
                        _duration,
                    )
                    return file_bytes
                # Invalid file content — safe signature only (no path)
                sig = _detect_text_signature(file_bytes)
                _log.info(
                    "[MarketWatcher] prepare diagnostic"
                    " renderer_callable=%s payload_present=%s"
                    " request_built=%s outcome=%s"
                    " result_type=%s string_kind=%s"
                    " invalid_signature=%s size=%s duration_ms=%s",
                    callable(self.html_render),
                    batch.card_payload is not None,
                    True,
                    "invalid_image",
                    _result_type,
                    "existing_file",
                    sig,
                    len(file_bytes),
                    _duration,
                )
                return None
            # URL or other non-file string — safe fallback, no fetch
            _string_kind: str = (
                "url" if result.startswith(("http://", "https://")) else "other"
            )
            _suffix_raw = os.path.splitext(result)[1]
            _suffix: str = (
                _suffix_raw if (_suffix_raw and _suffix_raw.isascii()) else "none"
            )
            _log.info(
                "[MarketWatcher] prepare diagnostic"
                " renderer_callable=%s payload_present=%s request_built=%s"
                " outcome=%s result_type=%s"
                " string_kind=%s suffix=%s duration_ms=%s",
                callable(self.html_render),
                batch.card_payload is not None,
                True,
                "string",
                _result_type,
                _string_kind,
                _suffix,
                _duration,
            )
            return None

        # None, or unexpected type
        _log.info(
            "[MarketWatcher] prepare diagnostic"
            " renderer_callable=%s payload_present=%s request_built=%s"
            " outcome=%s result_type=%s duration_ms=%s",
            callable(self.html_render),
            batch.card_payload is not None,
            True,
            "none" if result is None else "unexpected",
            _result_type,
            _duration,
        )
        return None

    async def send(self, target: str, message: str) -> tuple[bool, str | None]:
        # Image path: try image, fall back to text on the same attempt
        if self._pending_image_bytes is not None:
            try:
                MessageChain = self.message_chain_loader()
                Image = self.image_loader()
                image_component = Image.fromBytes(self._pending_image_bytes)
                chain = MessageChain(chain=[image_component])
                result = await self.context.send_message(target, chain)
            except asyncio.CancelledError:
                raise
            except Exception:
                result = False
            if result is not False:
                self.last_delivery_mode = "image"
                return True, None
        # Text path (fallback or direct).
        # text_fallback when prepare was actually attempted with image card
        # enabled; text when image card was not engaged at all.
        _is_fallback = self._prepare_attempted
        try:
            MessageChain = self.message_chain_loader()
            result = await self.context.send_message(
                target,
                MessageChain().message(message),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_delivery_mode = "text_fallback" if _is_fallback else "text"
            return False, "astrbot_send_exception"
        if result is False:
            self.last_delivery_mode = "text_fallback" if _is_fallback else "text"
            return False, "astrbot_send_false"
        self.last_delivery_mode = "text_fallback" if _is_fallback else "text"
        return True, None


class AstrBotAiClient:
    def __init__(self, context, timeout_seconds: float = AI_TIMEOUT_SECONDS) -> None:
        self.context = context
        self.timeout_seconds = timeout_seconds

    async def resolve_provider_id(self, origin: str) -> str | None:
        try:
            provider_id = await self.context.get_current_chat_provider_id(origin)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            provider_error = _provider_not_found_error()
            if (provider_error is not None and isinstance(exc, provider_error)) or type(
                exc
            ).__name__ == "ProviderNotFoundError":
                raise AiProviderMissing from exc
            raise AiCallFailed from exc
        return provider_id.strip() if isinstance(provider_id, str) else None

    async def generate(
        self, *, provider_id: str, prompt: str, system_prompt: str
    ) -> str:
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise AiTimeout from exc
        except Exception as exc:
            provider_error = _provider_not_found_error()
            if (provider_error is not None and isinstance(exc, provider_error)) or type(
                exc
            ).__name__ == "ProviderNotFoundError":
                raise AiProviderMissing from exc
            raise AiCallFailed from exc
        if getattr(response, "role", None) == "err":
            raise AiRoleError
        for name in ("completion_text", "content", "text"):
            value = getattr(response, name, None)
            if isinstance(value, str):
                return value
        if isinstance(response, str):
            return response
        return ""


def _provider_not_found_error():
    for module_name in (
        "astrbot.core.provider",
        "astrbot.core.provider.provider",
        "astrbot.api.provider",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        error_type = getattr(module, "ProviderNotFoundError", None)
        if isinstance(error_type, type) and issubclass(error_type, Exception):
            return error_type
    return None


class FakeNotifier:
    def __init__(
        self,
        outcomes: dict[str, list[bool | Exception]] | None = None,
        *,
        prepare_result: bytes | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, str]] = []
        self.prepare_result = prepare_result
        self.prepared_batches: list[DeliveryBatch] = []
        self._pending_image_bytes: bytes | None = None

    def clear_prepared(self) -> None:
        """Clear internally stored image bytes after a batch completes."""
        self._pending_image_bytes = None

    async def prepare(self, batch: DeliveryBatch) -> bytes | None:
        self.prepared_batches.append(batch)
        self._pending_image_bytes = self.prepare_result
        return self.prepare_result

    async def send(self, target: str, message: str) -> tuple[bool, str | None]:
        self.calls.append((target, message))
        queue = self.outcomes.get(target, [])
        if not queue:
            return True, None
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return (True, None) if outcome else (False, "fake_send_false")
