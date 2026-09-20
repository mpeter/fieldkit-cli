"""fieldkit.util — Shared low-level utilities.

Modules:
    atomic — Atomic file-write primitives (locked JSON update, atomic YAML write).
    jsonio — JSON serialization helpers for the ``--json`` CLI surface.

Note:
    The ``atomic`` module uses ``fcntl.flock`` and is POSIX-only (Linux/macOS).
    It is not compatible with Windows.
"""
