"""Loopback-only FastAPI facade for the coordinator-owned offline Platform.

Core response dictionaries are passed through, not wrapped. POST /api/runs waits
for completion. GET /api/health remains available while jobs run; state-based
reads return 409 while a write is pending. Use a single uvicorn worker bound to
127.0.0.1 (or ::1). Local checks are not multi-user authentication. No CORS or
broker connection is enabled here. The Vite same-origin /api proxy is supported.
"""
from __future__ import annotations

import ipaddress
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .runtime import ServiceError, ServiceRuntime


class RunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    strategy: Literal['trend', 'momentum'] = 'trend'
    lookback: int = Field(default=63, ge=10, le=126)
    seed: int = Field(default=7, ge=0, le=2_147_483_647)


class HaltRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    halted: bool
    reason: str = Field(min_length=1, max_length=500)

    @field_validator('reason')
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Control characters are not allowed in the reason')
        result = value.strip()
        if not result:
            raise ValueError('A non-blank reason is required')
        return result


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    authorized: bool

    @field_validator('authorized')
    @classmethod
    def confirmed(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError('Explicit cancellation authorization is required')
        return value


_LOCAL_AUTHORITY = re.compile(r'(?:localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?\Z', re.I)
_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
_API_PATHS = {
    '/api/health', '/api/state', '/api/runs', '/api/risk/halt',
    '/api/orders/cancel', '/api/experiments', '/api/audit',
}
_VALIDATION_LOCATIONS = {
    'body', 'query', 'path', 'header', 'cookie', 'strategy', 'lookback',
    'seed', 'halted', 'reason', 'authorized', 'limit',
}


def _local_authority(value: str) -> bool:
    match = _LOCAL_AUTHORITY.fullmatch(value)
    return bool(match and (match.group(1) is None or 0 < int(match.group(1)) <= 65535))


def _local_url(value: str, *, origin: bool) -> bool:
    try:
        parsed = urlsplit(value)
        return (not any(char.isspace() or ord(char) < 32 for char in value)
                and parsed.scheme in {'http', 'https'}
                and _local_authority(parsed.netloc)
                and (not origin or (not parsed.path and '?' not in value and '#' not in value)))
    except ValueError:
        return False


def _guard(request: Request) -> None:
    # Never trust Forwarded/X-Forwarded-* in the app to establish local identity.
    try:
        local_peer = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        local_peer = False
    hosts = request.headers.getlist('host')
    if not local_peer or len(hosts) != 1 or not _local_authority(hosts[0]):
        raise ServiceError('local_only', 'Only loopback clients and local Host values are allowed.', 403)

    origins = request.headers.getlist('origin')
    if len(origins) > 1 or (origins and not _local_url(origins[0], origin=True)):
        raise ServiceError('origin_forbidden', 'A local HTTP(S) Origin is required when provided.', 403)
    fetch_sites = request.headers.getlist('sec-fetch-site')
    if len(fetch_sites) > 1 or (fetch_sites and fetch_sites[0].lower() == 'cross-site'):
        raise ServiceError('cross_site_forbidden', 'Cross-site requests are not allowed.', 403)

    if request.method not in _SAFE_METHODS:
        referers = request.headers.getlist('referer')
        if len(referers) > 1 or (referers and not _local_url(referers[0], origin=False)):
            raise ServiceError('origin_forbidden', 'A non-local Referer is not allowed.', 403)
        content_types = request.headers.getlist('content-type')
        if (len(content_types) != 1 or
                content_types[0].split(';', 1)[0].strip().lower() != 'application/json'):
            raise ServiceError('json_required', 'Mutations require application/json.', 415)


def _error_response(error: ServiceError, request_id: str) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={'detail': {
        'code': error.code, 'message': error.message, 'request_id': request_id,
    }})


