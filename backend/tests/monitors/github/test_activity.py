import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from tracker import state, storage
from tracker.monitors.github.collector import collect, normalize, synchronize
from tracker.monitors.github.model import Matcher, Repository, Settings, settings
from tracker.providers.github import BudgetExhausted, RateLimited
from tracker.readmodel import activity

NOW = '2026-01-01T12:00:00+00:00'
REPO = 'owner/packages'


def raw(number=1, *, pr=False, **changes):
    result = dict(id=number + 100, number=number, title='foo3: update', body='', state='open',
                  state_reason=None, updated_at=NOW, created_at=NOW, closed_at=None, labels=[], user={'login': 'contributor'})
    if pr:
        result['pull_request'] = {}
    return {**result, **changes}


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, path, *, etag=None):
        self.calls.append((path, etag))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response, 'etag'


def detail(**changes):
    return dict(state='open', draft=False, merged_at=None, base={'sha': 'base'},
                head={'sha': 'head'}, changed_files=1, **changes)


def test_path_mapping_renames_and_prose(snapshot):
    matcher = Matcher(snapshot, Repository(aliases={'foo3': ['alias-foo']}, ambiguous_names=['unknown']))
    links = matcher.match(dict(paths=['SPECS/foo4/a.spec', 'SPECS/foo3/old.spec',
                                     'OTHER/binutils/x', 'SPECS/../binutils/x'],
                              title='not-foo3 alias-foo', body='unknown, `binutils`', labels=[]))
    assert set(links) == {'foo3', 'foo4', 'binutils'}
    assert {'kind': 'changed_path', 'value': 'SPECS/foo3/old.spec'} in links['foo3']
    assert matcher.match(dict(title='unknown: crash', body=''))['unknown']
    assert matcher.match(dict(title='foobar3 unknown', body='')) == {}
    assert matcher.match(dict(labels=['package:unknown']))['unknown']


def test_ambiguous_alias_does_not_guess(snapshot):
    matcher = Matcher(snapshot, Repository(aliases={'foo3': ['common'], 'foo4': ['common']}))
    assert matcher.match(dict(title='common')) == {}


def test_pr_states_and_rename():
    client = Client([detail(), [{'filename': 'SPECS/foo3/new', 'previous_filename': 'SPECS/foo4/old'}], detail()])
    item = normalize(raw(pr=True), REPO, None, client)
    assert item['paths'] == ['SPECS/foo3/new', 'SPECS/foo4/old']
    assert item['status'] == 'open' and item['paths_complete']
    changed = detail()
    changed.update(state='closed', merged_at=NOW)
    updated = normalize(raw(pr=True, state='closed'), REPO, item, Client([changed]))
    assert updated['status'] == 'merged' and updated['paths'] == item['paths']


def test_reappearance_and_full_refresh():
    old = normalize(raw(pr=True), REPO, None, Client([detail(), [{'filename': 'SPECS/foo3/a'}], detail()]))
    old['available'] = False
    assert normalize(raw(pr=True), REPO, old, Client([]))['available']
    newer = detail()
    newer['base'] = {'sha': 'newbase'}
    updated = normalize(raw(pr=True), REPO, old, Client([newer, [{'filename': 'SPECS/foo4/a'}], newer]), refresh=True)
    assert updated['paths'] == ['SPECS/foo4/a']


def test_incomplete_paths_stay_explicit():
    response = detail()
    response['changed_files'] = 4000
    item = normalize(raw(pr=True), REPO, None, Client([response, [{'filename': 'SPECS/foo3/a'}], response]))
    assert not item['paths_complete']


def test_checkpoint_304_and_overlap():
    items, checkpoint = synchronize(REPO, {}, {}, Client([[raw()], [raw()]]), Settings(), NOW)
    assert checkpoint['since'] == '2026-01-01T11:58:00+00:00'
    assert checkpoint['fetched_at'] == NOW and len(items) == 1
    client = Client([None])
    retained, updated = synchronize(REPO, items, checkpoint, client, Settings(), '2026-01-01T12:05:00+00:00')
    assert retained == items and updated['fetched_at'].endswith('12:05:00+00:00')
    assert client.calls[0][1] == 'etag'


