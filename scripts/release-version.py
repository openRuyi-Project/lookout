#!/usr/bin/env python3
"""Validate an immutable release version before building its image."""
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def version(value):
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value):
        raise ValueError("Use MAJOR.MINOR.PATCH without a v prefix")
    return tuple(map(int, value.split('.')))


def validate(value, tags, current_tags):
    proposed = version(value)
    released = []
    for tag in tags:
        if tag.startswith('v'):
            try:
                released.append(version(tag[1:]))
            except ValueError:
                continue
    if released and proposed <= max(released):
        raise ValueError("Release version must exceed existing release tags")
    if any(tag.startswith('v') for tag in current_tags):
        raise ValueError("This commit already has a release tag; use a new commit")


def main():
    tags = subprocess.check_output(['git', 'tag', '--list'], text=True).splitlines()
    project = Path(__file__).resolve().parents[1] / 'backend/pyproject.toml'
    tags.append('v' + tomllib.loads(project.read_text())['project']['version'])
    current = subprocess.check_output(['git', 'tag', '--points-at', 'HEAD'], text=True).splitlines()
    try:
        validate(sys.argv[1], tags, current)
    except (ValueError, IndexError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
