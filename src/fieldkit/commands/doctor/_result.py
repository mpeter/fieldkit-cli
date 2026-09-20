"""Shared result type for `fieldkit doctor [SERVICE]` checks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DoctorResult:
    """Outcome of a single service health check.

    ``configured=False`` means the service has no credentials/cache yet — reported
    as "not configured" rather than a failure (D6: partial configuration is a
    fully supported state).
    """

    service: str
    healthy: bool
    configured: bool
    message: str

    def render(self) -> str:
        if not self.configured:
            return f"{self.service}: optional — not configured ({self.message})"
        status = "OK" if self.healthy else "UNHEALTHY"
        return f"{self.service}: {status} — {self.message}"
