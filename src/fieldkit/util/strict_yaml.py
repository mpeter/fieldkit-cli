"""Safe YAML loading with fail-closed mapping-key semantics.

Callers remain responsible for bounding input before parsing. This module
prevents ambiguous mappings and keeps parser diagnostics free of input data; it
also bounds traversal of constructed alias graphs. Post-construction checks do
not provide time or memory isolation for the YAML parser or merge expansion.
"""

from collections.abc import Iterator
from itertools import chain
from typing import Literal

import yaml

MAX_YAML_GRAPH_DEPTH = 64
MAX_YAML_GRAPH_VISITS = 10_000

StrictYAMLReason = Literal["invalid_yaml", "duplicate_key", "non_scalar_key"]

_MESSAGES: dict[StrictYAMLReason, str] = {
    "invalid_yaml": "YAML document is invalid",
    "duplicate_key": "YAML document contains duplicate mapping keys",
    "non_scalar_key": "YAML mapping keys must be scalar values",
}


class StrictYAMLError(ValueError):
    """A YAML document is malformed or has an ambiguous mapping."""

    def __init__(self, reason: StrictYAMLReason) -> None:
        self.reason = reason
        super().__init__(_MESSAGES[reason])


class _StrictSafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader variant that rejects ambiguous mapping keys."""


def _construct_strict_mapping(
    loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            raise StrictYAMLError("non_scalar_key")
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError:
            raise StrictYAMLError("non_scalar_key") from None
        if duplicate:
            raise StrictYAMLError("duplicate_key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_strict_mapping)


def _validate_loaded_graph(value: object) -> None:
    """Bound expanded key/value visits without copying or memoizing aliases."""
    stack: list[tuple[int | None, Iterator[object]]] = [(None, iter((value,)))]
    active: set[int] = set()
    visits = 0
    while stack:
        identifier, children = stack[-1]
        try:
            child = next(children)
        except StopIteration:
            stack.pop()
            if identifier is not None:
                active.remove(identifier)
            continue
        visits += 1
        if visits > MAX_YAML_GRAPH_VISITS:
            raise StrictYAMLError("invalid_yaml") from None
        if isinstance(child, dict):
            descendants: Iterator[object] = chain.from_iterable(child.items())
        elif isinstance(child, (list, tuple, set)):
            descendants = iter(child)
        else:
            continue
        identifier = id(child)
        if identifier in active or len(stack) > MAX_YAML_GRAPH_DEPTH:
            raise StrictYAMLError("invalid_yaml") from None
        active.add(identifier)
        stack.append((identifier, descendants))


def load_strict_yaml(text: str) -> object:
    """Load safe YAML while rejecting duplicate or non-scalar mapping keys.

    Bounded acyclic aliases remain supported, preserving their shared objects.
    Constructed graphs permit at most 64 nested collections and 10,000 expanded
    visits, counting keys, values, scalars, and repeated alias occurrences.
    Merge keys are flattened before duplicate detection, so an unambiguous
    merge is accepted while conflicting merges or an explicit override are
    rejected. These graph checks run after YAML construction.
    """
    try:
        loaded = yaml.load(text, Loader=_StrictSafeLoader)
        _validate_loaded_graph(loaded)
        return loaded
    except StrictYAMLError:
        raise
    except (RecursionError, TypeError, ValueError, yaml.YAMLError):
        raise StrictYAMLError("invalid_yaml") from None
