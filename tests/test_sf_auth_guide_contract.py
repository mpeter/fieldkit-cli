"""Literal, ordered Salesforce auth guide source inventory and offline behavior evidence.

Browser DevTools, live SID, provider policy, and Lightning cookie claims await
separate manual authority; mock-backed CLI cases establish local behavior only.
"""

import os
import re
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from fieldkit.__main__ import main
from fieldkit.commands.sf.session_check import cli as session_check_cli

GUIDE = Path("docs/guides/salesforce-auth.md")
SID = "00D000000000000!fictional-session"
OWN_MODULE = "tests/test_sf_auth_guide_contract.py"

# Literal selectors are independent of the page and include the entire owner module.
SF_AUTH_GUIDE_NODES: tuple[str, ...] = (
    OWN_MODULE,
    "tests/test_auth_sf.py::test_authentication_rolls_back_every_unsuccessful_validation",
    "tests/test_auth_sf.py::test_reauthentication_refuses_unsafe_existing_file",
    "tests/test_auth_sf.py::test_reauthentication_replaces_ambiguous_or_lookalike_sessions",
    "tests/test_auth_sf.py::test_documentation_suspicious_sid_warning",
    "tests/test_auth_sf.py::test_documentation_interactive_auth_success_transcript",
    "tests/test_auth_sf.py::test_invalid_credential_does_not_create_parent",
    "tests/test_auth_sf.py::test_write_sid_cookie_recovers_malformed_json",
    "tests/test_auth_sf.py::test_write_sid_cookie_preserves_other_cookies",
    "tests/test_sf_session_check.py::test_documentation_session_transcripts",
    "tests/test_sf_session_check.py::test_session_diagnostics_are_payload_free",
    "tests/test_sf_session_check.py::test_unsafe_cookie_input_never_sends_credentials",
    "tests/test_sf_session_check.py::test_network_error_retry_exhausted",
    "tests/test_sf_session_check.py::test_bearer_auth_regression_uses_bearer_auth_header",
    "tests/test_sf_session_check.py::test_probe_hits_account_describe_endpoint",
    "tests/test_sf_session_check.py::test_mixed_valid_and_lookalike_sid_selects_only_valid_cookie",
)

