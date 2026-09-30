"""Strict JSON parsing helpers shared by release-policy validators."""

from __future__ import annotations

import json
from collections.abc import Iterator

MAX_JSON_CONTAINER_DEPTH = 64


def reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting ambiguous duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def require_bounded_json_depth(value: object) -> None:
    """Reject excessive container nesting without a recursive traversal.

    Root dictionaries and lists each count as one container. The iterator stack
    is bounded by depth rather than the number of children in a wide object.
    """
    stack: list[Iterator[object]] = [iter((value,))]
    while stack:
        try:
            current = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if isinstance(current, dict):
            children = iter(current.values())
        elif isinstance(current, list):
            children = iter(current)
        else:
            continue
        if len(stack) > MAX_JSON_CONTAINER_DEPTH:
            raise ValueError("JSON input exceeds nesting limit")
        stack.append(children)


def load_json_bytes(data: bytes) -> object:
    """Decode strict UTF-8 JSON without reflecting untrusted input in errors.

    Callers must apply their own named byte-acquisition limit before parsing.
    The shared depth contract also protects subsequent schema/error rendering.
    """
    try:
        value: object = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys)
    except RecursionError:
        raise ValueError("JSON input exceeds nesting limit") from None
    except ValueError:
        raise ValueError("invalid JSON input") from None
    require_bounded_json_depth(value)
    return value
