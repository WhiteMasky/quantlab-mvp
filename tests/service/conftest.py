"""Isolated service tests: explicit MOCK Platform, no engine or network calls."""
from __future__ import annotations

import asyncio
import builtins
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from quantlab.models import Position
from quantlab.service import create_app


class FakePlatform:
    """MOCK contract implementation, not a backtest or broker integration."""

    def __init__(self, runtime_dir: Path, factory):
        self.runtime_dir = runtime_dir
        self.factory = factory
        self.thread_id = threading.get_ident()
        self.halted = False
        self.experiments = []

    def _record(self, operation, parameters):
        assert threading.get_ident() == self.thread_id, 'Platform must stay on its owning thread'
        self.factory.calls.append((operation, parameters, threading.get_ident()))
        if self.factory.before_call:
            self.factory.before_call(operation, parameters)
        if operation in self.factory.errors:
            raise self.factory.errors[operation]

    def snapshot(self):
        self._record('snapshot', {})
        return {
            'environment': 'SIMULATED', 'live_enabled': False,
            'broker': {'connected': False, 'mock': True},
            'data': {'source': 'MOCK fixture', 'synthetic': True, 'version': None,
                     'symbols': [], 'start': date(2020, 1, 2), 'end': None,
                     'issues': [], 'limitations': ['MOCK: not market data'], 'updated_at': None},
            'portfolio': {'cash': Decimal('100000.0000'), 'equity': Decimal('100000.0000'),
                          'positions': [Position('MOCK', 1, Decimal('12.3456'), Decimal('12.3456'))],
                          'exposure': Decimal('0')},
            'orders': [], 'fills': [], 'experiments': list(self.experiments),
            'risk': {'halted': self.halted},
            'audit': [{'event': 'MOCK_ENGINE_EVENT', 'at': datetime(2020, 1, 2, tzinfo=timezone.utc)}],
            'latest_run': None, 'alerts': ['MOCK: not investment evidence'],
        }

    def run_demo(self, strategy='trend', lookback=63, seed=7):
        self._record('run_demo', {'strategy': strategy, 'lookback': lookback, 'seed': seed})
        result = {'run_id': f'mock-run-{len(self.experiments) + 1}', 'strategy': strategy,
                  'parameters': {'lookback': lookback}, 'seed': seed, 'synthetic': True,
                  'result': {'cash': Decimal('100000.0000')},
                  'validation': {'passed': False, 'verdict': 'MOCK_NOT_VALIDATED'},
                  'execution': {'orders': 0, 'fills': 0}, 'warnings': ['MOCK fixture only']}
        self.experiments.append(result)
        return result

    def set_halt(self, halted, reason):
        self._record('set_halt', {'halted': halted, 'reason': reason})
        self.halted = halted
        return {'halted': halted, 'reason': reason, 'does_not_cancel_or_liquidate': True}

    def cancel_all(self, authorized=False):
        self._record('cancel_all', {'authorized': authorized})
        assert authorized is True
        return {'cancelled': [], 'count': 0, 'no_liquidation': True}


class FakePlatformFactory:
    """Explicit injection seam; every test response from this factory is mocked."""

    def __init__(self):
        self.instances = []
        self.calls = []
        self.errors = {}
        self.before_call = None

    def __call__(self, runtime_dir):
        platform = FakePlatform(runtime_dir, self)
        self.instances.append(platform)
        return platform


class LocalASGIClient:
    """In-process HTTP only; never opens a listening socket or external connection."""

    def __init__(self, app, *, peer='127.0.0.1', base_url='http://127.0.0.1:8000'):
        self.app = app
        self.transport = httpx.ASGITransport(app=app, client=(peer, 50000))
        self.base_url = base_url

    def request(self, method, path, **kwargs):
        async def send():
            async with httpx.AsyncClient(transport=self.transport, base_url=self.base_url) as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def post(self, path, **kwargs):
        return self.request('POST', path, **kwargs)


@pytest.fixture(autouse=True)
def forbid_real_engine(monkeypatch):
    """Catch accidental integration; even a default-factory test cannot load Engine."""
    original = builtins.__import__
    attempts = []

    def guarded(name, *args, **kwargs):
        if name == 'quantlab.engine' or name.startswith('quantlab.engine.'):
            attempts.append(name)
            raise ImportError('Engine intentionally blocked in isolated service tests')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded)
    return attempts


@pytest.fixture
def factory():
    return FakePlatformFactory()


@pytest.fixture
def app_factory(tmp_path):
    apps = []

    def build(platform_factory, runtime_dir=None):
        directory = runtime_dir if runtime_dir is not None else tmp_path / f'app-{len(apps)}'
        app = create_app(directory, platform_factory=platform_factory)
        apps.append(app)
        return app

    yield build
    for app in apps:
        app.state.runtime.close()


@pytest.fixture
def app(app_factory, factory):
    return app_factory(factory)


@pytest.fixture
def client_factory():
    return LocalASGIClient


@pytest.fixture
def client(app, client_factory):
    return client_factory(app)
