"""Real nvchecker 2.22 selection; only PyPI HTTP transport points at local inputs."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import tomlkit
import subprocess
import sys
import threading
import tomllib

import pytest
from tracker import config, nv, state

NOW = '2026-09-20T00:00:00+00:00'


@pytest.mark.parametrize('event, expected', [
    ({'event':'no-result','error':'HTTP 599: Operation timed out after 300000 milliseconds with 4 bytes received'}, 'nvchecker request timeout'),
    ({'event':'no-result','error':'TimeoutError()'}, 'nvchecker request timeout'),
    ({'event':'no-result','error':'HTTP 404: https://user:password@example.invalid/?token=secret'}, 'nvchecker HTTP 404'),
    ({'event':'no-result','error':'HTTP 503: https://secret.invalid/'}, 'nvchecker HTTP 503'),
    ({'event':'no-result','error':"ValueError('no version returned')"}, 'nvchecker no matching version'),
    ({'event':'version string not found.'}, 'nvchecker no matching version'),
    ({'event':'no-result','error':'private provider detail secret'}, 'nvchecker reported no usable result'),
])
def test_safe_native_error_categories_preserve_last_success(event, expected):
    entry = {'source':'pypi', 'pypi':'fixture'}
    old = {'version':'1.0', 'fetched_at':'2026-01-01T00:00:00Z',
           'configuration_fingerprint':config.track_fingerprint(entry)}
    fact, error = nv.import_events(json.dumps({'name':'package','level':'error',**event}),
                                  {'package':entry}, {'package':old}, NOW)
    assert fact['package']['error'] == expected and error is None
    assert fact['package']['version'] == '1.0'
    assert fact['package']['fetched_at'] == old['fetched_at']
    assert fact['package']['attempted_at'] == NOW
    assert 'secret' not in json.dumps(fact) and 'password' not in json.dumps(fact)


def test_generic_followup_event_does_not_erase_specific_failure():
    lines = [ {'name':'package','level':'error','event':'unexpected error happened','error':'HTTP 403: private'},
              {'name':'package','level':'error','event':'no-result'} ]
    fact, _ = nv.import_events('\n'.join(map(json.dumps, lines)), {'package':{}}, {}, NOW)
    assert fact['package']['error'] == 'nvchecker HTTP 403'


def test_native_pypi_release_and_explicit_watch_policy(tmp_path):
    def release(yanked=False):
        return [{'yanked':yanked, 'upload_time_iso_8601':'2026-09-19T00:00:00Z'}]
    payloads = {
        'mixed': {'releases': {'1.9':release(), '2.0rc1':release(), '3.0':release(True)}},
        'onlypre': {'releases': {'0.64b0':release(), '0.65b0':release()}},
        'empty': {'releases': {}},
        'withdrawn': {'releases': {'1.0':release(True)}},
    }
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.split('/')[2]
            requests.append(name)
            body = json.dumps(payloads[name]).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_args): pass
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    entries = {
        'package': {'source':'pypi','pypi':'mixed'},
        'package@prerelease': {'source':'pypi','pypi':'mixed','use_pre_release':True},
        'onlypre': {'source':'pypi','pypi':'onlypre'},
        'onlypre@prerelease': {'source':'pypi','pypi':'onlypre','use_pre_release':True},
        'empty': {'source':'pypi','pypi':'empty'},
        'withdrawn': {'source':'pypi','pypi':'withdrawn'},
    }
    path = tmp_path / 'native.toml'
    path.write_text(tomlkit.dumps(entries))
    # No plugin replacement or ordering imitation: actual CLI/plugins/HTTP client
    # process the fixture. Reject every unexpected origin rather than going online.
    script = '''import sys
from nvchecker.httpclient.tornado_httpclient import session
original = session.request_impl
async def fixture_request(url, **kwargs):
    assert url.startswith('https://pypi.org/pypi/'), url
    return await original(BASE + url.removeprefix('https://pypi.org'), **kwargs)
session.request_impl = fixture_request
from nvchecker.__main__ import main
main()
'''.replace('BASE', repr(f'http://127.0.0.1:{server.server_port}'))
    old_entry = {**entries['onlypre'], 'use_pre_release':True}
    previous = {'onlypre': {'version':'0.64b0','source':config.public_source(old_entry),
                           'fetched_at':'2026-09-01T00:00:00Z',
                           'configuration_fingerprint':config.track_fingerprint(old_entry)}}
    try:
        command = [sys.executable,'-c',script,'--logger=json','--json-log-fd=1','--failures','-c',str(path)]
        proc = subprocess.run(command,capture_output=True,text=True,timeout=30)
        print('REAL PYPI CLI STDOUT:',proc.stdout,'STDERR:',proc.stderr,'EXIT:',proc.returncode)
        assert proc.returncode == 3
        facts, error = nv.import_events(proc.stdout, entries, previous, NOW)
        assert error is None and set(requests) == set(payloads)
        assert facts['package']['version'] == '1.9'
        assert facts['package@prerelease']['version'] == '2.0rc1'
        assert facts['onlypre@prerelease']['version'] == '0.65b0'
        for name in ('onlypre','empty','withdrawn'):
            assert facts[name]['error'] == 'nvchecker no matching version'
            assert 'version' not in facts[name] and 'fetched_at' not in facts[name]
        assert facts['onlypre']['previous_configuration']['version'] == '0.64b0'
        assert facts['onlypre']['configuration_fingerprint'] == config.track_fingerprint(entries['onlypre'])
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_production_prerelease_is_an_explicit_watch_not_main_comparison():
    root = Path(__file__).resolve().parents[2]
    native = __import__('tracker.version_rules',fromlist=['load']).load(root/'config/versions/nvchecker.toml').entries
    operator = config.load(root/'config/tracker.toml')
    name = 'python-opentelemetry-semantic-conventions'
    assert not native[name].get('use_pre_release', False)
    assert native[name+'@prerelease']['use_pre_release'] is True
    assert config.binding({**operator,'native':native},name)['compare'] == name
    assert config.binding({**operator,'native':native},name)['watch'] == [name+'@prerelease']
    assert [n for n,e in native.items() if e.get('use_pre_release')] == [name+'@prerelease']
