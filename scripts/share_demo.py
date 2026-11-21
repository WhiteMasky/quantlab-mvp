from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import socket
import signal
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from quantlab.service.app import create_app

ROOT = Path(__file__).resolve().parents[1]
CLOUDFLARED_SHA256 = '97bb3b02d23a5980fc9c15a1a270e692da1923f103f920d5b16d4931d5ab55e7'
TUNNEL_ORIGIN = re.compile(r'https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com\Z')
GET_PATHS = {'/api/health', '/api/state', '/api/experiments', '/api/audit'}
POST_PATHS = {'/api/runs', '/api/risk/halt', '/api/orders/cancel'}
MAX_BODY = 32 * 1024
BODY_TIMEOUT = 10
SECURITY_HEADERS = {
    'cache-control': 'no-store', 'x-content-type-options': 'nosniff',
    'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer',
    'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
}


class ShareGateway:
    def __init__(self, api, static_dir: Path, port: int):
        self.api = api
        self.static = StaticFiles(directory=static_dir, html=True)
        self.local_origins = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
        self.public_origin = None

    def enable_public(self, origin: str):
        if not TUNNEL_ORIGIN.fullmatch(origin):
            raise ValueError('Expected one exact HTTPS Quick Tunnel origin')
        self.public_origin = origin

    def allowed_request(self, scope) -> bool:
        try:
            if not scope.get('client') or not ipaddress.ip_address(scope['client'][0]).is_loopback:
                return False
        except ValueError:
            return False
        headers = Headers(scope=scope)
        hosts = headers.getlist('host')
        if len(hosts) != 1:
            return False
        origins = self.local_origins | ({self.public_origin} if self.public_origin else set())
        expected = next((origin for origin in origins if urlsplit(origin).netloc == hosts[0]), None)
        if expected is None:
            return False
        document_navigation = (scope['method'] == 'GET' and scope['path'] in {'/', '/index.html'}
                               and headers.getlist('sec-fetch-mode') == ['navigate']
                               and headers.getlist('sec-fetch-dest') == ['document'])
        supplied = headers.getlist('origin')
        if len(supplied) > 1 or (supplied and supplied != [expected] and not document_navigation):
            return False
        sites = headers.getlist('sec-fetch-site')
        allowed_sites = {'same-origin', 'same-site', 'none'}
        if document_navigation:
            # The authenticated broker callback redirects across sites to the static landing page.
            allowed_sites.add('cross-site')
        if len(sites) > 1 or (sites and sites[0] not in allowed_sites):
            return False
        referers = headers.getlist('referer')
        if len(referers) > 1:
            return False
        if referers:
            value = referers[0]
            try:
                parsed = urlsplit(value)
                if (any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
                        or parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username is not None
                        or (f'{parsed.scheme}://{parsed.netloc}' != expected and not document_navigation)):
                    return False
            except ValueError:
                return False
        return scope['method'] in {'GET', 'HEAD'} or supplied == [expected]

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self.api(scope, receive, send)
        if scope['type'] != 'http':
            return await send({'type': 'websocket.close', 'code': 1008})

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                replaced = {name.encode() for name in SECURITY_HEADERS}
                message = dict(message)
                message['headers'] = [(key, value) for key, value in message.get('headers', [])
                                      if key.lower() not in replaced]
                message['headers'] += [(key.encode(), value.encode()) for key, value in SECURITY_HEADERS.items()]
            await send(message)

        async def reject(status, code):
            await JSONResponse({'detail': {'code': code}}, status_code=status)(scope, receive, secure_send)

        if not self.allowed_request(scope):
            return await reject(403, 'share_boundary_forbidden')
        path, method = scope['path'], scope['method']
        if path.startswith('/api/'):
            if path not in GET_PATHS | POST_PATHS:
                return await reject(404, 'not_found')
            if method != ('GET' if path in GET_PATHS else 'POST'):
                return await reject(405, 'method_not_allowed')
            body = bytearray()
            deadline = asyncio.get_running_loop().time() + BODY_TIMEOUT
            while True:
                try:
                    remaining = max(0, deadline - asyncio.get_running_loop().time())
                    message = await asyncio.wait_for(receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    return await reject(408, 'request_body_timeout')
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > MAX_BODY:
                    return await reject(413, 'request_too_large')
                if not message.get('more_body', False):
                    break
            consumed = False

            async def buffered_receive():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()

            # Only normalize an already validated origin; the core's local-only guard stays unchanged.
            forwarded = [(key, value) for key, value in scope['headers']
                         if key.lower() in {b'content-type', b'content-length', b'accept'}]
            forwarded += [(b'host', b'localhost'), (b'origin', b'http://localhost')]
            local_scope = {**scope, 'headers': forwarded}
            return await self.api(local_scope, buffered_receive, secure_send)
        if method not in {'GET', 'HEAD'}:
            return await reject(405, 'method_not_allowed')
        try:
            await self.static(scope, receive, secure_send)
        except HTTPException as exc:
            await reject(exc.status_code, 'static_not_found')


def validate_email(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}', value):
        raise ValueError('Specify exactly one email address; wildcards and lists are forbidden')
    return value


def protected_redirect(response: httpx.Response, origin: str) -> bool:
    try:
        target = urlsplit(response.headers.get('location', ''))
        query = parse_qs(target.query)
        return (response.status_code == 302
                and target.scheme == 'https' and target.netloc == 'login.trycloudflare.com'
                and target.path == '/authorize'
                and query.get('hostname') == [urlsplit(origin).hostname]
                and len(query.get('state', [])) == 1 and bool(query['state'][0]))
    except ValueError:
        return False


async def wait_for_dns(origin: str, stopped):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    while not stopped():
        try:
            await asyncio.wait_for(loop.getaddrinfo(urlsplit(origin).hostname, 443, type=socket.SOCK_STREAM), timeout=5)
            return
        except socket.gaierror:
            if loop.time() >= deadline:
                raise RuntimeError('New tunnel DNS did not become available; refusing publication') from None
            # Connector registration can precede publication of the new DNS record.
            await asyncio.sleep(1)
    raise RuntimeError('Sharing stopped during DNS startup')


async def wait_for_edge(origin: str, client: httpx.AsyncClient, stopped):
    deadline = asyncio.get_running_loop().time() + 45
    while not stopped():
        response = await client.get(origin + '/', follow_redirects=False)
        if protected_redirect(response, origin):
            return
        if response.status_code not in {404, 502, 503, 504, 530}:
            raise RuntimeError(f'Unexpected anonymous edge response HTTP {response.status_code}; refusing publication')
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f'Tunnel edge did not become ready (HTTP {response.status_code}); refusing publication')
        await asyncio.sleep(1)
    raise RuntimeError('Sharing stopped during edge startup')


