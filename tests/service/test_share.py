from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx
import pytest

from scripts.share_demo import (
    GET_PATHS, POST_PATHS, MAX_BODY, ShareGateway, protected_redirect,
    validate_email, verify_protection, wait_for_dns, wait_for_edge,
)

PUBLIC = 'https://unit-fixture.trycloudflare.com'
LOCAL = 'http://127.0.0.1:18900'


@pytest.fixture
def gateway(app, tmp_path):
    static = tmp_path / 'static'
    static.mkdir()
    (static / 'index.html').write_text('<html><body>Production fixture</body></html>')
    (tmp_path / 'secret.txt').write_text('PRIVATE_TEST_FIXTURE')
    (static / 'outside.txt').symlink_to(tmp_path / 'secret.txt')
    return ShareGateway(app, static, 18900)


def request(gateway, method='GET', path='/api/health', *, base=PUBLIC, headers=None, peer='127.0.0.1', **kwargs):
    async def execute():
        transport = httpx.ASGITransport(app=gateway, client=(peer, 1234))
        async with httpx.AsyncClient(transport=transport, base_url=base) as client:
            return await client.request(method, path, headers=headers, **kwargs)
    return asyncio.run(execute())


def test_public_stays_closed_before_protection_verified(gateway):
    assert request(gateway).status_code == 403
    assert request(gateway, base=LOCAL).status_code == 200


@pytest.mark.parametrize('origin', [
    'http://unit-fixture.trycloudflare.com', 'https://unit-fixture.trycloudflare.com/',
    'https://unit-fixture.trycloudflare.com.attacker.example', 'https://*.trycloudflare.com',
    'https://a.b.trycloudflare.com', 'https://a@unit-fixture.trycloudflare.com',
    'https://unit-fixture.trycloudflare.com:443', 'https://-bad.trycloudflare.com',
])
def test_public_origin_is_exact_https_hostname(gateway, origin):
    with pytest.raises(ValueError):
        gateway.enable_public(origin)


@pytest.mark.parametrize('peer', ['192.0.2.1', '198.51.100.10', 'invalid'])
def test_remote_origin_connections_cannot_forge_local_identity(gateway, peer):
    gateway.enable_public(PUBLIC)
    response = request(gateway, peer=peer, headers={
        'forwarded': 'for=127.0.0.1', 'x-forwarded-for': '127.0.0.1',
        'cf-access-authenticated-user-email': 'owner@example.com',
    })
    assert response.status_code == 403


@pytest.mark.parametrize('headers', [
    {'host': 'attacker.example'}, {'host': 'other.trycloudflare.com'},
    {'origin': 'https://attacker.example'}, {'origin': 'null'},
    {'origin': PUBLIC + '/'}, {'origin': 'http://localhost:18900'},
    {'referer': 'https://attacker.example/x'}, {'referer': PUBLIC + '.attacker.example/x'},
    {'referer': 'https://user:password@unit-fixture.trycloudflare.com/'},
    {'sec-fetch-site': 'cross-site'}, {'sec-fetch-site': 'invalid'},
    [('host', 'unit-fixture.trycloudflare.com'), ('host', 'localhost:18900')],
    [('origin', PUBLIC), ('origin', PUBLIC)],
    [('referer', PUBLIC + '/'), ('referer', PUBLIC + '/')],
    [('sec-fetch-site', 'same-origin'), ('sec-fetch-site', 'same-origin')],
])
def test_boundary_rejects_ambiguous_and_cross_origin_headers(gateway, headers):
    gateway.enable_public(PUBLIC)
    assert request(gateway, headers=headers).status_code == 403


def test_public_json_mutation_reaches_real_core_guard_and_mock_platform(gateway, factory):
    gateway.enable_public(PUBLIC)
    response = request(gateway, 'POST', '/api/runs', headers={
        'origin': PUBLIC, 'referer': PUBLIC + '/?redacted=TEST_SECRET',
        'sec-fetch-site': 'same-origin', 'x-forwarded-for': '203.0.113.1',
        'cf-access-jwt-assertion': 'NOT_A_REAL_TOKEN',
    }, json={'strategy': 'trend', 'lookback': 63, 'seed': 7})
    assert response.status_code == 200, response.text
    assert response.json()['validation']['verdict'] == 'MOCK_NOT_VALIDATED'
    assert [call[0] for call in factory.calls] == ['run_demo']
    assert 'TEST_SECRET' not in response.text
    assert response.headers['x-frame-options'] == 'DENY'
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('base', [PUBLIC, LOCAL])
def test_mutation_requires_explicit_same_origin(gateway, factory, base):
    gateway.enable_public(PUBLIC)
    assert request(gateway, 'POST', '/api/runs', base=base, json={}).status_code == 403
    assert factory.calls == []


def test_incomplete_body_has_a_bounded_deadline(gateway, factory, monkeypatch):
    monkeypatch.setattr('scripts.share_demo.BODY_TIMEOUT', 0.01)
    gateway.enable_public(PUBLIC)
    sent = []

    async def execute():
        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)

        scope = {'type': 'http', 'method': 'POST', 'path': '/api/runs',
                 'client': ('127.0.0.1', 1234), 'headers': [
                     (b'host', b'unit-fixture.trycloudflare.com'),
                     (b'origin', PUBLIC.encode()), (b'content-type', b'application/json'),
                 ]}
        await asyncio.wait_for(gateway(scope, receive, send), timeout=1)

    asyncio.run(execute())
    assert sent[0]['status'] == 408
    assert factory.calls == []


def test_core_still_rejects_simple_content_types(gateway, factory):
    gateway.enable_public(PUBLIC)
    response = request(gateway, 'POST', '/api/runs', headers={
        'origin': PUBLIC, 'content-type': 'text/plain',
    }, content='{}')
    assert response.status_code == 415
    assert factory.calls == []


