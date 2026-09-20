"""JSON serialization helpers for the ``--json`` CLI surface.

``json.dumps(..., default=str)`` renders a ``datetime`` via ``str()``, which
produces ``"2026-07-27 12:34:56+00:00"`` — a space separator, not the ``T`` that
ISO-8601/RFC3339 requires. Python's own ``datetime.fromisoformat`` tolerates the
space, so a Python-only test suite never notices; Go's
``time.Parse(time.RFC3339, ...)``, JavaScript's ``new Date(...)`` and ``jq``'s
``fromdate`` all reject or misparse it.

``json_default`` is the ``default=`` callable every ``--json`` emitter should
pass instead of ``str``: it renders dates and datetimes via ``.isoformat()`` and
falls back to ``str`` for everything else, so it is a drop-in replacement.
"""

import datetime as _dt
from typing import Any


def json_default(obj: Any) -> str:
    """Return the JSON representation for an object ``json.dumps`` cannot encode.

    Args:
        obj: The value ``json.dumps`` failed to serialize natively.

    Returns:
        ``obj.isoformat()`` for :class:`datetime.date` and
        :class:`datetime.datetime` (``datetime`` is a ``date`` subclass, so one
        check covers both), otherwise ``str(obj)``.
    """
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    return str(obj)
