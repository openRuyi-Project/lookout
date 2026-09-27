"""Fixed cve-bin-tool adapter. No arbitrary executable or shell configuration.

The optional scanner and its database are operator-owned, outside the web image.
Only component lists are scanned; never execute or extract source package inputs.
"""
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

LOCK = threading.Lock()


class ScannerNotInstalled(RuntimeError): pass
class ScannerDatabaseMissing(RuntimeError): pass
class ScannerDatabaseExpired(RuntimeError): pass
class ScannerFailed(RuntimeError): pass


def parse_report(report, settings, version, now=None):
    now = now or datetime.now(timezone.utc)
    updated = datetime.strptime(report['database_info']['last_updated'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    if not -300 <= (now - updated).total_seconds() <= 172800:
        raise ScannerDatabaseExpired('scanner database older than two days')
    tool = report['metadata']['tool']
    if tool['name'] != 'cve-bin-tool':
        raise ValueError('unexpected scanner report')
    result = []
    for group in report['vulnerabilities']['report']:
        for row in group['entries']:
            if (row['vendor'], row['product'], row['version']) != (settings['vendor'], settings['product'], version):
                raise ValueError('scanner subject mismatch')
            identity = row['cve_number']
            if identity == 'UNKNOWN':
                continue
            if not re.fullmatch(r'CVE-\d{4}-\d{4,}', identity):
                raise ValueError('unexpected vulnerability identity')
            result.append({'id': identity, 'aliases': [], 'affected': []})
    return result


def scan(subject, settings):
    if set(settings) != {'vendor', 'product'} or any(
            not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,120}', v) for v in settings.values()):
        raise ValueError('CVE scanner requires explicit vendor/product identity')
    root = Path('/opt/cve')
    home = Path(os.environ.get('TRACKER_CVE_HOME', '/data/cve'))
    if not (root/'cve_bin_tool/cli.py').is_file():
        raise ScannerNotInstalled()
    if not (home/'.cache/cve-bin-tool/cve.db').is_file():
        raise ScannerDatabaseMissing()
    with LOCK, tempfile.TemporaryDirectory(prefix='monitor-cve-') as directory:
        work = Path(directory); data = work/'components.csv'; output = work/'report.json'
        with data.open('w') as f:
            writer = csv.writer(f);writer.writerow(['vendor', 'product', 'version'])
            writer.writerow([settings['vendor'], settings['product'], subject['version']])
        env = {**os.environ, 'PYTHONPATH': str(root), 'HOME': str(home), 'TZ': 'UTC',
               'PYTHONDONTWRITEBYTECODE': '1'}
        command = [sys.executable, '-m', 'cve_bin_tool.cli', '--offline', '--disable-version-check',
                   '--report', '--input-file', str(data), '--format', 'json2', '--output-file', str(output)]
        result = subprocess.run(command, cwd=work, env=env, capture_output=True, timeout=90)
        # cve-bin-tool returns 1 for findings. It is not a command failure if a
        # complete, identity-bound and fresh report exists and validates.
        if result.returncode not in (0, 1) or not output.is_file() or output.stat().st_size > 16 * 1024 * 1024:
            raise ScannerFailed()
        return parse_report(json.loads(output.read_text()), settings, subject['version'])
