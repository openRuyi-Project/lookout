"""One background producer publishes complete read models; requests never rebuild.

The SQLite snapshot remains the authority. This cache is disposable, carries its
own freshness deadline, and never modifies the database or invokes collectors.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
import time

from . import package_list, state, view

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Prepared:
    snapshot: dict
    index: package_list.PackageList
    collection: dict
    signature: tuple
    revision: str | None
    deadline: datetime | None
    built_at: datetime


class ProjectionCache:
    def __init__(self, db):
        self.db = Path(db)
        self._prepared = None
        self._error = None
        self._last_seen = None
        self._publishing = threading.Lock()
        self._building = threading.Lock()
        self._stop = threading.Event()

    @staticmethod
    def now():
        return datetime.fromtimestamp(time.time(), timezone.utc)

    def refresh(self):
        """Serialize producers, but hold no publication lock during IO/CPU work."""
        with self._building:
            try:
                self._refresh()
            except FileNotFoundError:
                with self._publishing:
                    self._error = ('Snapshot database is unavailable; showing the last prepared snapshot.'
                                   if self._prepared else None)
            except Exception:
                with self._publishing:
                    first_failure = self._error is None
                    self._error = 'Background snapshot refresh failed; showing the last prepared snapshot.'
                if first_failure:
                    LOG.exception('Snapshot projection failed; retaining previous read model')

    def _refresh(self):
        now = self.now()
        old = self._prepared
        st = self.db.stat()
        signature = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
        expired = old is None or (old.deadline is not None and now >= old.deadline)
        backwards = self._last_seen is not None and now < self._last_seen
        self._last_seen = now
        changed = old is None or signature != old.signature
        clock_only = False
        if changed:
            previous = (old.snapshot, old.revision) if old and signature[:2] == old.signature[:2] else None
            snapshot, revision = state.read_cached(self.db, previous)
            clock_only = bool(previous and revision is not None and revision == old.revision)
            if clock_only:
                before = old.snapshot['components'].get('builds', {}).get('fetched_at')
                after = snapshot['components'].get('builds', {}).get('fetched_at')
                clock_only = bool(before and after and datetime.fromisoformat(after) >= datetime.fromisoformat(before))
        else:
            snapshot, revision = old.snapshot, old.revision
        if not snapshot['generation']:
            raise ValueError('No collected snapshot yet')
        if changed or expired or backwards:
            if clock_only and not expired and not backwards:
                rows, collection = view.refresh_build_clock(snapshot, old.index.rows, now)
            else:
                rows, collection = view.project_monitors(snapshot, now)
            prepared = Prepared(snapshot, package_list.PackageList(rows, snapshot['targets']), collection,
                                signature, revision, view.next_transition(snapshot, now), now)
            with self._publishing:
                self._prepared = prepared
                self._error = None
        else:
            with self._publishing:
                self._error = None

    def read(self):
        with self._publishing:
            prepared, error = self._prepared, self._error
        if prepared is None:
            raise ValueError('No prepared snapshot yet')
        now = self.now()
        notice = error
        if not notice and (now < prepared.built_at or
                           (prepared.deadline is not None and now >= prepared.deadline)):
            notice = 'Snapshot freshness is being recalculated; showing the last prepared snapshot.'
        collection = prepared.collection
        if notice:
            collection = {**collection, 'errors': [*collection['errors'], notice], 'projection_notice': notice}
        return prepared.snapshot, prepared.index, collection

    def run(self):
        # One stat per second; payload parsing and projection only on change or
        # a semantic time boundary. A busy provider cannot queue refresh jobs.
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(1)

    def stop(self):
        self._stop.set()