async def verify_protection(origin: str, client: httpx.AsyncClient):
    for path in ['/', *sorted(GET_PATHS)]:
        response = await client.get(origin + path, follow_redirects=False)
        if not protected_redirect(response, origin):
            raise RuntimeError(f'Anonymous GET did not reach the protected Quick Tunnel login (HTTP {response.status_code}); refusing publication')
    for path in sorted(POST_PATHS):
        response = await client.post(origin + path, json={}, follow_redirects=False)
        if response.status_code != 401:
            raise RuntimeError('Anonymous mutation was not rejected by cloudflared; refusing publication')


class ManagedServer(uvicorn.Server):
    def capture_signals(self):
        # The supervisor must stop cloudflared before any signal can terminate Python.
        return nullcontext()


async def serve(args):
    validate_email(args.email)
    binary = args.cloudflared.resolve()
    if hashlib.sha256(binary.read_bytes()).hexdigest() != CLOUDFLARED_SHA256:
        raise RuntimeError('cloudflared checksum mismatch; install the pinned official 2026.11.1 binary')
    help_result = await asyncio.create_subprocess_exec(str(binary), 'tunnel', '--help', stdout=asyncio.subprocess.PIPE)
    help_text, _ = await help_result.communicate()
    if help_result.returncode or b'--allowed-mail' not in help_text:
        raise RuntimeError('Protected Quick Tunnels are unavailable; no public fallback is permitted')
    if not (ROOT / 'frontend/dist/index.html').is_file():
        raise RuntimeError('Build the frontend before sharing')
    directory = args.runtime.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'share.lock').open('a') as lock, socket.socket() as listener:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        listener.bind(('127.0.0.1', args.port))
        listener.listen(128)
        listener.setblocking(False)
        gateway = ShareGateway(create_app(directory / 'data'), ROOT / 'frontend/dist', args.port)
        server = ManagedServer(uvicorn.Config(gateway, host='127.0.0.1', port=args.port,
                                              proxy_headers=False, access_log=False, server_header=False,
                                              timeout_graceful_shutdown=15))
        tunnel = None
        reader_task = None

        def stop(*_):
            gateway.public_origin = None
            server.should_exit = True
            if tunnel and tunnel.returncode is None:
                try:
                    tunnel.terminate()
                except ProcessLookupError:
                    pass

        handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        state_path = directory / 'share-status.json'
        state = {'status': 'starting', 'local_url': f'http://127.0.0.1:{args.port}',
                 'runtime': str(directory / 'data'), 'public_origin': None,
                 'authenticated_browser_verified': False, 'live_enabled': False}
        try:
            state_path.write_text(json.dumps(state, indent=2) + '\n')
            config_path = directory / 'cloudflared-empty.yml'
            try:
                with config_path.open('x') as config_file:
                    config_file.write('{}\n')
            except FileExistsError:
                if config_path.read_text() != '{}\n':
                    raise RuntimeError('Isolated cloudflared configuration was modified; refusing to overwrite it')

            async def wait_server():
                while not server.started:
                    if server_task.done():
                        raise RuntimeError('Local server did not start')
                    await asyncio.sleep(0.05)
            await asyncio.wait_for(wait_server(), timeout=15)
            if server.should_exit:
                return
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith('TUNNEL_') and key not in {'NO_TLS_VERIFY', 'NO_AUTOUPDATE'}}
            tunnel = await asyncio.create_subprocess_exec(
                str(binary), 'tunnel', '--config', str(config_path), '--no-autoupdate',
                '--protocol', 'http2', '--metrics', '127.0.0.1:0', '--management-diagnostics=false',
                '--loglevel', 'info', '--grace-period', '5s',
                '--url', state['local_url'], '--allowed-mail', args.email,
                env=environment, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            ready = asyncio.get_running_loop().create_future()

            async def read_tunnel():
                protected, single_recipient, origin = False, False, None
                try:
                    with (directory / 'tunnel.log').open('a') as log:
                        async for raw in tunnel.stderr:
                            line = raw.decode(errors='replace')
                            log.write(line)
                            log.flush()
                            protected |= 'Your protected quick Tunnel has been created!' in line
                            single_recipient |= 'Allowed recipients: 1 address' in line
                            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                            if match and TUNNEL_ORIGIN.fullmatch(match[0]):
                                origin = match[0]
                            if 'Registered tunnel connection' in line and protected and single_recipient and origin:
                                if not ready.done():
                                    ready.set_result(origin)
                finally:
                    if not ready.done():
                        ready.set_exception(RuntimeError('Protected tunnel failed; inspect the local tunnel.log'))
                    stop()

            reader_task = asyncio.create_task(read_tunnel())
            if server.should_exit:
                return
            origin = await asyncio.wait_for(ready, timeout=75)
            await wait_for_dns(origin, lambda: server.should_exit)
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                await wait_for_edge(origin, client, lambda: server.should_exit)
                await verify_protection(origin, client)
            if server.should_exit:
                return
            gateway.enable_public(origin)
            state.update(status='ready', public_origin=origin, anonymous_checks_passed=True,
                         authentication='cloudflared email OTP; one exact email; no wildcard')
            state_path.write_text(json.dumps(state, indent=2) + '\n')
            print(f'Protected demo ready: {origin}', flush=True)
            print(f'Local production UI: {state["local_url"]}', flush=True)
            print('Keep this process and machine online. Ctrl+C stops sharing without deleting the ledger.', flush=True)
            await server_task
        finally:
            stop()
            try:
                if tunnel:
                    try:
                        await asyncio.wait_for(tunnel.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        tunnel.kill()
                        await tunnel.wait()
            finally:
                try:
                    await server_task
                finally:
                    try:
                        if reader_task:
                            await reader_task
                    finally:
                        for sig, handler in handlers.items():
                            signal.signal(sig, handler)
                        state['status'] = 'stopped'
                        state_path.write_text(json.dumps(state, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description='Share the complete simulated platform through a protected free Quick Tunnel.')
    parser.add_argument('--email', required=True, help='One allowed email; never a wildcard or comma-separated list')
    parser.add_argument('--port', type=int, default=18900)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime/shared-demo')
    parser.add_argument('--cloudflared', type=Path, default=ROOT / '.tools/cloudflared/cloudflared-linux-amd64')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Port must be between 1024 and 65535')
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
