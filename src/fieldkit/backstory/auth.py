"""Backstory authentication through MCPJungle's native OAuth flow."""

import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Protocol

from fieldkit.config import TIMEOUT_INTERACTIVE_AUTH, get_mcp_gateway_url
from fieldkit.errors import AuthError

BACKSTORY_SERVER_NAME = "backstory"
BACKSTORY_SERVER_URL = "https://mcp.people.ai/mcp"
BACKSTORY_DESCRIPTION = "People.ai Backstory sales intelligence"


class CommandRunner(Protocol):
    """Run MCPJungle with inherited streams and a bounded wait."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]: ...


class BackstoryAuthError(AuthError):
    """Raised when MCPJungle cannot complete Backstory authentication."""


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def authenticate(
    *,
    runner: CommandRunner = subprocess.run,
    stdin_is_tty: Callable[[], bool] = _stdin_is_tty,
) -> None:
    """Replace the Backstory registration through MCPJungle native OAuth."""
    if not stdin_is_tty():
        raise BackstoryAuthError(
            "Backstory authorization requires an interactive terminal on stdin. "
            "Rerun 'fieldkit auth backstory' from an interactive terminal."
        )

    gateway_url = get_mcp_gateway_url(strict=True)
    argv = [
        "mcpjungle",
        "--registry",
        gateway_url,
        "register",
        "--name",
        BACKSTORY_SERVER_NAME,
        "--description",
        BACKSTORY_DESCRIPTION,
        "--url",
        BACKSTORY_SERVER_URL,
        "--force",
    ]
    try:
        result = runner(argv, check=False, timeout=TIMEOUT_INTERACTIVE_AUTH)
    except FileNotFoundError as exc:
        raise BackstoryAuthError("MCPJungle is not installed. Install version 0.4.6 or newer, then rerun.") from exc
    except OSError as exc:
        raise BackstoryAuthError("MCPJungle could not be started. Repair the installation, then rerun.") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackstoryAuthError("Backstory browser authorization timed out. Rerun 'fieldkit auth backstory'.") from exc
    except KeyboardInterrupt as exc:
        raise BackstoryAuthError("Backstory authorization was cancelled. Rerun 'fieldkit auth backstory'.") from exc

    if result.returncode != 0:
        raise BackstoryAuthError("MCPJungle did not complete Backstory authorization. Rerun 'fieldkit auth backstory'.")
