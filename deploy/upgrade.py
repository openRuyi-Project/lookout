#!/usr/bin/env python3
"""Run the selected release's upgrader on the host, without an engine socket mount."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from automation_tools import refresh_tools, stage_tools
from deployment import (Docker, Quadlet, export_image_tree, healthy, resolve_image,
                        run, unchanged_image, validate_catalog_baseline)
from publication import channel_repository, published_image


def confirm_current(service, image, tools_link):
    healthy(service.engine, service.name, image)
    # A completed image switch may outlive a failed host-pointer publication.
    # Recover only its staged tools, without pulling or exporting an image.
    if tools_link and (tools_link.parent / image.removeprefix('sha256:')).is_dir():
        refresh_tools(service.engine, image, tools_link)


def upgrade(reference, unit, backups, *, container=None, apply=False, catalog_baseline=None, workflow=None, tools_link=None):
    if catalog_baseline is not None:
        validate_catalog_baseline(catalog_baseline)
    service = Docker(container) if container else Quadlet(unit)
    result = dict(reference=reference, data=str(service.settings['data']), catalog_baseline=catalog_baseline)
    if not apply:
        return {**result, 'status': 'plan'}
    if not backups.is_dir():
        raise ValueError('backup directory must already exist')
    running = json.loads(run([service.engine, 'inspect', service.name]))[0]
    old_image = 'sha256:' + running['Image'].removeprefix('sha256:')
    expected_revision = None
    if workflow:
        info = json.loads(run([service.engine, 'image', 'inspect', old_image]))[0]
        revision = (info['Config'].get('Labels') or {}).get('org.opencontainers.image.revision')
        selected, expected_revision = published_image(reference, old_image, revision, workflow)
        if selected == old_image and not catalog_baseline:
            confirm_current(service, old_image, tools_link)
            return {**result, 'image': old_image, 'revision': revision, 'status': 'unchanged'}
        try:
            cached = json.loads(run([service.engine, 'image', 'inspect', selected]))[0]
        except RuntimeError:
            cached = None
        if cached:
            if (cached['Config'].get('Labels') or {}).get('org.opencontainers.image.revision') != expected_revision:
                raise ValueError('cached image revision does not match the successful workflow')
            selected = cached['Id']
    else:
        selected = old_image if unchanged_image(service.engine, reference, old_image) else reference
    manifest = resolve_image(service.engine, selected)
    manifest['reference'] = reference
    if expected_revision and manifest['revision'] != expected_revision:
        raise ValueError('published image revision does not match the successful workflow')
    if manifest['image'] == old_image and not catalog_baseline:
        confirm_current(service, old_image, tools_link)
        return {**result, **manifest, 'status': 'unchanged'}
    # Executed code and migration policy come from the same pinned image. A
    # copied host script must not freeze upgrade behavior at installation time.
    with tempfile.TemporaryDirectory(prefix='.release-tools-', dir=backups) as temporary:
        if tools_link:
            tools = stage_tools(service.engine, manifest['image'], tools_link)
        else:
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
        if tools_link:
            refresh_tools(service.engine, manifest['image'], tools_link)
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
    parser.add_argument('--workflow', help='successful GitHub publication workflow for a GHCR main/latest channel')
    parser.add_argument('--tools-link', type=Path, help='stable host automation pointer')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = upgrade(args.image, args.unit.absolute() if args.unit else None,
                         args.backups.resolve(), container=args.container, apply=args.apply,
                         catalog_baseline=args.catalog_baseline, workflow=args.workflow or ('checks.yml' if channel_repository(args.image) else None),
                         tools_link=args.tools_link)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'upgrade failed: {error}', file=sys.stderr)
        return 2
    if not args.quiet or result['status'] != 'unchanged':
        print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
