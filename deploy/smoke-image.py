#!/usr/bin/env python3
"""Offline test of the real entrypoint, using only fresh disposable volumes."""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid


class Smoke:
    def __init__(self, engine, image):
        self.engine, self.image = engine, image
        self.prefix = 'openruyi-smoke-' + uuid.uuid4().hex[:12]
        self.containers, self.volumes = [], []
        self.mapping = []
        if engine == 'podman' and self.run('info', '--format', '{{.Host.Security.Rootless}}').stdout.strip() == 'true':
            self.mapping = ['--userns=keep-id:uid=10001,gid=10001']

    def run(self, *args, check=True, timeout=40):
        result = subprocess.run([self.engine, *args], text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f'{args[0]} failed: {result.stderr.strip()}')
        return result

    def fixture(self, mode):
        volumes = []
        for role in ('config', 'data'):
            name = f'{self.prefix}-{mode}-{role}'
            self.run('volume', 'create', name)
            self.volumes.append(name)
            volumes.append(name)
        # The fixture owns even an empty volume's permissions; engine copy-up
        # must not replace them with the image directory's ownership/mode.
        helper = f'{self.prefix}-{mode}-prepare'
        self.containers.append(helper)
        self.run('run', '--rm', '--name', helper, '--network', 'none', *self.mapping, '--user', '0',
                 '-e', 'TRACKER_SPEC_REPO=', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                 '-v', f'{volumes[0]}:/config:nocopy', '-v', f'{volumes[1]}:/data:nocopy',
                 '--entrypoint', '/opt/venv/bin/python', self.image,
                 '/app/backend/tests/container_smoke_fixture.py', mode)
        self.containers.remove(helper)
        return volumes

    def start(self, label, volumes, *, root=False):
        name = f'{self.prefix}-{label}'
        self.containers.append(name)
        self.run('run', '-d', '--name', name, '--network', 'none', *self.mapping,
                 '--read-only', '--cap-drop=all', '--security-opt=no-new-privileges',
                 '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,mode=1777',
                 '-v', f'{volumes[0]}:/config:ro,nocopy', '-v', f'{volumes[1]}:/data:rw,nocopy',
                 '-e', 'TRACKER_SPEC_REPO=', '-e', 'HOST=0.0.0.0', '-e', 'PORT=8080',
                 '-e', 'API_PORT=18731', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                 *(['--user', '0'] if root else []), self.image)
        return name

    def state(self, name):
        return json.loads(self.run('inspect', name).stdout)[0]['State']

    def python(self, name, code, *, check=True):
        return self.run('exec', name, '/opt/venv/bin/python', '-c', code, check=check, timeout=10)

    def wait_live(self, name):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not self.state(name)['Running']:
                raise RuntimeError(f'{name}: exited before livez')
            result = self.python(name, 'import urllib.request; '
                'urllib.request.urlopen("http://127.0.0.1:8080/livez", timeout=2).read()', check=False)
            if result.returncode == 0:
                return
            time.sleep(0.25)
        raise RuntimeError(f'{name}: livez deadline exceeded')

    def stop(self, name):
        self.run('stop', '--time', '20', name, timeout=30)
        status = self.state(name)
        if status['Running'] or status['ExitCode'] != 0:
            raise RuntimeError(f'{name}: SIGTERM did not exit cleanly: {status["ExitCode"]}')

    def assert_seeded(self, name):
        self.python(name, '''import json, urllib.request
base = 'http://127.0.0.1:8080'
def get(path):
    with urllib.request.urlopen(base + path, timeout=3) as response:
        return response.read()
assert json.loads(get('/readyz'))['status'] in ('ready', 'degraded')
for path in ('/', '/packages/smoke-fixture'):
    body = get(path).decode()
    assert 'smoke-fixture' in body and '1.2.3' in body
''')

    def negative(self, mode, *, root=False):
        name = self.start(mode, self.fixture(mode), root=root)
        deadline = time.monotonic() + 30
        while self.state(name)['Running'] and time.monotonic() < deadline:
            time.sleep(0.25)
        status = self.state(name)
        logs = self.run('logs', name)
        if status['Running'] or status['ExitCode'] != 2 or 'runtime preflight failed:' not in logs.stdout + logs.stderr:
            raise RuntimeError(f'{mode}: expected preflight exit 2')
        print(f'PASS {mode}: preflight exit 2', flush=True)

    def check(self):
        info = json.loads(self.run('image', 'inspect', self.image).stdout)[0]
        if info['Config']['User'] != '10001:10001':
            raise RuntimeError('image default USER must be 10001:10001')
        cold = self.start('cold', self.fixture('cold'))
        self.wait_live(cold)
        self.stop(cold)
        print('PASS cold: livez, real native preflight, SIGTERM exit 0', flush=True)
        volumes = self.fixture('seeded')
        name = self.start('seeded', volumes)
        self.wait_live(name)
        self.assert_seeded(name)
        self.python(name, '''import os
from pathlib import Path
assert os.geteuid() == 10001
for root in ('/config', '/app'):
    try:
        Path(root, '.smoke-write').write_text('no')
    except OSError:
        pass
    else:
        raise AssertionError(root + ' unexpectedly writable')
p = Path('/data/.smoke-write'); p.write_text('yes'); p.unlink()
''')
        self.python(name, '''import subprocess, sys
sys.path.insert(0, '/app/backend')
from tracker import state
subprocess.run([sys.executable, '/app/deploy/backup-snapshot.py', '--db', '/data/state/tracker.sqlite3',
                '--output', '/data/smoke-backup.sqlite3'], check=True)
snapshot = state.read('/data/smoke-backup.sqlite3')
assert snapshot['sources']['smoke-fixture']['version'] == '1.2.3'
''')
        self.stop(name)
        restarted = self.start('recreated', volumes)
        self.wait_live(restarted)
        self.assert_seeded(restarted)
        self.stop(restarted)
        print('PASS seeded: HTTP list/detail, UID, read-only config/app, writable data, backup, persistence, SIGTERM', flush=True)
        self.negative('invalid')
        self.negative('unwritable')
        self.negative('root', root=True)

    def cleanup(self, failed):
        for name in self.containers:
            if failed:
                result = self.run('logs', name, check=False)
                print(f'--- {name} ---\n{result.stdout}{result.stderr}', file=sys.stderr)
            self.run('rm', '-f', name, check=False)
        for name in self.volumes:
            self.run('volume', 'rm', name, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    args = parser.parse_args()
    engine = os.environ.get('CONTAINER_ENGINE', 'podman')
    if engine not in ('docker', 'podman'):
        parser.error('CONTAINER_ENGINE must be docker or podman')
    smoke = None
    failed = True
    try:
        smoke = Smoke(engine, args.image)
        smoke.check()
        failed = False
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'smoke failed: {error}', file=sys.stderr)
        return 1
    finally:
        if smoke:
            smoke.cleanup(failed)


if __name__ == '__main__':
    raise SystemExit(main())
