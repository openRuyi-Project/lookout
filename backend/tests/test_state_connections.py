"""Writer transactions commit/rollback before explicitly closing their connection."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tracker import state


class StateConnectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'snapshot.db'
        self.snapshot = state.empty()
        self.snapshot.update(inventory={'widget': 'widget'}, targets=[{'id': 'target'}])
        self.snapshot['builds'] = {'widget': {'target': state.success(
            {}, {'raw_status': 'succeeded'}, '2026-09-25T00:00:00+00:00')}}
        self.snapshot['components']['builds'] = state.success({}, {}, '2026-09-25T00:00:00+00:00')
        state.commit(self.db, self.snapshot)

    def record_connections(self, fail_sql=None):
        connections = []

        class Connection(sqlite3.Connection):
            closed = False

            def execute(self, sql, *args, **kwargs):
                if fail_sql and sql.startswith(fail_sql):
                    raise sqlite3.OperationalError('injected database failure')
                return super().execute(sql, *args, **kwargs)

            def close(self):
                self.closed = True
                super().close()

        connect = sqlite3.connect

        def open_connection(*args, **kwargs):
            connection = connect(*args, factory=Connection, **kwargs)
            connections.append(connection)
            return connection

        return connections, patch.object(state.sqlite3, 'connect', open_connection)

    def assert_closed(self, connections):
        self.assertEqual(len(connections), 1)
        self.assertTrue(connections[0].closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute('SELECT 1')

    def heartbeat(self):
        stamp = '2026-09-25T00:01:00+00:00'
        patches = {'widget': {'target': state.success({}, {'raw_status': 'succeeded'}, stamp)}}
        component = state.success({}, {}, stamp)
        return state.commit_build_heartbeat(self.db, self.snapshot, patches, component)

    def test_snapshot_commit_closes_after_committing(self):
        changed = {**self.snapshot, 'generation': 7}
        connections, recording = self.record_connections()
        with recording:
            state.commit(self.db, changed)
        self.assert_closed(connections)
        self.assertEqual(state.read(self.db), changed)

    def test_snapshot_commit_closes_and_rolls_back_on_error(self):
        changed = {**self.snapshot, 'generation': 7}
        connections, recording = self.record_connections('INSERT OR REPLACE INTO snapshot_clock')
        with recording, self.assertRaises(sqlite3.OperationalError):
            state.commit(self.db, changed)
        self.assert_closed(connections)
        self.assertEqual(state.read(self.db), self.snapshot)

    def test_heartbeat_closes_after_committing(self):
        connections, recording = self.record_connections()
        with recording:
            self.assertTrue(self.heartbeat())
        self.assert_closed(connections)
        actual = state.read(self.db)
        self.assertEqual(actual['components']['builds']['fetched_at'], '2026-09-25T00:01:00+00:00')
        self.assertEqual(actual['generation'], self.snapshot['generation'])

    def test_heartbeat_closes_on_legacy_early_return(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DROP TABLE snapshot_clock')
        connections, recording = self.record_connections()
        with recording:
            self.assertFalse(self.heartbeat())
        self.assert_closed(connections)
        self.assertEqual(state.read(self.db), self.snapshot)

    def test_heartbeat_closes_when_clock_row_is_absent(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM snapshot_clock')
        connections, recording = self.record_connections()
        with recording:
            self.assertFalse(self.heartbeat())
        self.assert_closed(connections)
        self.assertEqual(state.read(self.db), self.snapshot)

    def test_heartbeat_closes_on_database_error(self):
        connections, recording = self.record_connections('UPDATE snapshot_clock')
        with recording, self.assertRaises(sqlite3.OperationalError):
            self.heartbeat()
        self.assert_closed(connections)
        self.assertEqual(state.read(self.db), self.snapshot)

    def test_rejected_heartbeat_opens_no_connection(self):
        before = deepcopy(self.snapshot)
        connections, recording = self.record_connections()
        with recording:
            self.assertFalse(state.commit_build_heartbeat(self.db, self.snapshot, {}, {'error': 'offline'}))
        self.assertEqual(connections, [])
        self.assertEqual(self.snapshot, before)


if __name__ == '__main__':
    unittest.main()
