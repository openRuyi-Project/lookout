#!/usr/bin/env python3
"""Create a verified, non-overwriting SQLite snapshot backup."""
import argparse
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time


def backup(db, output, timeout_seconds):
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('timeout must be finite and positive')
    src = Path(db).resolve()
    requested = Path(output).absolute()
    # Resolve the parent, not the leaf: even a dangling output link is occupied.
    if requested.exists() or requested.is_symlink():
        raise ValueError('refusing to overwrite backup')
    dst = requested.parent.resolve() / requested.name
    if not src.is_file():
        raise ValueError('source database does not exist')
    if src == dst:
        raise ValueError('source and backup must differ')
    if not dst.parent.is_dir():
        raise ValueError('backup directory does not exist')
    deadline = time.monotonic() + timeout_seconds

    def check_deadline(*_):
        if time.monotonic() >= deadline:
            raise TimeoutError('backup timeout')

    tmp = None
    try:
        with closing(sqlite3.connect(src.as_uri() + '?mode=ro', uri=True,
                                     timeout=min(5, timeout_seconds))) as source:
            fd, name = tempfile.mkstemp(prefix='.snapshot-', dir=dst.parent)
            os.close(fd)
            tmp = Path(name)
            with closing(sqlite3.connect(tmp)) as target:
                source.backup(target, pages=256, progress=check_deadline, sleep=0.01)
                # Bound validation too, rather than only SQLite's lock wait.
                target.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('backup integrity check failed')
                row = target.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
                if row is None:
                    raise ValueError('snapshot row is missing')
                payload = json.loads(row[0])
                if (not isinstance(payload, dict) or type(payload.get('schema')) is not int
                        or payload['schema'] != 1 or type(payload.get('generation')) is not int
                        or payload['generation'] < 0):
                    raise ValueError('snapshot payload is invalid')
                check_deadline()
        with tmp.open('rb') as stream:
            os.fsync(stream.fileno())
        check_deadline()
        os.link(tmp, dst)  # Atomic publish, never replace a concurrent winner.
        tmp.unlink()
        tmp = None
        directory_fd = os.open(dst.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return dst
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--timeout-seconds', type=float, default=30)
    args = parser.parse_args(argv)
    try:
        result = backup(args.db, args.output, args.timeout_seconds)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f'backup failed: {error}', file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