# Each entry is one complete nonblank source block. The literal includes Markdown
# syntax, whitespace, link targets, fences, and the final newline.
# Authority names a fixed actual test or an explicitly pending manual claim.
INVENTORY: tuple[tuple[str, str, str], ...] = (
    (
        "metadata",
        "manual: source metadata",
        "---\nlast_reviewed: 2026-09-13\ncovers:\n  - src/fieldkit/sf/client.py\n  - src/fieldkit/commands/sf/\naudience: ae-user\n---",
    ),
    ("page-title", "manual: guide purpose", "# Authenticate with Salesforce"),
    ("cookie-heading", "manual: heading", "## Why cookie auth"),
    (
        "cookie-rationale",
        "manual: organization SSO policy and live browser session",
        "Some Salesforce organizations use SAML SSO and block programmatic username/password\nlogin. fieldkit reads the session cookie that\nyour browser already holds after you log in through the normal SSO flow. You copy\nthat cookie once, paste it into fieldkit, and fieldkit uses it for all Salesforce\nAPI calls until the session expires.",
    ),
    ("prerequisites-heading", "manual: heading", "## Prerequisites"),
    (
        "prerequisites",
        "manual: Chrome, installed command, and configuration locations; test_org_url_refusal_precedes_credential_write",
        "- Chrome is open and you are logged into `yourorg.my.salesforce.com`\n- fieldkit is installed (`fieldkit --version` returns a version string)\n- Configure the REST organization URL before installing the cookie: set\n  `sf_org_url: https://yourorg.my.salesforce.com` in `config.yaml`, or\n  `salesforce.org_url` in the workspace's `accounts.yaml`. See the\n  [configuration reference](../reference/config-file.md#optional-integration-keys)\n  for configuration locations. An absent or invalid organization URL is refused\n  before the cookie is written.",
    ),
    ("initial-heading", "manual: heading", "## Initial setup"),
    (
        "browser-org",
        "manual: live browser organization and logged-in identity",
        "1. In Chrome, navigate to `https://yourorg.my.salesforce.com` and confirm you\n   are logged in (your name appears in the top-right corner).",
    ),
    (
        "devtools-open",
        "manual: Browser DevTools UI",
        "2. Open Chrome DevTools: press **F12** (or right-click anywhere → Inspect).",
    ),
    ("devtools-application", "manual: Browser DevTools UI", "3. Click the **Application** tab in DevTools."),
    (
        "devtools-domain",
        "manual: Browser DevTools domain and cookie view",
        "4. In the left sidebar, expand **Cookies** and click\n   `https://yourorg.my.salesforce.com`.",
    ),
    (
        "sid-row",
        "manual: Browser DevTools cookie selection",
        "5. In the cookie list, find the row where **Name** is `sid`. Click that row.",
    ),
    (
        "sid-value",
        "manual: Browser DevTools actual SID value and format",
        "6. In the detail pane at the bottom, select the entire **Value** string and copy it\n   (Ctrl+C). The value contains a `!` character (format: `00DXXXXXXXXXXXXXXX!AQEA...`).",
    ),
    (
        "interactive-input",
        "test_documentation_interactive_auth_success_transcript; manual: browser paste",
        "7. Run the interactive command and paste the value when prompted. The input is\n   hidden and never placed in process arguments:",
    ),
    (
        "interactive-command",
        "test_documentation_interactive_auth_success_transcript",
        "   ```\n   fieldkit auth sf\n   ```",
    ),
    ("interactive-output-label", "manual: output presentation", "   Expected output (on stderr):"),
    (
        "interactive-output",
        "test_documentation_interactive_auth_success_transcript",
        "   ```\n   [auth-sf] sid stored in fieldkit configuration\n   [auth-sf] Validating session against Salesforce API...\n   [auth-sf] Session valid — Salesforce API session is active.\n   [auth-sf] Done.\n   ```",
    ),
    (
        "sanitized-validation",
        "test_offline_auth_and_session_exit_contract",
        "   The validation messages omit the credential-file path and organization hostname.",
    ),
    ("verify-heading", "manual: heading", "## Verify your session"),
    (
        "cookie-safety-and-rollback",
        "test_unsafe_cookie_input_never_sends_credentials; test_mixed_valid_and_lookalike_sid_selects_only_valid_cookie; test_reauthentication_refuses_unsafe_existing_file; test_reauthentication_replaces_ambiguous_or_lookalike_sessions; test_authentication_rolls_back_every_unsuccessful_validation",
        "The cookie file must contain at most one matching REST session on\n`my.salesforce.com` or a valid subdomain. Lookalike SID domains are skipped;\nif one valid SID remains, the probe uses only that session. Duplicate JSON keys,\nmalformed entries, and ambiguous matching sessions are refused before the probe\nsends credentials. Files must be regular UTF-8 files, not leaf symlinks or\nspecial files, and are limited to 1,000,000 bytes and 1,000 cookie entries.\nRe-run `fieldkit auth sf` to replace ambiguous or lookalike SID entries with one\nsession for the configured organization. Safely parsed non-SID cookies are kept;\nmalformed JSON is replaced with an explicit warning. Unsafe, oversized, or\nnon-UTF-8 files are not overwritten: repair or move the file aside first.\nBoth authentication input methods restore the prior credential bytes if session\nvalidation rejects the candidate, raises an error, or is interrupted. If no\ncredential file existed, the failed candidate file is removed. This rollback\ndoes not promise recovery from process termination or power loss.",
    ),
    ("session-command", "test_documentation_session_transcripts", "```\nfieldkit sf session-check\n```"),
    ("active-output-label", "manual: output presentation", "Expected output when the session is valid:"),
    (
        "active-output",
        "test_offline_auth_and_session_exit_contract",
        "```\nSession: ACTIVE\nSalesforce API session is active.\n```",
    ),
    (
        "session-exit",
        "test_documentation_session_transcripts; test_network_error_retry_exhausted; test_probe_hits_account_describe_endpoint; test_session_diagnostics_are_payload_free",
        "Exit code `0` means the stored cookie successfully accessed the Account metadata\nendpoint. The command currently uses exit `2` and the `EXPIRED` label for every\nunsuccessful probe, including network failures and unexpected HTTP responses.\nRead the accompanying message: a connectivity failure does not establish that\nthe credential expired.",
    ),
    ("lifespan-heading", "manual: heading", "## Session lifespan"),
    (
        "lifespan",
        "manual: organization session lifetime and invalidation; test_documentation_session_transcripts; test_session_diagnostics_are_payload_free",
        "Your Salesforce organization controls session lifetime and invalidation. Check\nthe session before work that depends on Salesforce. If `fieldkit sf session-check`\nexits `2`, read its message. Refresh the cookie for a confirmed authentication\nfailure; for a network error, check connectivity and retry. For an unexpected\nHTTP response, check Salesforce availability and permissions.",
    ),
    ("refresh-heading", "manual: heading", "## Refresh your session"),
    ("refresh-intro", "manual: browser refresh procedure", "Follow the same steps as initial setup:"),
    (
        "refresh-steps",
        "manual: Browser DevTools and live refreshed SID; test_authentication_rolls_back_every_unsuccessful_validation",
        "1. Confirm Chrome is logged into `yourorg.my.salesforce.com`.\n2. Open DevTools → Application → Cookies → `https://yourorg.my.salesforce.com`.\n3. Copy the `sid` cookie value.\n4. Run `fieldkit auth sf` and paste the new value when prompted.",
    ),
    ("automation-heading", "manual: heading", "## Automation and headless hosts"),
    (
        "headless-input",
        "test_sid_input_refusal_leaves_credentials_untouched; manual: operator secret-file creation",
        "For a non-interactive host, put the sid in a regular owner-only file. Do not\npass it on the command line or store it in an environment variable:",
    ),
    (
        "headless-command",
        "test_sid_input_refusal_leaves_credentials_untouched; manual: shell command and operator SID handling",
        "```console\ninstall -m 600 /dev/null ./sf-sid\n# Paste the sid into ./sf-sid, then:\nfieldkit auth sf --sid-file ./sf-sid\n```",
    ),
    (
        "headless-validation",
        "test_sid_input_refusal_leaves_credentials_untouched",
        "The file must contain non-empty UTF-8 text and be at most 8,192 bytes, including\nsurrounding whitespace. fieldkit rejects symlinks, pipes, directories, and\ngroup- or world-accessible secret files before installing credentials.",
    ),
    ("errors-heading", "manual: heading", "## Signs something is wrong"),
    ("expired-label", "manual: output presentation", "**Session expired:**"),
    (
        "expired-output",
        "test_documentation_session_transcripts",
        "```\nSession: EXPIRED\nSF session expired — run 'fieldkit auth sf' — copy the 'sid' cookie from your Salesforce org's browser DevTools\n```",
    ),
    (
        "expired-guidance",
        "manual: operator refresh response",
        "This is the most common error. Follow the refresh steps above.",
    ),
    ("redirect-label", "manual: output presentation", "**Session redirected to login:**"),
    (
        "redirect-output",
        "test_documentation_session_transcripts",
        "```\nSession: EXPIRED\nSF session expired (redirect to login) — run 'fieldkit auth sf' — copy the 'sid' cookie from your Salesforce org's browser DevTools\n```",
    ),
    (
        "redirect-guidance",
        "manual: live browser relogin",
        "Your Chrome session has expired. Log back into `yourorg.my.salesforce.com` in\nChrome, then re-inject the sid.",
    ),
    ("format-label", "manual: output presentation", "**Suspicious sid format:**"),
    (
        "format-output",
        "test_documentation_suspicious_sid_warning",
        "```\n[auth-sf] WARNING: sid does not look like a valid Salesforce session ID (expected format: 00DXXXXXXXXXXXXXXX!AQEA...). Proceeding anyway.\n```",
    ),
    (
        "format-guidance",
        "test_documentation_suspicious_sid_warning; manual: browser cookie choice",
        "You may have copied the wrong cookie or included extra whitespace. Return to\nDevTools, click the `sid` row again, and copy only the Value field. Interactive\ninput is considered unusual unless it contains `!` and is longer than 20\ncharacters. For SID format, the file-input path treats any nonempty value\ncontaining `!` as usual; its file-security checks still apply.\nThese checks warn rather than prove validity; the API session check decides\nwhether authentication succeeds.",
    ),
    ("avoid-heading", "manual: heading", "## What NOT to do"),
    (
        "avoid-list",
        "test_bearer_auth_regression_uses_bearer_auth_header; test_probe_hits_account_describe_endpoint; manual: organization SSO policy, provider behavior, Lightning cookie, and override handling",
        "- **Do not use undocumented login helpers** — they are not part of fieldkit and may\n  violate your organization's SSO configuration.\n- **Do not set `SALESFORCE_*` environment variables** — fieldkit does not read them.\n  API clients accept a non-empty `sf_session_id` configuration value before\n  falling back to the cookie injected by `fieldkit auth sf`. The session-check\n  command checks the cookie file, not that override; remove an obsolete override\n  before relying on cookie authentication. Treat either credential as a secret.\n- **Do not copy the sid from the Lightning URL** — always use the\n  `yourorg.my.salesforce.com` domain in DevTools, not `yourorg.lightning.force.com`.\n  The Lightning sid does not work for REST API calls.\n",
    ),
)
EXPECTED_BLOCKS = tuple(block for _, _, block in INVENTORY)
EXPECTED_IDS = tuple(identifier for identifier, _, _ in INVENTORY)
OWN_SOURCE = Path(__file__).read_text(encoding="utf-8")


