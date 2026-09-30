"""Regression checks for structured configuration examples in public docs."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

import fieldkit.config._accounts as accounts_config
import fieldkit.config._loader as config_loader
import fieldkit.config._settings as config_settings
from fieldkit.commands.doctor._result import DoctorResult
from fieldkit.commands.skill._runner import _skills_dir
from fieldkit.config import (
    ConfigError,
    get_driver_max_concurrent,
    get_fieldkit_data,
    get_google_token_path,
    get_harness_scratch_root,
    get_mcp_endpoint,
    get_user_email_from_env,
    llm_disabled,
    resolve_oauth_credentials,
)
from fieldkit.config._accounts import _load_accounts_yaml
from fieldkit.config._integrations import get_mcp_gateway_url
from fieldkit.config._loader import clear_config_caches
from fieldkit.config._quota import get_pipeline_quota
from fieldkit.config._schema import _FieldkitConfig
from fieldkit.driver.spend import cap_safe_max_concurrent, evaluate_daily_developer_spend_cap, evaluate_daily_spend_cap
from fieldkit.errors import LLMError
from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.llm.core import _resolve_model, _resolve_vertex_location, synthesize
from fieldkit.llm.log import get_db_path
from scripts.check_documentation_contract import fenced_blocks
from scripts.markdown_tables import parse_markdown_tables

pytestmark = pytest.mark.unit

_CONFIG_REFERENCE = Path("docs/reference/config-file.md")
_ENVIRONMENT_REFERENCE = Path("docs/reference/environment-vars.md")


def _assert_reference_prose(document: str, claims: tuple[str, ...]) -> None:
    normalized = {" ".join(paragraph.split()) for paragraph in document.split("\n\n")}
    for claim in claims:
        assert claim in normalized, f"Unapproved reference semantics: {claim}"


_DOCTOR_CLAIMS = (
    "The general doctor checks Salesforce, the Gmail cache, Google OAuth, and ShadowBot. "
    "Invalid or incomplete data detected by those checks exits `3`; an authentication problem exits `2`. "
    "Changing unrelated configuration will not fix an authentication failure.",
    "A passing result does not validate every LLM, MCP, or driver setting. Follow the affected workflow's "
    "documented diagnostics or non-writing preview before enabling its writes; do not treat doctor as a universal configuration validator.",
)
_LOG_ROOT_CLAIMS = (
    "The LLM log override must resolve beneath `~/.config/fieldkit`, `~/.local/share/fieldkit`, the workspace, "
    "the active runtime-data root, or the configured runtime-data root, when those configured roots are available. "
    "Changing `XDG_CONFIG_HOME` does not by itself approve that directory as an LLM log root. "
    "These checks resolve paths before comparing them with the allowed roots.",
)


@pytest.mark.parametrize(
    ("reference", "claims", "before", "after"),
    [
        (_CONFIG_REFERENCE, _DOCTOR_CLAIMS, "does not validate every", "does validate every"),
        (_CONFIG_REFERENCE, _DOCTOR_CLAIMS, "detected by those checks", "in all configuration"),
        (_CONFIG_REFERENCE, _DOCTOR_CLAIMS, "checks Salesforce", "does not check Salesforce"),
        (_ENVIRONMENT_REFERENCE, _LOG_ROOT_CLAIMS, "does not by itself approve", "does by itself approve"),
        (_ENVIRONMENT_REFERENCE, _LOG_ROOT_CLAIMS, "must resolve beneath", "need not resolve beneath"),
        (_ENVIRONMENT_REFERENCE, _LOG_ROOT_CLAIMS, "resolve paths before", "do not resolve paths before"),
    ],
)
def test_reference_prose_rejects_false_scope_claims(
    reference: Path, claims: tuple[str, ...], before: str, after: str
) -> None:
    document = "\n\n".join(" ".join(part.split()) for part in reference.read_text(encoding="utf-8").split("\n\n"))
    assert before in document
    with pytest.raises(AssertionError):
        _assert_reference_prose(document.replace(before, after), claims)


def test_reference_prose_preserves_doctor_and_log_scope() -> None:
    _assert_reference_prose(_CONFIG_REFERENCE.read_text(encoding="utf-8"), _DOCTOR_CLAIMS)
    _assert_reference_prose(_ENVIRONMENT_REFERENCE.read_text(encoding="utf-8"), _LOG_ROOT_CLAIMS)


@pytest.mark.parametrize(
    "reference,claims", [(_CONFIG_REFERENCE, _DOCTOR_CLAIMS), (_ENVIRONMENT_REFERENCE, _LOG_ROOT_CLAIMS)]
)
def test_reference_prose_rejects_prefixed_negation(reference: Path, claims: tuple[str, ...]) -> None:
    document = "\n\n".join(" ".join(part.split()) for part in reference.read_text(encoding="utf-8").split("\n\n"))
    with pytest.raises(AssertionError):
        _assert_reference_prose(document.replace(claims[0], "It is false that " + claims[0]), claims)


def test_configuration_reference_doctor_scope_matches_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    doctor = importlib.import_module("fieldkit.commands.doctor.cli")
    expected = [DoctorResult(service, True, True, "ready") for service in ("sf", "gmail", "google", "shadowbot")]
    for result in expected:
        monkeypatch.setattr(doctor, f"check_{result.service}", Mock(return_value=result))
    result = doctor._run_all()
    assert result == expected
    assert DoctorResult("sf", False, True, "invalid data", "data").exit_code == 3
    assert DoctorResult("sf", False, True, "authentication required", "auth").exit_code == 2


def test_environment_table_uses_canonical_markdown_cells(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = tmp_path / "environment.md"
    document.write_text(
        "## Path overrides\n\n| Variable | Purpose |\n|---|---|\n| `FIELDKIT_DATA_DIR` | literal \\| pipe |\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "_ENVIRONMENT_REFERENCE", document)

    result = _environment_table("Path overrides")

    assert result == [["FIELDKIT_DATA_DIR", "literal | pipe"]]


def test_documented_configuration_uses_only_canonical_workspace_key() -> None:
    document = _CONFIG_REFERENCE.read_text(encoding="utf-8")

    assert "`fieldkit_home` is the required workspace-root key" in document
    assert "data_repo" not in document
    assert "init migrate" not in document


def _environment_table(section: str) -> list[list[str]]:
    """Read published variable rows while removing Markdown-only code delimiters."""
    content = _ENVIRONMENT_REFERENCE.read_text(encoding="utf-8")
    body = content.split(f"## {section}\n", 1)[1].split("\n## ", 1)[0]
    tables = parse_markdown_tables(body)
    assert len(tables) == 1, f"Expected one environment table in {section}"
    return [[cell.replace("`", "") for cell in row] for row in tables[0].rows]


def _configuration_table(section: str) -> list[list[str]]:
    """Read one published configuration table through the canonical parser."""
    content = _CONFIG_REFERENCE.read_text(encoding="utf-8")
    body = content.split(f"## {section}\n", 1)[1].split("\n## ", 1)[0]
    tables = parse_markdown_tables(body)
    assert len(tables) == 1, f"Expected one configuration table in {section}"
    return [[cell.replace("`", "") for cell in row] for row in tables[0].rows]


def test_configuration_reference_core_table_matches_canonical_consumers() -> None:
    """Keep workspace, runtime, and global identity settings in their real file."""
    assert _configuration_table("Core keys") == [
        ["fieldkit_home", "Yes for a configured workspace", "None", "Absolute path to user-owned workspace files"],
        [
            "fieldkit_data",
            "No",
            "<fieldkit_home>/data",
            "Absolute path to runtime databases, tokens, logs, and generated state",
        ],
        ["name", "No", "None", "Display name used by identity-aware local workflows"],
        ["email", "No", "None", "User email used to exclude self-authored records"],
        [
            "email_domain",
            "No",
            "None",
            "Organization domain used only when a command must derive the user email",
        ],
        ["role", "No", "None", "User-provided role retained by interactive setup"],
        ["company", "No", "None", "User-provided organization retained by interactive setup"],
    ]


def test_configuration_reference_optional_table_uses_runtime_locations() -> None:
    """Do not publish workspace identity keys as global integration settings."""
    rows = _configuration_table("Optional integration keys")
    keys = [row[0] for row in rows]
    assert "territory" not in keys
    assert "salesforce_user_id" not in keys
    assert "gcp_project" not in keys
    assert rows[1:3] == [
        [
            "gmail_token",
            "Google commands, when overriding the default",
            "Google OAuth token path; relative paths resolve from the current working directory",
        ],
        [
            "gmail_db",
            "Gmail commands, when overriding the default",
            "Local Gmail SQLite cache path; relative paths resolve from the current working directory",
        ],
    ]
    assert rows[3] == [
        "llm_model",
        "AI-assisted workflows",
        "Supported LiteLLM model identifier",
    ]


def test_configuration_reference_three_root_table_is_complete() -> None:
    """Keep application, workspace, and runtime-data ownership distinct."""
    assert _configuration_table("Three-root boundary") == [
        ["Bundled package assets", "importlib.resources", "Application; available without a checkout"],
        [
            "Explicit or discovered source checkout",
            "get_fieldkit_root()",
            "Application source; unavailable in a checkout-free install without an override",
        ],
        ["Workspace", "get_fieldkit_home()", "User-authored and optionally versioned data"],
        ["Runtime data", "get_fieldkit_data()", "Application-managed caches, credentials, logs, and state"],
    ]


def test_environment_reference_path_table_claims() -> None:
    """Keep every published path override paired with its exact resolution contract."""
    assert _environment_table("Path overrides") == [
        [
            "XDG_CONFIG_HOME",
            "Absolute base directory for fieldkit configuration and Salesforce cookie files; useful for isolated trials and CI",
        ],
        ["XDG_CACHE_HOME", "Absolute cache base; the harness scratch root defaults to its fieldkit/ child"],
        ["FIELDKIT_DATA_DIR", "Absolute runtime-data root override"],
        ["FIELDKIT_HARNESS_ROOT", "Absolute scratch root for disposable harness worktrees; overrides XDG_CACHE_HOME"],
        ["FIELDKIT_LLM_LOG", "Absolute LLM-call database path within an allowed fieldkit root"],
        [
            "FIELDKIT_SKILLS_DIR",
            "Skill directory override; relative paths resolve against the current working directory",
        ],
        ["FIELDKIT_MCP_GATEWAY_URL", "MCP gateway base URL fallback when mcp_gateway_url is not configured"],
    ]


def test_environment_reference_ai_table_claims() -> None:
    """Bind model, offline, and region descriptions to the documented variable names."""
    rows = _environment_table("AI and transcription")
    assert rows[:3] == [
        ["FIELDKIT_LLM_MODEL", "LLM_MODEL", "Supported LiteLLM model override"],
        [
            "FIELDKIT_ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "Vertex AI model name fallback after the general model environment variables",
        ],
        ["FIELDKIT_NO_LLM", "None", "Disable provider calls and select documented deterministic no-AI behavior"],
    ]
    assert rows[4] == [
        "FIELDKIT_VERTEX_LOCATION",
        "CLOUD_ML_REGION, VERTEX_LOCATION, GOOGLE_CLOUD_REGION",
        "Vertex AI region fallback after configured vertex_location",
    ]


@pytest.mark.parametrize("selected", range(4))
def test_environment_reference_model_precedence(monkeypatch: pytest.MonkeyPatch, selected: int) -> None:
    """Explicit models win; empty higher-priority environment aliases are skipped."""
    rows = _environment_table("AI and transcription")
    names = [cell for row in rows[:2] for cell in row[:2]]
    assert names == ["FIELDKIT_LLM_MODEL", "LLM_MODEL", "FIELDKIT_ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"]
    for index, name in enumerate(names):
        monkeypatch.setenv(name, "" if index < selected else ("vertex_ai/" if index < 2 else "") + f"model-{index}")
    assert _resolve_model(None) == f"vertex_ai/model-{selected}"
    assert _resolve_model("vertex_ai/explicit") == "vertex_ai/explicit"


@pytest.mark.parametrize("configured", [None, "vertex_ai/configured", "unsupported/model"])
def test_environment_reference_model_configuration_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    """With no environment override, validate configured routes before using the default."""
    for name in ("FIELDKIT_LLM_MODEL", "LLM_MODEL", "FIELDKIT_ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"llm_model": configured} if configured else {}), encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config)
    clear_config_caches()
    try:
        if configured == "unsupported/model":
            with pytest.raises(LLMError, match="must start with 'vertex_ai/'"):
                _resolve_model(None)
        else:
            assert _resolve_model(None) == (configured or "vertex_ai/claude-sonnet-4-6")
    finally:
        clear_config_caches()


@pytest.mark.parametrize("explicit", [False, True])
def test_environment_reference_rejects_direct_provider_routes(monkeypatch: pytest.MonkeyPatch, explicit: bool) -> None:
    """Unsupported explicit and environment routes fail before invoking a provider."""
    import litellm

    import fieldkit.llm.log as llm_log

    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.setattr(llm_log, "_ensure_initialized", lambda: None)
    provider = Mock(side_effect=AssertionError("unsupported route reached provider"))
    monkeypatch.setattr(litellm, "completion", provider)
    monkeypatch.setenv("FIELDKIT_LLM_MODEL", "unsupported/model")
    with pytest.raises(LLMError, match="must start with 'vertex_ai/'"):
        synthesize("fictional prompt", model="unsupported/model" if explicit else None)
    provider.assert_not_called()


@pytest.mark.parametrize("value", ["", "0", "1", "false"])
def test_environment_reference_no_llm_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Offline flags use nonempty-string semantics, including the strings zero and false."""
    row = _environment_table("AI and transcription")[2]
    assert row == [
        "FIELDKIT_NO_LLM",
        "None",
        "Disable provider calls and select documented deterministic no-AI behavior",
    ]
    monkeypatch.delenv(row[0], raising=False)
    monkeypatch.setenv(row[0], value)
    assert llm_disabled() is bool(value)


