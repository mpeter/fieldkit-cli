"""Reviewed Salesforce instruction claims and two missing local behavior owners.

The prose guard is structural. Its fixed owner command must also select the
existing client, error, retry, auth and completeness behavior tests.
"""

from pathlib import Path
from typing import Literal

import httpx
import pytest
import yaml

import fieldkit.config as config
import fieldkit.config._loader as config_loader
from fieldkit.config import ConfigError, get_sf_rest_base_url
from fieldkit.sf.client import API_VERSION, SFDirectClient
from fieldkit.sf.errors import SFAuthError
from tests.documentation_workflow_support import snapshot_workflow

pytestmark = pytest.mark.unit
_GUIDE = Path(__file__).parents[1] / "src/fieldkit/sf/AGENTS.md"
_OPPORTUNITY_ID = "006000000000AAA"
_CLAIMS = (
    """Use `SFDirectClient` for synchronous Salesforce REST access. It owns connection
lifetime, authentication headers, request retries, and response validation.
Use it as a context manager so the httpx connection closes.
The client owns lifetime and operation orchestration. Its private
`_transport.py` module owns request dispatch under the shared retry policy;
`_responses.py` owns response validation and UI API projections. Extend those
homes rather than copying helpers into consumers or adding client re-exports.
`fieldkit.sf.client.API_VERSION` is the canonical REST version for client calls
and session probes; do not maintain a separate probe version.""",
    """Import Salesforce exceptions from `fieldkit.sf.errors`. This lightweight leaf
does not import the HTTP client or optional dependencies, so error handling need
not initialize an integration. The package and client do not re-export errors.""",
    """The supported credential is an explicitly supplied, REST-capable Salesforce
`sid` from an authorized session. `fieldkit auth sf` stores it in the active
configuration root. `fieldkit.config.get_sf_rest_base_url()` owns organization
URL selection and REST normalization: `sf_org_url` in `config.yaml` takes
precedence over `salesforce.org_url` in account configuration. Reuse that
accessor rather than duplicating precedence in consumers.
Session lifetime depends on the deployment's policy;
do not promise a fixed duration or assume a browser session grants REST access.""",
    """The client sends the credential as a Bearer authorization header. Do not log
that header, the cookie, or raw session data. fieldkit does not ship an OAuth
connected app or mint a contributor's Salesforce credentials.""",
    """`SFAuthError` inherits from `fieldkit.errors.AuthError` and reaches the top-level
CLI handler as exit 2. Preserve authentication failures even when fetching
supplemental data: an empty result must not disguise an expired session.
`SFNotFoundError` distinguishes missing records, and `SFDataAccessError`
distinguishes denied data access where a method supports it. Consult the called
method's contract before interpreting an empty collection as complete.""",
    """The client uses SOSL, direct sObject REST requests, and UI API relationship
routes. Reuse the existing methods rather than adding a second transport or
assuming a deployment permits SOQL. Keep opportunity searches scoped to
`IN NAME FIELDS`; searching all fields can match another account mentioned
inside free-text next steps.""",
    """`fetch_record()` targets opportunities; `fetch_sobject()` accepts an object
type. UI API collection results can carry completeness and issue information.
Preserve that information through consumers rather than converting an incomplete
response into a successful empty list.""",
    """The current services filter uses the custom fields `Consulting_Total_USD__c`
and `Training_Total_USD__c`. These are deployment-specific assumptions, not
standard Salesforce fields or a portable definition of services revenue.
Keep the filter in its canonical client constant; do not duplicate it in
commands. Changes require fixtures covering the supported deployment mapping,
including accounts with no matching services opportunities.""",
    """Reuse the client's retry and timeout behavior. Authentication failures must
escape without retrying them as transient network failures. Do not layer a
second retry loop around client calls or narrow the shared transient-status
policy to make a test pass.""",
    """New domain authentication exceptions inherit from `AuthError`; CLI adapters
must not recognize them by class-name strings or implement their own exit-code
mapping.""",
)
_REVIEWED_STREAM = (
    ("heading", "# fieldkit Salesforce contributor guide"),
    ("claim", _CLAIMS[0]),
    ("heading", "## Authentication and errors"),
    ("claim", _CLAIMS[1]),
    ("claim", _CLAIMS[2]),
    ("claim", _CLAIMS[3]),
    ("claim", _CLAIMS[4]),
    ("heading", "## Query and deployment boundaries"),
    ("claim", _CLAIMS[5]),
    ("claim", _CLAIMS[6]),
    ("claim", _CLAIMS[7]),
    ("heading", "## Request policy"),
    ("claim", _CLAIMS[8]),
    ("claim", _CLAIMS[9]),
)


def _reviewed_claims(text: str) -> tuple[str, ...]:
    paragraphs = tuple(" ".join(paragraph.split()) for paragraph in text.split("\n\n") if paragraph.strip())
    expected = tuple(" ".join(paragraph.split()) for _, paragraph in _REVIEWED_STREAM)
    assert paragraphs == expected, "Salesforce instruction claim changed"
    return tuple(
        paragraph for paragraph, (kind, _) in zip(paragraphs, _REVIEWED_STREAM, strict=True) if kind == "claim"
    )


