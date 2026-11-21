"""Single-worker lifecycle and durable audit tests; all Platform calls are MOCKED."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import closing

import httpx
import pytest

from quantlab.service.runtime import AuditStore, ServiceError


async def wait_event(event):
    assert await asyncio.to_thread(event.wait, 5), 'Timed out waiting for a deterministic test barrier'


def test_all_http_writes_are_serial_and_health_remains_responsive(app, factory, monkeypatch):
    runtime = app.state.runtime
    entered = threading.Event()
    release = threading.Event()
    second_submitted = threading.Event()
    third_submitted = threading.Event()
    original_submit = runtime._executor.submit
    submitted = 0

    def submit(*args, **kwargs):
        nonlocal submitted
        future = original_submit(*args, **kwargs)
        submitted += 1
        if submitted == 2:
            second_submitted.set()
        elif submitted == 3:
            third_submitted.set()
        return future

    def block_run(operation, parameters):
        if operation == 'run_demo':
            entered.set()
            assert release.wait(5), 'Test must release the MOCK run'

    monkeypatch.setattr(runtime._executor, 'submit', submit)
    factory.before_call = block_run

    async def scenario():
        transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 50000))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            tasks = [asyncio.create_task(client.post('/api/runs', json={}))]
            try:
                await wait_event(entered)
                health = await asyncio.wait_for(client.get('/api/health'), 2)
                assert health.status_code == 200
                state = health.json()['service']
                assert state['busy'] is True
                assert state['active_job']['operation'] == 'run_demo'
                assert state['active_job']['status'] == 'running'
                assert 'parameters' not in state['active_job']
                tasks.append(asyncio.create_task(client.post('/api/risk/halt', json={
                    'halted': True, 'reason': 'MOCK queued pause',
                })))
                await wait_event(second_submitted)
                tasks.append(asyncio.create_task(client.post('/api/orders/cancel', json={'authorized': True})))
                await wait_event(third_submitted)
                health = await asyncio.wait_for(client.get('/api/health'), 2)
                assert health.json()['service']['queued_jobs'] == 2
                for path in ['/api/state', '/api/experiments', '/api/audit']:
                    response = await asyncio.wait_for(client.get(path), 2)
                    assert response.status_code == 409
                    assert response.json()['detail']['code'] == 'busy'
                assert [call[0] for call in factory.calls] == ['run_demo']
                assert not any(task.done() for task in tasks)
            finally:
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert all(response.status_code == 200 for response in responses)
            assert [call[0] for call in factory.calls] == ['run_demo', 'set_halt', 'cancel_all']
            assert (await client.get('/api/state')).json()['risk']['halted'] is True
            health = (await client.get('/api/health')).json()['service']
            assert health['busy'] is False
            assert health['queued_jobs'] == 0
            assert health['active_job'] is None
            assert health['last_job']['operation'] == 'cancel_all'
            assert health['last_job']['status'] == 'completed'
            assert len(factory.instances) == 1
            assert {call[2] for call in factory.calls} == {factory.instances[0].thread_id}
            assert factory.instances[0].thread_id != threading.get_ident()
            entries = runtime.audit.entries()
            for response in responses:
                entry = next(item for item in entries if item['request_id'] == response.headers['x-request-id'])
                assert entry['outcome'] == 'completed'
                assert entry['status_code'] == 200

    asyncio.run(scenario())


@pytest.mark.parametrize('fail', [False, True])
def test_cancelled_waiter_does_not_cancel_accepted_job_or_lose_audit(app, factory, fail):
    runtime = app.state.runtime
    request_id = 'detached-mock-job'
    runtime.audit.begin(request_id, 'POST', '/api/runs')
    entered = threading.Event()
    release = threading.Event()

    def block(operation, parameters):
        if operation == 'run_demo':
            entered.set()
            assert release.wait(5)

    factory.before_call = block
    if fail:
        factory.errors['run_demo'] = RuntimeError('ACCOUNT_SECRET_DETACHED')

    async def scenario():
        loop = asyncio.get_running_loop()
        unhandled = []
        loop.set_exception_handler(lambda _, context: unhandled.append(context))
        task = asyncio.create_task(runtime.mutate(request_id, 'run_demo', {}))
        try:
            await wait_event(entered)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime.health()['service']['busy'] is True
        finally:
            release.set()
            # Shutdown drains the accepted job even though its HTTP-equivalent waiter left.
            await asyncio.to_thread(runtime.close)
        entry = runtime.audit.entries()[-1]
        assert entry['outcome'] == ('failed' if fail else 'completed')
        assert entry['error_code'] == ('operation_failed' if fail else None)
        assert runtime.health()['service']['last_job']['status'] == entry['outcome']
        # Give pending future callbacks a deterministic event-loop turn, without sleeps.
        turn = loop.create_future()
        loop.call_soon(turn.set_result, None)
        await turn
        assert not unhandled
        assert 'ACCOUNT_SECRET_DETACHED' not in json.dumps(runtime.audit.entries())
        assert len(factory.calls) == 1

    asyncio.run(scenario())


def test_lifespan_shutdown_drains_queue_and_rejects_new_work(app, factory, monkeypatch):
    runtime = app.state.runtime
    first_entered = threading.Event()
    second_queued = threading.Event()
    closing_started = threading.Event()
    release = threading.Event()
    original_shutdown = runtime._executor.shutdown
    original_submit = runtime._executor.submit
    count = 0

    def submit(*args, **kwargs):
        nonlocal count
        future = original_submit(*args, **kwargs)
        count += 1
        if count == 2:
            second_queued.set()
        return future

    def shutdown(*args, **kwargs):
        closing_started.set()
        return original_shutdown(*args, **kwargs)

    def block(operation, parameters):
        if operation == 'run_demo':
            first_entered.set()
            assert release.wait(5)

    monkeypatch.setattr(runtime._executor, 'submit', submit)
    monkeypatch.setattr(runtime._executor, 'shutdown', shutdown)
    factory.before_call = block
    for request_id in ['first', 'second', 'late']:
        runtime.audit.begin(request_id, 'POST', '/api/runs')

    async def scenario():
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        first = asyncio.create_task(runtime.mutate('first', 'run_demo', {}))
        shutdown_task = None
        second = None
        try:
            await wait_event(first_entered)
            second = asyncio.create_task(runtime.mutate('second', 'cancel_all', {'authorized': True}))
            await wait_event(second_queued)
            shutdown_task = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await wait_event(closing_started)
            assert runtime.health()['service']['closed'] is True
            assert not shutdown_task.done()
            with pytest.raises(ServiceError, match='stopping') as error:
                await runtime.snapshot()
            assert error.value.status_code == 503
            with pytest.raises(ServiceError, match='stopping'):
                await runtime.mutate('late', 'run_demo', {})
        finally:
            release.set()
            await first
            if second is not None:
                await second
            if shutdown_task is not None:
                await shutdown_task
            else:
                await lifespan.__aexit__(None, None, None)
        assert [call[0] for call in factory.calls] == ['run_demo', 'cancel_all']
        assert runtime.health()['service']['busy'] is False
        entries = {entry['request_id']: entry for entry in runtime.audit.entries()}
        assert entries['first']['outcome'] == entries['second']['outcome'] == 'completed'

    asyncio.run(scenario())


def test_exceptional_lifespan_exit_still_closes_worker(app):
    async def scenario():
        with pytest.raises(RuntimeError, match='test lifespan failure'):
            async with app.router.lifespan_context(app):
                raise RuntimeError('test lifespan failure')
        assert app.state.runtime.health()['service']['closed'] is True
        with pytest.raises(ServiceError, match='stopping'):
            await app.state.runtime.snapshot()

    asyncio.run(scenario())


def test_http_disconnect_keeps_accepted_job_and_worker_audit(app, factory):
    runtime = app.state.runtime
    entered = threading.Event()
    release = threading.Event()

    def block(operation, parameters):
        entered.set()
        assert release.wait(5)

    factory.before_call = block

    async def scenario():
        transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 50000))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            request = asyncio.create_task(client.post('/api/runs', json={}))
            try:
                await wait_event(entered)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert runtime.health()['service']['active_job']['status'] == 'running'
            finally:
                release.set()
                await asyncio.to_thread(runtime.close)
        entries = runtime.audit.entries()
        assert len(entries) == 1
        assert entries[0]['path'] == '/api/runs'
        assert entries[0]['outcome'] == 'completed'
        assert len(factory.calls) == 1

    asyncio.run(scenario())


def test_missing_audit_admission_fails_closed(app, factory):
    with pytest.raises(ServiceError) as error:
        asyncio.run(app.state.runtime.mutate('never-admitted', 'run_demo', {}))
    assert error.value.code == 'audit_unavailable'
    assert error.value.status_code == 503
    assert factory.instances == []


def test_unknown_runtime_operation_cannot_reach_execution_gateway(app, factory):
    runtime = app.state.runtime
    runtime.audit.begin('unknown-operation', 'POST', '<other>')
    with pytest.raises(ServiceError) as error:
        asyncio.run(runtime.mutate('unknown-operation', 'connect_live', {}))
    assert error.value.code == 'unsupported_operation'
    assert not factory.calls
    assert runtime.audit.entries()[-1]['outcome'] == 'failed'


def test_audit_persists_and_recovery_marks_pending_without_replaying(app_factory, factory,
                                                                  client_factory, tmp_path):
    directory = tmp_path / 'restart'
    app = app_factory(factory, directory)
    response = client_factory(app).post('/api/runs', json={})
    assert response.status_code == 200
    completed_id = response.headers['x-request-id']
    audit = app.state.runtime.audit
    for outcome in ['received', 'queued', 'running']:
        audit.begin(f'interrupted-{outcome}', 'POST', '/api/runs')
        if outcome != 'received':
            audit.update(f'interrupted-{outcome}', outcome)
    app.state.runtime.close()
    restarted = app_factory(factory, directory)
    entries = {entry['request_id']: entry for entry in restarted.state.runtime.audit.entries()}
    assert entries[completed_id]['outcome'] == 'completed'
    for outcome in ['received', 'queued', 'running']:
        entry = entries[f'interrupted-{outcome}']
        assert entry['outcome'] == 'interrupted'
        assert entry['error_code'] == 'service_restarted'
        assert entry['status_code'] == 503
        assert entry['finished_at'] is not None
    assert len(factory.calls) == 1  # Recovery must never replay an uncertain write.
    assert restarted.state.runtime.health()['platform_ready'] is False
    assert len(restarted.state.runtime.audit.entries(limit=2)) == 2
    with closing(sqlite3.connect(audit.path)) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_real_audit_storage_failure_returns_service_error(tmp_path):
    # Deliberately use a directory as SQLite's file, without relying on permissions/root.
    store = AuditStore(tmp_path)
    store.path.mkdir()
    with pytest.raises(ServiceError) as error:
        store.begin('bad-storage', 'GET', '/api/health')
    assert error.value.code == 'audit_unavailable'
    assert error.value.status_code == 503
