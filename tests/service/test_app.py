"""HTTP contract and safety checks against an explicitly injected MOCK Platform."""
from __future__ import annotations

import importlib
import json
from datetime import datetime

import pytest

from quantlab.service import create_app
from quantlab.service.runtime import ServiceError


API_PATHS = {
    '/api/health', '/api/state', '/api/runs', '/api/risk/halt',
    '/api/orders/cancel', '/api/experiments', '/api/audit',
}


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()['detail']['code'] == code
    assert response.json()['detail']['request_id'] == response.headers['x-request-id']
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert 'access-control-allow-origin' not in response.headers


def test_public_factory_and_lazy_singleton(app, client, factory, forbid_real_engine):
    module = importlib.import_module('quantlab.service.app')
    assert module.create_app is create_app
    assert module.app.state.runtime._platform is None
    assert not app.state.runtime.runtime_dir.exists()
    health = client.get('/api/health')
    assert health.status_code == 200
    assert health.json() == {
        'status': 'ok', 'environment': 'SIMULATED', 'live_enabled': False,
        'platform_ready': False,
        'service': {'busy': False, 'queued_jobs': 0, 'active_job': None,
                    'last_job': None, 'last_control': None, 'platform_error': None, 'closed': False},
    }
    assert factory.instances == []
    assert forbid_real_engine == []
    assert {route.path for route in app.routes if route.path.startswith('/api/')} == API_PATHS
    assert app.state.runtime.audit.path.exists()


def test_default_engine_is_lazy_and_no_hidden_mock_fallback(app_factory, client_factory,
                                                           forbid_real_engine):
    app = app_factory(None)
    client = client_factory(app)
    assert client.get('/api/health').status_code == 200
    assert forbid_real_engine == []
    assert_error(client.get('/api/state'), 503, 'platform_unavailable')
    assert forbid_real_engine == ['quantlab.engine']
    health = client.get('/api/health').json()
    assert health['status'] == 'degraded'
    assert health['platform_ready'] is False
    assert health['service']['platform_error'] == 'platform_unavailable'


def test_mock_state_and_results_are_unwrapped_json(client, factory):
    response = client.get('/api/state')
    assert response.status_code == 200
    state = response.json()
    assert state['portfolio']['cash'] == '100000.0000'
    assert state['portfolio']['positions'][0]['average_cost'] == '12.3456'
    assert state['data']['start'] == '2020-01-02'
    assert state['audit'][0]['at'] == '2020-01-02T00:00:00+00:00'
    assert state['experiments'] == []
    response = client.post('/api/runs', json={})
    assert response.status_code == 200
    run = response.json()
    assert run['run_id'] == 'mock-run-1'
    assert run['result']['cash'] == '100000.0000'
    assert run['validation']['passed'] is False
    assert factory.calls[-1][0:2] == ('run_demo', {'strategy': 'trend', 'lookback': 63, 'seed': 7})
    assert client.get('/api/experiments').json() == [run]
    assert len(factory.instances) == 1
    assert factory.instances[0].runtime_dir.is_absolute()


@pytest.mark.parametrize('strategy,lookback,seed', [
    ('trend', 10, 0), ('momentum', 126, 2_147_483_647), ('trend', 63, 7),
])
def test_run_accepts_engine_boundaries(client, factory, strategy, lookback, seed):
    body = {'strategy': strategy, 'lookback': lookback, 'seed': seed}
    assert client.post('/api/runs', json=body).status_code == 200
    assert factory.calls[-1][0:2] == ('run_demo', body)


@pytest.mark.parametrize('body', [
    {'lookback': 2}, {'lookback': 9}, {'lookback': 127}, {'lookback': 252},
    {'lookback': True}, {'lookback': 63.0}, {'lookback': '63'}, {'lookback': None},
    {'seed': -1}, {'seed': 2_147_483_648}, {'seed': False}, {'seed': 7.0}, {'seed': '7'},
    {'strategy': 'live'}, {'strategy': 'TREND'}, {'strategy': None},
    {'account': 'ACCOUNT_SECRET_123456'}, {'live_enabled': True}, [], None,
])
def test_invalid_runs_never_reach_platform(client, factory, app, body):
    # Explicit content also tests a JSON null rather than an absent HTTP body.
    response = client.post('/api/runs', content=json.dumps(body),
                           headers={'content-type': 'application/json'})
    assert_error(response, 422, 'validation_error')
    assert 'ACCOUNT_SECRET_123456' not in response.text
    assert factory.instances == []
    entry = app.state.runtime.audit.entries()[-1]
    assert entry['outcome'] == 'rejected'
    assert entry['error_code'] == 'validation_error'


