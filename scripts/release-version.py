#!/usr/bin/env python3
"""Select the next release version from the project version and stable tags."""
import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def version(value):
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value):
        raise ValueError("Use MAJOR.MINOR.PATCH without a v prefix")
    return tuple(map(int, value.split('.')))


def release_versions(tags):
    for tag in tags:
        if tag.startswith('v'):
            try:
                yield version(tag[1:])
            except ValueError:
                continue


def validate(value, tags, current_tags):
    proposed = version(value)
    released = list(release_versions(tags))
    if released and proposed <= max(released):
        raise ValueError("Release version must exceed existing release tags")
    if any(tag.startswith('v') for tag in current_tags):
        raise ValueError("This commit already has a release tag; use a new commit")


def next_version(bump, project_version, tags, current_tags):
    if bump not in ('major', 'minor', 'patch'):
        raise ValueError("Choose patch, minor, or major")
    base = max([version(project_version), *release_versions(tags)])
    parts = list(base)
    index = ('major', 'minor', 'patch').index(bump)
    parts[index] += 1
    parts[index + 1:] = [0] * (2 - index)
    selected = '.'.join(map(str, parts))
    validate(selected, tags, current_tags)
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bump', choices=('patch', 'minor', 'major'))
    args = parser.parse_args()
    tags = subprocess.check_output(['git', 'tag', '--list'], text=True).splitlines()
    project = Path(__file__).resolve().parents[1] / 'backend/pyproject.toml'
    project_version = tomllib.loads(project.read_text())['project']['version']
    current = subprocess.check_output(['git', 'tag', '--points-at', 'HEAD'], text=True).splitlines()
    try:
        selected = next_version(args.bump, project_version, tags, current)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(selected)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
