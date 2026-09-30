"""Shared result type for `fieldkit doctor [SERVICE]` checks."""

from dataclasses import dataclass
from typing import Literal

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL, EXIT_SUCCESS


@dataclass(frozen=True)
class DoctorResult:
    """Outcome of a single service health check.

    ``configured=False`` means the service has no credentials/cache yet. An
    absent optional service is informational in aggregate checks; an explicitly
    requested service still reports its failure category.
    """

    service: str
    healthy: bool
    configured: bool
    message: str
    failure_kind: Literal["auth", "data", "retryable"] | None = None

    def __post_init__(self) -> None:
        if self.healthy != (self.failure_kind is None):
            raise ValueError("healthy results must have no failure kind; unhealthy results require one")

    @property
    def exit_code(self) -> int:
        """Map the explicit failure category to the canonical CLI status."""
        if self.healthy:
            return EXIT_SUCCESS
        if self.failure_kind == "retryable":
            return EXIT_PARTIAL
        return EXIT_DATA if self.failure_kind == "data" else EXIT_AUTH

    def render(self) -> str:
        if not self.configured:
            return f"{self.service}: optional — not configured ({self.message})"
        status = "OK" if self.healthy else "UNHEALTHY"
        return f"{self.service}: {status} — {self.message}"
