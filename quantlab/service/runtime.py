"""Single-process service worker and payload-free, durable API audit.

Platform construction and regular calls occur on one worker thread, including reads.
An explicitly thread-safe emergency_halt capability has an independent control lane.
This supports engines using thread-affine SQLite connections. Run one ASGI worker
per runtime directory; this is not a distributed job scheduler. Accepted jobs
continue if an HTTP client disconnects, and shutdown drains the worker queue.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from quantlab.models import jsonable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class AuditStore:
    """No bodies, query strings, headers, account data or exception text stored."""

    def __init__(self, runtime_dir: Path):
        self.path = runtime_dir / 'service.sqlite3'
        self._init_lock = threading.Lock()
        self._initialized = False

    def _ensure(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('''CREATE TABLE IF NOT EXISTS service_api_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    at TEXT NOT NULL,
                    finished_at TEXT,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    status_code INTEGER,
                    error_code TEXT
                )''')
                # An unfinished operation may have had effects before process loss.
                # Never replay it, and never label it successfully completed.
                db.execute('''UPDATE service_api_audit
                    SET outcome='interrupted', finished_at=?, status_code=503,
                        error_code='service_restarted'
                    WHERE outcome IN ('received', 'queued', 'running')''', (utc_now(),))
            self._initialized = True

    def begin(self, request_id: str, method: str, path: str) -> None:
        try:
            self._ensure()
            with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
                db.execute('''INSERT INTO service_api_audit
                    (request_id, at, method, path, outcome) VALUES (?, ?, ?, ?, 'received')''',
                           (request_id, utc_now(), method, path))
        except (OSError, sqlite3.Error) as exc:
            raise ServiceError('audit_unavailable', 'API audit storage is unavailable.', 503) from exc

    def update(self, request_id: str, outcome: str, status_code: int | None = None,
               error_code: str | None = None) -> None:
        finished = None if outcome in {'received', 'queued', 'running'} else utc_now()
        try:
            self._ensure()
            with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
                changed = db.execute('''UPDATE service_api_audit SET outcome=?, status_code=?,
                    error_code=?, finished_at=? WHERE request_id=?''',
                                     (outcome, status_code, error_code, finished, request_id))
                if changed.rowcount != 1:
                    raise sqlite3.DatabaseError('Missing API audit admission')
        except (OSError, sqlite3.Error) as exc:
            raise ServiceError(
                'audit_unavailable',
                'API audit storage is unavailable; an accepted operation may have taken effect. '
                'Inspect state before retrying.', 503,
            ) from exc

    def entries(self, limit: int = 200) -> list[dict]:
        try:
            self._ensure()
            with closing(sqlite3.connect(self.path, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute('''SELECT * FROM service_api_audit
                    ORDER BY id DESC LIMIT ?''', (limit,)).fetchall()
            return [dict(row, source='service', action=f"{row['method']} {row['path']}")
                    for row in reversed(rows)]
        except (OSError, sqlite3.Error) as exc:
            raise ServiceError('audit_unavailable', 'API audit storage is unavailable.', 503) from exc


def default_platform_factory(runtime_dir: Path) -> Any:
    # The engine is coordinator-owned and need not exist at import/startup time.
    from quantlab.engine import Platform
    return Platform(runtime_dir)


class ServiceRuntime:
    def __init__(self, runtime_dir: Path, platform_factory: Callable[[Path], Any] | None = None):
        self.runtime_dir = runtime_dir
        self.audit = AuditStore(runtime_dir)
        self._factory = platform_factory or default_platform_factory
        self._platform: Any = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='quantlab-service')
        self._control_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='quantlab-stop')
        self._last_control: dict | None = None
        self._write_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._queued = 0
        self._active: dict | None = None
        self._last: dict | None = None
        self._platform_ready = False
        self._platform_error: str | None = None
        self._closed = False

    def _get_platform(self) -> Any:
        if self._platform is None:
            try:
                platform = self._factory(self.runtime_dir)
                if platform is None:
                    raise TypeError('Platform factory returned None')
                self._platform = platform
            except Exception as exc:
                with self._status_lock:
                    self._platform_error = 'platform_unavailable'
                raise ServiceError('platform_unavailable',
                                   'Platform could not be initialized. No fallback results are available.',
                                   503) from exc
            with self._status_lock:
                self._platform_ready = True
                self._platform_error = None
        return self._platform

    @staticmethod
    def _serialize(value: Any) -> Any:
        result = jsonable(value)
        # Validate before recording success: Decimal stays a string, NaN is not JSON.
        json.dumps(result, allow_nan=False)
        return result

    def health(self) -> dict:
        with self._status_lock:
            degraded = bool(self._platform_error or
                            (self._last and self._last['status'] == 'failed'))
            return {
                'status': 'degraded' if degraded else 'ok',
                'environment': 'SIMULATED',
                'live_enabled': False,
                'platform_ready': self._platform_ready,
                'service': {
                    'busy': bool(self._active or self._queued),
                    'queued_jobs': self._queued,
                    'active_job': dict(self._active) if self._active else None,
                    'last_job': dict(self._last) if self._last else None,
                    'last_control': dict(self._last_control) if self._last_control else None,
                    'platform_error': self._platform_error,
                    'closed': self._closed,
                },
            }

    @staticmethod
    async def _await_worker(future) -> dict:
        pending = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # The worker owns completion/audit even after the requester leaves.
            # Retrieve detached failures instead of emitting an unhandled-future log.
            pending.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            raise

    async def snapshot(self) -> dict:
        with self._status_lock:
            if self._closed:
                raise ServiceError('service_stopping', 'Service is stopping.', 503)
            if self._active or self._queued:
                raise ServiceError('busy', 'A write job is active; inspect /api/health and retry.', 409)
            future = self._executor.submit(self._read_snapshot)
        return await self._await_worker(future)

    def _read_snapshot(self) -> dict:
        try:
            result = self._serialize(self._get_platform().snapshot())
            if not isinstance(result, dict):
                raise TypeError('Expected a snapshot dictionary')
            return result
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError('platform_error',
                               'Platform state could not be read. No fallback snapshot is available.') from exc

    async def mutate(self, request_id: str, operation: str, parameters: dict) -> dict:
        # Audit admission is durable BEFORE any side effects are scheduled.
        self.audit.update(request_id, 'queued')
        job = {'request_id': request_id, 'operation': operation, 'status': 'queued',
               'queued_at': utc_now()}
        with self._status_lock:
            if self._closed:
                raise ServiceError('service_stopping', 'Service is stopping.', 503)
            emergency = getattr(self._platform, 'emergency_halt', None)
            if operation == 'set_halt' and parameters.get('halted') is True and callable(emergency):
                future = self._control_executor.submit(self._run_emergency, job, emergency, parameters['reason'])
            else:
                self._queued += 1
                future = self._executor.submit(self._run_job, job, parameters)
        return await self._await_worker(future)

    def _run_emergency(self, job, emergency, reason):
        request_id = job['request_id']
        error = None
        try:
            self.audit.update(request_id, 'running')
            result = self._serialize(emergency(reason))
            if not isinstance(result, dict):
                raise TypeError('Expected a control result dictionary')
        except ServiceError as exc:
            error = exc
        except Exception:
            error = ServiceError('halt_failed', 'Emergency stop could not be confirmed. Inspect risk state.', 503)
        try:
            self.audit.update(request_id, 'failed' if error else 'completed',
                              error.status_code if error else 200, error.code if error else None)
        except ServiceError as exc:
            error = exc
        with self._status_lock:
            self._last_control = dict(job, status='failed' if error else 'completed',
                                      finished_at=utc_now(), error_code=error.code if error else None)
        if error:
            raise error
        return result

    def _run_job(self, job: dict, parameters: dict) -> dict:
        request_id = job['request_id']
        with self._write_lock:
            with self._status_lock:
                self._queued -= 1
                job = dict(job, status='running', started_at=utc_now())
                self._active = job
            error: ServiceError | None = None
            result: dict | None = None
            try:
                self.audit.update(request_id, 'running')
                platform = self._get_platform()
                # Deliberately narrow gateway: never expose submit/connect/liquidate.
                if job['operation'] == 'run_demo':
                    value = platform.run_demo(**parameters)
                elif job['operation'] == 'set_halt':
                    value = platform.set_halt(**parameters)
                elif job['operation'] == 'cancel_all':
                    value = platform.cancel_all(**parameters)
                else:
                    raise ServiceError('unsupported_operation', 'Unsupported operation.', 400)
                result = self._serialize(value)
                if not isinstance(result, dict):
                    raise TypeError('Expected an operation result dictionary')
            except ServiceError as exc:
                error = exc
            except Exception:
                # Exception strings/tracebacks can contain full accounts or secrets.
                error = ServiceError('operation_failed',
                                     'Platform operation failed and may have had partial effects. '
                                     'Inspect state and experiments before retrying.')
            try:
                self.audit.update(request_id, 'failed' if error else 'completed',
                                  error.status_code if error else 200, error.code if error else None)
            except ServiceError as exc:
                error = exc
            finally:
                with self._status_lock:
                    self._last = dict(job, status='failed' if error else 'completed',
                                      finished_at=utc_now(), error_code=error.code if error else None)
                    self._active = None
            if error:
                raise error
            return result

    def _close_platform(self):
        close = getattr(self._platform, 'close', None)
        if callable(close):
            close()

    def close(self) -> None:
        with self._status_lock:
            if self._closed:
                return
            self._closed = True
        self._control_executor.shutdown(wait=True, cancel_futures=False)
        closing = self._executor.submit(self._close_platform)
        self._executor.shutdown(wait=True, cancel_futures=False)
        closing.result()
