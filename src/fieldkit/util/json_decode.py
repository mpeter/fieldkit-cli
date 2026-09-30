"""Strict JSON object decoding for retained local intent."""

from collections.abc import Iterator


def require_json_container_depth(value: object, *, maximum_depth: int) -> None:
    """Bound a decoded JSON graph with root containers at depth one.

    Only iterator state is retained for each nesting level; wide objects and
    arrays do not require an additional list of their children. This validates
    an already decoded value, not memory used by the JSON parser itself.
    """
    if type(maximum_depth) is not int or maximum_depth <= 0:
        raise ValueError("JSON container depth limit must be a positive integer")
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
        if len(stack) > maximum_depth:
            raise ValueError("JSON input exceeds nesting limit")
        stack.append(children)


def unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate members instead of silently taking the last value."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object member")
        result[key] = value
    return result
