"""Batched advisory transport does not invent successful empty observations."""
import json

import httpx

from tracker.monitors.schedule import Schedule
from tracker.monitors.security import monitor
from tracker.providers.client import IO


def test_osv_batch_deduplicates_identities_and_details_across_pages():
    calls = []

    def send(request):
        path = request.url.path
        calls.append(path)
        if path.endswith('/querybatch'):
            queries = json.loads(request.content)['queries']
            if len(queries) == 1:
                assert queries[0]['page_token'] == 'next'
                return httpx.Response(200, json={'results': [{}]})
            assert [q['version'] for q in queries] == ['1', '2', '3', '4']
            advisory = {'id': 'OSV-1', 'modified': '2025-01-01T00:00:00Z'}
            return httpx.Response(200, json={'results': [
                {'vulns': [advisory], 'next_page_token': 'next'}, {'vulns': [advisory]}, {}, {},
            ]})
        assert path.endswith('/OSV-1')
        return httpx.Response(200, json={'id': 'OSV-1', 'modified': '2025-01-01T00:00:00Z', 'affected': []})

    jobs = [(name, {'inputs': {'ecosystem': 'PyPI', 'name': 'widget'}, 'subject': {'version': version}},
             {}, Schedule(3600)) for name, version in [('a', '1'), ('duplicate', '1'), ('b', '2'),
                                                       ('c', '3'), ('d', '4')]]
    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        prepared = monitor.prepare(jobs, IO(client=client))
    assert prepared['a'][0] == prepared['duplicate'][0] == prepared['b'][0]
    assert prepared['a'][0][0]['id'] == 'OSV-1'
    assert prepared['c'][0] == prepared['d'][0] == []
    # Four unique single queries + one continuation vs two batches + one detail.
    assert calls == ['/v1/querybatch', '/v1/vulns/OSV-1', '/v1/querybatch']


def test_invalid_batch_never_becomes_an_empty_success():
    jobs = [(name, {'inputs': {'ecosystem': 'PyPI', 'name': name}, 'subject': {'version': '1'}},
             {}, Schedule(3600)) for name in ('a', 'b')]
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'results': []}))) as client:
        prepared = monitor.prepare(jobs, IO(client=client))
    assert all(isinstance(result[0], ValueError) for result in prepared.values())


def test_epss_union_only_once_and_distributes_scores(monkeypatch):
    entries = {
        'a': [{'id': 'CVE-2025-1000', 'aliases': ['CVE-2025-1001']}],
        'b': [{'id': 'CVE-2025-1001', 'aliases': ['CVE-2025-1002']}],
    }
    calls = []

    def send(request):
        assert request.url.host == 'api.first.org'
        cves = request.url.params['cve'].split(',')
        calls.append(cves)
        return httpx.Response(200, json={'data': [
            {'cve': cve, 'epss': '0.1', 'date': '2025-01-02'} for cve in cves]})

    monkeypatch.setattr(monitor, 'load_entries', lambda subject, settings, io: entries[settings['vendor']])
    jobs = [(name, {'inputs': {'vendor': name}, 'subject': {'version': '1'}}, {}, Schedule(3600))
            for name in entries]
    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        prepared = monitor.prepare(jobs, IO(client=client))
    assert calls == [['CVE-2025-1000', 'CVE-2025-1001', 'CVE-2025-1002']]
    assert prepared['a'][2][0] == dict.fromkeys(('CVE-2025-1000', 'CVE-2025-1001'), (0.1, '2025-01-02'))
    assert prepared['b'][2][0] == dict.fromkeys(('CVE-2025-1001', 'CVE-2025-1002'), (0.1, '2025-01-02'))


def test_epss_batches_respect_encoded_list_limit():
    ids = [f'CVE-2025-{number:048d}' for number in range(100)]
    batches = list(monitor.epss_batches(ids))
    assert [cve for batch in batches for cve in batch] == ids
    assert len(batches) > 1
    assert all(len(','.join(batch)) <= 2000 and len(batch) <= 100 for batch in batches)


def test_batch_keeps_completed_rows_when_a_sibling_cannot_finish():
    from types import SimpleNamespace
    for tail in ({'error': 'failed'}, {'results': [{'next_page_token': 'p'}]}, {'results': []}):
        pages = iter([{'results': [{}, {'next_page_token': 'p'}, {'error': 'invalid row'}]}, tail])
        result = monitor.osv_batch([{}, {}, {}], SimpleNamespace(json=lambda *_: next(pages)), None)
        assert result[0] == []
        assert all(isinstance(error, Exception) for error in result[1:])