def test_halt_resume_and_cancel_are_distinct_authorized_actions(client, factory):
    halt = client.post('/api/risk/halt', json={'halted': True, 'reason': '  manual pause  '})
    assert halt.json() == {'halted': True, 'reason': 'manual pause',
                           'does_not_cancel_or_liquidate': True}
    assert [call[0] for call in factory.calls] == ['set_halt']
    assert client.get('/api/state').json()['risk']['halted'] is True
    resume = client.post('/api/risk/halt', json={'halted': False, 'reason': 'manual resume'})
    assert resume.json()['halted'] is False
    cancel = client.post('/api/orders/cancel', json={'authorized': True})
    assert cancel.json() == {'cancelled': [], 'count': 0, 'no_liquidation': True}
    assert factory.calls[-1][0:2] == ('cancel_all', {'authorized': True})


@pytest.mark.parametrize('body', [
    {}, {'halted': True}, {'halted': 'true', 'reason': 'pause'},
    {'halted': 1, 'reason': 'pause'}, {'halted': None, 'reason': 'pause'},
    {'halted': True, 'reason': ''}, {'halted': True, 'reason': '   '},
    {'halted': True, 'reason': '\npause'}, {'halted': True, 'reason': 'pause\t'},
    {'halted': True, 'reason': 'pa\x00use'}, {'halted': True, 'reason': 'pause\x7f'},
    {'halted': True, 'reason': 'x' * 501}, {'halted': True, 'reason': 123},
    {'halted': True, 'reason': 'pause', 'liquidate': True},
])
def test_halt_requires_strict_boolean_and_nonblank_safe_reason(client, factory, body):
    assert_error(client.post('/api/risk/halt', json=body), 422, 'validation_error')
    assert not factory.calls


@pytest.mark.parametrize('body', [{}, {'authorized': False}, {'authorized': 'true'},
                                 {'authorized': 1}, {'authorized': None},
                                 {'authorized': True, 'liquidate': True}])
def test_cancel_requires_explicit_true_only(client, factory, body):
    assert_error(client.post('/api/orders/cancel', json=body), 422, 'validation_error')
    assert not factory.calls


@pytest.mark.parametrize('content_type', [None, 'text/plain', 'application/x-www-form-urlencoded',
                                         'multipart/form-data', 'application/jsonp', 'text/json'])
@pytest.mark.parametrize('path', ['/api/runs', '/api/risk/halt', '/api/orders/cancel'])
def test_all_mutations_require_json(client, factory, path, content_type):
    headers = {'content-type': content_type} if content_type else {}
    assert_error(client.post(path, content='{}', headers=headers), 415, 'json_required')
    assert factory.instances == []


def test_json_charset_and_case_are_allowed(client):
    assert client.post('/api/runs', content='{}',
                       headers={'content-type': 'Application/JSON; charset=utf-8'}).status_code == 200


@pytest.mark.parametrize('content', ['', '{', '{"seed":', '{"seed":7,}', 'not-json'])
def test_malformed_json_is_sanitized(client, factory, content):
    response = client.post('/api/runs', content=content, headers={'content-type': 'application/json'})
    assert_error(response, 422, 'validation_error')
    assert factory.instances == []


@pytest.mark.parametrize('peer,authority', [
    ('127.0.0.1', '127.0.0.1:8000'), ('127.0.0.1', 'localhost:5173'),
    ('::1', '[::1]:8000'), ('127.0.0.2', 'LOCALHOST'),
])
def test_allowed_loopback_peers_and_hosts(app, client_factory, peer, authority):
    response = client_factory(app, peer=peer).get('/api/health', headers={'host': authority})
    assert response.status_code == 200


@pytest.mark.parametrize('peer', ['203.0.113.1', '10.0.0.1', '::', 'testclient'])
def test_remote_peer_cannot_spoof_forwarding_headers(app, factory, client_factory, peer):
    client = client_factory(app, peer=peer)
    response = client.post('/api/runs', json={}, headers={
        'forwarded': 'for=127.0.0.1;host=localhost',
        'x-forwarded-for': '127.0.0.1', 'x-forwarded-host': 'localhost',
    })
    assert_error(response, 403, 'local_only')
    assert factory.instances == []


@pytest.mark.parametrize('host', [
    'evil.example', 'localhost.evil.example', '127.0.0.1.evil.example', '0.0.0.0',
    '127.1', '2130706433', 'localhost@evil.example', 'user@localhost', 'localhost:0',
    'localhost:65536', 'localhost:', 'localhost.', 'localhost/anything', 'localhost,evil.example',
    '[::1', '[not-an-ip]', 'localhost:abc', '',
])
def test_untrusted_host_is_rejected_for_reads_and_writes(client, host):
    assert_error(client.get('/api/health', headers={'host': host}), 403, 'local_only')
    assert_error(client.post('/api/runs', json={}, headers={'host': host}), 403, 'local_only')


