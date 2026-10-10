"""Shared result type for `fieldkit doctor [SERVICE]` checks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DoctorResult:
    """Outcome of a single service health check.

    ``configured=False`` means the service has no credentials/cache yet — reported
    as "not configured" rather than a failure (D6: partial configuration is a
    fully supported state). ``warnings`` carries advisory findings only and
    never changes ``healthy`` or the exit status.
    """

    service: str
    healthy: bool
    configured: bool
    message: str
    warnings: tuple[str, ...] = ()

    def render(self) -> str:
        if not self.configured:
            headline = f"{self.service}: optional — not configured ({self.message})"
        else:
            status = "OK" if self.healthy else "UNHEALTHY"
            headline = f"{self.service}: {status} — {self.message}"
        return "\n".join([headline, *self.render_warnings()])

    def render_warnings(self) -> list[str]:
        """Return advisory findings as indented lines; they never affect health."""
        return [f"  warning: {warning}" for warning in self.warnings]