def test_environment_reference_no_llm_skips_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical offline setting bypasses synthesis, transcription, and audio-file access."""
    import litellm

    from fieldkit.llm._transcribe import transcribe
    from fieldkit.llm.core import _NO_LLM_STUB

    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.setenv("FIELDKIT_NO_LLM", "0")
    completion = Mock(side_effect=AssertionError("offline synthesis reached provider"))
    transcription = Mock(side_effect=AssertionError("offline transcription reached provider"))
    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "transcription", transcription)
    assert synthesize("fictional prompt") == _NO_LLM_STUB
    assert transcribe(tmp_path / "absent.wav").startswith("[TRANSCRIBE STUB]")
    completion.assert_not_called()
    transcription.assert_not_called()


@pytest.mark.parametrize(
    ("explicit", "configured", "expected"),
    [
        ("test/explicit", "test/configured", "test/explicit"),
        (None, "test/configured", "test/configured"),
        (None, "", None),
    ],
)
def test_environment_reference_transcription_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
    configured: str,
    expected: str | None,
) -> None:
    """Resolve explicit and configured models with a timeout, never an implicit provider."""
    import litellm

    import fieldkit.llm.log as llm_log
    from fieldkit.llm._transcribe import TranscribeError, transcribe

    row = _environment_table("AI and transcription")[3]
    assert row == [
        "FIELDKIT_TRANSCRIBE_MODEL",
        "None",
        "Explicit transcription model; no provider is selected by default",
    ]
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.setenv(row[0], configured)
    monkeypatch.setattr(llm_log, "_ensure_initialized", lambda: None)
    provider = Mock(return_value=Mock(text="fictional transcript"))
    monkeypatch.setattr(litellm, "transcription", provider)
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"fictional audio for a mocked provider")
    if expected is None:
        with pytest.raises(TranscribeError, match="No transcription model configured"):
            transcribe(audio, model=explicit)
        provider.assert_not_called()
    else:
        assert transcribe(audio, model=explicit) == "fictional transcript"
        provider.assert_called_once()
        assert provider.call_args.kwargs["model"] == expected
        assert provider.call_args.kwargs["timeout"] == 90


@pytest.mark.parametrize("project", [None, "environment-project"])
def test_environment_reference_google_adc_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project: str | None
) -> None:
    """Exercise ADC project precedence with anonymous credentials and a mocked file loader."""
    import google.auth
    import google.auth._default as google_default
    from google.auth.credentials import AnonymousCredentials

    rows = _environment_table("AI and transcription")
    assert rows[6:] == [
        [
            "GOOGLE_CLOUD_PROJECT",
            "None",
            "Project used by Google application-default credential discovery when no explicit LiteLLM project is selected",
        ],
        ["GOOGLE_APPLICATION_CREDENTIALS", "None", "Standard Google credential-file path used by provider tooling"],
    ]
    credential_path = tmp_path / "fictional-credentials.json"
    credentials = AnonymousCredentials()
    load_credentials = Mock(return_value=(credentials, "credential-project"))
    monkeypatch.setattr(google_default, "load_credentials_from_file", load_credentials)
    monkeypatch.setenv(rows[7][0], str(credential_path))
    monkeypatch.delenv(rows[6][0], raising=False)
    monkeypatch.delenv("GCLOUD_PROJECT", raising=False)
    if project is not None:
        monkeypatch.setenv(rows[6][0], project)
    result = google.auth.default()
    assert result == (credentials, project or "credential-project")
    load_credentials.assert_called_once_with(str(credential_path), quota_project_id=None)


def test_environment_reference_explicit_vertex_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trace the explicit project through LiteLLM dispatch without contacting Vertex."""
    import litellm
    import litellm.main as litellm_main

    import fieldkit.llm.log as llm_log

    row = _environment_table("AI and transcription")[5]
    assert row == ["VERTEXAI_PROJECT", "None", "Explicit Vertex AI project read by LiteLLM"]
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.setenv(row[0], "explicit-project")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "adc-project")
    monkeypatch.setattr(litellm, "vertex_project", None)
    monkeypatch.setattr(litellm, "success_callback", [])
    monkeypatch.setattr(litellm, "failure_callback", [])
    monkeypatch.setattr(litellm, "callbacks", [])
    monkeypatch.setattr(llm_log, "_ensure_initialized", lambda: None)
    response = litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "fictional response"}}])
    provider = Mock(return_value=response)
    monkeypatch.setattr(litellm_main.vertex_chat_completion, "completion", provider)
    assert synthesize("fictional prompt", model="vertex_ai/gemini-2.5-flash") == "fictional response"
    provider.assert_called_once()
    assert provider.call_args.kwargs["vertex_project"] == "explicit-project"


