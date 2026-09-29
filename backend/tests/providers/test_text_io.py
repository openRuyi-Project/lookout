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
