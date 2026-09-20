"""One atomic snapshot, one writer. No ORM, event log, or speculative domain model."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import time

def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def empty():
    return dict(schema=1, generation=0, mode='live', sources={}, tracks={}, builds={}, specs={},
                inventory={}, index={}, components={}, last_attempt=None)

def read(db):
    if not Path(db).is_file():
        return empty()
    with sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True, timeout=10) as conn:
        row = conn.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
    return json.loads(row[0]) if row else empty()

def commit(db, snapshot):
    # Serialization happens before opening the transaction: invalid data cannot replace a snapshot.
    body = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db, timeout=10) as conn:
        # Read-only systemd API mounts cannot recreate WAL/SHM after a writer exits.
        # Short, infrequent writes use rollback journals so closed-writer reads work.
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        conn.execute('INSERT INTO snapshot VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (body,))

@contextmanager
def writer_lock(db, timeout=0):
    lock = Path(str(db) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def success(previous, fields, now):
    return {**previous, **fields, 'fetched_at': now, 'attempted_at': now, 'error': None}

def failure(previous, error, now):
    return {**previous, 'attempted_at': now, 'error': error}

def stale(observation, now, ttl):
    stamp = observation.get('checked_at') or observation.get('fetched_at')
    if not stamp:
        return True
    try:
        age = (now - datetime.fromisoformat(stamp)).total_seconds()
        return age < -300 or age > ttl
    except (ValueError, TypeError):
        return True

def next_stale_change(observation, now, ttl):
    """Next UTC instant where stale() can change without a new observation.

    A timestamp more than 300 seconds ahead becomes valid at stamp - 300.
    Expiry uses > ttl, so the first stale datetime is one microsecond after it.
    Invalid/missing or already-expired timestamps never become fresh forwards.
    """
    stamp = observation.get('checked_at') or observation.get('fetched_at')
    if not stamp:
        return None
    try:
        observed = datetime.fromisoformat(stamp)
        age = (now - observed).total_seconds()
        if age < -300:
            return observed - timedelta(seconds=300)
        if age <= ttl:
            return observed + timedelta(seconds=ttl, microseconds=1)
    except (ValueError, TypeError, OverflowError):
        pass
    return None

def compare(current, latest, comparable=True):
    """Only RPM VERSION; never compare the Epoch or package Release to an upstream tag."""
    if not comparable or not all(usable_version(v) for v in (current, latest)):
        return 'unknown'
    try:
        import rpm
    except ImportError:
        return 'unknown'
    result = rpm.labelCompare(('0', current, '0'), ('0', latest, '0'))
    return 'outdated' if result < 0 else 'ahead' if result > 0 else 'current'

def usable_version(value):
    # OBS's source parser may emit literal MACRO placeholders, not an expanded RPM VERSION.
    return bool(isinstance(value, str) and value not in ('unknown', 'None') and 'MACRO' not in value
                and re.fullmatch(r'[A-Za-z0-9._+~^]+', value))


# Small executable ownership contract. Shared config projections are explicit;
# observations owned by another phase can never be overwritten by a slow job.
PHASE_FIELDS = {
    'obs': frozenset(('last_attempt', 'mode', 'targets', 'bindings', 'native_ids', 'obs',
                      'stale_after_seconds', 'obs_stale_after_seconds', 'build_history_interval_seconds',
                      'inventory', 'index', 'sources', 'builds', 'presentation')),
    'upstreams': frozenset(('tracks', 'native_ids', 'nv_digest', 'bindings')),
    'specs': frozenset(('specs', 'spec_interval_seconds')),
    'monitors': frozenset(('monitors', 'monitor_stale_after_seconds')),
}


def merge(latest, phase, fields, components=None):
    """Merge only owned fields into the latest snapshot; never accept generation."""
    if phase not in PHASE_FIELDS or set(fields) - PHASE_FIELDS[phase]:
        raise ValueError('snapshot phase field ownership violation')
    components = components or {}
    for key in components:
        if phase == 'monitors':
            raise ValueError('monitor observations own no collection component')
        valid = (key == 'nvchecker' if phase == 'upstreams' else key == 'spec_git' if phase == 'specs'
                 else key in ('targets', 'inventory', 'source_index', 'builds') or key.startswith('build_history:'))
        if not valid:
            raise ValueError('snapshot component ownership violation')
    result = deepcopy(latest)
    result.update(deepcopy(fields), generation=latest['generation'] + 1)
    result['components'].update(deepcopy(components))
    return result