@pytest.mark.parametrize("selected", range(5))
@pytest.mark.parametrize("configured", [None, "europe-west4"])
@pytest.mark.parametrize("sentinel", ["", "global"])
def test_environment_reference_region_precedence(
    monkeypatch: pytest.MonkeyPatch, selected: int, configured: str | None, sentinel: str
) -> None:
    """Configured regions win; empty and global sentinels defer to aliases or the default."""
    row = _environment_table("AI and transcription")[4]
    names = [row[0], *[name.strip("`") for name in row[1].split(", ")]]
    assert names == ["FIELDKIT_VERTEX_LOCATION", "CLOUD_ML_REGION", "VERTEX_LOCATION", "GOOGLE_CLOUD_REGION"]
    monkeypatch.setattr(config_settings, "get_vertex_location", lambda: configured)
    for index, name in enumerate(names):
        monkeypatch.setenv(name, sentinel if index < selected else f"region-{index}")
    expected = configured or (f"region-{selected}" if selected < len(names) else "us-east5")
    assert _resolve_vertex_location() == expected


@pytest.mark.parametrize("primary_id", [None, "", "primary-id"])
@pytest.mark.parametrize("primary_secret", [None, "", "primary-secret"])
def test_environment_reference_oauth_names_and_precedence(
    monkeypatch: pytest.MonkeyPatch, primary_id: str | None, primary_secret: str | None
) -> None:
    """Resolve each canonical client setting independently."""
    rows = _environment_table("Google OAuth")
    assert rows == [
        ["GOOGLE_OAUTH_CLIENT_ID", "None", "OAuth client identifier for Google consent"],
        ["GOOGLE_OAUTH_CLIENT_SECRET", "None", "OAuth client secret"],
    ]
    for (name, _alias, _purpose), primary in zip(rows, (primary_id, primary_secret), strict=True):
        monkeypatch.delenv(name, raising=False)
        if primary is not None:
            monkeypatch.setenv(name, primary)
    assert resolve_oauth_credentials() == (primary_id or None, primary_secret or None)


