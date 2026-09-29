#!/usr/bin/env python3
"""Upgrade a Docker container or rootless Quadlet; preserve configuration and data."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deployment import (Docker, Quadlet, healthy, image_command, load_image,
                        no_data_users, read_release, run, upgrade_lock, write_exclusive)


def upgrade(directory, unit, backups, *, container=None, apply=False):
    service = Docker(container) if container else Quadlet(unit)
    manifest = read_release(directory)
    result = dict(version=manifest['version'], image=manifest['image'], data=str(service.settings['data']))
    if not apply:
        return {**result, 'status': 'plan'}
    if not backups.is_dir():
        raise ValueError('backup directory must already exist')
    with upgrade_lock(backups / '.upgrade.lock'):
        load_image(service.engine, directory, manifest)
        if isinstance(service, Quadlet):
            service.check()
        old_image = json.loads(run([service.engine, 'image', 'inspect', service.settings['image']]))[0]['Id']
        service.settings['image'] = old_image
        # Missing state is not a fresh install. Query before stopping or changing anything.
        check = ('from pathlib import Path; from tracker import state; '
                 'p=Path("/data/state/tracker.sqlite3"); '
                 'assert p.is_file(), "existing database required"; '
                 'print(p.stat().st_size)')
        # Do not relabel a live Podman :Z mount merely to inspect it.
        if isinstance(service, Quadlet):
            db = service.settings['data'] / 'state/tracker.sqlite3'
            if not db.is_file():
                raise ValueError('existing database required; use initial installation for a new instance')
            size = db.stat().st_size
        else:
            size = int(run(['docker', 'exec', service.name, '/opt/venv/bin/python', '-c', check]))
        if shutil.disk_usage(backups).free < max(size * 3, 64 * 1024 * 1024):
            raise ValueError('insufficient backup space for the stopped-state transaction')
        transaction = backups / ('upgrade-' + manifest['version'] + '-' + uuid.uuid4().hex[:12])
        transaction.mkdir(mode=0o700)
        service.save(transaction)
        meta = {**manifest, 'previous_image': old_image, 'engine': service.engine,
                'container': service.name, 'unit': str(unit) if unit else None,
                'data': str(service.settings['data'])}
        write_exclusive(transaction / 'release.json', json.dumps(meta, indent=2).encode())
        service.stop()
        before = transaction.name + '-before.sqlite3' if container else 'before.sqlite3'
        migrated = transaction.name + '-migration.sqlite3' if container else 'pre-migration.sqlite3'
        try:
            no_data_users(service)
            run(image_command(service, manifest['image'], '-c',
                'from tracker import state; state.recover("/data/state/tracker.sqlite3")'))
            run(image_command(service, manifest['image'], '/app/deploy/backup-snapshot.py',
                '--db', '/data/state/tracker.sqlite3', '--output', '/backup/' + before,
                backup_dir=transaction))
            service.export_backup(manifest['image'], before, transaction / 'before.sqlite3')
            run(image_command(service, manifest['image'], '/app/deploy/migrate-state.py',
                '--db', '/data/state/tracker.sqlite3', '--backup', '/backup/' + migrated,
                backup_dir=transaction))
            run(image_command(service, manifest['image'], '-m', 'tracker.runtime_checks',
                '--config', '/config/tracker.toml', '--db', '/data/state/tracker.sqlite3'))
            service.stage(manifest['image'])
            service.start()
            healthy(service.engine, service.name, manifest['image'])
        except BaseException:
            service.stop()
            no_data_users(service)
            try:
                run(image_command(service, old_image, '-m', 'tracker.runtime_checks',
                    '--config', '/config/tracker.toml', '--db', '/data/state/tracker.sqlite3'))
            except (RuntimeError, subprocess.SubprocessError) as error:
                raise RuntimeError(f'upgrade stopped; old image preflight failed; inspect {transaction}') from error
            service.rollback()
            healthy(service.engine, service.name, old_image)
            raise
        service.finish()
        write_exclusive(transaction / 'result.json', json.dumps({**result, 'status': 'ready'}, indent=2).encode())
        return {**result, 'status': 'ready', 'backup': str(transaction / 'before.sqlite3')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, default=Path(__file__).resolve().parent)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--unit', type=Path)
    target.add_argument('--container')
    parser.add_argument('--backups', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = upgrade(args.release.resolve(), args.unit.absolute() if args.unit else None,
                         args.backups.resolve(), container=args.container, apply=args.apply)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'upgrade failed: {error}', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
