#!/usr/bin/env python3
"""Read deployment, collection and update status without changing the instance."""
import argparse
import json
import sys
from pathlib import Path

from deployment import PYTHON, Docker, Quadlet, run
from maintain import status

PROBES = '''import json, urllib.request, urllib.error
result = {}
for name, path in [('live', '/livez'), ('ready', '/readyz'), ('collection', '/api/v2/status')]:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080' + path, timeout=5) as r:
            result[name] = {'http': r.status, 'data': json.loads(r.read(4194305))}
    except (OSError, ValueError) as error:
        result[name] = {'error': type(error).__name__}
print(json.dumps(result))
'''


def diagnose(service, backups):
    info = json.loads(run([service.engine, 'inspect', service.name]))[0]
    # Never include Config.Env or raw inspect output: it can contain credentials.
    image = json.loads(run([service.engine, 'image', 'inspect', info['Image']]))[0]
    labels = image['Config'].get('Labels') or {}
    result = {'container': service.name, 'state': info['State'].get('Status'),
              'image': info['Image'], 'version': labels.get('org.opencontainers.image.version'),
              'revision': labels.get('org.opencontainers.image.revision'),
              'ports': info.get('NetworkSettings', {}).get('Ports', {}),
              'credential_configured': bool(service.settings.get('github_env_file'))}
    for key, action in [('probes', lambda: json.loads(run([service.engine, 'exec', service.name, PYTHON, '-c', PROBES]))),
                        ('backup', lambda: status(service, backups, 26))]:
        try:
            result[key] = action()
        except (OSError, ValueError, RuntimeError):
            result[key] = {'error': 'unavailable'}
    checkpoint = backups / 'publication.json'
    if checkpoint.is_file():
        value = json.loads(checkpoint.read_text())
        result['update'] = {k: value[k] for k in ('checked_at', 'next_attempt_at', 'reason') if k in value}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--unit', type=Path)
    group.add_argument('--container')
    parser.add_argument('--backups', type=Path, required=True)
    args = parser.parse_args()
    try:
        service = Docker(args.container) if args.container else Quadlet(args.unit.absolute())
        print(json.dumps(diagnose(service, args.backups), indent=2))
    except (OSError, ValueError, RuntimeError, KeyError):
        print('diagnosis failed: check service owner, unit and engine access', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
