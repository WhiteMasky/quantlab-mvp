"""Real service -> engine -> modules workflows, isolated from the demo runtime.

No replacement Platform, fabricated backtest result, external broker, or network
is used. Run only this directory while coordinating module integration.
"""
from __future__ import annotations

import json
import socket
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from quantlab.service.app import create_app


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline E2E must not open broker or external sockets')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


def local_client(runtime_dir):
    return TestClient(
        create_app(runtime_dir), base_url='http://127.0.0.1:8000',
        client=('127.0.0.1', 50123),
        headers={'Origin': 'http://127.0.0.1:5173'},
    )


def ok(response):
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def assert_account_reconciles(snapshot):
    portfolio = snapshot['portfolio']
    assert isinstance(portfolio['cash'], str), 'API Decimal money must stay a string'
    cash, equity = Decimal(portfolio['cash']), Decimal(portfolio['equity'])
    assert cash >= 0
    positions = portfolio['positions']
    assert all(type(position['quantity']) is int and position['quantity'] > 0 for position in positions)
    marked = sum((Decimal(position['market_price']) * position['quantity'] for position in positions), Decimal(0))
    assert abs(equity - cash - marked) <= Decimal('0.0001')
    fills = snapshot['fills']
    assert len({fill['fill_id'] for fill in fills}) == len(fills)
    assert len({order['intent']['intent_id'] for order in snapshot['orders']}) == len(snapshot['orders'])
    assert all(order['intent']['mode'] == 'SIMULATED' for order in snapshot['orders'])


def test_real_demo_halt_cancel_and_restart(tmp_path):
    """Exercise actual computations and persistent ledgers, never claiming edge."""
    runtime = tmp_path / 'workflow'
    parameters = {'strategy': 'trend', 'lookback': 63, 'seed': 7}
    with local_client(runtime) as client:
        health = ok(client.get('/api/health'))
        assert health['environment'] == 'SIMULATED'
        assert health['live_enabled'] is False
        initial = ok(client.get('/api/state'))
        assert initial['latest_run'] is None
        assert initial['orders'] == initial['fills'] == initial['experiments'] == []
        assert initial['data']['symbols'] == []
        assert_account_reconciles(initial)

        first = ok(client.post('/api/runs', json=parameters))
        assert first['synthetic'] is True
        assert first['validation']['passed'] is False
        assert first['validation']['verdict'] == 'NOT_VALIDATED_SYNTHETIC'
        assert first['strategy'] == parameters['strategy']
        assert first['parameters']['lookback'] == parameters['lookback']
        curve = first['result']['equity_curve']
        assert len(curve) > 100
        assert first['result']['orders'], 'The demo must expose actual historical order evidence'
        assert first['result']['fills'], 'The demo must exercise actual simulated fill accounting'
        assert all(Decimal(point['equity']).is_finite() for point in curve)
        assert 'buy_and_hold' in first['result']['benchmarks']
        assert first['result']['benchmarks']['buy_and_hold']['equity_curve']
        assert first['warnings']
        assert first['execution']['fills'] > 0, 'Default demo should exercise current simulated execution'
        after_first = ok(client.get('/api/state'))
        assert after_first['data']['synthetic'] is True
        assert after_first['data']['version']
        assert after_first['latest_run']['run_id'] == first['run_id']
        assert any(experiment['run_id'] == first['run_id'] and experiment['status'] == 'completed' for experiment in after_first['experiments'])
        assert len(after_first['fills']) == first['execution']['fills']
        assert_account_reconciles(after_first)

        halt = ok(client.post('/api/risk/halt', json={'halted': True, 'reason': 'E2E operator pause'}))
        assert halt['halted'] is True
        after_halt = ok(client.get('/api/state'))
        assert after_halt['orders'] == after_first['orders'], 'Halt must not implicitly cancel'
        assert after_halt['portfolio'] == after_first['portfolio'], 'Halt must not liquidate'

        # New execution key, not merely a replay of the deduplicated first run.
        held = ok(client.post('/api/runs', json={**parameters, 'seed': 8}))
        after_held = ok(client.get('/api/state'))
        assert held['execution']['orders'] == held['execution']['fills'] == 0
        assert after_held['fills'] == after_halt['fills']
        assert after_held['portfolio']['cash'] == after_halt['portfolio']['cash']
        assert [(p['symbol'], p['quantity']) for p in after_held['portfolio']['positions']] == [(p['symbol'], p['quantity']) for p in after_halt['portfolio']['positions']]
        assert after_held['risk']['halted'] is True

        denied = client.post('/api/orders/cancel', json={'authorized': False})
        assert denied.status_code == 422
        cancellation = ok(client.post('/api/orders/cancel', json={'authorized': True}))
        assert cancellation['no_liquidation'] is True
        final = ok(client.get('/api/state'))
        assert final['portfolio'] == after_held['portfolio']
        assert final['fills'] == after_held['fills']
        assert_account_reconciles(final)
        audit = ok(client.get('/api/audit'))
        audit_text = json.dumps(audit)
        assert 'MANUAL_HALT' in audit_text
        assert 'MANUAL_CANCEL' in audit_text
        assert 'RESEARCH_COMPLETED' in audit_text

    with local_client(runtime) as restarted:
        restored = ok(restarted.get('/api/state'))
        assert restored['risk']['halted'] is True
        assert restored['latest_run']['run_id'] == final['latest_run']['run_id']
        assert restored['fills'] == final['fills']
        assert restored['orders'] == final['orders']
        assert restored['portfolio'] == final['portfolio']
        assert restored['data']['version'] == final['data']['version']
        assert_account_reconciles(restored)
        resumed = ok(restarted.post('/api/risk/halt', json={'halted': False, 'reason': 'E2E operator resume'}))
        assert resumed['halted'] is False