def test_budget_replays_page_without_repeating_completed_pr():
    responses = [[raw()], [raw(1, pr=True), raw(2, pr=True)], detail(),
                 [{'filename': 'SPECS/foo3/a'}], detail(), BudgetExhausted()]
    items, checkpoint = synchronize(REPO, {}, {}, Client(responses), Settings(), NOW)
    assert list(items) == ['101'] and checkpoint['page'] == 1
    client = Client([detail(), [{'filename': 'SPECS/foo4/a'}], detail()])
    items, checkpoint = synchronize(REPO, items, checkpoint, client, Settings(), NOW)
    assert set(items) == {'101', '102'} and 'page' not in checkpoint
    assert len(client.calls) == 3


def test_failure_cooldown_and_retention():
    items, checkpoint = synchronize(REPO, {}, {}, Client([[raw()], [raw()]]), Settings(), NOW)
    retained, failed = synchronize(REPO, items, checkpoint, Client([RateLimited('2026-01-01T13:00:00Z')]), Settings(), NOW)
    assert retained == items and failed['fetched_at'] == NOW and failed['error']
    client = Client([])
    assert synchronize(REPO, retained, failed, client, Settings(), NOW) == (retained, failed)
    assert not client.calls


def test_full_reconcile_marks_removed_without_deleting():
    old = normalize(raw(), REPO, None, Client([]))
    items, checkpoint = synchronize(REPO, {'101': old}, {}, Client([[], []]), Settings(), NOW)
    assert items['101']['available'] is False and checkpoint['reconciled_at'] == NOW


