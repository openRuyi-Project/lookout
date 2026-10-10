"""Configured lifecycle identities must reach the provider without embedded dates."""
import tomllib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracker.monitors import eol

POLICIES = tomllib.loads(
    (Path(__file__).resolve().parents[3] / 'config/packages.toml').read_text())
LIFECYCLES = [(name, policy['monitors']['eol']) for name, policy in POLICIES.items()
              if policy.get('monitors', {}).get('eol')]


@pytest.mark.parametrize('name,settings', LIFECYCLES, ids=[name for name, _ in LIFECYCLES])
def test_lifecycle_catalog_queries_and_refreshes(name, settings):
    subject = {'version': '91.82.73'}
    cycle = '.'.join(subject['version'].split('.')[:settings['cycle_parts']])
    release = {'name': cycle, 'isEol': False, 'eolFrom': '2030-01-02'}
    calls = []

    def query(method, url):
        calls.append((method, url))
        return {'result': {'releases': [release]}}

    io = SimpleNamespace(today=date(2030, 1, 1), json=query)
    active = eol.check(subject, settings, io)
    assert active['status'] == 'ok' and not active['findings']
    io.today = date(2030, 1, 2)
    expired = eol.check(subject, settings, io)
    assert expired['status'] == 'ok'
    assert [item['label'] for item in expired['findings']] == ['EOL']
    assert eol.query_subject(subject, settings) == {'cycle': cycle}
    assert calls == [('GET', f'https://endoflife.date/api/v1/products/{settings["product"]}/')] * 2