def test_identical_demo_rerun_does_not_repeat_execution(tmp_path):
    parameters = {'strategy': 'trend', 'lookback': 63, 'seed': 7}
    with local_client(tmp_path / 'repeat') as client:
        first = ok(client.post('/api/runs', json=parameters))
        before = ok(client.get('/api/state'))
        response = client.post('/api/runs', json=parameters)
        after = ok(client.get('/api/state'))
        if response.status_code != 200:
            # Preserve useful module diagnostics without accepting failure as success.
            failures = [row.get('error') for row in after['experiments'] if row['status'] == 'failed']
            pytest.fail(f'Identical offline rerun failed: {response.text}; persisted errors: {failures}')
        repeated = ok(response)
        assert repeated['run_id'] != first['run_id'], 'Every research attempt is separately registered'
        assert repeated['result']['equity_curve'] == first['result']['equity_curve']
        assert repeated['execution']['deduplicated'] is True
        assert repeated['execution']['orders'] == repeated['execution']['fills'] == 0
        assert after['orders'] == before['orders']
        assert after['fills'] == before['fills']
        assert after['portfolio'] == before['portfolio']
        assert_account_reconciles(after)


def test_frontend_request_boundary_rejects_invalid_or_remote_mutations(tmp_path):
    with local_client(tmp_path / 'boundaries') as client:
        for body in [
            {'strategy': 'trend', 'lookback': 9, 'seed': 7},
            {'strategy': 'trend', 'lookback': 127, 'seed': 7},
            {'strategy': 'trend', 'lookback': 63.5, 'seed': 7},
            {'strategy': 'trend', 'lookback': 63, 'seed': -1},
            {'strategy': 'trend', 'lookback': 63, 'seed': 2147483648},
            {'strategy': 'trend', 'lookback': 63, 'seed': 7.5},
            {'strategy': 'trend', 'lookback': 63, 'seed': True},
            {'strategy': 'live', 'lookback': 63, 'seed': 7},
        ]:
            response = client.post('/api/runs', json=body)
            assert response.status_code == 422, f'Invalid request must be rejected before engine work: {body}: {response.text}'
        assert client.post('/api/risk/halt', json={'halted': True, 'reason': '   '}).status_code == 422
        assert client.post('/api/orders/cancel', json={}).status_code == 422
        assert client.post('/api/orders/cancel', json={'authorized': True}, headers={'Origin': 'https://example.com'}).status_code == 403
        assert client.post('/api/orders/cancel', content='{"authorized":true}', headers={'Content-Type': 'text/plain'}).status_code == 415
        exposed = {route.path for route in client.app.routes}
        assert '/api/orders/submit' not in exposed
        assert '/api/portfolio/liquidate' not in exposed
        assert '/api/broker/connect' not in exposed
        state = ok(client.get('/api/state'))
        assert state['latest_run'] is None
        assert state['orders'] == state['fills'] == state['experiments'] == []
