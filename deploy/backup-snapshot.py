#!/usr/bin/env python3
"""Create a verified, non-overwriting SQLite snapshot backup."""
import argparse, json, os, sqlite3, sys, tempfile, time
from pathlib import Path


def fail(message):
    print(message, file=sys.stderr)
    return 2


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--db', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--timeout-seconds', type=float, default=30)
    a = p.parse_args(argv)
    src, dst = Path(a.db).resolve(), Path(a.output).resolve()
    if not src.is_file(): return fail('source database does not exist')
    if src == dst or dst.exists() or dst.is_symlink(): return fail('refusing to overwrite backup')
    if not dst.parent.is_dir(): return fail('backup directory does not exist')
    tmp = None
    try:
        uri = src.as_uri() + '?mode=ro'
        source = sqlite3.connect(uri, uri=True, timeout=min(5, a.timeout_seconds))
        fd, name = tempfile.mkstemp(prefix='.snapshot-', dir=dst.parent)
        os.close(fd); tmp = Path(name); os.chmod(tmp, 0o600)
        target = sqlite3.connect(tmp)
        deadline = time.monotonic() + a.timeout_seconds
        def progress(status, remaining, total):
            if time.monotonic() > deadline: raise TimeoutError('backup timeout')
        source.backup(target, pages=256, progress=progress, sleep=0.01)
        target.commit()
        ok = target.execute('PRAGMA integrity_check').fetchone()[0]
        if ok != 'ok': raise RuntimeError('backup integrity check failed')
        row = target.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
        if not row: raise RuntimeError('snapshot row is missing')
        payload = json.loads(row[0])
        if not isinstance(payload, dict) or payload.get('schema') != 1 or not isinstance(payload.get('generation'), int) or payload['generation'] < 0:
            raise RuntimeError('snapshot payload is invalid')
        target.close(); source.close()
        with tmp.open('rb') as f: os.fsync(f.fileno())
        os.link(tmp, dst); tmp.unlink(); tmp = None
        dfd = os.open(dst.parent, os.O_DIRECTORY); os.fsync(dfd); os.close(dfd)
        print(dst)
        return 0
    except Exception as e:
        return fail(str(e))
    finally:
        if tmp:
            try: tmp.unlink()
            except FileNotFoundError: pass

if __name__ == '__main__': raise SystemExit(main())
