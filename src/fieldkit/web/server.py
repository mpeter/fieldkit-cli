"""fieldkit.web.server — FastAPI app factory for the local dashboard.

Security posture:
- Nothing listens unless the operator runs ``fieldkit web serve``.
- The built-in listener binds loopback addresses only.
- When a token is configured, every ``/api/*`` and ``/events`` request
  must carry ``Authorization: Bearer <token>``. Static assets stay open
  (they contain no data — all data flows through the guarded API).
"""

import hmac
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import Any, Literal, TypeAlias

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fieldkit.companion.runner import run_action
from fieldkit.errors import AuthError, FieldkitError, LLMError, WebDataError
from fieldkit.gtask import client as tasks_mod
from fieldkit.web import actions as actions_mod
from fieldkit.web import chat as chat_mod
from fieldkit.web import prs as prs_mod
from fieldkit.web.data import DataSource
from fieldkit.web.events import alert_event_stream

log = logging.getLogger(__name__)

_GUARDED_PREFIXES = ("/api/", "/events")
_WRITE_PREFIXES = ("/api/tasks/", "/api/proposals/", "/api/prs/")
_MAX_REQUEST_BYTES = 32_768
_MAX_HISTORY_TURNS = 6
_MAX_HISTORY_TURN_CHARS = 2_000
_MAX_CHAT_MESSAGE_CHARS = 4_000
_MAX_COMMENT_CHARS = 4_000
_MAX_TASK_TITLE_CHARS = 500

ASGIMessage: TypeAlias = MutableMapping[str, Any]
ASGIScope: TypeAlias = MutableMapping[str, Any]
ASGIReceive: TypeAlias = Callable[[], Awaitable[ASGIMessage]]
ASGISend: TypeAlias = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp: TypeAlias = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