def semantic_blocks(source: str) -> tuple[str, ...]:
    return tuple(source.split("\n\n"))


def assert_guide(source: str) -> tuple[str, ...]:
    actual = semantic_blocks(source)
    assert actual == EXPECTED_BLOCKS, "Salesforce auth semantic inventory changed"
    return actual


@pytest.mark.unit
def test_complete_ordered_source_inventory() -> None:
    actual = assert_guide(GUIDE.read_text(encoding="utf-8"))
    assert actual == EXPECTED_BLOCKS
    assert len(actual) == len(INVENTORY) == len(set(EXPECTED_IDS)) == 45
    assert all(block.strip() for block in actual)
    assert "\n\n".join(actual) == GUIDE.read_text(encoding="utf-8")
    assert SF_AUTH_GUIDE_NODES.count(OWN_MODULE) == 1
    assert not any(node.startswith(f"{OWN_MODULE}::") for node in SF_AUTH_GUIDE_NODES)
    assert len(SF_AUTH_GUIDE_NODES) == len(set(SF_AUTH_GUIDE_NODES))
    for _, authorities, _ in INVENTORY:
        for authority in authorities.split("; "):
            if authority.startswith("manual: "):
                continue
            assert any(node.endswith(f"::{authority}") for node in SF_AUTH_GUIDE_NODES) or any(
                node.startswith(f"def {authority}(") for node in OWN_SOURCE.splitlines()
            )