@pytest.mark.parametrize(
    ("config_text", "explicit", "username", "expected"),
    [
        ("{}", " override@example.com ", "local", "override@example.com"),
        ("email_domain: example.com", "", " local ", "local@example.com"),
        ("email: configured@example.com", "", "local", "local@example.com"),
        ("identity:\n  email: configured@example.com", "", "local", "local@example.com"),
        ("email_domain: example.com", "", "", None),
        ("{}", "", "local", None),
    ],
)
def test_environment_reference_identity_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_text: str,
    explicit: str,
    username: str,
    expected: str | None,
) -> None:
    """Prefer explicit email, otherwise combine the local username with a configured domain."""
    rows = _environment_table("User identity")
    assert rows == [
        ["FIELDKIT_USER_EMAIL", "Explicit current-user email for email-derived workflows"],
        ["USER", "Shell username used with the configured email domain when FIELDKIT_USER_EMAIL is empty"],
    ]
    config = tmp_path / "config.yaml"
    config.write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config)
    monkeypatch.setenv(rows[0][0], explicit)
    monkeypatch.setenv(rows[1][0], username)
    clear_config_caches()
    try:
        assert get_user_email_from_env() == expected
    finally:
        clear_config_caches()


def test_environment_reference_relative_root_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skills resolve relative roots immediately."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FIELDKIT_SKILLS_DIR", "local-skills")
    _skills_dir.cache_clear()
    try:
        assert _skills_dir() == tmp_path / "local-skills"
    finally:
        _skills_dir.cache_clear()


