"""fieldkit.gmail — Gmail discovery and name resolution."""

from fieldkit.gmail.discover import (
    NOISE_REGEX as NOISE_REGEX,
)
from fieldkit.gmail.discover import (
    GmailCandidate as GmailCandidate,
)
from fieldkit.gmail.discover import (
    clear_gmail_caches as clear_gmail_caches,
)
from fieldkit.gmail.discover import (
    get_gmail_db_path as get_gmail_db_path,
)
from fieldkit.gmail.discover import (
    scan_gemini_candidates as scan_gemini_candidates,
)
from fieldkit.gmail.names import (
    resolve_name as resolve_name,
)

__all__ = [
    "NOISE_REGEX",
    "GmailCandidate",
    "clear_gmail_caches",
    "get_gmail_db_path",
    "resolve_name",
    "scan_gemini_candidates",
]
