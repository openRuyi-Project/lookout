"""Package identity comes from SPEC directories, not OBS support objects."""
from copy import deepcopy
from datetime import datetime, timezone
import subprocess

import pytest

from tests.helpers.documents import client_for
from tracker import collector, state
from tracker.monitors import runner
from tracker.monitors.requires.model import Resolver
from tracker.monitors.source import git as source_git, rpm
from tracker.monitors.version import compare
from tracker.readmodel import snapshot as view


@pytest.fixture
def repository(config, snapshot, tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(name, content='1.0'):
        directory = repo / 'SPECS' / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'package.spec').write_text(content)

    def commit():
        git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'fixture')
        return git('rev-parse', 'HEAD')

    git('init', '-qb', 'main')
    write('binutils')
    write('git-only')
    write('invalid-spec', 'bad')
    (repo / 'LICENSES').mkdir()
    (repo / 'LICENSES' / 'MIT').write_text('fixture')
    docs = repo / 'SPECS' / 'docs'
    docs.mkdir()
    (docs / 'README').write_text('not a package')
    commit()
    config['spec'] = dict(repo=str(repo), macro_package=None, changelog_limit=20,
                          fetch_timeout_seconds=10, interval_seconds=60)
    snapshot['sources']['LICENSES'] = {'error': 'no source version'}
    snapshot['inventory']['LICENSES'] = 'LICENSES'
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    monkeypatch.setattr(source_git, 'fetch', lambda *args, **kwargs: (True, None))

    def describe(data, macros=()):
        return dict(metadata=None if data == b'bad' else {'version': data.decode()},
                    metadata_error='fixture parse failed' if data == b'bad' else None,
                    native_query={'context': {'resolver': rpm.RESOLVER, 'additional_macros': []}})

    def poll():
        return collector.check_specs(config, db, describe)

    return repo, git, write, commit, db, poll


def test_pinned_tree_counts_only_regular_direct_spec_blobs(repository):
    repo, git, write, commit, _, _ = repository
    old = git('rev-parse', 'HEAD')
    nested = repo / 'SPECS' / 'nested' / 'fixtures'
    nested.mkdir(parents=True)
    (nested / 'example.spec').write_text('not a source package')
    link = repo / 'SPECS' / 'symlink'
    link.mkdir()
    (link / 'package.spec').symlink_to('../../binutils/package.spec')
    write('new-package')
    commit()
    assert source_git.packages(str(repo), old) == (['binutils', 'git-only', 'invalid-spec'], None)
    assert source_git.packages(str(repo)) == (['binutils', 'git-only', 'invalid-spec', 'new-package'], None)


@pytest.mark.parametrize('listing,error', [(None, 'git exited 128'), ('malformed\0', None)])
def test_unconfirmed_tree_is_not_an_empty_catalogue(monkeypatch, listing, error):
    monkeypatch.setattr(source_git, '_git_text', lambda *args, **kwargs: (listing, error))
    names, failure = source_git.packages('/fixture')
    assert names is None and failure


def test_all_package_consumers_use_the_spec_catalogue(repository, snapshot):
    _, _, _, _, _, poll = repository
    before = deepcopy(snapshot)
    observed = poll()
    names = {'binutils', 'git-only', 'invalid-spec'}
    assert set(state.package_names(observed)) == names
    assert set(compare.evaluate_all(observed)) == names
    assert Resolver(observed, datetime.now(timezone.utc)).packages == names
    rows, collection = view.project_monitors(observed)
    assert {row['name'] for row in rows} == names
    assert collection['packages'] == len(names)
    invalid = next(row for row in rows if row['name'] == 'invalid-spec')
    assert invalid['monitors']['source']['check']['status'] == 'error'
    missing = next(row for row in rows if row['name'] == 'git-only')
    assert missing['monitors']['source']['data']['obs'] == {}
    assert observed['inventory'] == before['inventory']
    assert observed['sources'] == before['sources']
    assert observed['builds'] == before['builds']