@pytest.mark.parametrize("retired", ["FIELDKIT_SF_PIPELINE_ROOT", "SF_PIPELINE_ROOT"])
def test_environment_reference_pipeline_root_ignores_retired_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: str
) -> None:
    """Retired Salesforce overrides cannot alter the canonical workspace/cache."""
    import fieldkit.sf.sync as sf_sync

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "accounts" / "acme-corp" / "pursuits" / "expansion.md"
    path.parent.mkdir(parents=True)
    identity = "006000000000AAA"
    path.write_text(
        f"---\nstage: discover\ngate-status: pending\nsf_opportunity_id: {identity}\n---\n", encoding="utf-8"
    )
    data = tmp_path / "runtime"
    monkeypatch.setenv(retired, str(tmp_path / "retired"))
    monkeypatch.setattr(sf_sync, "get_fieldkit_home", lambda: workspace)
    monkeypatch.setattr(sf_sync, "get_fieldkit_data", lambda: data)
    result = sf_sync.sync_opportunity(identity, path, {"opportunity_id": identity, "stage": "Propose"}, dry_run=True)
    assert result.frontmatter.path == path
    assert result.cache == data / "salesforce" / f"{identity}.json"
    assert not data.exists()


@pytest.mark.parametrize("location", ["allowed", "outside", "relative"])
def test_environment_reference_llm_log_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str) -> None:
    """Log-path resolution rejects relative and out-of-root paths without creating files."""
    root = tmp_path / "runtime"
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(root))
    candidate = root / "calls.db" if location == "allowed" else tmp_path / "outside.db"
    raw = "relative.db" if location == "relative" else str(candidate)
    monkeypatch.setenv("FIELDKIT_LLM_LOG", raw)
    if location == "allowed":
        assert get_db_path() == candidate
    else:
        message = "absolute path" if location == "relative" else "approved fieldkit root"
        with pytest.raises(ConfigError, match=message):
            get_db_path()
    assert not candidate.exists()


