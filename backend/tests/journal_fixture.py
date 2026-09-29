"""Interrupt a real SQLite writer on disposable test data, without committing."""
import subprocess
import sys


def leave_hot_journal(db):
    subprocess.run([sys.executable, '-c', '''
import json, os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA cache_size=1')
conn.execute('BEGIN EXCLUSIVE')
snapshot = json.loads(conn.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0])
snapshot.get('snapshot', snapshot)['generation'] = 999999
snapshot['uncommitted'] = 'x' * 1000000
conn.execute('UPDATE snapshot SET payload=? WHERE id=1', (json.dumps(snapshot),))
os._exit(0)
''', str(db)], check=True, timeout=10)
