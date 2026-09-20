"""fieldkit doctor [SERVICE] — consolidated integration health checks.

Bare `fieldkit doctor` runs every supported integration check.
`fieldkit doctor sf` (etc.) runs one service check. Exit 0 when enabled services
are healthy, 2 when one needs authentication, and 3 for incomplete configuration.
Absent optional integrations are informational.
"""

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_SUCCESS
from fieldkit.commands.doctor._result import DoctorResult
from fieldkit.commands.doctor.gmail import check_gmail
from fieldkit.commands.doctor.google import check_google
from fieldkit.commands.doctor.sf import check_sf
from fieldkit.commands.doctor.shadowbot import check_shadowbot
from fieldkit.config import IntegrationConfigurationState, get_integration_configuration_state


def _configuration_state(service: str) -> IntegrationConfigurationState:
    if service == "sf":
        return get_integration_configuration_state("sf")
    if service == "google":
        return get_integration_configuration_state("google")
    if service == "shadowbot":
        return get_integration_configuration_state("shadowbot")
    return "disabled"


def _run_all() -> list[DoctorResult]:
    return [check_sf(), check_gmail(), check_google(), check_shadowbot()]


def _effective_configuration_state(
    result: DoctorResult, declared_state: IntegrationConfigurationState
) -> IntegrationConfigurationState:
    """Combine declaration and runtime state while preserving invalid configuration."""
    if declared_state == "invalid":
        return "invalid"
    if result.configured:
        return "enabled"
    return declared_state


def _render_result(result: DoctorResult, state: IntegrationConfigurationState) -> None:
    """Render one service with declaration-aware health semantics."""
    if state == "invalid":
        click.echo(f"{result.service}: CONFIG ERROR — integration configuration is incomplete; {result.message}")
    elif not result.configured and state == "enabled":
        click.echo(f"{result.service}: AUTH REQUIRED — {result.message}")
    else:
        click.echo(result.render())


def _render_all(results: list[DoctorResult], configuration_states: dict[str, IntegrationConfigurationState]) -> None:
    for result in results:
        _render_result(result, configuration_states[result.service])


def _configuration_states(results: list[DoctorResult]) -> dict[str, IntegrationConfigurationState]:
    """Resolve declaration state independently before combining runtime results."""
    declared: dict[str, IntegrationConfigurationState] = {
        result.service: _configuration_state(result.service) for result in results
    }
    return {result.service: _effective_configuration_state(result, declared[result.service]) for result in results}


def _emit_results(
    results: list[DoctorResult], configuration_states: dict[str, IntegrationConfigurationState], as_json: bool
) -> None:
    """Emit either the stable JSON contract or human-readable diagnostics."""
    if not as_json:
        _render_all(results, configuration_states)
        return
    import json as _json

    click.echo(
        _json.dumps(
            [
                {
                    "service": result.service,
                    "healthy": result.healthy,
                    "configured": result.configured,
                    "enabled": configuration_states[result.service] != "disabled",
                    "configuration_state": configuration_states[result.service],
                    "message": result.message,
                }
                for result in results
            ]
        )
    )


def _exit_code(results: list[DoctorResult], configuration_states: dict[str, IntegrationConfigurationState]) -> int:
    """Return the aggregate exit code with invalid configuration taking precedence."""
    if "invalid" in configuration_states.values():
        return EXIT_DATA
    healthy = all(result.healthy or configuration_states[result.service] == "disabled" for result in results)
    return EXIT_SUCCESS if healthy else EXIT_AUTH


@click.group(
    name="doctor",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit results as JSON.")
@click.pass_context
def cli(ctx: click.Context, as_json: bool) -> None:
    """Check the health of every configured service, or a single one.

    Exits 0 when enabled services are healthy, 2 for auth, and 3 for invalid configuration.
    """
    if ctx.invoked_subcommand is not None:
        return

    results = _run_all()
    configuration_states = _configuration_states(results)
    _emit_results(results, configuration_states, as_json)
    raise SystemExit(_exit_code(results, configuration_states))


from fieldkit.commands.doctor.gmail import doctor_gmail_cmd  # noqa: E402
from fieldkit.commands.doctor.google import doctor_google_cmd  # noqa: E402
from fieldkit.commands.doctor.sf import doctor_sf_cmd  # noqa: E402
from fieldkit.commands.doctor.shadowbot import doctor_shadowbot_cmd  # noqa: E402

cli.add_command(doctor_sf_cmd)
cli.add_command(doctor_gmail_cmd)
cli.add_command(doctor_google_cmd)
cli.add_command(doctor_shadowbot_cmd)