def create_app(runtime_dir: str | Path | None = None, *,
               platform_factory: Callable[[Path], Any] | None = None) -> FastAPI:
    """Create a local app. platform_factory is an explicit test/integration seam.

    The default factory lazily imports quantlab.engine.Platform. Neither app
    construction nor /api/health creates a Platform or connects to a broker.
    """
    directory = (Path(runtime_dir) if runtime_dir is not None else
                 Path(__file__).resolve().parents[2] / 'runtime').resolve()
    runtime = ServiceRuntime(directory, platform_factory)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            yield
        finally:
            await run_in_threadpool(runtime.close)

    application = FastAPI(title='QuantLab local simulation API', version='0.1.0', lifespan=lifespan)
    application.state.runtime = runtime

    @application.middleware('http')
    async def local_guard_and_audit(request: Request, call_next):
        request_id = uuid4().hex
        request.state.request_id = request_id
        request.state.error_code = None
        # Unknown paths and methods are redacted to prevent logging secrets in URLs.
        # Read ASGI's path directly: parsing request.url here can raise on a malformed Host.
        path = request.scope.get('path', '')
        audit_path = path if path in _API_PATHS else '<other>'
        audit_method = request.method if request.method in {'GET', 'POST', 'PUT', 'DELETE',
                                                           'PATCH', 'HEAD', 'OPTIONS'} else '<other>'
        audited = False
        try:
            await run_in_threadpool(runtime.audit.begin, request_id, audit_method, audit_path)
            audited = True
            _guard(request)
            response = await call_next(request)
        except ServiceError as exc:
            request.state.error_code = exc.code
            response = _error_response(exc, request_id)
        except Exception:
            # Do not log/return arbitrary engine exceptions or request content.
            request.state.error_code = 'internal_error'
            response = _error_response(ServiceError('internal_error',
                                       'The request could not be completed.'), request_id)
        if audited:
            outcome = ('completed' if response.status_code < 400 else
                       'rejected' if response.status_code < 500 else 'failed')
            try:
                await run_in_threadpool(runtime.audit.update, request_id, outcome,
                                        response.status_code, request.state.error_code)
            except ServiceError as exc:
                response = _error_response(exc, request_id)
        response.headers['X-Request-ID'] = request_id
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @application.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        request.state.error_code = exc.code
        return _error_response(exc, request.state.request_id)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        request.state.error_code = 'validation_error'
        # Neither raw input nor arbitrary extra-field names may echo account secrets.
        return JSONResponse(status_code=422, content={'detail': {
            'code': 'validation_error', 'message': 'Invalid request parameters.',
            'request_id': request.state.request_id,
            'errors': [{'loc': [part if isinstance(part, int) or part in _VALIDATION_LOCATIONS
                                else '<field>' for part in error['loc']],
                        'type': error['type']} for error in exc.errors()],
        }})

    @application.get('/api/health')
    async def health():
        return JSONResponse(runtime.health())

    @application.get('/api/state')
    async def state():
        return JSONResponse(await runtime.snapshot())

    @application.post('/api/runs')
    async def run(body: RunRequest, request: Request):
        return JSONResponse(await runtime.mutate(request.state.request_id, 'run_demo', body.model_dump()))

    @application.post('/api/risk/halt')
    async def halt(body: HaltRequest, request: Request):
        return JSONResponse(await runtime.mutate(request.state.request_id, 'set_halt', body.model_dump()))

    @application.post('/api/orders/cancel')
    async def cancel(body: CancelRequest, request: Request):
        return JSONResponse(await runtime.mutate(request.state.request_id, 'cancel_all', body.model_dump()))

    @application.get('/api/experiments')
    async def experiments():
        snapshot = await runtime.snapshot()
        if not isinstance(snapshot.get('experiments'), list):
            raise ServiceError('platform_error', 'Platform did not provide an experiment list.')
        return JSONResponse(snapshot['experiments'])

    @application.get('/api/audit')
    async def audit(limit: int = Query(default=200, ge=1, le=1000)):
        snapshot = await runtime.snapshot()
        if not isinstance(snapshot.get('audit'), list):
            raise ServiceError('platform_error', 'Platform did not provide an audit list.')
        entries = await run_in_threadpool(runtime.audit.entries, limit)
        return JSONResponse(snapshot['audit'] + entries)

    return application


# Lazy: importing this module creates neither SQLite files nor an engine.
app = create_app()
