"""The two opt-in, read-only workflow projections. No scan is started here."""

import hmac
import logging
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from filmpaw_server.workflow_models import (
    CandidatePage, CandidateQuery, IndexOverview, WorkflowIndexConfig, WorkflowIndexError,
)


PREFIX = "/api/workflow/"
ROUTES = {"/api/workflow/v1/index": "GET", "/api/workflow/v1/candidates/query": "POST"}
BODY_LIMIT = 64 * 1024
router = APIRouter(prefix="/api/workflow/v1", tags=["workflow-index"])


def error_response(scope: Scope, code: str, status: int) -> JSONResponse:
    request_id = scope.setdefault("state", {}).setdefault("workflow_request_id", str(uuid4()))
    return JSONResponse(status_code=status, content={"code": code, "request_id": request_id})


class WorkflowReadGuard:
    """Authenticate before parsing, bound streamed bodies, and keep errors private."""

    def __init__(self, app: ASGIApp, *, config: WorkflowIndexConfig | None, origins: list[str]):
        self.app, self.config, self.origins = app, config, frozenset(origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if not scope["path"].startswith(PREFIX):
            if self.config is not None:
                credential = ("Bearer " + self.config.read_token.get_secret_value()).encode("utf-8")
                if any(k.lower() == b"authorization" and hmac.compare_digest(v, credential)
                       for k, v in scope.get("headers", [])):
                    return await error_response(scope, "read_scope_required", 403)(scope, receive, send)
            return await self.app(scope, receive, send)

        async def refuse(code: str, status: int):
            response = error_response(scope, code, status)
            if origin in self.origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
            await response(scope, receive, send)

        headers = scope.get("headers", [])
        origins = [v.decode("latin1") for k, v in headers if k.lower() == b"origin"]
        origin = origins[0] if len(origins) == 1 else None
        if self.config is None or scope["path"] not in ROUTES:
            return await refuse("not_found", 404)
        if len(origins) > 1 or (origin is not None and origin not in self.origins):
            return await refuse("origin_denied", 403)
        method = ROUTES[scope["path"]]
        if scope["method"] == "OPTIONS":
            values = {k.lower(): v.decode("latin1") for k, v in headers}
            requested_headers = {h.strip().lower() for h in values.get(b"access-control-request-headers", "").split(",") if h.strip()}
            if (origin not in self.origins or values.get(b"access-control-request-method") != method
                    or not requested_headers.issubset({"authorization", "content-type"})):
                return await refuse("invalid_preflight", 400)
            response = JSONResponse(content={}, headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": method,
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Vary": "Origin",
            })
            return await response(scope, receive, send)
        supplied = [v for k, v in headers if k.lower() == b"authorization"]
        expected = ("Bearer " + self.config.read_token.get_secret_value()).encode("utf-8")
        if len(supplied) != 1 or not hmac.compare_digest(supplied[0], expected):
            return await refuse("unauthorized", 401)
        scope.setdefault("state", {})["workflow_read_authorized"] = True
        if scope["method"] != method:
            return await refuse("method_not_allowed", 405)
        if scope.get("query_string"):
            return await refuse("invalid_query", 422)
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        if lengths:
            try:
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise ValueError
                if int(lengths[0]) > BODY_LIMIT:
                    return await refuse("body_too_large", 413)
            except ValueError:
                return await refuse("invalid_request", 422)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > BODY_LIMIT:
                return await refuse("body_too_large", 413)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        if method == "POST":
            content_types = [v.decode("latin1").split(";", 1)[0].strip().lower()
                             for k, v in headers if k.lower() == b"content-type"]
            if content_types != ["application/json"]:
                return await refuse("invalid_content_type", 415)
        body, sent = b"".join(chunks), False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        try:
            await self.app(scope, replay, send)
        except Exception as exc:
            # Do not expose connection strings, native paths, or malformed
            # input in either the public response or a traceback log.
            logging.getLogger(__name__).error("workflow_request_failed: %s", type(exc).__name__)
            await refuse("internal_error", 500)


@router.get("/index", response_model=IndexOverview)
def overview(request: Request):
    return request.app.state.workflow_index.overview(request.app.state.workflow_config)


@router.post("/candidates/query", response_model=CandidatePage, openapi_extra={
    "requestBody": {"required": True, "content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/CandidateQuery"},
    }}},
})
async def candidates(request: Request):
    # JSON arrays are valid tuple wire values. Parse in Pydantic's JSON mode,
    # rather than coercing Python dictionaries through strict internal models.
    try:
        query = CandidateQuery.model_validate_json(await request.body())
    except (ValidationError, ValueError, RecursionError):
        raise WorkflowIndexError("invalid_request", 422) from None
    request.state.workflow_request_id = query.request_id
    config = request.app.state.workflow_config
    if not set(query.root_ids).issubset(config.allowed_root_ids):
        raise WorkflowIndexError("root_not_allowed", 403)
    # Querying SQLite can run beside another request without occupying the
    # event loop; the index itself owns its short connection lock.
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(request.app.state.workflow_index.query, query, config)


def install_openapi(app):
    """Describe the same JSON-validated input used above, including its definitions."""
    def schema():
        if app.openapi_schema is None:
            document = get_openapi(title=app.title, version=app.version, routes=app.routes)
            query_schema = CandidateQuery.model_json_schema(ref_template="#/components/schemas/{model}")
            definitions = query_schema.pop("$defs", {})
            components = document.setdefault("components", {}).setdefault("schemas", {})
            for name, definition in definitions.items():
                components.setdefault(name, definition)
            components["CandidateQuery"] = query_schema
            app.openapi_schema = document
        return app.openapi_schema
    app.openapi = schema