def test_sf_instruction_paragraphs_have_reviewed_claim_scope() -> None:
    result = _reviewed_claims(_GUIDE.read_text(encoding="utf-8"))

    assert len(result) == 10


@pytest.mark.parametrize(
    ("original", "mutation"),
    [
        ("", "\n\n## Extra instructions\nAuthentication failures may be retried.\n"),
        ("", "\n\n## Request policy\nAuthentication failures may be retried.\n"),
        ("## Request policy", "## Extra instructions"),
    ],
    ids=["appended-body", "known-heading-inline-body", "unknown-heading-known-paragraph"],
)
def test_sf_instruction_headings_cannot_hide_unreviewed_content(original: str, mutation: str) -> None:
    text = _GUIDE.read_text(encoding="utf-8").rstrip()
    changed = text.replace(original, mutation) if original else text + mutation

    with pytest.raises(AssertionError, match="Salesforce instruction"):
        _reviewed_claims(changed)


@pytest.mark.parametrize(
    ("original", "mutation"),
    [
        ("does not import the HTTP client", "does import the HTTP client"),
        ("do not re-export errors", "re-export errors"),
        ("Do not log that header", "Do log that header"),
        ("scoped to `IN NAME FIELDS`", "scoped to `IN ALL FIELDS`"),
        ("can carry completeness", "always carry completeness"),
        ("without retrying them", "by retrying them"),
        ("do not promise a fixed duration", "promise a fixed duration"),
        ("The supported credential", "It is false that The supported credential"),
        (
            "The package and client do not re-export errors.",
            "The package and client should re-export errors. The package and client do not re-export errors.",
        ),
    ],
)
def test_sf_instruction_negation_and_scope_mutations_fail_closed(original: str, mutation: str) -> None:
    text = "\n\n".join(" ".join(paragraph.split()) for paragraph in _GUIDE.read_text(encoding="utf-8").split("\n\n"))
    assert original in text

    with pytest.raises(AssertionError, match="Salesforce instruction claim changed"):
        _reviewed_claims(text.replace(original, mutation))


@pytest.mark.parametrize(
    ("primary", "expected"),
    [
        ("https://primary-example.lightning.force.com/", "https://primary-example.my.salesforce.com"),
        (None, "https://acme-example.my.salesforce.com"),
    ],
)
def test_sf_instruction_url_precedence_uses_real_configuration(
    documented_workspace: Path, primary: str | None, expected: str
) -> None:
    settings = yaml.safe_load(config_loader.CONFIG_PATH.read_text(encoding="utf-8"))
    if primary is not None:
        settings["sf_org_url"] = primary
    config_loader.CONFIG_PATH.write_text(yaml.safe_dump(settings), encoding="utf-8")
    config.clear_config_caches()
    before = snapshot_workflow(documented_workspace.parent)

    result = get_sf_rest_base_url()

    assert result == expected
    assert snapshot_workflow(documented_workspace.parent) == before


def test_sf_instruction_invalid_primary_url_does_not_fall_back(documented_workspace: Path) -> None:
    settings = yaml.safe_load(config_loader.CONFIG_PATH.read_text(encoding="utf-8"))
    settings["sf_org_url"] = "https://invalid.example.com"
    config_loader.CONFIG_PATH.write_text(yaml.safe_dump(settings), encoding="utf-8")
    config.clear_config_caches()
    before = snapshot_workflow(documented_workspace.parent)

    with pytest.raises(ConfigError, match="Unrecognised Salesforce org URL format"):
        get_sf_rest_base_url()

    assert snapshot_workflow(documented_workspace.parent) == before


@pytest.mark.parametrize("failure", ["none", "auth", "body"])
def test_sf_instruction_context_closes_real_http_client_on_every_exit(
    monkeypatch: pytest.MonkeyPatch, deny_documentation_network: None, failure: Literal["none", "auth", "body"]
) -> None:
    clients: list[httpx.Client] = []
    requests: list[httpx.Request] = []
    client_type = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/services/data/{API_VERSION}/sobjects/Opportunity/{_OPPORTUNITY_ID}"
        assert request.headers["Authorization"] == "Bearer fictional-session"
        return httpx.Response(401 if failure == "auth" else 200, json={"Id": _OPPORTUNITY_ID})

    def http_client(*, timeout: httpx.Timeout) -> httpx.Client:
        result = client_type(timeout=timeout, transport=httpx.MockTransport(respond), trust_env=False)
        clients.append(result)
        return result

    monkeypatch.setattr(httpx, "Client", http_client)

    def read() -> dict[str, object]:
        with SFDirectClient(session_id="fictional-session", base_url="https://crm.example.com") as client:
            result = client.fetch_record(_OPPORTUNITY_ID)
            if failure == "body":
                raise RuntimeError("fictional consumer failure")
            return dict(result)

    if failure == "none":
        result = read()
        assert result == {"Id": _OPPORTUNITY_ID}
    else:
        with pytest.raises(
            SFAuthError if failure == "auth" else RuntimeError, match=r"authentication|consumer failure"
        ):
            read()
    assert len(requests) == 1
    assert len(clients) == 1
    assert clients[0].is_closed
