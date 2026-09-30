"""Public pipeline and Salesforce-sync guidance matches shipped behavior."""

import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

import click
import httpx
import pytest
import yaml
from click.testing import CliRunner

import fieldkit.config as config
import fieldkit.config._loader as config_loader
from fieldkit.commands.pursuit.forecast import STAGE_WEIGHT
from fieldkit.commands.pursuit.forecast import cli as forecast_cli
from fieldkit.commands.pursuit.pipeline_health import cli as health_cli
from fieldkit.commands.pursuit.projects_health import cli as projects_cli
from fieldkit.commands.sf.account import cli as sf_account_cli
from fieldkit.commands.sf.listview import cli as sf_listview_cli
from fieldkit.commands.sf.opportunity import cli as sf_opportunity_cli
from fieldkit.commands.sf.session_check import cli as sf_session_check_cli
from fieldkit.pursuit.projects import COMPLETED_STAGES
from fieldkit.util.atomic import prepare_runtime_lock_path
from tests.documentation_workflow_support import invoke_workflow, snapshot_workflow

pytestmark = pytest.mark.unit

_SKILLS = Path(__file__).parents[1] / "src/fieldkit/skills"
_PIPELINE = _SKILLS / "pipeline"
_SF_SYNC = _SKILLS / "sf-sync"
_OPPORTUNITY_ID = "006000000000AAA"
_ACCOUNT_ID = "001000000000AAA"


@dataclass
class SalesforceScenario:
    workspace: Path
    requests: list[httpx.Request] = dataclass_field(default_factory=list)
    failure_path: str | None = None
    failure_status: int = 400

    @property
    def pursuit(self) -> Path:
        return self.workspace / "accounts/acme-corp/pursuits/expansion.md"

    def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "documentation reads must not mutate Salesforce"
        assert request.url.host == "acme-example.my.salesforce.com"
        self.requests.append(request)
        if self.failure_path is not None and self.failure_path in request.url.path:
            return httpx.Response(self.failure_status, json=[{"errorCode": "FICTIONAL_FAILURE"}])
        opportunity = {
            "Id": _OPPORTUNITY_ID,
            "Name": "Acme Expansion",
            "StageName": "Propose",
            "CloseDate": "2027-12-31",
            "Consulting_Total_USD__c": 25000,
            "Training_Total_USD__c": 5000,
            "Account": {"Id": _ACCOUNT_ID, "Name": "Acme Corp"},
            "Owner": {"Name": "Alex Example"},
            "Next_Steps__c": "Review the fictional proposal",
        }
        if request.url.path.endswith("/search/"):
            assert "Acme" in request.url.params["q"]
            return httpx.Response(200, json={"searchRecords": [opportunity]})
        if request.url.path.endswith(f"/sobjects/Opportunity/{_OPPORTUNITY_ID}"):
            return httpx.Response(200, json=opportunity)
        if request.url.path.endswith(f"/sobjects/Account/{_ACCOUNT_ID}"):
            return httpx.Response(
                200,
                json={"Id": _ACCOUNT_ID, "Name": "Acme Corp", "Industry": "Technology"},
            )
        if request.url.path.endswith("/Deal_Splits1__r") or request.url.path.endswith("/SBQQ__Quotes2__r"):
            return httpx.Response(200, json={"records": [], "count": 0})
        pytest.fail(f"unowned Salesforce request path: {request.url.path}")


