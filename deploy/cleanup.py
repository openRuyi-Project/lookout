#!/usr/bin/env python3
"""Remove unused upgrade images recorded by this instance; keep one rollback image."""
import argparse
import json
import re
import sys
from pathlib import Path

from deployment import Docker, Quadlet, instance_lock, run, upgrade_lock

DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


def candidates(service, backups):
    current = json.loads(run([service.engine, 'inspect', service.name]))[0]['Image']
    current = 'sha256:' + current.removeprefix('sha256:')
    protected = {current}
    records = []
    for directory in backups.glob('upgrade-*'):
        if directory.is_symlink() or not directory.is_dir():
            continue
        meta, outcome = directory / 'image.json', directory / 'result.json'
        if meta.is_symlink() or outcome.is_symlink() or not meta.is_file() or not outcome.is_file():
            continue
        record, result = json.loads(meta.read_text()), json.loads(outcome.read_text())
        if record.get('container') != service.name or record.get('engine') != service.engine:
            continue
        if result.get('status') != 'ready':
            continue
        records.append((outcome.stat().st_mtime_ns, record))
    records.sort(key=lambda item: item[0], reverse=True)
    if records:
        protected.add(records[0][1]['previous_image'])
    ids = run([service.engine, 'ps', '-aq']).splitlines()
    if ids:
        for container in json.loads(run([service.engine, 'inspect', *ids])):
            protected.add('sha256:' + container['Image'].removeprefix('sha256:'))
    known = {record[key] for _, record in records for key in ('image', 'previous_image')}
    if any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in known):
        raise ValueError('invalid image identity in upgrade records')
    available = {'sha256:' + line.removeprefix('sha256:') for line in
                 run([service.engine, 'images', '--no-trunc', '--quiet']).splitlines()}
    return sorted((known & available) - protected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--container')
    group.add_argument('--unit', type=Path)
    parser.add_argument('--backups', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        service = Docker(args.container) if args.container else Quadlet(args.unit.absolute())
        with upgrade_lock(instance_lock(service)):
            images = candidates(service, args.backups)
            removed = []
            if args.apply:
                for image in images:
                    # No force: an image acquired by another container stays protected.
                    run([service.engine, 'image', 'rm', image])
                    removed.append(image)
        print(json.dumps({'candidates': images, 'removed': removed}))
    except (OSError, ValueError, RuntimeError, KeyError):
        print('cleanup failed; no volumes or backups were deleted', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
