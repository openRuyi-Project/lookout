#!/usr/bin/env python3
"""Run the selected release's upgrader on the host, without an engine socket mount."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from deployment import (Docker, Quadlet, export_image_tree, healthy, resolve_image,
                        run, validate_catalog_baseline)


def upgrade(reference, unit, backups, *, container=None, apply=False, catalog_baseline=None):
    if catalog_baseline is not None:
        validate_catalog_baseline(catalog_baseline)
    service = Docker(container) if container else Quadlet(unit)
    result = dict(reference=reference, data=str(service.settings['data']), catalog_baseline=catalog_baseline)
    if not apply:
        return {**result, 'status': 'plan'}
    if not backups.is_dir():
        raise ValueError('backup directory must already exist')
    manifest = resolve_image(service.engine, reference)
    running = json.loads(run([service.engine, 'inspect', service.name]))[0]
    old_image = 'sha256:' + running['Image'].removeprefix('sha256:')
    if manifest['image'] == old_image and not catalog_baseline:
        healthy(service.engine, service.name, old_image)
        return {**result, **manifest, 'status': 'unchanged'}
    # Executed code and migration policy come from the same pinned image. A
    # copied host script must not freeze upgrade behavior at installation time.
    with tempfile.TemporaryDirectory(prefix='.release-tools-', dir=backups) as temporary:
        tools = Path(temporary) / 'deploy'
        export_image_tree(service.engine, manifest['image'], '/app/deploy', tools)
        worker = tools / 'release-upgrade.py'
        if not worker.is_file():
            raise ValueError('selected image does not provide the release upgrade protocol')
        command = [sys.executable, str(worker), '--image', manifest['image'],
                   '--backups', str(backups), '--apply']
        command += ['--container', container] if container else ['--unit', str(unit)]
        if catalog_baseline:
            command += ['--catalog-baseline', catalog_baseline]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=840)
        if completed.returncode:
            # The worker emits bounded, credential-free deployment errors.
            raise RuntimeError(completed.stderr.strip() or f'release upgrader exited {completed.returncode}')
        updated = json.loads(completed.stdout)
        if updated.get('status') not in ('ready', 'unchanged') or updated.get('image') != manifest['image']:
            raise ValueError('release upgrader did not confirm the selected image')
        return {**updated, 'reference': reference}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--unit', type=Path)
    target.add_argument('--container')
    parser.add_argument('--backups', type=Path, required=True)
    parser.add_argument('--catalog-baseline', help='immutable image that originally supplied the copied catalogs')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--quiet', action='store_true')
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