class RequestBodyTooLarge(Exception):
    """Raised when an ASGI request body exceeds the dashboard limit."""


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before FastAPI parses or buffers them."""

    def __init__(self, app: ASGIApp, *, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_size:
                    await self._too_large(scope, receive, send)
                    return
            except ValueError:
                await self._too_large(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> ASGIMessage:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_size:
                    raise RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            await self._too_large(scope, receive, send)

    async def _too_large(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        response = JSONResponse({"error": "request body exceeds 32768 bytes"}, status_code=413)
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Frame-Options"] = "DENY"
        await response(scope, receive, send)


class ChatTurn(BaseModel):
    """One bounded, client-supplied chat turn."""

    role: Literal["user", "assistant"]
    content: str = Field(max_length=_MAX_HISTORY_TURN_CHARS)


class ChatRequest(BaseModel):
    """POST /api/chat body."""

    message: str = Field(min_length=1, max_length=_MAX_CHAT_MESSAGE_CHARS)
    history: list[ChatTurn] = Field(default_factory=list, max_length=_MAX_HISTORY_TURNS)


class CommentRequest(BaseModel):
    """POST /api/prs/{number}/comment body."""

    body: str = Field(min_length=1, max_length=_MAX_COMMENT_CHARS)


class CreateTaskRequest(BaseModel):
    """POST /api/tasks/create body."""

    title: str = Field(min_length=1, max_length=_MAX_TASK_TITLE_CHARS)
    section: Literal["today", "active"]
    account: str | None = None
    due: str | None = None


def _static_root() -> Path:
    """Return the packaged static asset directory (AP-004: importlib.resources)."""
    return Path(str(resources.files("fieldkit.web").joinpath("static")))


def _unauthorized() -> JSONResponse:
    resp = JSONResponse({"error": "unauthorized"}, status_code=401)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# Host values (port stripped) accepted in tokenless loopback mode. Rejecting
# everything else blocks DNS-rebinding: an attacker page rebound to 127.0.0.1
# sends its own domain in the Host header and gets a 403 instead of data.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _host_only(host_header: str) -> str:
    """Strip the port from a Host header value (IPv6-bracket aware)."""
    if host_header.startswith("["):
        return host_header.split("]", 1)[0] + "]"
    return host_header.rsplit(":", 1)[0] if ":" in host_header else host_header


def _token_matches(request: Request, token: str) -> bool:
    """Check a bearer header with a constant-time comparison."""
    header = request.headers.get("authorization", "")
    return hmac.compare_digest(header.encode(), f"Bearer {token}".encode())


def is_loopback_bind_host(host: str) -> bool:
    """Return whether *host* is an exact loopback bind address."""
    return host in _LOOPBACK_HOSTS - {"[::1]"}


def create_app(
    source: DataSource | None = None,
    *,
    token: str | None = None,
    synthesize_fn: Any = None,
    github_repo: str | None = None,
    gh_runner: Any = None,
    tier_provider: Callable[[], str] | None = None,
    allowlist_provider: Callable[[], list[str]] | None = None,
    data_path_provider: Callable[[], Path] | None = None,
    action_runner: actions_mod.ActionRunner = run_action,
) -> FastAPI:
    """Build the fieldkit web application.

    Args:
        source: Injectable data source (defaults to config-derived paths).
        token: Optional bearer token; when set, guards /api/* and /events
            through an ``Authorization: Bearer <t>`` header.
            When unset, requests must carry a loopback Host header
            (DNS-rebinding guard).
        synthesize_fn: Injectable LLM call for /api/chat (tests).
        github_repo: GitHub slug for the PR queue (defaults to the
            ``github_repo`` config key, resolved lazily per request).
        gh_runner: Injectable gh invocation for the PR queue (tests).

    Returns:
        A configured FastAPI instance.

    Raises:
        WebDataError: When *token* is empty or whitespace (a blank token
            would silently disable auth on a public bind).
    """
    if token is not None and not token.strip():
        raise WebDataError("web auth token is empty — regenerate with: fieldkit web token")

    src = source if source is not None else DataSource.from_config()
    app = FastAPI(title="fieldkit web", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=_MAX_REQUEST_BYTES)

    @app.middleware("http")
    async def _guard(request: Request, call_next: Any) -> Response:
        response: Response
        if token is not None:
            if request.url.path.startswith(_GUARDED_PREFIXES) and not _token_matches(request, token):
                response = _unauthorized()
                response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
                response.headers["X-Frame-Options"] = "DENY"
                return response
        elif _host_only(request.headers.get("host", "")) not in _LOOPBACK_HOSTS:
            # Tokenless mode is loopback-only; a foreign Host header means a
            # rebound DNS name or a proxy — refuse the whole request.
            response = JSONResponse({"error": "forbidden host — local dashboard only"}, status_code=403)
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            response.headers["X-Frame-Options"] = "DENY"
            return response
        elif request.method == "POST" and request.url.path.startswith(_WRITE_PREFIXES):
            response = JSONResponse(
                {"error": "write actions disabled — serve with a token (fieldkit web token) to enable writes"},
                status_code=403,
            )
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            response.headers["X-Frame-Options"] = "DENY"
            return response
        result: Response = await call_next(request)
        if token is not None and request.url.path.startswith("/api/"):
            # Token mode enables guarded dashboard access and write actions —
            # deal data must not persist at rest in the browser's Cache Storage
            # (implementation change, frontier review finding L5 on PR #1186). Unconditional
            # overwrite is intentional: no /api/* route sets its own
            # Cache-Control today, and this header is a security requirement
            # that must win over any weaker value a route might add later.
            result.headers["Cache-Control"] = "no-store"
        result.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        result.headers["X-Frame-Options"] = "DENY"
        return result

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        """Liveness probe plus a data-availability summary."""
        return {
            "ok": True,
            "briefs_dir": str(src.briefs_dir),
            "watchers_dir": str(src.watchers_dir),
            "has_brief": src.latest_brief() is not None,
            "alert_files": len(src.alert_mtimes()),
        }

    @app.get("/api/brief")
    def brief() -> JSONResponse:
        """Return the latest saved morning brief as markdown."""
        doc = src.latest_brief()
        if doc is None:
            return JSONResponse({"error": "no saved brief — run: fieldkit brief generate"}, status_code=404)
        return JSONResponse(doc.to_dict())

    @app.get("/api/alerts")
    def alerts() -> JSONResponse:
        """Return all watcher alert files, newest first."""
        return JSONResponse({"alerts": [a.to_dict() for a in src.list_alerts()]})

    @app.get("/api/tasks")
    def tasks() -> JSONResponse:
        """Return Google Tasks from the list named ``fieldkit``."""
        try:
            return JSONResponse({"tasks": [task.to_dict() for task in tasks_mod.list_tasks()]})
        except (WebDataError, AuthError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    @app.get("/api/operations")
    def operations() -> JSONResponse:
        """Return the freshness, failures, and safe next action for the attention loop."""
        return JSONResponse(src.operations())

    @app.get("/api/proposals/{name}")
    def proposal(name: str) -> JSONResponse:
        """Return one proposal after the operator explicitly opens it."""
        doc = src.proposal(name)
        if doc is None:
            return JSONResponse({"error": "proposal not found"}, status_code=404)
        return JSONResponse(doc.to_dict())

    @app.get("/api/pipeline/health")
    def pipeline_health() -> JSONResponse:
        """Return the risk-ranked pursuit health list."""
        return _cli_proxy(src.pipeline_health)

    @app.get("/api/pipeline/forecast")
    def forecast() -> JSONResponse:
        """Return the weighted pipeline forecast."""
        return _cli_proxy(src.forecast)

    @app.get("/api/pipeline/quota")
    def quota() -> JSONResponse:
        """Return the quota gap summary."""
        return _cli_proxy(src.quota)

    @app.post("/api/chat")
    def chat(body: ChatRequest) -> JSONResponse:
        """Answer a fieldkit-context chat message."""
        try:
            history = [{"role": turn.role, "content": turn.content} for turn in body.history]
            reply = chat_mod.answer(body.message, src, history=history, synthesize_fn=synthesize_fn)
        except WebDataError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except LLMError as exc:
            log.warning("chat LLM failure: %s", exc)
            return JSONResponse({"error": f"LLM unavailable: {exc}"}, status_code=503)
        return JSONResponse({"reply": reply})

    def _resolve_repo() -> str:
        if github_repo is not None:
            return github_repo
        from fieldkit.config import get_github_repo

        return get_github_repo()

    def _require_write_auth(request: Request) -> JSONResponse | None:
        """Gate outward-facing write actions (merge/comment) on the token.

        Unlike reads, writes require a token even on loopback: any local
        page that survived the Host check (or a future guard regression)
        must still not be able to merge PRs. No token configured = writes
        disabled entirely.
        """
        if token is None:
            return JSONResponse(
                {"error": "write actions disabled — serve with a token (fieldkit web token) to enable merge/comment"},
                status_code=403,
            )
        if not _token_matches(request, token):
            return _unauthorized()
        return None

    def _action_config() -> tuple[str, list[str], Path]:
        from fieldkit.config import get_companion_act_allowlist, get_companion_tier, get_fieldkit_data

        tier = tier_provider() if tier_provider is not None else get_companion_tier()
        allowlist = allowlist_provider() if allowlist_provider is not None else get_companion_act_allowlist()
        data_path = data_path_provider() if data_path_provider is not None else get_fieldkit_data()
        return tier, allowlist, Path(data_path)

    def _action_response(outcome: actions_mod.WebActionOutcome, *, approval: bool = False) -> JSONResponse:
        if approval and outcome.approval_status == "missing":
            status_code = 404
        elif approval and outcome.approval_status == "not-approvable":
            status_code = 409
        else:
            status_code = {
                "proposed": 202,
                "executed": 200,
                "disabled": 403,
                "denied": 403,
                "failed": 502,
                "conflict": 409 if approval else 502,
            }[outcome.state]
        return JSONResponse(asdict(outcome), status_code=status_code)

    @app.get("/api/companion")
    def companion_status() -> JSONResponse:
        try:
            tier, _, _ = _action_config()
        except OSError:
            log.warning("dashboard companion config filesystem failure", exc_info=True)
            return JSONResponse({"error": "dashboard action storage unavailable"}, status_code=502)
        except FieldkitError as exc:
            return JSONResponse({"error": str(exc)[:500]}, status_code=502)
        return JSONResponse(
            {
                "tier": tier,
                "writes_enabled": token is not None and tier in {"propose", "act"},
                "minimum_write_tier": "propose",
            }
        )

    @app.get("/api/feed")
    def feed() -> JSONResponse:
        try:
            return JSONResponse({"items": src.feed()})
        except WebDataError as exc:
            return JSONResponse({"error": str(exc)[:500]}, status_code=502)

    @app.post("/api/tasks/create")
    def task_create(body: CreateTaskRequest, request: Request) -> JSONResponse:
        denied = _require_write_auth(request)
        if denied is not None:
            return denied
        try:
            tier, allowlist, data_path = _action_config()
            outcome = actions_mod.create_task(
                body.title,
                body.section,
                body.account,
                body.due,
                tier=tier,
                allowlist=allowlist,
                data_path=data_path,
                runner=action_runner,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)[:500]}, status_code=422)
        except OSError:
            log.warning("dashboard task proposal filesystem failure", exc_info=True)
            return JSONResponse({"error": "dashboard action storage unavailable"}, status_code=502)
        except FieldkitError as exc:
            return JSONResponse({"error": str(exc)[:500]}, status_code=502)
        return _action_response(outcome)

    @app.post("/api/tasks/{task_id}/complete")
    def task_complete(task_id: str, request: Request) -> JSONResponse:
        denied = _require_write_auth(request)
        if denied is not None:
            return denied
        try:
            tier, allowlist, data_path = _action_config()
            outcome = actions_mod.complete_task(
                task_id, tier=tier, allowlist=allowlist, data_path=data_path, runner=action_runner
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)[:500]}, status_code=422)
        except OSError:
            log.warning("dashboard task completion filesystem failure", exc_info=True)
            return JSONResponse({"error": "dashboard action storage unavailable"}, status_code=502)
        except FieldkitError as exc:
            return JSONResponse({"error": str(exc)[:500]}, status_code=502)
        return _action_response(outcome)

    @app.post("/api/proposals/{name}/approve")
    def proposal_approve(name: str, request: Request) -> JSONResponse:
        denied = _require_write_auth(request)
        if denied is not None:
            return denied
        try:
            tier, allowlist, data_path = _action_config()
            outcome = actions_mod.approve(
                name, tier=tier, allowlist=allowlist, data_path=data_path, runner=action_runner
            )
        except OSError:
            log.warning("dashboard proposal approval filesystem failure", exc_info=True)
            return JSONResponse({"error": "dashboard action storage unavailable"}, status_code=502)
        except FieldkitError as exc:
            return JSONResponse({"error": str(exc)[:500]}, status_code=502)
        return _action_response(outcome, approval=True)

    @app.get("/api/prs")
    def prs() -> JSONResponse:
        """List open PRs with CI rollup summaries."""
        try:
            repo = _resolve_repo()
            runner = gh_runner if gh_runner is not None else prs_mod.run_gh
            return JSONResponse({"prs": prs_mod.list_prs(repo, gh_runner=runner), "writes_enabled": token is not None})
        except WebDataError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except FieldkitError as exc:
            return JSONResponse({"error": f"config: {exc}"}, status_code=502)

    @app.post("/api/prs/{number}/merge")
    def merge(number: int, request: Request) -> JSONResponse:
        """Squash-merge a PR. Requires the token even on loopback."""
        denied = _require_write_auth(request)
        if denied is not None:
            return denied
        try:
            result = prs_mod.merge_pr(_resolve_repo(), number, gh_runner=gh_runner or prs_mod.run_gh)
        except WebDataError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except FieldkitError as exc:
            return JSONResponse({"error": f"config: {exc}"}, status_code=502)
        return JSONResponse({"result": result})

    @app.post("/api/prs/{number}/comment")
    def comment(number: int, body: CommentRequest, request: Request) -> JSONResponse:
        """Comment on a PR (the bounce action). Requires the token."""
        denied = _require_write_auth(request)
        if denied is not None:
            return denied
        try:
            result = prs_mod.comment_pr(_resolve_repo(), number, body.body, gh_runner=gh_runner or prs_mod.run_gh)
        except WebDataError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FieldkitError as exc:
            return JSONResponse({"error": f"config: {exc}"}, status_code=502)
        return JSONResponse({"result": result})

    @app.get("/events")
    def events() -> StreamingResponse:
        """SSE stream of watcher-alert changes."""
        return StreamingResponse(
            alert_event_stream(src),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    static_root = _static_root()

    @app.get("/")
    def index() -> FileResponse:
        """Serve the PWA shell."""
        return FileResponse(static_root / "index.html")

    @app.get("/manifest.json")
    def manifest() -> FileResponse:
        """PWA manifest (must be served from the app scope root)."""
        return FileResponse(static_root / "manifest.json")

    @app.get("/sw.js")
    def service_worker() -> FileResponse:
        """Service worker (must be served from the app scope root)."""
        return FileResponse(static_root / "sw.js", media_type="application/javascript")

    app.mount("/static", StaticFiles(directory=static_root), name="static")
    return app


def _cli_proxy(provider: Any) -> JSONResponse:
    """Call a CLI-backed provider, mapping WebDataError to HTTP 502."""
    try:
        return JSONResponse({"data": provider()})
    except WebDataError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 6096,
    token: str | None = None,
) -> None:
    """Run the web server (blocking).

    Raises:
        WebDataError: When binding beyond loopback.
    """
    if not is_loopback_bind_host(host):
        raise WebDataError(f"refusing to bind {host} — fieldkit web serves loopback addresses only")
    import uvicorn

    uvicorn.run(create_app(token=token), host=host, port=port, log_level="info")
