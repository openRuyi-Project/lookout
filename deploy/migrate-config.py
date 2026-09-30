#!/usr/bin/env python3
"""Separate an old copied catalog from operator edits using its original baseline."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import tomlkit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from tracker import config
from tracker.monitors.version.rules import same_values


def changed_values(baseline, actual, *, defaults=None):
    result = {}
    for name in baseline.keys() | actual.keys():
        if same_values(baseline.get(name), actual.get(name)):
            continue
        if name in actual:
            result[name] = actual[name]
        elif defaults is not None and name in defaults:
            result[name] = defaults[name]
        else:
            raise ValueError('removed setting needs an explicit override: ' + name)
    return result


def package_changes(baseline, actual):
    neutral = {'compare': '', 'watch': [], 'comparable': True,
               'not_applicable': False, 'track_label': ''}
    result = {}
    for name in baseline.keys() | actual.keys():
        old, current = baseline.get(name, {}), actual.get(name, {})
        values = changed_values({k: v for k, v in old.items() if k != 'monitors'},
                                {k: v for k, v in current.items() if k != 'monitors'}, defaults=neutral)
        monitors = changed_values(old.get('monitors', {}), current.get('monitors', {}),
                                  defaults={key: False for key in old.get('monitors', {})})
        if monitors:
            values['monitors'] = monitors
        if values:
            result[name] = values
    return result


def migrate(baseline, source, destination, *, release=ROOT / 'config'):
    source, baseline, release = (Path(path).resolve() for path in (source, baseline, release))
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination must be new and outside the input directories')
    destination = destination.resolve()
    if any(destination.is_relative_to(path) for path in (source, baseline, release)):
        raise ValueError('destination must be new and outside the input directories')
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in source.rglob('*')):
        raise ValueError('configuration source must contain regular files and directories')
    before = config.load(source / 'tracker.toml')
    original = config.load(baseline / 'tracker.toml')
    supplied = config.load(release / 'tracker.toml')
    if before.get('version_overrides_path') or before.get('package_overrides_path'):
        raise ValueError('configuration already has explicit overrides')
    catalog_files = [Path(before[key]) for key in ('nvpath', 'packages_path', 'distribution_path') if before.get(key)]
    if any(not path.is_relative_to(source) for path in catalog_files):
        raise ValueError('configuration already references an external catalog')
    entries = {name: value for name, value in before['native'].items()
               if not same_values(original['native'].get(name), value)}
    excluded = sorted(original['native'].keys() - before['native'].keys())
    options = changed_values(original['native_options'], before['native_options'])
    if options.get('keyfile'):
        keyfile = Path(os.path.expandvars(os.path.expanduser(options['keyfile'])))
        keyfile = (Path(before['native_options_path']).parent / keyfile).resolve()
        options['keyfile'] = os.path.relpath(keyfile, source) if keyfile.is_relative_to(source) else str(keyfile)
    packages = package_changes(original['packages'], before['packages'])
    distribution = {}
    for section in original['openruyi'].keys() | before['openruyi'].keys():
        values = changed_values(original['openruyi'].get(section, {}), before['openruyi'].get(section, {}))
        if values:
            distribution[section] = values
    document = tomlkit.parse((source / 'tracker.toml').read_text())
    document['collector']['nvchecker_config'] = supplied['nvpath']
    for key, path in (('packages_config', supplied['packages_path']),
                      ('distribution_config', supplied['distribution_path'])):
        if path:
            document[key] = path
        else:
            document.pop(key, None)
    if distribution:
        document['openruyi'] = distribution
    else:
        document.pop('openruyi', None)
    files = {'tracker.toml': tomlkit.dumps(document)}
    if entries or options:
        document['collector']['version_overrides'] = 'version-overrides.toml'
        files['version-overrides.toml'] = tomlkit.dumps({'__config__': options, **entries})
    if excluded:
        document['collector']['exclude_tracks'] = sorted(set(excluded + document['collector'].get('exclude_tracks', [])))
    if packages:
        document['package_overrides'] = 'package-overrides.toml'
        files['package-overrides.toml'] = tomlkit.dumps(packages)
    files['tracker.toml'] = tomlkit.dumps(document)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.catalog-migration-', dir=destination.parent) as temporary:
        prepared = Path(temporary) / 'config'
        shutil.copytree(source, prepared)
        for path in catalog_files:
            (prepared / path.relative_to(source)).unlink()
        for name, text in files.items():
            path = prepared / name
            if name != 'tracker.toml' and path.exists():
                raise ValueError('override destination already exists: ' + name)
            path.write_text(text)
        for path in sorted(prepared.rglob('*'), reverse=True):
            if path.is_dir() and not list(path.iterdir()):
                path.rmdir()
            elif path.is_file():
                path.chmod(0o600)
        prepared.chmod(0o700)
        config.load(prepared / 'tracker.toml')
        for path, loaded in ((source, before), (baseline, original), (release, supplied)):
            config.require_unchanged(loaded, path / 'tracker.toml')
        destination.mkdir(mode=0o700)
        try:
            for path in prepared.iterdir():
                shutil.move(str(path), destination / path.name)
        except Exception:
            shutil.rmtree(destination)
            raise
    return {'destination': str(destination), 'version_overrides': sorted(entries),
            'excluded_tracks': excluded, 'package_overrides': sorted(packages),
            'source_unchanged': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True,
                        help='original release config used to initialize this installation')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(migrate(args.baseline, args.source, args.destination), indent=2))
        return 0
    except (OSError, ValueError) as error:
        print('configuration migration failed: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
