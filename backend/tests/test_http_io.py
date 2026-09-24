"""Transport boundaries use pooled clients and bounded decoded responses."""
from copy import deepcopy
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs

import httpx
import pytest

from tracker import discover, http_io, monitor_io, obs
from test_discover import response, snapshot


class Stream(httpx.SyncByteStream):
    def __init__(self, parts, clock=None):
        self.parts = parts
        self.clock = clock
        self.closed = False

    def __iter__(self):
        for part in self.parts:
            if self.clock is not None:
                self.clock[0] += 1
            yield part

    def close(self):
        self.closed = True


@pytest.mark.parametrize('phase', ['headers', 'chunks', 'end'])
def test_response_budget_rejects_late_completion_and_closes_stream(monkeypatch, phase):
    clock = [0.0]
    monkeypatch.setattr(http_io.time, 'monotonic', lambda: clock[0])
    class Body(Stream):
        def __iter__(self):
            yield from super().__iter__()
            if phase == 'end':
                clock[0] = 10
    body = Body([b'a', b'b', b'c'], clock if phase == 'chunks' else None)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))) as client:
        deadline = 2
        with pytest.raises(httpx.ReadTimeout, match='elapsed-time budget'):
            with client.stream('GET', 'https://example.org/test') as reply:
                if phase == 'headers':
                    clock[0] = 10
                http_io.read_response(reply, max_bytes=1024, deadline=deadline)
    assert body.closed


def test_response_limit_applies_after_decompression():
    raw = gzip.compress(b'a' * 10000)
    assert len(raw) < 100
    with httpx.Client(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, headers={'Content-Encoding': 'gzip'}, stream=Stream([raw])))) as client:
        with pytest.raises(ValueError, match='size limit'):
            with client.stream('GET', 'https://example.org/test') as reply:
                http_io.read_response(reply, max_bytes=100, deadline=float('inf'))


@pytest.mark.parametrize('caller,budget', [('discovery', 20), ('monitor', 30), ('obs', 20)])
def test_request_budget_starts_before_headers(config, monkeypatch, caller, budget):
    clock = [0.0]
    monkeypatch.setattr(http_io.time, 'monotonic', lambda: clock[0])
    def late_headers(request):
        clock[0] = budget + 1
        return httpx.Response(200, stream=Stream([b'{}']))
    with httpx.Client(transport=httpx.MockTransport(late_headers)) as client:
        if caller == 'discovery':
            record = discover.fetch_project('widget', client)
            assert record['error'] == 'ReadTimeout'
            assert record['http_status'] == 200
            assert 'body' not in record and 'body_sha256' not in record
        elif caller == 'monitor':
            owner = monitor_io.IO(client=client)
            with pytest.raises(httpx.ReadTimeout):
                owner.for_hosts({'example.org'}).json('GET', 'https://example.org/fact')
            with pytest.raises(ValueError, match='failed earlier'):
                owner.for_hosts({'example.org'}).json('GET', 'https://example.org/fact')
        else:
            owner = obs.Client(config, attempts=1)
            owner.client.close()
            owner.client = client
            with pytest.raises(httpx.ReadTimeout):
                owner.get('/build/project/_result')


def test_discovery_keeps_status_hash_and_sanitized_failure():
    body = json.dumps(response()).encode()
    def handle(request):
        if request.url.params['name'] == 'failed':
            raise httpx.ConnectError('secret-token-at-provider', request=request)
        return httpx.Response(200, content=body)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        record = discover.fetch_project('widget', client)
        assert record['body'] == response()
        assert record['body_sha256'] == hashlib.sha256(body).hexdigest()
        assert record['http_status'] == 200 and record['at']
        failed = discover.fetch_project('failed', client)
        assert failed['error'] == 'ConnectError' and 'secret-token' not in str(failed)
        assert not client.is_closed


def test_discovery_run_shares_one_client_and_closes_it(tmp_path, monkeypatch):
    native = tmp_path / 'native.toml'
    native.write_text('')
    config = {'native': {}, 'packages': {}, 'nvpath': str(native),
              'nv_digest': hashlib.sha256(b'').hexdigest()}
    data = snapshot()
    data['generation'] = 1
    data['sources']['gadget'] = deepcopy(data['sources']['widget'])
    data['specs']['gadget'] = deepcopy(data['specs']['widget'])
    data['specs']['gadget']['metadata'].update(name='gadget', url='https://github.com/team/gadget')
    monkeypatch.setattr(discover.cfg, 'load', lambda path: config)
    monkeypatch.setattr(discover.state, 'read', lambda path: data)
    requested = []
    def handle(request):
        name = parse_qs(request.url.query.decode())['name'][0]
        requested.append(name)
        record = response()
        record['items'][0].update(name=name, homepage='https://github.com/team/' + name)
        return httpx.Response(200, json=record)
    constructor = httpx.Client
    clients = []
    def pooled(**options):
        assert options['limits'].max_connections == options['limits'].max_keepalive_connections == 4
        assert options['timeout'].as_dict() == dict(connect=20, read=20, write=20, pool=20)
        client = constructor(transport=httpx.MockTransport(handle), **options)
        clients.append(client)
        return client
    monkeypatch.setattr(discover.httpx, 'Client', pooled)
    output = tmp_path / 'evidence'
    discover.main(['--config', 'unused', '--db', 'unused', '--output', str(output)])
    assert sorted(requested) == ['gadget', 'widget']
    assert len(clients) == 1 and clients[0].is_closed
    assert native.read_bytes() == b''


def test_monitor_pool_matches_worker_count_and_keeps_injected_client(monkeypatch):
    constructor = httpx.Client
    seen = []
    def pooled(**options):
        seen.append(options)
        return constructor(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})), **options)
    monkeypatch.setattr(monitor_io.httpx, 'Client', pooled)
    owner = monitor_io.IO(workers=7)
    assert seen[0]['limits'].max_connections == seen[0]['limits'].max_keepalive_connections == 7
    assert seen[0]['timeout'].as_dict() == dict(connect=15, read=15, write=15, pool=15)
    owner.close()
    assert owner.client.is_closed
    with constructor() as injected:
        monitor_io.IO(client=injected, workers=1).close()
        assert not injected.is_closed


def test_obs_pool_matches_source_workers(config, monkeypatch):
    seen = []
    constructor = httpx.Client
    def pooled(**options):
        seen.append(options)
        return constructor(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'<directory/>')), **options)
    monkeypatch.setattr(obs.httpx, 'Client', pooled)
    config['collector'].update(timeout_seconds=3, source_workers=7)
    owner = obs.Client(config, attempts=1)
    try:
        assert seen[0]['limits'].max_connections == seen[0]['limits'].max_keepalive_connections == 7
        assert seen[0]['timeout'].as_dict() == dict(connect=3, read=3, write=3, pool=3)
        assert owner.get('/source/project') == b'<directory/>'
    finally:
        owner.close()


def test_obs_finite_header_timeout(config):
    release = threading.Event()
    entered = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            release.wait(2)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    config['obs']['api_url'] = f'http://127.0.0.1:{server.server_port}'
    config['collector'].update(timeout_seconds=0.1, source_workers=3)
    owner = obs.Client(config, attempts=1)
    try:
        assert owner.client.timeout.as_dict() == dict(connect=0.1, read=0.1, write=0.1, pool=0.1)
        with pytest.raises(httpx.ReadTimeout):
            owner.get('/delayed-headers')
        assert entered.is_set()
    finally:
        release.set()
        owner.close()
        server.shutdown()
        server.server_close()
        thread.join()
