#!/usr/bin/env python3
"""Test rootless Quadlet installation and persistence with a disposable instance."""
import argparse
import json
import os
import socket
import tempfile
import uuid
from urllib.request import ProxyHandler, build_opener
from pathlib import Path

from deployment import PROTECTION, PYTHON, Quadlet, healthy, manager, resolve_image, run
from install import install
from maintain import backup


def check(image):
    if os.geteuid() == 0:
        raise ValueError('run as a non-root user with a systemd user manager')
    if run(['podman', 'info', '--format', '{{.Host.Security.Rootless}}']) != 'true':
        raise ValueError('rootless Podman is required')
    manifest = resolve_image('podman', image)
    name = 'lookout-host-smoke-' + uuid.uuid4().hex[:12]
    unit = Path.home() / '.config/containers/systemd' / (name + '.container')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='lookout-host-smoke-') as temporary:
        root = Path(temporary)
        source, data = root / 'fixture', root / 'data'
        source.mkdir(); data.mkdir()
        run(['podman', 'run', '--rm', '--network', 'none', '--userns=keep-id:uid=10001,gid=10001',
             '--user', '0', '-e', 'TRACKER_SPEC_REPO=', '-e', 'PYTHONDONTWRITEBYTECODE=1',
             '-v', f'{source}:/config:Z', '-v', f'{data}:/data:Z', '--entrypoint', PYTHON,
             manifest['image'], '/app/backend/tests/container_smoke_fixture.py', 'seeded'])
        try:
            install(manifest['image'], name, config=source, data=data, directory=root / 'instance',
                    engine='podman', network='pasta', port=port, environment=['TRACKER_SPEC_REPO='])
            with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/livez', timeout=5) as response:
                assert response.status == 200
            service = Quadlet(unit)
            def observe():
                code = ("import json,urllib.request; "
                        "d=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/v2/packages/smoke-fixture',timeout=5)); "
                        "assert d['monitors']['source']['data']['version']=='1.2.3'")
                run(['podman', 'exec', name, PYTHON, '-c', code])
            observe()
            restore = root / 'restore'
            restore.mkdir()
            saved = restore / 'snapshot.sqlite3'
            backup(service, saved)
            service.stop(); service.start()
            healthy('podman', name, manifest['image'])
            observe()
            run(['podman', 'run', '--rm', '--network', 'none', *PROTECTION,
                 '--userns=keep-id:uid=10001,gid=10001', '-v', f'{restore}:/restore:ro,Z',
                 '--entrypoint', PYTHON, manifest['image'], '-c',
                 ("from tracker import state; s=state.read('/restore/snapshot.sqlite3'); "
                 "assert s['sources']['smoke-fixture']['version']=='1.2.3'")])
            print(json.dumps({'status': 'passed', 'checks': ['install', 'native-preflight', 'HTTP',
                             'stop-start', 'persistent-version', 'backup-read'],
                              'host': dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                                           if line.startswith(('ID=', 'VERSION_ID=')))}))
        finally:
            if unit.exists():
                Quadlet(unit).stop()
                unit.unlink()
                manager('Reload')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    check(parser.parse_args().image)
