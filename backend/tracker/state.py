"""One atomic snapshot, one writer. No ORM, event log, or speculative domain model."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
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


def current_source(snapshot, name):
    """Select the same source observation for display, comparison and monitors.

    A recorded SPEC owns the source version even on failure; do not silently
    substitute OBS's different source. OBS remains the fallback before the first
    SPEC observation, and independently owns build/artifact evidence.
    """
    obs_ttl = snapshot.get('obs_stale_after_seconds', snapshot.get('stale_after_seconds', 86400))
    spec = snapshot.get('specs', {}).get(name)
    if spec is not None:
        metadata = spec.get('metadata') or {}
        provenance = {'head': spec.get('head'), 'native_query': spec.get('native_query'),
                      'origin': spec.get('source_origin')}
        revision = ('spec:' + hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
                    if spec.get('head') or spec.get('native_query', {}).get('spec_sha256') else None)
        value = metadata.get('version')
        ttl = max(obs_ttl, snapshot['spec_interval_seconds'] * 2) if 'spec_interval_seconds' in snapshot else obs_ttl
        return {**spec, 'version': value if usable_version(value) else None,
                'revision': revision, 'origin': 'spec', 'stale_after_seconds': ttl,
                'error': spec.get('error') or snapshot.get('components', {}).get('spec_git', {}).get('error')}
    source = snapshot.get('sources', {}).get(name, {})
    value = source.get('version')
    return {**source, 'version': value if usable_version(value) else None,
            'revision': source.get('srcmd5') or source.get('native_query', {}).get('spec_sha256'),
            'origin': 'obs', 'stale_after_seconds': obs_ttl}

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
    'monitors': frozenset(('monitors', 'monitor_catalog', 'monitor_stale_after_seconds')),
    'builds': frozenset(('builds',)),
}

# Status and history share a displayed build, but have independent writers and
# cadences. Neither writer may replay the other one's old observation.
BUILD_FIELDS = {
    'builds': frozenset(('raw_status', 'details', 'matches_source',
                         'fetched_at', 'attempted_at', 'error')),
    'obs': frozenset(('last_success', 'history_attempted_at', 'history_checked_at',
                     'history_error', 'history_unresolved',
                     'version_attempt_identity', 'version_attempted_at')),
}


def owns_component(phase, key):
    if phase == 'obs':
        return key in ('targets', 'inventory', 'source_index') or key.startswith('build_history:')
    return key == {'upstreams': 'nvchecker', 'specs': 'spec_git', 'builds': 'builds'}.get(phase)


def merge(latest, phase, fields, components=None):
    """Merge only owned fields into the latest snapshot; never accept generation."""
    if phase not in PHASE_FIELDS or set(fields) - PHASE_FIELDS[phase]:
        raise ValueError('snapshot phase field ownership violation')
    components = components or {}
    for key in components:
        if phase == 'monitors':
            raise ValueError('monitor observations own no collection component')
        if not owns_component(phase, key):
            raise ValueError('snapshot component ownership violation')
    result = deepcopy(latest)
    result.update(deepcopy({k: v for k, v in fields.items() if k != 'builds'}),
                  generation=latest['generation'] + 1)
    if phase == 'obs':
        prior = {t['id']: (t['repository'], t['architecture']) for t in latest.get('targets', [])}
        current = {t['id']: (t['repository'], t['architecture']) for t in result.get('targets', [])}
        result['builds'] = {
            name: {tid: fact for tid, fact in builds.items()
                   if tid in current and prior.get(tid) == current[tid]}
            for name, builds in result['builds'].items() if name in result['inventory']
        }
    for name, targets in fields.get('builds', {}).items():
        for tid, facts in targets.items():
            if set(facts) - BUILD_FIELDS[phase]:
                raise ValueError('build observation field ownership violation')
            result['builds'].setdefault(name, {}).setdefault(tid, {}).update(deepcopy(facts))
    result['components'].update(deepcopy(components))
    return result