def test_environment_reference_xdg_config_alone_does_not_approve_log_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "xdg-config" / "fieldkit" / "calls.db"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(candidate.parent.parent))
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(candidate))
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "runtime"))
    with pytest.raises(ConfigError, match="approved fieldkit root"):
        get_db_path()
    assert not candidate.exists()


@pytest.mark.parametrize("relative", [False, True])
def test_environment_reference_absolute_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool) -> None:
    """Runtime-data and harness overrides require absolute paths before any use."""
    rows = _environment_table("Path overrides")
    assert [row[0] for row in rows[2:4]] == ["FIELDKIT_DATA_DIR", "FIELDKIT_HARNESS_ROOT"]
    raw = "relative-root" if relative else str(tmp_path / "absolute-root")
    monkeypatch.setenv(rows[2][0], raw)
    monkeypatch.setenv(rows[3][0], raw)
    if relative:
        with pytest.raises(ConfigError, match="FIELDKIT_DATA_DIR must be an absolute path"):
            get_fieldkit_data()
        with pytest.raises(ConfigError, match="FIELDKIT_HARNESS_ROOT must be an absolute path"):
            get_harness_scratch_root()
    else:
        assert get_fieldkit_data() == Path(raw)
        assert get_harness_scratch_root() == Path(raw)