def test_oversized_input_stops_before_platform(gateway, factory):
    gateway.enable_public(PUBLIC)
    response = request(gateway, 'POST', '/api/runs', headers={
        'origin': PUBLIC, 'content-type': 'application/json',
    }, content='x' * (MAX_BODY + 1))
    assert response.status_code == 413
    assert factory.calls == []


@pytest.mark.parametrize('method,path,status', [
    ('GET', '/api/unknown', 404), ('GET', '/api/runs', 405),
    ('POST', '/api/state', 405), ('DELETE', '/index.html', 405),
    ('GET', '/openapi.json', 404), ('GET', '/docs', 404),
    ('GET', '/%2e%2e/secret.txt', 404), ('GET', '/outside.txt', 404),
])
def test_only_production_assets_and_explicit_api_routes_are_served(gateway, method, path, status):
    gateway.enable_public(PUBLIC)
    response = request(gateway, method, path, headers={'origin': PUBLIC})
    assert response.status_code == status
    assert 'PRIVATE_TEST_FIXTURE' not in response.text


@pytest.mark.parametrize('method,path,destination,status', [
    ('GET', '/', 'document', 200), ('GET', '/index.html', 'document', 200),
    ('GET', '/api/state', 'document', 403), ('POST', '/api/runs', 'document', 403),
    ('GET', '/', 'iframe', 403), ('GET', '/', 'empty', 403),
])
def test_authenticated_login_navigation_does_not_relax_api_csrf(gateway, method, path, destination, status):
    gateway.enable_public(PUBLIC)
    response = request(gateway, method, path, headers={
        'sec-fetch-site': 'cross-site', 'sec-fetch-mode': 'navigate', 'sec-fetch-dest': destination,
        'referer': 'https://login.trycloudflare.com/',
    })
    assert response.status_code == status


def test_static_head_and_security_headers(gateway):
    gateway.enable_public(PUBLIC)
    page = request(gateway, path='/')
    assert page.status_code == 200
    assert 'Production fixture' in page.text
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    assert request(gateway, 'HEAD', '/').content == b''


@pytest.mark.parametrize('email', [
    '*@example.com', 'alice@example.com,bob@example.com', '',
    'alice@example.com\n', 'alice@example.com bob@example.com', 'example.com',
])
def test_no_wildcard_or_multiple_email_policy(email):
    with pytest.raises(ValueError):
        validate_email(email)


def test_one_email_policy():
    assert validate_email('owner+demo@example.com') == 'owner+demo@example.com'


def login_response(status=302, location=None):
    query = urlencode({'hostname': 'unit-fixture.trycloudflare.com', 'state': 'mock-state'})
    return httpx.Response(status, headers={'location': location or 'https://login.trycloudflare.com/authorize?' + query})


@pytest.mark.parametrize('status,location', [
    (200, None), (503, None), (302, 'http://login.trycloudflare.com/authorize'),
    (302, 'https://login.trycloudflare.com.attacker.example/authorize'),
    (302, 'https://login.trycloudflare.com/authorize?hostname=another.trycloudflare.com&state=x'),
    (302, 'https://login.trycloudflare.com/authorize?hostname=unit-fixture.trycloudflare.com'),
])
def test_publication_does_not_accept_generic_redirects_or_failures(status, location):
    assert not protected_redirect(login_response(status, location), PUBLIC)


def test_expected_connector_redirect_is_recognized():
    assert protected_redirect(login_response(), PUBLIC)


def test_dns_propagation_waits_without_enabling_public_access(monkeypatch):
    import socket
    calls = []

    async def execute():
        async def lookup(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1:
                raise socket.gaierror(-2, 'not yet published')
            return [('MOCK_ADDRESS',)]

        async def pause(_):
            pass

        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', lookup)
        monkeypatch.setattr('scripts.share_demo.asyncio.sleep', pause)
        await wait_for_dns(PUBLIC, lambda: False)

    asyncio.run(execute())
    assert calls == ['unit-fixture.trycloudflare.com'] * 2


def test_dns_wait_honors_shutdown():
    with pytest.raises(RuntimeError, match='stopped'):
        asyncio.run(wait_for_dns(PUBLIC, lambda: True))


@pytest.mark.parametrize('initial_status', [530, 200, 403])
def test_edge_warmup_never_accepts_an_unprotected_or_blocked_page(initial_status, monkeypatch):
    calls = []

    def transport(req):
        calls.append(req.url.path)
        return httpx.Response(initial_status) if len(calls) == 1 else login_response()

    async def execute():
        async def pause(_):
            pass
        monkeypatch.setattr('scripts.share_demo.asyncio.sleep', pause)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await wait_for_edge(PUBLIC, client, lambda: False)

    if initial_status == 530:
        asyncio.run(execute())
        assert calls == ['/', '/']
    else:
        with pytest.raises(RuntimeError, match='refusing publication'):
            asyncio.run(execute())
        assert calls == ['/']


@pytest.mark.parametrize('unsafe_status', [200, 403, 500, 401])
def test_publication_gate_checks_all_reads_and_rejects_unguarded_mutations(unsafe_status):
    seen = []

    def transport(req):
        seen.append((req.method, req.url.path))
        return login_response() if req.method == 'GET' else httpx.Response(unsafe_status)

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await verify_protection(PUBLIC, client)

    if unsafe_status == 401:
        asyncio.run(execute())
        assert set(seen) == {('GET', path) for path in GET_PATHS | {'/'}} | {('POST', path) for path in POST_PATHS}
    else:
        with pytest.raises(RuntimeError, match='refusing publication'):
            asyncio.run(execute())
