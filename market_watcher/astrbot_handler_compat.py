"""Narrow AstrBot handler rebinding compatibility for plugin re-enable cycles."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Iterable
from functools import partial
from typing import Any, cast


def normalize_plugin_handler_bindings(instance: object) -> int:
    """Collapse safe stale instance bindings to exactly the current instance."""
    try:
        star_handler = importlib.import_module("astrbot.core.star.star_handler")
    except ImportError:
        return 0

    star_handlers_registry = getattr(star_handler, "star_handlers_registry", None)
    if star_handlers_registry is None:
        return 0
    get_handlers = getattr(star_handlers_registry, "get_handlers_by_module_name", None)
    if not callable(get_handlers):
        return 0

    plugin_type = type(instance)
    handlers = cast(Iterable[Any], get_handlers(plugin_type.__module__))
    normalized = 0
    for metadata in handlers:
        handler_name = getattr(metadata, "handler_name", None)
        if not isinstance(handler_name, str):
            continue
        original = plugin_type.__dict__.get(handler_name)
        if not inspect.isfunction(original):
            continue
        root, bound_args, has_keywords = _flatten_partial(
            getattr(metadata, "handler", None)
        )
        if (
            root is not original
            or has_keywords
            or not bound_args
            or not all(isinstance(arg, plugin_type) for arg in bound_args)
        ):
            continue
        if len(bound_args) == 1 and bound_args[0] is instance:
            continue
        metadata.handler = partial(original, instance)
        normalized += 1
    return normalized


def _flatten_partial(handler: Any) -> tuple[Any, tuple[Any, ...], bool]:
    current = handler
    bound_args: tuple[Any, ...] = ()
    has_keywords = False
    while isinstance(current, partial):
        bound_args = (*current.args, *bound_args)
        has_keywords = has_keywords or bool(current.keywords)
        current = current.func
    return current, bound_args, has_keywords