@pytest.fixture
def sf_scenario(documented_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> SalesforceScenario:
    scenario = SalesforceScenario(documented_workspace)
    configuration = yaml.safe_load(config_loader.CONFIG_PATH.read_text(encoding="utf-8"))
    configuration["sf_session_id"] = "fictional-session-not-a-credential"
    config_loader.CONFIG_PATH.write_text(yaml.safe_dump(configuration), encoding="utf-8")
    accounts_path = documented_workspace / "config/accounts.yaml"
    accounts = yaml.safe_load(accounts_path.read_text(encoding="utf-8"))
    accounts["accounts"]["acme-corp"]["keywords"] = ["Acme"]
    accounts_path.write_text(yaml.safe_dump(accounts), encoding="utf-8")
    scenario.pursuit.write_text(
        f"---\nstage: discover\ngate-status: pending\nsf_opportunity_id: {_OPPORTUNITY_ID}\n"
        "sf_stage: Discover\n---\n\n# Acme Expansion\n\nFictional notes must survive.\n",
        encoding="utf-8",
    )
    (documented_workspace / "accounts/acme-corp/account.md").write_text(
        f"---\nsf_account_id: {_ACCOUNT_ID}\n---\n\n# Acme Corp\n\nFictional account notes.\n",
        encoding="utf-8",
    )
    config.clear_config_caches()
    client_type = httpx.Client

    def client(*, timeout: httpx.Timeout) -> httpx.Client:
        return client_type(timeout=timeout, transport=httpx.MockTransport(scenario.respond), trust_env=False)

    monkeypatch.setattr(httpx, "Client", client)
    return scenario


@pytest.mark.parametrize("command", ["opportunity", "account"])
def test_documented_sf_json_reads_preserve_every_local_file(sf_scenario: SalesforceScenario, command: str) -> None:
    target = _OPPORTUNITY_ID if command == "opportunity" else "acme-corp"
    before = snapshot_workflow(sf_scenario.workspace.parent)

    result = invoke_workflow(["sf", command, target, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    identity_key = "opportunity_id" if command == "opportunity" else "account_id"
    assert payload[identity_key] == (_OPPORTUNITY_ID if command == "opportunity" else _ACCOUNT_ID)
    assert sf_scenario.requests
    assert snapshot_workflow(sf_scenario.workspace.parent) == before


@pytest.mark.parametrize("command", ["opportunity", "account"])
def test_documented_sf_no_write_validates_without_persisting(sf_scenario: SalesforceScenario, command: str) -> None:
    target = _OPPORTUNITY_ID if command == "opportunity" else "acme-corp"
    before = snapshot_workflow(sf_scenario.workspace.parent)
    argv = ["sf", command, target]
    if command == "opportunity":
        argv.append(str(sf_scenario.pursuit))
    argv.append("--no-write")

    result = invoke_workflow(argv)

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert sf_scenario.requests
    assert snapshot_workflow(sf_scenario.workspace.parent) == before


def test_documented_listview_local_scan_failure_is_partial_without_writes(sf_scenario: SalesforceScenario) -> None:
    sf_scenario.pursuit.write_text("---\nstage: [malformed\n---\n", encoding="utf-8")
    before = snapshot_workflow(sf_scenario.workspace.parent)

    result = invoke_workflow(["sf", "listview", "acme-corp", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial"
    assert payload["updated"] == 0
    assert payload["errors"] == 1
    assert sf_scenario.requests
    assert snapshot_workflow(sf_scenario.workspace.parent) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_documented_listview_json_format_does_not_choose_write_authority(
    sf_scenario: SalesforceScenario, dry_run: bool
) -> None:
    before = snapshot_workflow(sf_scenario.workspace.parent)
    argv = ["sf", "listview", "acme-corp", "--json"]
    if dry_run:
        argv.append("--dry-run")

    result = invoke_workflow(argv)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["updated"] == (0 if dry_run else 1)
    assert sf_scenario.requests
    after = snapshot_workflow(sf_scenario.workspace.parent)
    if dry_run:
        assert payload["would_update"] == 1
        assert payload["dry_run"] is True
        assert set(payload) == {"status", "updated", "would_update", "untracked", "errors", "dry_run"}
        assert after == before
    else:
        assert "sf_stage: Propose" in sf_scenario.pursuit.read_text(encoding="utf-8")
        assert "Fictional notes must survive." in sf_scenario.pursuit.read_text(encoding="utf-8")
        cache = sf_scenario.workspace.parent / f"runtime/salesforce/{_OPPORTUNITY_ID}.json"
        assert json.loads(cache.read_text(encoding="utf-8"))["opportunity_id"] == _OPPORTUNITY_ID
        lock = prepare_runtime_lock_path(sf_scenario.pursuit, sf_scenario.workspace.parent / "runtime", "pursuit")
        assert lock.read_bytes() == b""
        assert lock.stat().st_mode & 0o777 == 0o600
        assert snapshot_workflow(sf_scenario.workspace.parent) == after
        changed_files = {
            path
            for path, state in after.items()
            if state[2] is not None and (path not in before or state[2] != before[path][2])
        }
        assert changed_files == {
            "workspace/accounts/acme-corp/pursuits/expansion.md",
            f"runtime/salesforce/{_OPPORTUNITY_ID}.json",
            str(lock.relative_to(sf_scenario.workspace.parent)),
        }
        changed_entries = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
        assert changed_entries == changed_files | {
            "workspace/accounts/acme-corp/pursuits",
            "runtime",
            "runtime/locks",
            "runtime/locks/pursuit",
            "runtime/salesforce",
        }


@pytest.mark.parametrize("command", ["opportunity", "account", "listview"])
@pytest.mark.parametrize(("http_status", "exit_code"), [(400, 1), (401, 2)])
def test_documented_sf_provider_failures_remain_nonpassing_and_never_write(
    sf_scenario: SalesforceScenario, command: str, http_status: int, exit_code: int
) -> None:
    sf_scenario.failure_path = "/Deal_Splits1__r" if command == "opportunity" else "/search/"
    sf_scenario.failure_status = http_status
    target = _OPPORTUNITY_ID if command == "opportunity" else "acme-corp"
    before = snapshot_workflow(sf_scenario.workspace.parent)

    result = invoke_workflow(["sf", command, target, "--json"])

    assert result.exit_code == exit_code, result.output
    if http_status == 400:
        assert json.loads(result.stdout)["status"] == "partial"
    else:
        assert "auth" in result.output.lower()
    assert any(sf_scenario.failure_path in request.url.path for request in sf_scenario.requests)
    assert snapshot_workflow(sf_scenario.workspace.parent) == before


def _read_tree(root: Path) -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".json", ".md"}
    }


def _one_line(content: str) -> str:
    return " ".join(content.split())


def test_pipeline_guidance_is_read_only_and_does_not_claim_freshness() -> None:
    root = (_PIPELINE / "SKILL.md").read_text(encoding="utf-8")
    health = (_PIPELINE / "ops/pipeline-health.md").read_text(encoding="utf-8")
    health_line = _one_line(health)

    for command in (
        "fieldkit pursuit health --json",
        "fieldkit pursuit forecast --json",
        "fieldkit pursuit projects --json",
    ):
        assert command in root
    assert "do not establish that local frontmatter is current" in root
    assert "All three report commands are read-only" in root
    assert "This report does not detect Salesforce staleness" in health
    assert "MEDIUM alone is not a strict failure" in health_line
    assert "qualification scores" in health


@pytest.mark.parametrize("page", ["ops/engagement-health.md", "ops/forecast.md"])
def test_pipeline_glob_account_scope_discloses_pattern_expansion(page: str) -> None:
    text = (_PIPELINE / page).read_text(encoding="utf-8")

    assert "literal" in text
    assert "wildcard" in text


def test_pipeline_root_distinguishes_glob_and_validated_account_scopes() -> None:
    text = (_PIPELINE / "SKILL.md").read_text(encoding="utf-8")

    assert "forecast` and `projects`" in text
    assert "filesystem pattern" in text
    assert "health`" in text
    assert "validated literal" in text


def test_pipeline_health_account_scope_rejects_wildcards() -> None:
    text = (_PIPELINE / "ops/pipeline-health.md").read_text(encoding="utf-8")

    assert "validated literal" in text
    assert "wildcard" in text
    assert "rejected" in text


def test_sf_sync_bulk_preview_is_not_a_per_record_payload() -> None:
    text = (_SF_SYNC / "SKILL.md").read_text(encoding="utf-8")

    assert "JSON summary reports counts only" in text
    assert "stderr progress" in text
    assert "per-record mapped fields" in text
    assert "same preview payload" not in text


def test_forecast_guidance_matches_amount_weights_and_scenarios() -> None:
    content = (_PIPELINE / "ops/forecast.md").read_text(encoding="utf-8")

    for stage, weight in (
        ("closed-won", "100%"),
        ("negotiate", "75%"),
        ("propose", "50%"),
        ("validate", "25%"),
        ("discover", "10%"),
        ("qualify", "5%"),
    ):
        assert STAGE_WEIGHT[stage] == int(weight.removesuffix("%")) / 100
        assert f"{stage} {weight}" in content
    for field in ("sf_consulting_acv", "sf_acv", "sf_arr", "fixed_price"):
        assert field in content
    assert "Best Case is the face-value sum of active pursuits and excludes closed-won" in content
    assert "It does not report a gap from Best Case" in content


def test_project_guidance_matches_supported_completed_stages() -> None:
    content = (_PIPELINE / "ops/engagement-health.md").read_text(encoding="utf-8")
    content_line = _one_line(content)

    for stage in COMPLETED_STAGES:
        assert stage in content
    for tier in ("ZOMBIE", "EXPIRING", "SOON", "ACTIVE", "UNKNOWN"):
        assert tier in content
    assert "EXPIRING and SOON alone are not strict failures" in content_line
    assert "does not query Salesforce" in content


@pytest.mark.parametrize(
    ("command", "options"),
    [
        (health_cli, ("--account", "--include-prospect", "--json", "--strict")),
        (forecast_cli, ("--account", "--quota", "--json")),
        (projects_cli, ("--account", "--json", "--strict")),
        (sf_session_check_cli, ("--json",)),
        (sf_opportunity_cli, ("--no-write", "--dry-run", "--json")),
        (sf_account_cli, ("--no-write", "--dry-run", "--json")),
        (sf_listview_cli, ("--all", "--dry-run", "--json", "--services-only", "--limit")),
    ],
)
def test_documented_cli_options_exist(command: click.Command, options: tuple[str, ...]) -> None:
    result = CliRunner().invoke(command, ["--help"])

    assert result.exit_code == 0
    for option in options:
        assert option in result.output


def test_sf_sync_separates_previews_from_write_by_default_commands() -> None:
    content = (_SF_SYNC / "SKILL.md").read_text(encoding="utf-8")
    content_line = _one_line(content)

    for preview in (
        "fieldkit sf opportunity OPPORTUNITY_ID --json",
        "fieldkit sf opportunity OPPORTUNITY_ID PURSUIT_FILE --no-write",
        "fieldkit sf account ACCOUNT --json",
        "fieldkit sf account ACCOUNT --no-write",
        "fieldkit sf listview ACCOUNT --dry-run --json",
    ):
        assert preview in content
    assert "--json alone changes only the summary format and still writes" in content_line
    assert "--dry-run flag is what suppresses those workspace writes" in content_line
    assert "it does not refresh that account's pursuit files" in content_line
    assert "Exit 1 means one or more search, scan, or write errors were summarized" in content_line
    assert "Never accept a session ID in chat" in content


def test_pipeline_and_sf_sync_docs_have_no_unowned_blocks_or_eval_placeholders() -> None:
    contents = {**_read_tree(_PIPELINE), **_read_tree(_SF_SYNC)}

    for path, content in contents.items():
        if path.suffix == ".md":
            assert "```" not in content, path
            assert "|---" not in content, path

    for root in (_PIPELINE, _SF_SYNC):
        evaluations = json.loads((root / "evals/evals.json").read_text(encoding="utf-8"))
        serialized = json.dumps(evaluations)
        assert "{{" not in serialized
        assert evaluations["static_checks"]
        assert evaluations["evals"]
        assert all(case["assertions"] for case in evaluations["evals"])
