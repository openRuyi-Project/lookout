"""Real nvchecker CLI with loopback HTTP fixtures; no external providers."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from tracker.config import track_fingerprint
from tracker.monitors.version.nvchecker import import_events

BACKEND = Path(__file__).resolve().parents[2]
NOW = '2026-09-20T00:00:00+00:00'

@pytest.fixture
def native_check(tmp_path):
    requests = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            super().do_GET()
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def run(entry, payload, expected, previous=None):
        (tmp_path / 'versions.json').write_text(payload if isinstance(payload, str) else json.dumps(payload))
        transport = {**entry, 'url': f'http://127.0.0.1:{server.server_port}/versions.json'}
        # These sources have mutually exclusive identity/URL authoring modes.
        # This CLI fixture deliberately exercises URL mode. Separate tests pass
        # identities through the real resolver and mock only provider transport.
        if entry.get('source') == 'crates_index':
            transport.pop('cratesio', None)
        elif entry.get('source') == 'anitya_stable':
            transport.pop('anitya_id', None)
        path = tmp_path / 'native.toml'
        path.write_text('[fixture]\n' + '\n'.join(k + ' = ' + json.dumps(v)
                                                 for k, v in transport.items()) + '\n')
        command = ['nvchecker', '--logger=json', '--json-log-fd=1', '--failures', '-c', str(path)]
        first_request = len(requests)
        process = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                 env={**os.environ, 'PYTHONPATH': str(BACKEND)})
        observed_requests = requests[first_request:]
        print('PROVIDER_ORDER:', json.dumps(dict(command=command, stdout=process.stdout,
                                                stderr=process.stderr, exit_status=process.returncode,
                                                requests=observed_requests)))
        assert observed_requests and set(observed_requests) == {'/versions.json'}
        assert process.returncode == (0 if expected is not None else 3)
        # Keep the real production identity when importing transport-only fixtures.
        facts, error = import_events(process.stdout, {'fixture': entry}, previous or {}, NOW)
        fact = facts['fixture']
        if expected is not None:
            assert fact['version'] == expected and not fact.get('error') and error is None
            assert fact['configuration_fingerprint'] == track_fingerprint(entry)
        else:
            assert fact['error']
        return fact

    try:
        yield run
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