@pytest.mark.parametrize("xdg", ["absolute", "relative", "missing"])
def test_environment_reference_scratch_cache_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: str
) -> None:
    """Ignore relative XDG cache roots and let an explicit harness root override fallback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FIELDKIT_HARNESS_ROOT", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    if xdg != "missing":
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache") if xdg == "absolute" else "relative")
    expected = tmp_path / ("cache" if xdg == "absolute" else ".cache") / "fieldkit"
    assert get_harness_scratch_root() == expected
    monkeypatch.setenv("FIELDKIT_HARNESS_ROOT", str(tmp_path / "explicit"))
    assert get_harness_scratch_root() == tmp_path / "explicit"


@pytest.mark.parametrize("configured", [None, "https://configured.example.com/"])
def test_environment_reference_gateway_is_configuration_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    """Configured MCP gateways override the environment fallback and lose trailing slashes."""
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"mcp_gateway_url": configured} if configured else {}), encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config)
    monkeypatch.setenv("FIELDKIT_MCP_GATEWAY_URL", "https://environment.example.com/")
    clear_config_caches()
    try:
        assert get_mcp_gateway_url() == (configured.rstrip("/") if configured else "https://environment.example.com")
    finally:
        clear_config_caches()


def test_configuration_reference_relative_google_paths_are_truthful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured Gmail paths resolve from cwd while the guide recommends absolutes."""
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text("gmail_token: token.json\ngmail_db: cache.db\n", encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config)
    clear_config_caches()
    get_gmail_db_path.cache_clear()
    try:
        assert get_google_token_path() == tmp_path / "token.json"
        assert get_gmail_db_path() == tmp_path / "cache.db"
    finally:
        clear_config_caches()
        get_gmail_db_path.cache_clear()