@pytest.mark.parametrize('origin', [
    'https://evil.example', 'null', 'file://localhost', 'http://localhost.evil.example',
    'http://localhost@evil.example', 'http://user@localhost', 'http://localhost/',
    'http://localhost?query', 'http://localhost#fragment', 'http://localhost:65536',
    'http://[::1', 'http://localhost http://evil.example',
])
def test_nonlocal_and_malformed_origins_are_rejected(client, factory, origin):
    assert_error(client.post('/api/runs', json={}, headers={'origin': origin}),
                 403, 'origin_forbidden')
    assert factory.instances == []


@pytest.mark.parametrize('origin', ['http://localhost:5173', 'http://127.0.0.1:8000', 'https://[::1]'])
def test_local_origin_is_allowed_without_enabling_cors(client, origin):
    response = client.post('/api/runs', json={}, headers={'origin': origin})
    assert response.status_code == 200
    assert 'access-control-allow-origin' not in response.headers


def test_referer_fetch_metadata_and_duplicate_headers_fail_closed(client, factory):
    cases = [
        ([('referer', 'https://evil.example/path')], 'origin_forbidden'),
        ([('sec-fetch-site', 'cross-site')], 'cross_site_forbidden'),
        ([('sec-fetch-site', 'same-origin'), ('sec-fetch-site', 'cross-site')], 'cross_site_forbidden'),
        ([('host', 'localhost'), ('host', 'evil.example')], 'local_only'),
        ([('origin', 'http://localhost'), ('origin', 'http://localhost')], 'origin_forbidden'),
        ([('referer', 'http://localhost'), ('referer', 'http://localhost')], 'origin_forbidden'),
        ([('content-type', 'application/json'), ('content-type', 'text/plain')], 'json_required'),
    ]
    for headers, code in cases:
        if code != 'json_required':
            headers = headers + [('content-type', 'application/json')]
        assert_error(client.post('/api/runs', content='{}', headers=headers),
                     415 if code == 'json_required' else 403, code)
    assert factory.instances == []
    assert client.post('/api/runs', json={}, headers={
        'referer': 'http://localhost:5173/research?tab=run', 'sec-fetch-site': 'same-origin',
    }).status_code == 200


def test_cors_preflight_is_not_enabled(client):
    response = client.request('OPTIONS', '/api/runs', headers={
        'origin': 'http://localhost:5173', 'access-control-request-method': 'POST',
    })
    assert response.status_code == 405
    assert not any(name.startswith('access-control-') for name in response.headers)


@pytest.mark.parametrize('path', ['/api/live', '/api/orders/submit', '/api/broker/connect',
                                 '/api/liquidate', '/api/data/import', '/api/research/run'])
def test_no_live_submission_liquidation_or_future_data_routes(client, factory, path):
    assert client.post(path, json={}).status_code == 404
    assert factory.instances == []


def test_openapi_matches_actual_engine_range(client):
    response = client.get('/openapi.json')
    assert response.status_code == 200
    schema = response.json()
    assert set(schema['paths']) == API_PATHS
    lookback = schema['components']['schemas']['RunRequest']['properties']['lookback']
    assert lookback['minimum'] == 10
    assert lookback['maximum'] == 126
    assert lookback['default'] == 63


@pytest.mark.parametrize('limit', ['0', '-1', '1001', '1.5', 'invalid'])
def test_audit_limit_is_validated(client, factory, limit):
    assert_error(client.get('/api/audit', params={'limit': limit}), 422, 'validation_error')
    assert factory.instances == []


def test_api_audit_merges_engine_entries_and_persists_payload_free_metadata(client, app, caplog):
    secret = 'ACCOUNT_SECRET_987654321'
    response = client.post('/api/risk/halt', json={'halted': True, 'reason': secret},
                           headers={'authorization': f'Bearer {secret}'})
    assert response.status_code == 200
    request_id = response.headers['x-request-id']
    validation = client.post('/api/runs', json={secret: secret})
    assert_error(validation, 422, 'validation_error')
    assert secret not in validation.text
    assert client.get(f'/unknown/{secret}', params={'token': secret}).status_code == 404
    rejection = client.post('/api/runs', json={}, headers={'origin': f'https://{secret}.example'})
    assert_error(rejection, 403, 'origin_forbidden')
    audit = client.get('/api/audit', params={'limit': 2})
    assert audit.status_code == 200
    assert audit.json()[0]['event'] == 'MOCK_ENGINE_EVENT'
    assert len([entry for entry in audit.json() if entry.get('source') == 'service']) == 2
    entries = app.state.runtime.audit.entries()
    assert secret not in json.dumps(entries)
    assert secret not in caplog.text
    mutation = next(entry for entry in entries if entry['request_id'] == request_id)
    assert mutation['outcome'] == 'completed'
    assert mutation['status_code'] == 200
    assert mutation['path'] == '/api/risk/halt'
    assert mutation['error_code'] is None
    assert datetime.fromisoformat(mutation['at']).utcoffset().total_seconds() == 0
    assert datetime.fromisoformat(mutation['finished_at']).utcoffset().total_seconds() == 0
    assert any(entry['path'] == '<other>' for entry in entries)
    assert all(entry['outcome'] in {'completed', 'rejected'} for entry in entries)


