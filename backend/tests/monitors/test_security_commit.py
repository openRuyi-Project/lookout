import pytest

from tracker.monitors.security import monitor


COMMIT = {'repository': 'https://github.com/example/project', 'commit': 'a' * 40}


def test_snapshot_identity_reuses_full_source_commit_without_guessing_a_version():
    settings = monitor.inputs({'version': '0+git20260101.aaaaaaa', 'source_commit': COMMIT}, None)
    assert settings == COMMIT
    assert monitor.query({'version': '0+git20260101.aaaaaaa'}, settings) == {'commit': 'a' * 40}
    assert monitor.inputs({'version': '1', 'identity': {'source': 'cpan', 'cpan': 'Example'}}, None) is None


def test_registry_release_identity_wins_over_repository_commit():
    package = {'version': '1.2.3', 'identity': {'source': 'pypi', 'pypi': 'example'}, 'source_commit': COMMIT}
    assert monitor.inputs(package, None) == {'ecosystem': 'PyPI', 'name': 'example'}


@pytest.mark.parametrize('commit', ['a' * 7, 'not-a-commit', '', None])
def test_abbreviated_or_invalid_commit_is_rejected(commit):
    with pytest.raises(ValueError):
        monitor.query({'version': '1'}, {**COMMIT, 'commit': commit})


def test_commit_evidence_does_not_invent_package_version_or_other_repository_fixes():
    class IO:
        def json(self, method, url, body=None):
            assert body == {'commit': 'a' * 40}
            return {'vulns': [{'id': 'OSV-FIXTURE', 'affected': [
                {'ranges': [{'type': 'GIT', 'repo': COMMIT['repository'], 'events': [{'fixed': 'b' * 40}]}]},
                {'ranges': [{'type': 'GIT', 'repo': 'https://github.com/other/project', 'events': [{'fixed': 'c' * 40}]}]},
            ]}]}
    result = monitor.check({'version': '0+git'}, COMMIT, IO())
    facts = result['findings'][0]['facts']
    assert not any(f['key'] == 'Query version' for f in facts)
    assert next(f['value'] for f in facts if f.get('code') == 'fixed_events') == ['b' * 40]