@pytest.mark.parametrize("index", range(len(INVENTORY)), ids=EXPECTED_IDS)
@pytest.mark.parametrize("mutation", ("insert", "remove", "alter", "negate", "duplicate", "reorder"))
@pytest.mark.unit
def test_every_source_block_mutation_is_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED_BLOCKS)
    if mutation == "insert":
        blocks.insert(index, "<aside>Unreviewed authentication promise.</aside>")
    elif mutation == "remove":
        blocks.pop(index)
    elif mutation == "alter":
        blocks[index] += " changed"
    elif mutation == "negate":
        blocks[index] = "Not " + blocks[index]
    elif mutation == "duplicate":
        blocks.insert(index, blocks[index])
    else:
        other = (index + 1) % len(blocks)
        blocks[index], blocks[other] = blocks[other], blocks[index]
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize("index", (14, 16, 20, 22, 31, 35, 38, 41))
@pytest.mark.parametrize("part", ("opening", "body", "closing"))
@pytest.mark.unit
def test_every_fence_mutation_is_rejected(index: int, part: str) -> None:
    blocks = list(EXPECTED_BLOCKS)
    lines = blocks[index].splitlines(keepends=True)
    line = 0 if part == "opening" else -1 if part == "closing" else 1
    lines[line] += "-changed"
    blocks[index] = "".join(lines)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


INLINE_CASES = tuple(
    (index, inline)
    for index, block in enumerate(EXPECTED_BLOCKS)
    for inline in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", block)
)


@pytest.mark.parametrize("index,inline", INLINE_CASES)
@pytest.mark.unit
def test_every_inline_value_mutation_is_rejected(index: int, inline: str) -> None:
    blocks = list(EXPECTED_BLOCKS)
    blocks[index] = blocks[index].replace(f"`{inline}`", "`changed`", 1)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


LINK_CASES = tuple(
    (index, label, target)
    for index, block in enumerate(EXPECTED_BLOCKS)
    for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", block)
)


