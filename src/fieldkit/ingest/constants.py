"""Named constants for the transcript-ingest pipeline protocol strings.

These constants replace bare string literals used as identifiers across
gmail/discover.py and the ingest runner.  Importing from this module
ensures that any rename is caught by the type checker.
"""

from typing import Final, Literal

SourceStatus = Literal["pending", "in_progress", "processed", "failed"]

GEMINI_TRANSCRIPT_PIPELINE: Final = "transcript-ingest"
AMBIENT_TRANSCRIPT_PIPELINE: Final = "ambient-transcript-ingest"
SOURCE_STATUS_PENDING: Final[SourceStatus] = "pending"
SOURCE_STATUS_IN_PROGRESS: Final[SourceStatus] = "in_progress"
SOURCE_STATUS_PROCESSED: Final[SourceStatus] = "processed"
SOURCE_STATUS_FAILED: Final[SourceStatus] = "failed"
