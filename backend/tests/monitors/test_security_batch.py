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
    assert prepared['a'] == prepared['duplicate'] == prepared['b']
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