def test_audit_begin_failure_prevents_any_platform_effect(client, app, factory, monkeypatch):
    def fail(*args):
        raise ServiceError('audit_unavailable', 'API audit storage is unavailable.', 503)
    monkeypatch.setattr(app.state.runtime.audit, 'begin', fail)
    assert_error(client.post('/api/runs', json={}), 503, 'audit_unavailable')
    assert factory.instances == []


@pytest.mark.parametrize('phase,expected_calls', [('queued', 0), ('running', 0), ('completed', 1)])
def test_audit_update_failure_is_not_reported_as_success(client, app, factory, monkeypatch,
                                                        phase, expected_calls):
    original = app.state.runtime.audit.update

    def fail(request_id, outcome, *args):
        if outcome == phase:
            raise ServiceError('audit_unavailable', 'Inspect state before retrying.', 503)
        return original(request_id, outcome, *args)

    monkeypatch.setattr(app.state.runtime.audit, 'update', fail)
    assert_error(client.post('/api/runs', json={}), 503, 'audit_unavailable')
    assert len(factory.calls) == expected_calls
    health = client.get('/api/health')
    # With a completed-phase audit failure, even the health response must fail closed.
    assert health.status_code == (503 if phase == 'completed' else 200)


def test_platform_failure_is_redacted_without_success_or_fallback(client, app, factory, caplog):
    secret = 'ACCOUNT_SECRET_444444444'
    factory.errors['run_demo'] = RuntimeError(secret)
    response = client.post('/api/runs', json={})
    assert_error(response, 500, 'operation_failed')
    assert secret not in response.text
    assert 'partial effects' in response.json()['detail']['message']
    health = client.get('/api/health').json()
    assert health['status'] == 'degraded'
    assert health['service']['last_job']['status'] == 'failed'
    assert health['service']['last_job']['error_code'] == 'operation_failed'
    assert secret not in json.dumps(health)
    assert secret not in json.dumps(app.state.runtime.audit.entries())
    assert secret not in caplog.text
    factory.errors.clear()
    assert client.post('/api/runs', json={}).status_code == 200
    assert client.get('/api/health').json()['status'] == 'ok'


def test_snapshot_failure_is_redacted(client, factory, caplog):
    factory.errors['snapshot'] = RuntimeError('ACCOUNT_SECRET_555555555')
    response = client.get('/api/state')
    assert_error(response, 500, 'platform_error')
    assert 'ACCOUNT_SECRET_555555555' not in response.text + caplog.text


@pytest.mark.parametrize('value', [[], None, {'metric': float('nan')},
                                   {'metric': float('inf')}, {'opaque': object()}])
def test_invalid_platform_snapshot_never_produces_invalid_json(client, factory, monkeypatch, value):
    assert client.get('/api/state').status_code == 200
    monkeypatch.setattr(factory.instances[0], 'snapshot', lambda: value)
    assert_error(client.get('/api/state'), 500, 'platform_error')


@pytest.mark.parametrize('value', [[], None, {'metric': float('nan')}, {'opaque': object()}])
def test_invalid_operation_result_is_a_failed_job(client, factory, app, monkeypatch, value):
    assert client.get('/api/state').status_code == 200
    monkeypatch.setattr(factory.instances[0], 'run_demo', lambda **kwargs: value)
    assert_error(client.post('/api/runs', json={}), 500, 'operation_failed')
    assert app.state.runtime.audit.entries()[-1]['outcome'] == 'failed'


@pytest.mark.parametrize('path,key', [('/api/experiments', 'experiments'), ('/api/audit', 'audit')])
def test_convenience_endpoints_reject_missing_engine_lists(client, factory, monkeypatch, path, key):
    assert client.get('/api/state').status_code == 200
    monkeypatch.setattr(factory.instances[0], 'snapshot', lambda: {key: None})
    assert_error(client.get(path), 500, 'platform_error')
