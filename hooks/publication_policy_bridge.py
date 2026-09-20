#!/usr/bin/env python3
"""Process boundary for the OpenCode publication-policy plugin."""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fieldkit.publication_policy import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
