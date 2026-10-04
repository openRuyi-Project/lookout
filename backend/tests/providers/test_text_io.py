import httpx
import pytest

from tracker.providers.client import IO


def test_text_uses_shared_bounded_cache_and_separate_json_representation(tmp_path):
    calls = []
    def handle(request):
        calls.append(request.url)
        return httpx.Response(200, text='123')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        owner = IO(tmp_path, client=client)
        io = owner.for_hosts({'proxy.golang.org'})
        url = 'https://proxy.golang.org/example.org/m/@v/list'
        assert io.text(url) == io.text(url) == '123'
        assert io.json('GET', url) == 123
        assert len(calls) == 2
        assert IO(tmp_path, client=client).for_hosts({'proxy.golang.org'}).text(url) == '123'
        assert len(calls) == 2
        with pytest.raises(ValueError, match='outside'):
            io.text('https://not-allowed.example/list')


def test_oversized_text_response_is_rejected():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text='x' * (512 * 1024 + 1)))) as client:
        with pytest.raises(ValueError):
            IO(client=client).for_hosts({'proxy.golang.org'}).text('https://proxy.golang.org/list')


def test_cached_requests_reuse_locks_without_constructing_discarded_ones(monkeypatch):
    import threading

    original = threading.Lock
    created = []
    requests = []

    def lock():
        result = original()
        created.append(result)
        return result

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'ok': True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(threading, 'Lock', lock)
        owner = IO(client=client)
        assert owner.json('GET', 'https://example.org/data') == {'ok': True}
        created.clear()
        for _ in range(20):
            assert owner.json('GET', 'https://example.org/data') == {'ok': True}
            owner.wait_for_host('example.org', 0)
        assert created == []
        assert len(requests) == 1
        owner.close()


@pytest.mark.parametrize('delays', [(3600, 60), (60, 3600), (3600, 'invalid')])
def test_host_cooldowns_cannot_shorten_an_existing_deadline(monkeypatch, delays):
    from tracker.providers import client as provider_client

    now = 100.0
    monkeypatch.setattr(provider_client.time, 'monotonic', lambda: now)
    owner = IO()
    try:
        for delay in delays:
            owner.defer_host('example.org', delay)
        now = 161.0
        with pytest.raises(ValueError, match='cooldown'):
            owner.wait_for_host('example.org', 0)
        owner.wait_for_host('other.example.org', 0)
        now = 3700.0
        owner.wait_for_host('example.org', 0)
        owner.defer_host('example.org', 60)
        assert owner.cooldowns['example.org'] == 3760.0
    finally:
        owner.close()
