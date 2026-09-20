"""dotenv_compat — load_dotenv wrapper that suppresses export-prefix UserWarnings.

fieldkit-data/.env uses shell `export KEY=val` syntax. python-dotenv emits a
UserWarning for each such line. This wrapper silences those warnings so callers
get clean output without changing parse behaviour.
"""

import logging
import warnings
from typing import Any

from dotenv import load_dotenv


def load_dotenv_safe(**kwargs: Any) -> bool:
    """Call load_dotenv() suppressing UserWarnings and logger warnings from python-dotenv.

    fieldkit-data/.env and ~/.env use shell syntax (export KEY=val, if-blocks)
    that python-dotenv cannot parse.  It emits both a Python UserWarning *and*
    a logger.warning() via the "dotenv.main" logger.  This wrapper silences both
    so callers get clean stderr output without changing parse behaviour.

    Accepts and forwards all keyword arguments supported by load_dotenv()
    (dotenv_path, stream, verbose, override, interpolate, encoding, etc.).
    Returns the same bool as load_dotenv() (True if a .env file was found).
    """
    dotenv_logger = logging.getLogger("dotenv.main")
    original_level = dotenv_logger.level
    dotenv_logger.setLevel(logging.CRITICAL)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module=r"dotenv")
            return load_dotenv(**kwargs)
    finally:
        dotenv_logger.setLevel(original_level)