def test_list_detail_batch_export_and_ui_share_package_identity(repository, tmp_path):
    _, _, _, _, _, poll = repository
    client, _ = client_for(poll(), tmp_path / 'api')
    names = {'binutils', 'git-only', 'invalid-spec'}
    listing = client.get('/api/v2/packages').json()
    assert {row['name'] for row in listing['items']} == names
    assert {row['name'] for row in client.get('/api/v2/export').json()['packages']} == names
    for name in names:
        assert client.get('/api/v2/packages/' + name).status_code == 200
        assert client.get('/api/ui/packages/' + name).status_code == 200
    for prefix in ('/api/v2/packages/', '/api/ui/packages/'):
        assert client.get(prefix + 'LICENSES').status_code == 404
    batch = client.get('/api/v2/packages:batchGet', params={'names': ['git-only', 'binutils']})
    assert batch.status_code == 200
    assert [row['name'] for row in batch.json()['items']] == ['git-only', 'binutils']
    assert client.get('/api/v2/packages:batchGet', params={'names': ['LICENSES']}).status_code == 404


@pytest.mark.parametrize('failed_step', ['fetch', 'packages', 'changelogs', 'read_macros'])
def test_failed_refresh_retains_catalogue_and_evidence(repository, monkeypatch, failed_step):
    repo, _, write, commit, _, poll = repository
    first = poll()
    write('new-package')
    commit()
    failure = 'fixture unavailable'
    if failed_step == 'read_macros':
        # The repository fixture disables macros; enable one explicit input for this case.
        monkeypatch.setattr(source_git, 'macro_packages', lambda spec: ['fixture'])
        def fail(*args, **kwargs):
            raise source_git.MacroReadError(failure)
        monkeypatch.setattr(source_git, 'read_macros', fail)
    else:
        value = False if failed_step == 'fetch' else {} if failed_step == 'changelogs' else None
        response = (value, failure)
        monkeypatch.setattr(source_git, failed_step, lambda *args, **kwargs: response)
    retained = poll()
    assert retained['specs'] == first['specs']
    assert set(state.package_names(retained)) == set(state.package_names(first))
    assert retained['components']['spec_git']['catalog_head'] == first['components']['spec_git']['catalog_head']
    assert retained['components']['spec_git']['error'] == failure


def test_git_rename_and_addition_do_not_wait_for_obs(repository):
    _, git, write, commit, _, poll = repository
    first = poll()
    git('mv', 'SPECS/binutils', 'SPECS/renamed')
    write('new-package')
    commit()
    second = poll()
    assert set(state.package_names(second)) == {'renamed', 'new-package', 'git-only', 'invalid-spec'}
    assert second['inventory'] == first['inventory']
    assert second['sources'] == first['sources']
    # A concurrent OBS metadata publication cannot erase source-only packages.
    merged = state.merge(second, 'obs', {'sources': {}, 'inventory': {}})
    assert set(state.package_names(merged)) == set(state.package_names(second))


def test_unchanged_revision_reuses_complete_catalogue(repository, monkeypatch):
    _, _, _, _, _, poll = repository
    first = poll()
    monkeypatch.setattr(source_git, 'packages', lambda *args, **kwargs: pytest.fail('unchanged tree'))
    second = poll()
    assert second['components']['spec_git']['catalog_head'] == first['components']['spec_git']['catalog_head']
    assert second['components']['spec_git']['selected_packages'] == 1  # failed parse only


def test_independent_monitor_check_accepts_source_only_package(repository, config, monkeypatch):
    _, _, _, _, _, poll = repository
    observed = poll()
    monkeypatch.setattr(runner, 'plan', lambda *args, **kwargs: {'fingerprint': 'fixture'})
    monkeypatch.setattr(runner, 'refresh_policy', lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, 'execute', lambda *args, **kwargs: {'status': 'fixture'})
    assert runner.check(config, observed, 'git-only', 'requires', None) == {'status': 'fixture'}
    with pytest.raises(ValueError, match='unknown package'):
        runner.check(config, observed, 'LICENSES', 'requires', None)
