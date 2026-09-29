#!/usr/bin/env python3
"""Export a consistent online backup, or check disk and backup age."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from deployment import Docker, Quadlet, PYTHON, run


def backup(service, output):
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError('use a new backup file in an existing directory')
    remote = '/data/.backup-' + uuid.uuid4().hex + '.sqlite3'
    # Execute in the live container: a second :Z mount would revoke its SELinux label.
    run([service.engine, 'exec', service.name, PYTHON, '/app/deploy/backup-snapshot.py',
         '--db', '/data/state/tracker.sqlite3', '--output', remote], timeout=60)
    try:
        with tempfile.TemporaryDirectory(dir=output.parent) as directory:
            staged = Path(directory) / 'snapshot.sqlite3'
            run([service.engine, 'cp', service.name + ':' + remote, str(staged)])
            staged.chmod(0o600)
            with staged.open('rb') as stream:
                os.fsync(stream.fileno())
            os.link(staged, output)
            fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        run([service.engine, 'exec', service.name, PYTHON, '-c',
             'from pathlib import Path; import sys; Path(sys.argv[1]).unlink()', remote])
    return str(output.resolve())


def status(service, backups, max_age_hours):
    code = '''import json, shutil
from pathlib import Path
p = Path('/data/state/tracker.sqlite3')
print(json.dumps(dict(database_bytes=p.stat().st_size, free_bytes=shutil.disk_usage(p.parent).free)))'''
    result = json.loads(run([service.engine, 'exec', service.name, PYTHON, '-c', code]))
    files = [p for p in backups.glob('*.sqlite3') if p.is_file() and not p.is_symlink()]
    latest = max(files, key=lambda p: p.stat().st_mtime, default=None)
    age = time.time() - latest.stat().st_mtime if latest else None
    result.update(backup=str(latest) if latest else None, backup_age_seconds=age,
                  backup_free_bytes=shutil.disk_usage(backups).free)
    minimum = max(3 * result['database_bytes'], 64 * 1024 * 1024)
    result['ok'] = (age is not None and 0 <= age <= max_age_hours * 3600
                    and min(result['free_bytes'], result['backup_free_bytes']) >= minimum)
    return result


def configure(service, config, port):
    """Recreate Docker's runtime; the config candidate gets its own volume."""
    from deployment import LABEL, PROTECTION, healthy, image_command, no_data_users
    from install import PREPARE, config_archive
    if not isinstance(service, Docker):
        raise ValueError('edit the Quadlet for configuration/port changes')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('invalid loopback port')
    image = service.settings['image']
    if config:
        archive = config_archive(config)
        volume = service.name + '-config-' + uuid.uuid4().hex[:12]
        run(['docker', 'volume', 'create', '--label', f'{LABEL}={service.instance}', volume])
        run(['docker', 'run', '--rm', '-i', '--network', 'none', *PROTECTION,
             '--user', '0', '--cap-add=CHOWN', '--mount',
             f'type=volume,src={volume},dst=/config,volume-nocopy', '--entrypoint', PYTHON,
             image, '-c', PREPARE, 'archive', '/config'], input=archive)
        service.settings['config'] = volume
    service.stop()
    try:
        no_data_users(service)
        run(image_command(service, image, '-m', 'tracker.runtime_checks',
                          '--config', '/config/tracker.toml', '--db', '/data/state/tracker.sqlite3'))
        service.stage(image, port=port)
        service.start()
        healthy(service.engine, service.name, image)
    except BaseException:
        service.stop()
        service.rollback()
        raise
    service.finish()
    return dict(container=service.name, config=service.settings['config'], status='ready')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--container')
    target.add_argument('--unit', type=Path)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--output', type=Path, help='create an online backup at this new path')
    operation.add_argument('--status', type=Path, metavar='BACKUP_DIR')
    parser.add_argument('--port', type=int, help='recreate Docker service on this loopback port')
    parser.add_argument('--config', type=Path, help='import reviewed configuration into a new Docker volume')
    parser.add_argument('--max-age-hours', type=float, default=26)
    args = parser.parse_args(argv)
    try:
        if args.port is not None or args.config:
            if args.output or args.status:
                raise ValueError('configure and backup/status are separate operations')
        elif not (args.output or args.status):
            raise ValueError('choose --output, --status, --config or --port')
        service = Docker(args.container) if args.container else Quadlet(args.unit.absolute())
        if args.port is not None or args.config:
            print(json.dumps(configure(service, args.config, args.port), indent=2))
            return 0
        if args.output:
            print(backup(service, args.output.absolute()))
            return 0
        if not args.status.is_dir() or args.max_age_hours <= 0:
            raise ValueError('status requires a backup directory and positive age budget')
        result = status(service, args.status, args.max_age_hours)
        print(json.dumps(result, indent=2))
        return 0 if result['ok'] else 2
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'maintenance failed: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
