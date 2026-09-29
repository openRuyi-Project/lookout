#!/usr/bin/env python3
"""Exercise Docker installation, reconfiguration, upgrade and recovery offline."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid

from deployment import Docker, PROTECTION, PYTHON, healthy, run
from install import install
from maintain import backup, configure, status
from upgrade import upgrade


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def bundle(image, path):
    """Test bundles use actual image metadata; publication uses release.py instead."""
    path.mkdir()
    info = json.loads(run(['docker', 'image', 'inspect', image]))[0]
    code = ('import json,tomllib; from tracker import storage; '
            'p=tomllib.load(open("/app/backend/pyproject.toml","rb")); '
            'print(json.dumps([p["project"]["version"],storage.FORMAT]))')
    version, storage = json.loads(run(['docker', 'run', '--rm', '--network', 'none',
        *PROTECTION, '--entrypoint', PYTHON, image, '-c', code]))
    run(['docker', 'save', '--output', str(path / 'image.tar'), image], timeout=600)
    with (path / 'image.tar').open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = dict(version=version, storage=storage, image=info['Id'],
                    revision=info['Config']['Labels']['org.opencontainers.image.revision'],
                    platform=info['Os'] + '/' + info['Architecture'], sha256=digest)
    (path / 'release.json').write_text(json.dumps(manifest))
    return manifest


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def exercise(image, root):
    smoke = module('smoke-image').Smoke('docker', image)
    name = smoke.prefix + '-installed'
    extra_images, volumes = [], set()
    try:
        fixture = smoke.fixture('seeded')
        helper = smoke.start('source', fixture)
        smoke.wait_live(helper)
        config = root / 'config'
        config.mkdir(mode=0o700)
        run(['docker', 'cp', helper + ':/config/.', str(config)])
        for path in config.iterdir():
            path.chmod(0o600)
        source = root / 'release'
        initial = bundle(image, source)
        port = free_port()
        smoke.containers.append(name)
        volumes.update(name + '-' + role for role in ('config', 'data', 'backups'))
        result = install(source, name, config=config, port=port, memory='4g', cpus=2,
                         environment=['TRACKER_SPEC_REPO='])
        assert result['status'] == 'ready'
        service = Docker(name)
        original_data = service.settings['data']
        assert service.info['HostConfig']['PortBindings']['8080/tcp'][0] == {
            'HostIp': '127.0.0.1', 'HostPort': str(port)}
        assert service.info['HostConfig']['Memory'] == 4 * 1024 ** 3
        assert service.info['Config']['User'] == '10001:10001'
        # Seed through the actual SQLite writer in the stopped test instance.
        service.stop()
        seed = '''from pathlib import Path
from tracker import state
p=Path('/data/state/tracker.sqlite3')
s=state.read(p)
s['inventory']['release-fixture']='release-fixture'
s['sources']['release-fixture']=state.success({}, {'version':'1.2.3'}, state.utcnow())
state.commit(p,s)
'''
        run(['docker', 'run', '--rm', '--network', 'none', *PROTECTION,
             '--mount', f'type=volume,src={original_data},dst=/data,volume-nocopy',
             '--entrypoint', PYTHON, image, '-c', seed])
        service.start()
        healthy('docker', name, initial['image'])
        print('PASS install: default UID, private host config, custom loopback port, explicit persistent volume', flush=True)

        config_file = config / 'tracker.toml'
        config_file.write_text(config_file.read_text().replace('timeout_seconds = 1', 'timeout_seconds = 2'))
        next_port = free_port()
        configure(service, config, next_port)
        service = Docker(name)
        volumes.add(service.settings['config'])
        assert service.settings['data'] == original_data
        assert service.info['HostConfig']['PortBindings']['8080/tcp'][0]['HostPort'] == str(next_port)
        observed_config = run(['docker', 'exec', name, PYTHON, '-c',
            'from pathlib import Path; print(Path("/config/tracker.toml").read_text())'])
        assert 'timeout_seconds = 2' in observed_config
        saved = root / 'saved.sqlite3'
        backup(service, saved)
        assert saved.stat().st_mode & 0o777 == 0o600
        assert status(service, root, 26)['ok']
        print('PASS configure: port and config changed, data identity unchanged; online backup verified', flush=True)

        # Different image identities exercise replacement, not just a container restart.
        for label, instruction in [('next', 'LABEL org.openruyi.test=next'),
                                    ('failed', 'ENTRYPOINT ["/bin/sh", "-c", "exit 2"]')]:
            tag = smoke.prefix + ':' + label
            extra_images.append(tag)
            run(['docker', 'build', '--network', 'none', '-t', tag, '-'],
                input=f'FROM {image}\n{instruction}\n')
            destination = root / label
            manifest = bundle(tag, destination)
            if label == 'next':
                outcome = upgrade(destination, None, root, container=name, apply=True)
                assert outcome['status'] == 'ready'
                upgraded = Docker(name)
                assert upgraded.settings['data'] == original_data
                assert upgraded.settings['config'] == service.settings['config']
                assert upgraded.info['HostConfig']['PortBindings'] == service.info['HostConfig']['PortBindings']
                assert upgraded.info['HostConfig']['Memory'] == service.info['HostConfig']['Memory']
                expected = manifest['image']
                print('PASS upgrade: changed image; same port, limits, config, data and observed version', flush=True)
            else:
                try:
                    upgrade(destination, None, root, container=name, apply=True)
                except (RuntimeError, ValueError):
                    pass
                else:
                    raise AssertionError('failing image was accepted')
                healthy('docker', name, expected)
                print('PASS failed upgrade: compatible previous image resumed without restoring old data', flush=True)
        code = ('from tracker import state; '
                'assert state.read("/data/state/tracker.sqlite3")["sources"]["release-fixture"]["version"]=="1.2.3"')
        run(['docker', 'exec', name, PYTHON, '-c', code])
        # Restore into a new test volume; never overwrite the original instance.
        restore = smoke.prefix + '-restored'
        volumes.add(restore)
        run(['docker', 'volume', 'create', restore])
        loader = smoke.prefix + '-restore-loader'
        smoke.containers.append(loader)
        run(['docker', 'create', '--name', loader, '--network', 'none', '--user', '0',
             '--mount', f'type=volume,src={restore},dst=/data,volume-nocopy',
             '--entrypoint', PYTHON, image, '-c',
             'import os; os.chown("/data/restored.sqlite3",10001,10001)'])
        run(['docker', 'cp', str(saved), loader + ':/data/restored.sqlite3'])
        run(['docker', 'start', '--attach', loader])
        restore_code = ('from tracker import state; import sqlite3; '
                        'p="/data/restored.sqlite3"; '
                        'assert sqlite3.connect("file:"+p+"?mode=ro",uri=True).execute("pragma integrity_check").fetchone()[0]=="ok"; '
                        'assert state.read(p)["sources"]["release-fixture"]["version"]=="1.2.3"')
        run(['docker', 'run', '--rm', '--network', 'none', *PROTECTION,
             '--mount', f'type=volume,src={restore},dst=/data,readonly,volume-nocopy',
             '--entrypoint', PYTHON, image, '-c', restore_code])
        print('PASS restore: independent volume, SQLite integrity and source observation retained', flush=True)
    finally:
        for container in smoke.containers:
            log = subprocess.run(['docker', 'logs', container], capture_output=True, text=True)
            (root / (container + '.log')).write_text(log.stdout + log.stderr)
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True)
        # Include retained config and stopped transaction containers owned by this test only.
        for container in run(['docker', 'ps', '-aq', '--filter', 'name=' + smoke.prefix]).split():
            run(['docker', 'rm', '-f', container])
        for volume in run(['docker', 'volume', 'ls', '--format', '{{.Name}}']).splitlines():
            if volume.startswith(smoke.prefix):
                volumes.add(volume)
        for volume in volumes | set(smoke.volumes):
            subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)
        for tag in extra_images:
            subprocess.run(['docker', 'image', 'rm', tag], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('--output', type=Path, help='new directory retaining logs and test backups')
    args = parser.parse_args()
    try:
        if args.output:
            args.output.mkdir(mode=0o700)
            exercise(args.image, args.output)
        else:
            with tempfile.TemporaryDirectory(prefix='openruyi-release-smoke-') as directory:
                exercise(args.image, Path(directory))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, AssertionError) as error:
        print(f'release smoke failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
