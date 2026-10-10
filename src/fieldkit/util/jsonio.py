"""JSON serialization helpers for the ``--json`` CLI surface.

``json.dumps(..., default=str)`` renders a ``datetime`` via ``str()``, which
produces ``"2026-07-27 12:34:56+00:00"`` — a space separator, not the ``T`` that
ISO-8601/RFC3339 requires. Python's own ``datetime.fromisoformat`` tolerates the
space, so a Python-only test suite never notices; Go's
``time.Parse(time.RFC3339, ...)``, JavaScript's ``new Date(...)`` and ``jq``'s
``fromdate`` all reject or misparse it.

``json_default`` is the ``default=`` callable every ``--json`` emitter should
pass instead of ``str``: it renders dates, datetimes and times as ISO-8601,
writes a UTC offset as ``Z`` (the only zone ``jq``'s ``fromdate`` accepts), and
falls back to ``str`` for everything else, so it is a drop-in replacement.
"""

import datetime as _dt
from typing import Any

_UTC_OFFSET = "+00:00"


def _isoformat(value: _dt.date | _dt.time) -> str:
    """Render ISO-8601, writing a UTC offset as ``Z`` as ``jq``'s ``fromdate`` requires."""
    rendered = value.isoformat()
    if isinstance(value, _dt.datetime) and value.utcoffset() == _dt.timedelta(0):
        return rendered.removesuffix(_UTC_OFFSET) + "Z"
    return rendered


def json_default(obj: Any) -> str:
    """Return the JSON representation for an object ``json.dumps`` cannot encode.

    Args:
        obj: The value ``json.dumps`` failed to serialize natively.

    Returns:
        ISO-8601 for dates, datetimes and times (a UTC datetime ends in
        ``Z``), otherwise ``str(obj)``.
    """
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return _isoformat(obj)
    return str(obj)