def test_collection_row_reuse_and_version_independence(config, snapshot, tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    config['github'] = {'repositories': {REPO: {'source_root': 'SPECS'}}}
    monkeypatch.setattr('tracker.config.require_unchanged', lambda _: None)
    state.commit(db, snapshot)
    initial = collect(config, db, client=Client([[raw()], [raw()]]), now=NOW)
    with closing(sqlite3.connect(db)) as conn, conn:
        revision = conn.execute("SELECT revision FROM snapshot_records WHERE section='github_items'").fetchone()[0]
    updated = deepcopy(initial)
    updated['sources']['foo3']['version'] = '99.0'
    state.commit(db, updated)
    result = collect(config, db, client=Client([None]), now='2026-01-01T12:05:00+00:00')
    assert result['github_items'] == initial['github_items']
    assert result['github_links'] == initial['github_links']
    with closing(sqlite3.connect(db)) as conn, conn:
        assert conn.execute("SELECT revision FROM snapshot_records WHERE section='github_items'").fetchone()[0] == revision


def populated(snapshot, count=45):
    snapshot['github_repositories'] = [REPO]
    snapshot['github_items'] = {REPO: {str(i): normalize(raw(i), REPO, None, Client([])) for i in range(1, count+1)}}
    snapshot['github_links'] = {'foo3': {str(i): {'repository': REPO, 'id': str(i), 'evidence': []} for i in range(1, count+1)}}
    snapshot['components']['github:' + REPO] = {'fetched_at': state.utcnow(), 'stale_after_seconds': 1800}
    return snapshot


def test_keyset_pages_and_package_scope(snapshot):
    populated(snapshot)
    first = activity.page(snapshot, 'foo3', 'issue')
    second = activity.page(snapshot, 'foo3', 'issue', first['next_cursor'])
    last = activity.page(snapshot, 'foo3', 'issue', second['next_cursor'])
    assert [len(p['items']) for p in (first, second, last)] == [20, 20, 5]
    assert len({i['id'] for p in (first, second, last) for i in p['items']}) == 45
    assert last['next_cursor'] is None
    with pytest.raises(ValueError):
        activity.page(snapshot, 'foo4', 'issue', first['next_cursor'])
    with pytest.raises(ValueError):
        activity.page(snapshot, 'foo3', 'issue', 'not-a-cursor')
    assert all('body' not in i and 'paths' not in i for i in first['items'])


def test_v2_migration_preserves_observations(snapshot, tmp_path):
    db = tmp_path / 'old.db'
    state.commit(db, snapshot)
    with closing(sqlite3.connect(db)) as conn, conn:
        header = json.loads(conn.execute('SELECT payload FROM snapshot').fetchone()[0])
        header['storage'] = 2
        conn.execute('UPDATE snapshot SET payload=?', (json.dumps(header),))
        conn.execute('PRAGMA user_version=2')
        records = conn.execute('SELECT * FROM snapshot_records ORDER BY section,subject,slot').fetchall()
    before = state.read(db)
    assert storage.migrate(db)
    assert state.read(db) == before
    assert not storage.migrate(db)
    with closing(sqlite3.connect(db)) as conn, conn:
        assert conn.execute('SELECT * FROM snapshot_records ORDER BY section,subject,slot').fetchall() == records


def test_invalid_config():
    with pytest.raises(ValueError):
        settings({'github': {'repositories': {'bad/repo/name': {}}}})
    with pytest.raises(ValueError):
        settings({'github': {'repositories': {REPO: {'source_root': '../SPECS'}}}})


def test_projection_and_api(snapshot, tmp_path, monkeypatch):
    from tests.conftest import ProjectedClient
    from tracker.api import create_app
    populated(snapshot)
    monkeypatch.setattr(state, 'compare', lambda *_: 'current')
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    with ProjectedClient(create_app(db)) as client:
        result = client.get('/api/v2/packages/foo3').json()
        assert result['monitors']['github']['data']['labels'][0]['count'] == 45
        listing = client.get('/api/ui/packages?Issue=AND').json()
        assert listing['total'] == 1
        assert any(v['text'] == 'Issue 45' for v in listing['table']['rows'][0]['cells'][0]['lines'][0])
        document = client.get('/api/ui/packages/foo3').json()
        history = next(s for s in document['sections'] if s['id'] == 'github-issue')
        assert len(history['entries']) == 20 and history['more']
        assert client.get('/api/v2/packages/foo3/activity?kind=issue&per_page=51').status_code == 422
        assert client.get('/api/v2/packages/foo3/activity?kind=issue').json()['total'] == 45

@pytest.mark.parametrize('code,headers', [(429, {'Retry-After': '120'}), (403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1'})])
def test_http_rate_limit(code, headers, monkeypatch):
    import httpx

    from tracker.providers.client import IO
    from tracker.providers.github import Client as HTTPClient
    monkeypatch.setenv('LOOKOUT_GITHUB_TOKEN', 'secret-fixture')
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(code, headers=headers)
    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False) as transport:
        io = IO(client=transport, workers=1)
        monkeypatch.setattr(io, 'wait_for_host', lambda *_: None)
        client = HTTPClient(2, io=io)
        with pytest.raises(RateLimited) as error:
            client.get('/repos/owner/repo/issues')
        assert 'secret' not in str(error.value)
        assert datetime.fromisoformat(error.value.retry_at) > datetime.now(UTC)
        assert calls[0].headers['Authorization'] == 'Bearer secret-fixture'
        with pytest.raises(RateLimited):
            client.get('/repos/other/repo/issues')
        assert len(calls) == 1


def test_http_conditional_and_redirect(monkeypatch):
    import httpx

    from tracker.providers.client import IO
    from tracker.providers.github import Client as HTTPClient
    responses = iter([httpx.Response(304), httpx.Response(302, headers={'Location': 'https://evil.invalid'})])
    with httpx.Client(transport=httpx.MockTransport(lambda _: next(responses)), follow_redirects=False) as transport:
        io = IO(client=transport, workers=1)
        monkeypatch.setattr(io, 'wait_for_host', lambda *_: None)
        client = HTTPClient(2, io=io)
        assert client.get('/repos/owner/repo/issues', etag='known') == (None, 'known')
        with pytest.raises(httpx.HTTPStatusError):
            client.get('/repos/owner/repo/issues')


def test_disable_collection_hides_links_without_erasing_history(config, snapshot, tmp_path, monkeypatch):
    populated(snapshot)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    monkeypatch.setattr('tracker.config.require_unchanged', lambda _: None)
    result = collect(config, db, client=Client([]), now=NOW)
    assert result['github_items'] == snapshot['github_items']
    assert result['github_repositories'] == [] and result['github_links'] == {}
    assert 'github:' + REPO not in result['components']
    assert collect(config, db, client=Client([]), now=NOW) == result


def test_watermark_uses_provider_clock():
    _, checkpoint = synchronize(REPO, {}, {}, Client([[raw()], [raw()]]), Settings(), '2026-01-03T12:00:00Z')
    assert checkpoint['since'] == '2026-01-01T11:58:00+00:00'


def test_author_is_observed_and_changes_invalidate_normalization():
    first = normalize(raw(), REPO, None, Client([]))
    assert first['author'] == 'contributor'
    changed = normalize(raw(user={'login': 'renamed'}), REPO, first, Client([]))
    assert changed['author'] == 'renamed'
    assert changed['source_key'] != first['source_key']
    deleted = normalize(raw(user=None), REPO, changed, Client([]))
    assert deleted['author'] is None


def test_activity_byline_keeps_title_and_author_without_repository():
    from tracker.presentation.activity import section
    item = normalize(raw(title='widget: maintenance 7'), REPO, None, Client([]))
    view = section('widget', 'issue', {'items': [item], 'total': 1, 'next_cursor': None})
    assert view.entries[0].heading[1].text == 'widget: maintenance 7'
    values = view.entries[0].fields[0].values
    assert values[0].text == 'contributor'
    assert all(value.text != REPO for value in values)
    assert not view.collapsible


def test_issue_processed_before_expensive_pr():
    client = Client([[raw()], [raw(1, pr=True), raw(2)], BudgetExhausted()])
    items, checkpoint = synchronize(REPO, {}, {}, client, Settings(), NOW)
    assert set(items) == {'102'}
    assert checkpoint['batch'] and not checkpoint.get('fetched_at')


def test_pr_file_cursor_survives_budget_without_repeating_detail():
    info = detail()
    info['changed_files'] = 201
    files = [{'filename': f'SPECS/foo3/{i}'} for i in range(100)]
    client = Client([[raw()], [raw(pr=True)], info, files, BudgetExhausted()])
    items, checkpoint = synchronize(REPO, {}, {}, client, Settings(), NOW)
    assert not items and checkpoint['pr_progress']['file_page'] == 2
    resumed = Client([[{'filename': f'SPECS/foo3/{i}'} for i in range(100, 200)], [{'filename': 'SPECS/foo4/a'}], info])
    items, checkpoint = synchronize(REPO, items, checkpoint, resumed, Settings(), NOW)
    assert len(items['101']['paths']) == 201 and items['101']['paths_complete']
    assert len(resumed.calls) == 3 and 'page=2' in resumed.calls[0][0]
    assert 'pr_progress' not in checkpoint and 'batch' not in checkpoint


def test_idle_linear_backoff_resets_only_on_changed_data():
    options = Settings()
    items, checkpoint = synchronize(REPO, {}, {}, Client([[], []]), options, NOW)
    assert checkpoint['poll_delay'] == 600
    items, checkpoint = synchronize(REPO, items, checkpoint, Client([None]), options, NOW)
    assert checkpoint['poll_delay'] == 1200
    items, failed = synchronize(REPO, items, checkpoint, Client([OSError('offline')]), options, NOW)
    assert failed['poll_delay'] == 1200 and failed['error']
    items, checkpoint = synchronize(REPO, items, failed, Client([[raw()], [raw()]]), options, NOW)
    assert checkpoint['poll_delay'] == 600 and items['101']['number'] == 1


def test_old_issue_closure_is_an_incremental_change():
    items, checkpoint = synchronize(REPO, {}, {}, Client([[raw(10)], [raw(10)]]), Settings(), NOW)
    changed = raw(10, state='closed', state_reason='completed', updated_at='2026-01-02T12:00:00Z')
    client = Client([[changed], [changed]])
    items, checkpoint = synchronize(REPO, items, checkpoint, client, Settings(), '2026-01-02T12:00:00Z')
    assert items['110']['status'] == 'completed' and checkpoint['poll_delay'] == 600
    assert 'since=' in client.calls[1][0]


def test_anonymous_budget_and_successful_quota_reserve(monkeypatch):
    import httpx
    from tracker.providers.client import IO
    from tracker.providers.github import Client as HTTPClient
    monkeypatch.delenv('LOOKOUT_GITHUB_TOKEN', raising=False)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=[], headers={'X-RateLimit-Remaining': '2', 'X-RateLimit-Reset': '2000000000'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        io = IO(client=transport, workers=1)
        monkeypatch.setattr(io, 'wait_for_host', lambda *_: None)
        client = HTTPClient(60, io=io)
        assert client.remaining == 60 and client.interval_floor == 600
        assert client.get('/repos/owner/repo/issues')[0] == []
        with pytest.raises(RateLimited):
            client.get('/repos/owner/repo/issues')
        assert len(calls) == 1


def test_scheduled_poll_does_not_issue_requests_early(config, snapshot, tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    config['github'] = {'repositories': {REPO: {}}}
    monkeypatch.setattr('tracker.config.require_unchanged', lambda _: None)
    state.commit(db, snapshot)
    collect(config, db, client=Client([[], []]), now=NOW)
    client = Client([])
    collect(config, db, client=client, now='2026-01-01T12:09:59Z')
    assert not client.calls
    client = Client([None])
    result = collect(config, db, client=client, now='2026-01-01T12:10:00Z')
    checkpoint = result['components']['github:' + REPO]
    assert len(client.calls) == 1 and checkpoint['poll_delay'] == 1200
    assert checkpoint['stale_after_seconds'] > checkpoint['poll_delay']


def test_force_push_during_paginated_diff_discards_mixed_paths():
    info = detail()
    info['changed_files'] = 101
    progress = {}
    client = Client([info, [{'filename': f'SPECS/foo3/{i}'} for i in range(100)], BudgetExhausted()])
    with pytest.raises(BudgetExhausted):
        normalize(raw(pr=True), REPO, None, client, progress=progress)
    moved = detail()
    moved['head'] = {'sha': 'moved'}
    with pytest.raises(BudgetExhausted):
        normalize(raw(pr=True), REPO, None, Client([[{'filename': 'SPECS/foo4/new'}], moved]), progress=progress)
    assert progress == {}
    item = normalize(raw(pr=True), REPO, None, Client([moved, [{'filename': 'SPECS/foo4/new'}], moved]), progress=progress)
    assert item['paths'] == ['SPECS/foo4/new'] and item['diff_key'][1] == 'moved'


def test_validation_pause_does_not_refetch_completed_file_pages():
    progress = {}
    with pytest.raises(BudgetExhausted):
        normalize(raw(pr=True), REPO, None, Client([detail(), [{'filename': 'SPECS/foo3/a'}], BudgetExhausted()]), progress=progress)
    assert progress['files_done']
    closed = detail()
    closed.update(state='closed', merged_at=NOW)
    client = Client([closed])
    item = normalize(raw(pr=True), REPO, None, client, progress=progress)
    assert item['status'] == 'merged' and item['paths_complete'] and len(client.calls) == 1


def test_weekly_reconcile_checks_open_pr_base_without_refreshing_closed_pr():
    opened = normalize(raw(pr=True), REPO, None, Client([detail(), [{'filename': 'SPECS/foo3/a'}], detail()]))
    info = detail()
    info['base'] = {'sha': 'newbase'}
    items, checkpoint = synchronize(REPO, {'101': opened}, {}, Client([[raw(pr=True)], [raw(pr=True)], info,
        [{'filename': 'SPECS/foo4/a'}], info]), Settings(), NOW)
    assert items['101']['paths'] == ['SPECS/foo4/a'] and checkpoint['reconciled_at'] == NOW
    closed = raw(pr=True, state='closed')
    closed_info = detail()
    closed_info.update(state='closed')
    old = normalize(closed, REPO, None, Client([closed_info, [{'filename': 'SPECS/foo3/a'}], closed_info]))
    client = Client([[closed], [closed]])
    items, _ = synchronize(REPO, {'101': old}, {}, client, Settings(), NOW)
    assert items['101'] == old and len(client.calls) == 2


def test_full_pages_use_creation_order_and_round_start_watermark():
    latest = raw(updated_at='2026-01-01T11:00:00Z')
    during = raw(2, updated_at='2026-01-01T12:00:00Z')
    client = Client([[latest], [latest, during]])
    _, checkpoint = synchronize(REPO, {}, {}, client, Settings(), NOW)
    assert 'sort=created' in client.calls[1][0]
    assert checkpoint['since'] == '2026-01-01T10:58:00+00:00'


def test_authenticated_idle_poll_keeps_fixed_interval():
    client = Client([[], []])
    client.interval_floor = 0
    items, checkpoint = synchronize(REPO, {}, {}, client, Settings(interval_seconds=300), NOW)
    again = Client([None])
    again.interval_floor = 0
    _, checkpoint = synchronize(REPO, items, checkpoint, again, Settings(interval_seconds=300), NOW)
    assert checkpoint['poll_delay'] == 300


def test_anonymous_scheduler_has_global_ten_minute_floor():
    from tracker.monitors.github.model import polling
    config = {'github': {'interval_seconds': 60}}
    assert polling(config).interval_seconds == 600
    assert polling(config, authenticated=True).interval_seconds == 60


def test_no_budget_does_not_mark_unpolled_repositories_attempted(config, tmp_path, monkeypatch):
    class Limited:
        interval_floor = 600
        retry_at = None
        def __init__(self):
            self.remaining = 6
            self.calls = []
        def get(self, path, etag=None):
            if not self.remaining:
                raise BudgetExhausted
            self.remaining -= 1
            self.calls.append(path)
            return ([raw(i) for i in range(1, 101)] if '/owner/a/' in path else []), 'etag'
    config['github'] = {'repositories': {'owner/a': {}, 'owner/b': {}}}
    monkeypatch.setattr('tracker.config.require_unchanged', lambda _: None)
    db = tmp_path / 'state.db'
    state.commit(db, state.empty())
    collect(config, db, client=Limited(), now=NOW)
    assert 'github:owner/b' not in state.read(db)['components']
    client = Limited()
    collect(config, db, client=client, now='2026-01-01T12:10:00Z')
    assert client.calls[0].startswith('/repos/owner/b/')


@pytest.mark.parametrize('code', [200, 429])
@pytest.mark.parametrize('reset', ['inf', 'nan', '1e300', '999999999999999999999', 'invalid'])
def test_http_invalid_reset_keeps_a_finite_cooldown(code, reset, monkeypatch):
    import httpx
    from tracker.providers import github
    from tracker.providers.client import IO
    now = 1700000000
    monkeypatch.setattr(github.time, 'time', lambda: now)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(code, headers={'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': reset}, json={'fact': True})
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        io = IO(client=transport)
        monkeypatch.setattr(io, 'wait_for_host', lambda *_: None)
        client = github.Client(2, io=io)
        if code == 200:
            assert client.get('/repos/owner/repo/issues')[0] == {'fact': True}
        else:
            with pytest.raises(RateLimited):
                client.get('/repos/owner/repo/issues')
        assert datetime.fromisoformat(client.retry_at).timestamp() == now + 300
        with pytest.raises(RateLimited):
            client.get('/repos/owner/repo/issues')
        assert len(calls) == 1


@pytest.mark.parametrize('value,delay', [
    ('120', 120), ('0', 60), ('-1', 60),
    ('Tue, 14 Nov 2023 22:15:20 GMT', 120),
    ('inf', 300), ('nan', 300), ('1e300', 300), ('invalid', 300),
])
def test_http_retry_after_parsing(value, delay, monkeypatch):
    import httpx
    from tracker.providers import github
    from tracker.providers.client import IO
    now = 1700000000
    monkeypatch.setattr(github.time, 'time', lambda: now)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={'Retry-After': value}))) as transport:
        io = IO(client=transport)
        monkeypatch.setattr(io, 'wait_for_host', lambda *_: None)
        client = github.Client(2, io=io)
        with pytest.raises(RateLimited) as error:
            client.get('/repos/owner/repo/issues')
        assert datetime.fromisoformat(error.value.retry_at).timestamp() == now + delay


@pytest.mark.parametrize('count', [0, 1, 20, 21, 101])
@pytest.mark.parametrize('per_page', [1, 20, 50])
def test_activity_pages_preserve_order_totals_and_snapshot(snapshot, count, per_page):
    populated(snapshot, count)
    for index, item in enumerate(snapshot['github_items'][REPO].values()):
        item['kind'] = 'pr' if index % 3 == 0 else 'issue'
        item['updated_at'] = f'2026-01-{index % 7 + 1:02d}T12:00:00+00:00'
    before = deepcopy(snapshot)
    for kind in ('issue', 'pr'):
        expected = sorted((item for item in snapshot['github_items'][REPO].values()
                           if item['kind'] == kind), key=activity.order, reverse=True)
        found, cursor = [], None
        while True:
            result = activity.page(snapshot, 'foo3', kind, cursor, per_page)
            assert result['total'] == len(expected)
            assert len(result['items']) <= per_page
            found.extend(item['id'] for item in result['items'])
            cursor = result['next_cursor']
            if cursor is None:
                break
            assert len(found) < len(expected)
        assert found == [item['id'] for item in expected]
    assert snapshot == before


@pytest.mark.parametrize('repetitions', [0, 10000])
def test_prose_matching_preserves_evidence_after_unrelated_tokens(snapshot, repetitions):
    matcher = Matcher(snapshot, Repository(aliases={'foo3': ['alias-foo']}, ambiguous_names=['unknown']))
    evidence = ' `unknown` alias-foo SPECS/foo4/a.spec foo3 foo3 '
    expected = matcher.match({'title': evidence, 'body': evidence})
    noise = 'unrelated-token ' * repetitions
    assert matcher.match({'title': noise + evidence, 'body': noise + evidence}) == expected
    assert set(expected) == {'unknown', 'foo3', 'foo4'}


@pytest.mark.parametrize('timestamp', [True, False, float('nan'), float('inf'), -float('inf'), 10**400])
def test_activity_cursor_rejects_nonfinite_and_boolean_timestamps(snapshot, timestamp):
    import base64

    populated(snapshot)
    cursor = base64.urlsafe_b64encode(json.dumps(['foo3', 'issue', timestamp, REPO, '1']).encode()).decode()
    with pytest.raises(ValueError, match='Invalid activity cursor'):
        activity.page(snapshot, 'foo3', 'issue', cursor)


def test_successful_github_quota_pause_is_not_a_failed_check(monkeypatch):
    import httpx

    from tracker.monitors.github.collector import synchronize
    from tracker.monitors.github.model import Settings
    from tracker.providers.client import IO
    from tracker.providers.github import Client

    monkeypatch.delenv('LOOKOUT_GITHUB_TOKEN', raising=False)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=[], headers={
            'X-RateLimit-Remaining': '2', 'X-RateLimit-Reset': '2000000000'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        io = IO(client=transport, workers=1)
        monkeypatch.setattr(io, 'wait_for_host', lambda *_: None)
        client = Client(60, io=io)
        client.get('/repos/owner/repo/issues')
        items, checkpoint = synchronize('owner/repo', {}, {}, client, Settings(),
                                        '2026-01-01T12:00:00+00:00')
    assert checkpoint['error'] is None
    assert checkpoint['retry_at']
    assert len(calls) == 1
    assert items == {}


@pytest.mark.parametrize('code,body', [(401, {}), (403, {'message': 'Resource not accessible by personal access token'})])
def test_invalid_credentials_retry_public_once_and_persist_cooldown(monkeypatch, code, body):
    import httpx
    from tracker.providers.client import IO
    from tracker.providers.github import Client as HTTPClient
    monkeypatch.setenv('LOOKOUT_GITHUB_TOKEN', 'fixture_only')
    calls = []
    def respond(request):
        calls.append(bool(request.headers.get('Authorization')))
        return httpx.Response(code, json=body) if calls[-1] else httpx.Response(200, json=[])
    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False) as transport:
        io = IO(client=transport, workers=1)
        monkeypatch.setattr(io, 'wait_for_host', lambda *_: None)
        client = HTTPClient(120, io=io)
        assert client.get('/repos/owner/public/issues')[0] == []
        assert client.auth_retry_at > 0
        resumed = HTTPClient(120, io=io, auth_retry_at=client.auth_retry_at)
        assert resumed.get('/repos/owner/public/issues')[0] == []
        assert resumed.interval_floor == 600
    assert calls == [True, False, False]


def test_authenticated_rate_limit_never_retries_anonymously(monkeypatch):
    import httpx
    from tracker.providers.client import IO
    from tracker.providers.github import Client as HTTPClient
    monkeypatch.setenv('LOOKOUT_GITHUB_TOKEN', 'fixture_only')
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(403, headers={'Retry-After': '300'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        io = IO(client=transport, workers=1)
        monkeypatch.setattr(io, 'wait_for_host', lambda *_: None)
        client = HTTPClient(120, io=io)
        with pytest.raises(RateLimited):
            client.get('/repos/owner/public/issues')
    assert len(calls) == 1


@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('auth_retry', [0, 900])
def test_auth_probe_preserves_public_schedule_until_recovery(config, snapshot, tmp_path, monkeypatch, accepted, auth_retry):
    import httpx
    from tracker.providers.client import IO
    from tracker.providers.github import Client as HTTPClient
    monkeypatch.setenv('LOOKOUT_GITHUB_TOKEN', 'fixture_only')
    monkeypatch.setattr('tracker.providers.github.time.time', lambda: 1000)
    monkeypatch.setattr('tracker.config.require_unchanged', lambda _: None)
    config['github'] = {'repositories': {REPO: {}}}
    checkpoint = {'auth_retry_at': auth_retry, 'next_poll_at': '2026-01-02T12:00:00+00:00',
                  'retry_at': '2026-01-02T12:00:00+00:00', 'poll_delay': 7200}
    snapshot['components']['github:' + REPO] = checkpoint
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    calls = []
    def respond(request):
        calls.append(bool(request.headers.get('Authorization')))
        return httpx.Response(200, json=[]) if accepted else httpx.Response(401, json={})
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        io = IO(client=transport, workers=1)
        monkeypatch.setattr(io, 'wait_for_host', lambda *_: None)
        owner = HTTPClient(120, io=io, auth_retry_at=auth_retry)
        result = collect(config, db, client=owner, now=NOW)
    observed = result['components']['github:' + REPO]
    if accepted:
        assert observed['auth_retry_at'] == 0
        assert observed['next_poll_at'] < checkpoint['next_poll_at']
        assert len(calls) == 3
    else:
        assert observed == {**checkpoint, 'auth_retry_at': 4600, 'authenticated': False}
        assert calls == [True]