@pytest.mark.parametrize("index,label,target", LINK_CASES)
@pytest.mark.parametrize("part", ("label", "target"))
@pytest.mark.unit
def test_every_link_mutation_is_rejected(index: int, label: str, target: str, part: str) -> None:
    blocks = list(EXPECTED_BLOCKS)
    replacement = f"[changed]({target})" if part == "label" else f"[{label}](changed.md)"
    blocks[index] = blocks[index].replace(f"[{label}]({target})", replacement, 1)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(
    "index,needle", ((0, "last_reviewed"), (0, "covers:"), (5, "../reference/config-file.md"), (31, "```console"))
)
@pytest.mark.unit
def test_hidden_reference_mutations_are_rejected(index: int, needle: str) -> None:
    blocks = list(EXPECTED_BLOCKS)
    assert needle in blocks[index]
    blocks[index] = blocks[index].replace(needle, f"{needle}-changed", 1)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["group-readable", "symlink", "fifo", "directory", "oversize", "non-utf8", "empty"])
def test_sid_input_refusal_leaves_credentials_untouched(tmp_path: Path, kind: str) -> None:
    sid_file = tmp_path / "sid"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text(SID, encoding="utf-8")
        target.chmod(0o600)
        sid_file.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(sid_file)
    elif kind == "directory":
        sid_file.mkdir()
    else:
        sid_file.write_bytes(
            b"\xff"
            if kind == "non-utf8"
            else b" "
            if kind == "empty"
            else b"x" * 8193
            if kind == "oversize"
            else SID.encode()
        )
        sid_file.chmod(0o640 if kind == "group-readable" else 0o600)
    cookie_file = tmp_path / "cookies.json"
    original = b'{"cookies": []}\n'
    cookie_file.write_bytes(original)
    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.commands.auth.sf._session_check") as check,
    ):
        status = main(["auth", "sf", "--sid-file", str(sid_file)])
    assert status == 3
    assert cookie_file.read_bytes() == original
    check.assert_not_called()


@pytest.mark.integration
@pytest.mark.parametrize(
    "org_url",
    ["", "https://evil.example.com", "https://org.my.salesforce.com@evil.example.com", "http://org.my.salesforce.com"],
)
def test_org_url_refusal_precedes_credential_write(tmp_path: Path, org_url: str) -> None:
    sid_file = tmp_path / "sid"
    sid_file.write_text(SID, encoding="utf-8")
    sid_file.chmod(0o600)
    cookie_file = tmp_path / "absent" / "cookies.json"
    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value=org_url),
        patch("fieldkit.commands.auth.sf._session_check") as check,
    ):
        status = main(["auth", "sf", "--sid-file", str(sid_file)])
    assert status == 3
    assert not cookie_file.parent.exists()
    check.assert_not_called()


@pytest.mark.integration
def test_offline_auth_and_session_exit_contract(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sid_file = tmp_path / "sid"
    sid_file.write_text(SID, encoding="utf-8")
    sid_file.chmod(0o600)
    cookie_file = tmp_path / "cookies.json"
    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.commands.sf.session_check.httpx.get", return_value=httpx.Response(200)) as request,
    ):
        assert main(["auth", "sf", "--sid-file", str(sid_file)]) == 0
        assert main(["sf", "session-check"]) == 0
    output = capsys.readouterr()
    assert "Session: ACTIVE" in output.out
    assert SID not in output.out + output.err
    assert str(cookie_file) not in output.out + output.err
    assert cookie_file.stat().st_mode & 0o777 == 0o600
    request.assert_called()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.ConnectError("private-network-sentinel"),
            "Network error checking SF session; check connectivity and retry.",
        ),
        (httpx.Response(500), "Unexpected response HTTP 500; check Salesforce availability and permissions."),
    ],
)
def test_session_failure_guidance_distinguishes_connectivity_and_unexpected_response(
    tmp_path: Path, response: httpx.Response | httpx.ConnectError, expected: str
) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(
        '{"cookies": [{"name": "sid", "value": "private-session-sentinel", "domain": "org.my.salesforce.com"}]}',
        encoding="utf-8",
    )
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.sf.session_check.time.sleep"),
        patch(
            "fieldkit.commands.sf.session_check.httpx.get",
            side_effect=response if isinstance(response, httpx.ConnectError) else None,
            return_value=response if isinstance(response, httpx.Response) else None,
        ),
    ):
        result = CliRunner().invoke(session_check_cli, [])
    assert result.exit_code == 2
    assert expected in result.output
    assert "private-" not in result.output
    guide = GUIDE.read_text(encoding="utf-8")
    assert "Refresh the cookie for a confirmed authentication\nfailure" in guide
    assert "for a network error, check connectivity and retry" in guide
    assert "For an unexpected\nHTTP response, check Salesforce availability and permissions" in guide
