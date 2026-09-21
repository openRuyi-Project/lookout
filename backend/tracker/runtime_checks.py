"""Fail-closed checks performed before the supervisor starts children."""
import argparse
import os
import sqlite3
import tempfile
from pathlib import Path

from . import native_spec, state
from .config import load

PROBE = b"""Name: openruyi-runtime-probe
Version: 1.0
Release: 1
Summary: Runtime probe
License: MIT

%description
Runtime probe.
"""


def _writable(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, prefix='.runtime-check-', delete=True):
        pass


def check_runtime(config, db):
    if os.geteuid() == 0:
        raise RuntimeError('runtime must run as a non-root UID')
    data = Path(db)
    _writable(data.parent)
    spec = config.get('spec', {})
    repo = spec.get('repo')
    if repo:
        _writable(Path(repo).parent)
    if data.exists():
        if not data.is_file():
            raise RuntimeError('tracker database is not a regular file')
        try:
            state.read(data)
        except (sqlite3.Error, OSError, ValueError) as error:
            raise RuntimeError(f'tracker database is unreadable: {error}') from error
    if spec.get('repo') or config.get('collector', {}).get('native_spec_fallback'):
        result = native_spec.query(PROBE)
        context = (result.get('native_query') or {}).get('context') or {}
        sandbox = context.get('sandbox') or {}
        if result.get('version') != '1.0' or result.get('version_error') is not None:
            raise RuntimeError('native SPEC runtime probe failed')
        if int(sandbox.get('landlock_abi', 0)) < 6 or sandbox.get('seccomp') != 'allow-list':
            raise RuntimeError('native SPEC sandbox does not meet the runtime contract')
    return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--db', required=True)
    args = parser.parse_args(argv)
    try:
        check_runtime(load(Path(args.config)), args.db)
    except Exception as error:
        print(f'runtime preflight failed: {error}')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
