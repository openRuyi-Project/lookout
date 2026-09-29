"""Footer links come from saved repository evidence, independent of pagination."""
from copy import deepcopy

import pytest

from tests.helpers.documents import client_for
from tracker.presentation.pages import collection_meta
from tracker.readmodel.snapshot import source_repository


@pytest.mark.parametrize('branch', ['main', 'stable/3.x'])
def test_repository_checkpoint_not_package_change_is_exposed(snapshot, tmp_path, branch):
    head = 'abcdef' * 6 + '1234'
    origin = {'url': 'https://github.com/fixture/project.git', 'branch': branch}
    snapshot['components']['spec_git'] = {'head': head}
    snapshot['specs'] = {'retained': {'head': '0' * 40, 'source_origin': origin}}
    before = deepcopy(snapshot)
    client, _ = client_for(snapshot, tmp_path)
    page = client.get('/api/ui/packages?per_page=1').json()
    service, source = page['meta']
    assert service['label'] == 'BuildService'
    assert service['href'] == snapshot['obs']['web_url']
    assert source['label'] == branch
    assert source['href'].endswith('/tree/' + branch.replace('/', '%2F'))
    revision, = source['values']
    assert revision['text'] == head[:6] and revision['title'] == head
    assert revision['href'] == 'https://github.com/fixture/project/commit/' + head
    raw = client.get('/api/v2/packages?per_page=1').json()['collection']['source_repository']
    assert raw == {'url': origin['url'], 'branch': branch, 'revision': head}
    assert snapshot == before


@pytest.mark.parametrize('problem', ['no_checkpoint', 'short_hash', 'no_origin', 'mixed_repositories', 'mixed_branches'])
def test_repository_is_not_guessed_from_incomplete_or_conflicting_evidence(snapshot, problem):
    snapshot['components']['spec_git'] = {'head': 'a' * 40}
    origin = {'url': 'https://github.com/fixture/project', 'branch': 'main'}
    snapshot['specs'] = {'one': {'source_origin': origin}, 'two': {'source_origin': dict(origin)}}
    if problem == 'no_checkpoint':
        snapshot['components'].pop('spec_git')
    elif problem == 'short_hash':
        snapshot['components']['spec_git']['head'] = 'abc123'
    elif problem == 'no_origin':
        snapshot['specs'] = {}
    elif problem == 'mixed_repositories':
        snapshot['specs']['two']['source_origin']['url'] = 'https://github.com/fixture/other'
    else:
        snapshot['specs']['two']['source_origin']['branch'] = 'other'
    assert source_repository(snapshot) is None


@pytest.mark.parametrize('url', ['https://example.org/repo.git', 'https://secret:token@github.com/org/repo',
                                 'https://github.com/org/repo?token=secret', 'file:///local/repo', 'http://[broken'])
def test_unknown_or_unsafe_forge_does_not_get_invented_links(url):
    fields = collection_meta({'obs_updated_at': None, 'source_repository': {
        'url': url, 'branch': 'branch', 'revision': 'a' * 40}})
    assert fields[-1].label == 'branch'
    assert fields[-1].href is None and fields[-1].values[0].href is None
