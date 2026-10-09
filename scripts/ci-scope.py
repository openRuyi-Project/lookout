#!/usr/bin/env python3
"""Select full CI unless every changed path is an explicitly reviewed document."""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

DOCUMENTS = frozenset({
    'README.md', 'CONTRIBUTING.md', 'SECURITY.md', 'ACCESSIBILITY.md',
    'CODE_OF_CONDUCT.md', 'docs/design.md', 'docs/deployment.md',
    'docs/monitor-porting.md',
})


def needs_full(paths, *, release=False):
    return release or not paths or any(path not in DOCUMENTS for path in paths)


def changed_paths(event, *, root=Path('.')):
    base = event.get('pull_request', {}).get('base', {}).get('sha') or event.get('before', '')
    if not re.fullmatch(r'[0-9a-f]{40}', base) or base == '0' * 40:
        return None
    result = subprocess.run(
        ['git', 'diff', '--no-renames', '--name-only', '-z', base, 'HEAD', '--'],
        cwd=root, capture_output=True, check=False,
    )
    if result.returncode:
        return None
    subprocess.run(['git', 'diff', '--check', base, 'HEAD', '--'], cwd=root, check=True)
    return result.stdout.decode('utf-8', errors='surrogateescape').rstrip('\0').split('\0') if result.stdout else []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', action='store_true')
    args = parser.parse_args()
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    paths = changed_paths(event)
    full = needs_full(paths, release=args.release)
    if paths is None:
        subprocess.run(['git', 'show', '--format=', '--check', 'HEAD'], check=True)
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write(f'full={str(full).lower()}\n')
    print('Full checks required.' if full else 'Only allowlisted documentation changed.')


if __name__ == '__main__':
    main()
