#!/usr/bin/env python3
"""Upgrade a registry image; preserve the instance's configuration and data."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalog_upgrade import CATALOG_STATUS, prepare
from deployment import (Docker, Quadlet, healthy, image_command, instance_lock,
                        no_data_users, resolve_image, run, upgrade_lock,
                        validate_catalog_baseline, write_exclusive)


def upgrade(reference, unit, backups, *, container=None, apply=False, catalog_baseline=None):
    if catalog_baseline is not None:
        validate_catalog_baseline(catalog_baseline)
    service = Docker(container) if container else Quadlet(unit)
    result = dict(reference=reference, data=str(service.settings['data']), catalog_baseline=catalog_baseline)
    if not apply:
        return {**result, 'status': 'plan'}
    if not backups.is_dir():
        raise ValueError('backup directory must already exist')
    with upgrade_lock(instance_lock(service)):
        if isinstance(service, Quadlet):
            service.check()
        # Capture the running identity before pulling a mutable registry channel.
        running = json.loads(run([service.engine, 'inspect', service.name]))[0]
        if running.get('State', {}).get('Status') != 'running':
            raise ValueError('instance must be running before upgrade')
        old_image = running['Image']
        if not old_image.startswith('sha256:'):
            old_image = 'sha256:' + old_image
        service.settings['image'] = old_image
        manifest = resolve_image(service.engine, reference)
        result.update(version=manifest['version'], image=manifest['image'], revision=manifest['revision'])
        pending_catalog = False
        if catalog_baseline and manifest['image'] == old_image:
            pending_catalog = json.loads(run([service.engine, 'exec', service.name,
                                              '/opt/venv/bin/python', '-c', CATALOG_STATUS]))['local']
        if manifest['image'] == old_image and not pending_catalog:
            healthy(service.engine, service.name, old_image)
            return {**result, 'status': 'unchanged'}
        check = ('from pathlib import Path; '
                 'p=Path("/data/state/tracker.sqlite3"); '
                 'assert p.is_file(), "existing database required"; '
                 'print(p.stat().st_size)')
        # Inspect host files rather than relabelling a live Podman :Z mount.
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
        write_exclusive(transaction / 'image.json', json.dumps(meta, indent=2).encode())
        previous_config = service.settings['config']
        before = transaction.name + '-before.sqlite3' if container else 'before.sqlite3'
        migrated = transaction.name + '-migration.sqlite3' if container else 'pre-migration.sqlite3'
        try:
            service.stop()
            no_data_users(service)
            run(image_command(service, old_image, '-c',
                'from tracker import state; state.recover("/data/state/tracker.sqlite3")'))
            run(image_command(service, old_image, '/app/deploy/backup-snapshot.py',
                '--db', '/data/state/tracker.sqlite3', '--output', '/backup/' + before,
                backup_dir=transaction))
            service.export_backup(old_image, before, transaction / 'before.sqlite3')
            run(image_command(service, manifest['image'], '/app/deploy/migrate-state.py',
                '--db', '/data/state/tracker.sqlite3', '--backup', '/backup/' + migrated,
                backup_dir=transaction))
            prepared, catalogs = prepare(service, manifest['image'], transaction,
                                         catalog_baseline or service.settings.get('catalog_image'))
            write_exclusive(transaction / 'catalogs.json', json.dumps(catalogs, indent=2).encode())
            service.settings['config'] = prepared
            result['catalogs'] = catalogs
            run(image_command(service, manifest['image'], '-m', 'tracker.runtime_checks',
                '--config', '/config/tracker.toml', '--db', '/data/state/tracker.sqlite3'))
            service.stage(manifest['image'])
            service.start()
            healthy(service.engine, service.name, manifest['image'])
        except BaseException as failure:
            try:
                service.stop()
                no_data_users(service)
                service.settings['config'] = previous_config
                run(image_command(service, old_image, '-m', 'tracker.runtime_checks',
                    '--config', '/config/tracker.toml', '--db', '/data/state/tracker.sqlite3'))
                service.rollback()
                healthy(service.engine, service.name, old_image)
            except Exception as error:
                write_exclusive(transaction / 'result.json', b'{"status":"stopped","rollback":"failed"}\n')
                raise RuntimeError(f'upgrade stopped; rollback/preflight failed; inspect {transaction}') from error
            write_exclusive(transaction / 'result.json', b'{"status":"failed","rollback":"ready"}\n')
            raise failure
        service.finish()
        write_exclusive(transaction / 'result.json', json.dumps({**result, 'status': 'ready'}, indent=2).encode())
        return {**result, 'status': 'ready', 'backup': str(transaction / 'before.sqlite3')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='registry channel, digest, or tested local image ID')
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--unit', type=Path)
    target.add_argument('--container')
    parser.add_argument('--backups', type=Path, required=True)
    parser.add_argument('--catalog-baseline', help='immutable original installation image for copied catalogs')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--quiet', action='store_true', help='suppress unchanged-image output')
    args = parser.parse_args(argv)
    try:
        result = upgrade(args.image, args.unit.absolute() if args.unit else None,
                         args.backups.resolve(), container=args.container, apply=args.apply,
                         catalog_baseline=args.catalog_baseline)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'upgrade failed: {error}', file=sys.stderr)
        return 2
    if not args.quiet or result['status'] != 'unchanged':
        print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
