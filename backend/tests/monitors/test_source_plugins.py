"""Public source contracts, independent of today's package versions/counts."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

from nvchecker.api import AsyncCache, GetVersionError
from nvchecker.core import apply_list_options
from nvchecker_source import anitya_stable, crates_index
import pytest

from tracker.identity import from_native


@pytest.mark.parametrize('rows, expected', [
    ([('1.2.0', False), ('1.3.0', False), ('1.4.0', True), ('2.0.0', False), ('1.9.0-rc.1', False)], '1.3.0'),
    ([('1.2.0+build.1', False)], '1.2.0'),
    ([('1.2.0', True)], None),
    ([('2.0.0', False)], None),
    ([], None),
])
def test_sparse_native_selection(native_check, rows, expected):
    entry = dict(source='crates_index', url='https://index.crates.io/wi/dg/widget',
                 include_regex=r'1\.[0-9]+\.[0-9]+(?:\+[^ ]+)?', from_pattern=r'\+.*$', to_pattern='')
    payload = '\n'.join(json.dumps(dict(name='widget', vers=v, yanked=y)) for v, y in rows)
    native_check(entry, payload, expected)


@pytest.mark.parametrize('payload', [
    'not json', '{}', '[]', '{"vers":null,"yanked":false}',
    '{"vers":"1.0.0","yanked":0}', '{"vers":"1.0.0","yanked":"false"}',
])
def test_malformed_index_is_not_a_valid_empty_observation(native_check, payload):
    native_check(dict(source='crates_index', url='https://index.crates.io/wi/dg/widget'), payload, None)


def test_top_level_yanked_not_nested_text(native_check):
    payload = {'vers': '1.0.0', 'yanked': True, 'extra': {'yanked': False}}
    native_check(dict(source='crates_index', url='https://index.crates.io/wi/dg/widget'), json.dumps(payload), None)


@pytest.mark.parametrize('version, expected', [
    ('v1.2.3', '1.2.3'), ('v2.0.0+incompatible', '2.0.0'),
    ('v0.0.0-20260101000000-abcdef123456', None), ('v2.3.0-rc.1', None),
    ('v1.2.3\n', None), ('v1.2.3+other', None), (None, None),
])
def test_go_proxy_releases(native_check, version, expected):
    entry = dict(source='go_proxy', url='https://proxy.golang.org/example.org/widget/@latest',
                 prefix='v', from_pattern=r'\+incompatible$', to_pattern='')
    native_check(entry, {'Version': version}, expected)


@pytest.mark.parametrize('history, expected', [
    (['4.8.0', '3100'], '4.8.0'), (['v2.0', 'v1.0'], '2.0'), (['R2', 'R1'], 'R2'),
    ([], None), (None, None), ([None], None), ('1.0', None),
])
def test_anitya_order_and_missing_history(native_check, history, expected):
    native_check(dict(source='anitya_stable', url='https://release-monitoring.org/api/v2/versions/?project_id=1', prefix='v'),
                 {'stable_versions': history, 'latest_version': '999.0-rc1'}, expected)


def test_sparse_cache_shared_without_filter_mutation(monkeypatch):
    records = [dict(vers=v, yanked=False) for v in ('2.1.0', '1.3.0', '1.2.0')]
    get = AsyncMock(return_value=SimpleNamespace(body='\n'.join(map(json.dumps, records)).encode()))
    monkeypatch.setattr(crates_index, 'session', SimpleNamespace(get=get))

    async def run():
        cache = AsyncCache()
        rules = [dict(source='crates_index', url='https://index.crates.io/wi/dg/widget'),
                 dict(source='crates_index', cratesio='widget')]
        original = deepcopy(rules)
        a, b = await asyncio.gather(*(crates_index.get_version('name-is-not-identity', rule, cache=cache)
                                      for rule in rules))
        assert a is not b
        assert apply_list_options(a, {'include_regex': r'1\..*'}, 'one') == '1.3.0'
        assert apply_list_options(b, {'include_regex': r'2\..*'}, 'two') == '2.1.0'
        again = await crates_index.get_version('any', rules[1], cache=cache)
        assert again == [row['vers'] for row in records]
        assert rules == original
    asyncio.run(run())
    get.assert_awaited_once_with('https://index.crates.io/wi/dg/widget')


@pytest.mark.parametrize('name, path', [
    ('a', '1/a'), ('ab', '2/ab'), ('abc', '3/a/abc'),
    ('widget', 'wi/dg/widget'), ('my_crate', 'my/_c/my_crate'),
])
def test_crate_identity_reaches_the_real_plugin_transport(monkeypatch, name, path):
    get = AsyncMock(return_value=SimpleNamespace(body=b'{"vers":"1.2.3","yanked":false}\n'))
    monkeypatch.setattr(crates_index, 'session', SimpleNamespace(get=get))

    async def run():
        return await crates_index.get_version('unrelated-track',
                                             dict(source='crates_index', cratesio=name), cache=AsyncCache())

    assert asyncio.run(run()) == ['1.2.3']
    get.assert_awaited_once_with('https://index.crates.io/' + path)


def test_anitya_identity_and_url_share_the_same_request():
    url = 'https://release-monitoring.org/api/v2/versions/?project_id=270'
    payload = {'stable_versions': ['4.8.0', '3100']}
    cache = SimpleNamespace(get_json=AsyncMock(return_value=payload))

    async def run():
        rules = [dict(source='anitya_stable', anitya_id=270), dict(source='anitya_stable', url=url)]
        return [await anitya_stable.get_version('unrelated-track', rule, cache=cache) for rule in rules]

    assert asyncio.run(run()) == [['4.8.0'], ['4.8.0']]
    assert [call.args for call in cache.get_json.await_args_list] == [(url,), (url,)]
    assert payload == {'stable_versions': ['4.8.0', '3100']}


@pytest.mark.parametrize('plugin', [crates_index, anitya_stable])
def test_explicit_url_is_not_reconstructed(plugin, monkeypatch):
    url = 'https://mirror.example/custom?channel=stable&path=%2Fwidget'
    source = plugin.__name__.rsplit('.', 1)[-1]
    get = AsyncMock(return_value=SimpleNamespace(body=b'{"vers":"1.2.3","yanked":false}'))
    cache = AsyncCache()
    if plugin is crates_index:
        monkeypatch.setattr(crates_index, 'session', SimpleNamespace(get=get))
    else:
        get = AsyncMock(return_value={'stable_versions': ['1.2.3']})
        cache.get_json = get

    async def run():
        return await plugin.get_version('unrelated-track', dict(source=source, url=url), cache=cache)

    assert asyncio.run(run()) == ['1.2.3']
    get.assert_awaited_once_with(url)


@pytest.mark.parametrize('source, fields', [
    ('crates_index', {}), ('crates_index', {'cratesio': ''}),
    ('crates_index', {'cratesio': '../SECRET'}), ('crates_index', {'cratesio': True}),
    ('crates_index', {'cratesio': None}),
    ('crates_index', {'cratesio': 'widget', 'url': 'https://mirror.example/SECRET'}),
    ('anitya_stable', {}), ('anitya_stable', {'anitya_id': 0}),
    ('anitya_stable', {'anitya_id': -1}), ('anitya_stable', {'anitya_id': True}),
    ('anitya_stable', {'anitya_id': 'SECRET'}), ('anitya_stable', {'anitya_id': None}),
    ('anitya_stable', {'anitya_id': 270, 'url': 'https://mirror.example/SECRET'}),
])
def test_invalid_source_identity_fails_before_io(source, fields, monkeypatch):
    plugin = crates_index if source == 'crates_index' else anitya_stable
    cache = SimpleNamespace(get=AsyncMock(), get_json=AsyncMock())
    get = AsyncMock()
    monkeypatch.setattr(crates_index, 'session', SimpleNamespace(get=get))
    rule = dict(source=source, **fields)
    original = deepcopy(rule)

    async def run():
        with pytest.raises(GetVersionError) as caught:
            await plugin.get_version('unrelated-track', rule, cache=cache)
        expected = 'invalid Cargo index source' if source == 'crates_index' else 'invalid Anitya stable source'
        assert caught.value.msg == expected

    asyncio.run(run())
    cache.get.assert_not_awaited()
    cache.get_json.assert_not_awaited()
    get.assert_not_awaited()
    assert rule == original


@pytest.mark.parametrize('entry, payload', [
    ({'source': 'crates_index', 'cratesio': 'widget'}, '{"vers":"1.2.3","yanked":false}\n'),
    ({'source': 'anitya_stable', 'anitya_id': 270}, {'stable_versions': ['1.2.3', '3100']}),
])
def test_identity_rule_cli_uses_explicit_loopback_transport(native_check, entry, payload):
    # The fixture removes identity fields to select URL mode; it is not an
    # end-to-end test of production URL derivation (tested at transport above).
    native_check(entry, payload, '1.2.3')


@pytest.mark.parametrize('source, url, expected', [
    ('crates_index', 'https://index.crates.io/wi/dg/widget', {'ecosystem': 'crates.io', 'name': 'widget'}),
    ('go_proxy', 'https://proxy.golang.com.cn/github.com/!team/!widget/v2/@latest', {'ecosystem': 'Go', 'name': 'github.com/Team/Widget/v2'}),
    ('go_proxy', 'https://impostor.example/example.org/widget/@latest', None),
    ('crates_index', 'https://index.crates.io/wrong/widget', None),
])
def test_identity_projection_stays_protocol_bound(source, url, expected):
    assert from_native(dict(source=source, url=url)) == expected