def test_environment_reference_driver_limit_table_matches_fail_closed_consumers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind every published driver limit to its parser and concurrency effect."""
    assert _environment_table("Driver limits") == [
        [
            "FIELDKIT_DRIVER_SPEND_CAP",
            "Optional non-negative USD daily cap for driver LLM calls; invalid, unreadable, or reached caps deny the run, and any configured cap limits an admitted non-dry batch to one issue",
        ],
        [
            "FIELDKIT_DEVELOPER_DAILY_RUN_LIMIT",
            "Required positive daily run count for fieldkit driver admit",
        ],
        [
            "FIELDKIT_DEVELOPER_SPEND_CAP",
            "Required non-negative USD cap for fieldkit driver admit",
        ],
    ]

    import fieldkit.driver.spend as spend

    monkeypatch.setattr(spend, "get_daily_spend_total", lambda: 0.0)
    monkeypatch.setattr(spend, "get_daily_developer_spend_total", lambda: 0.0)
    assert evaluate_daily_spend_cap("0", required=False).reason_code == "spend-cap-reached"
    assert evaluate_daily_developer_spend_cap("0", required=True).reason_code == "spend-cap-reached"
    assert evaluate_daily_spend_cap("not-a-number", required=False).reason_code == "spend-cap-invalid"
    monkeypatch.setattr(spend, "get_daily_spend_total", lambda: None)
    assert evaluate_daily_spend_cap("1", required=False).reason_code == "spend-unreadable"
    assert cap_safe_max_concurrent(4, dry_run=False, raw_cap="1") == 1
    assert cap_safe_max_concurrent(4, dry_run=True, raw_cap="1") == 4


def _yaml_examples() -> list[dict[str, object]]:
    """Load YAML fenced blocks exactly as published in the configuration reference."""
    examples = []
    for block in fenced_blocks(_CONFIG_REFERENCE):
        if block.language != "yaml":
            continue
        example = yaml.safe_load(block.body)
        assert isinstance(example, dict), "Configuration examples must be mappings"
        examples.append(example)
    return examples


@pytest.mark.parametrize("value,expected", [(0, 1), (1, 1), (4, 4), (5, 4), ("invalid", 1)])
def test_configuration_reference_driver_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: int | str, expected: int
) -> None:
    document = _CONFIG_REFERENCE.read_text(encoding="utf-8")
    assert "integer values are clamped to that range" in document
    example = next(example for example in _yaml_examples() if "driver" in example)
    assert example == {"driver": {"max_concurrent": 1}}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"driver": {"max_concurrent": value}}), encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    clear_config_caches()
    try:
        result = get_driver_max_concurrent()
        assert result == expected
    finally:
        clear_config_caches()


def test_configuration_reference_yaml_examples_are_accepted_by_their_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Published YAML examples parse through the same configuration readers users invoke."""
    examples = _yaml_examples()
    quota = next(example for example in examples if "pipeline" in example)
    shadowbot = next(example["shadowbot"] for example in examples if "shadowbot" in example)
    endpoints = next(example for example in examples if "mcp_endpoints" in example)
    accounts = next(example for example in examples if "accounts" in example)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(quota), encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    clear_config_caches()

    try:
        assert get_pipeline_quota() == {"target": 5_000_000, "period": "2026-H2"}
    finally:
        clear_config_caches()

    _FieldkitConfig.model_validate({"shadowbot": shadowbot})

    config_path.write_text(yaml.safe_dump(endpoints), encoding="utf-8")
    clear_config_caches()
    try:
        assert get_mcp_endpoint("calendar") == "https://gateway.example.com/calendar/mcp"
    finally:
        clear_config_caches()

    accounts_path = tmp_path / "accounts.yaml"
    accounts_path.write_text(yaml.safe_dump(accounts), encoding="utf-8")
    monkeypatch.setattr(accounts_config, "get_config_path", lambda _: accounts_path)
    clear_config_caches()

    try:
        assert _load_accounts_yaml() == accounts
    finally:
        clear_config_caches()
