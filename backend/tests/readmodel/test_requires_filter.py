"""Filter plumbing consumes projected dimensions, not requirement/provider facts."""
from tests.helpers.query import conjunction, query_url
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from tracker.api import create_app
from tracker.readmodel import snapshot as view
from tracker.readmodel.packages import PackageList


TARGETS = [dict(id=name, label=name) for name in ('first', 'second')]


def dimension_rows():
    # Duplicate unmet values model several failed requirements in one package.
    examples = [
        ('pkg-a', ['unmet', 'changes', 'unmet'], 'cmake', 'Advisory', 'blocked', 'failed', 'ok'),
        ('pkg-b', ['changes'], 'cmake', 'Advisory', 'blocked', 'succeeded', 'ok'),
        ('pkg-c', ['unknown'], 'cmake', 'Advisory', 'failed', 'succeeded', 'partial'),
        ('pkg-d', ['unmet'], 'meson', 'EOL', 'succeeded', 'failed', 'ok'),
        ('pkg-e', ['unmet'], 'cmake', 'EOL', 'blocked', 'failed', 'error'),
        ('pkg-f', [], 'cmake', 'Advisory', 'succeeded', 'succeeded', 'ok'),
        ('other', [], 'cmake', 'Advisory', 'succeeded', 'succeeded', 'pending'),
    ]
    return [dict(name=name, monitors={'requires': dict(dimensions={
        'requires': values, 'buildsystem': [system], 'maintenance': [label],
        'view': ['updates'] if 'changes' in values else [],
        'build:first': [first], 'build:second': [second],
        'check:requires': [check], 'findings:requires': ['yes'] if name != 'other' else [],
    })}) for name, values, system, label, first, second, check in examples]


def select(index, *, requires='', check='', **kwargs):
    return index.select(filters=conjunction({'requires': requires, 'check:requires': check}),
                        page=1, per_page=100, monitor='requires', **kwargs)


def test_unknown_and_changes_do_not_imply_unmet():
    index = PackageList(dimension_rows(), TARGETS)
    unmet = select(index, requires='unmet')
    assert [row['name'] for row in unmet['items']] == ['pkg-a', 'pkg-d', 'pkg-e']
    assert unmet['requires_counts'] == {'all': 3, 'changes': 1, 'unknown': 0, 'unmet': 3}
    assert [row['name'] for row in select(index, requires='changes')['items']] == ['pkg-a', 'pkg-b']
    # Adding either condition to the other computes the same intersection.
    assert select(index, requires='changes')['requires_counts']['unmet'] == unmet['requires_counts']['changes']


def test_requires_results_and_coverage_preserve_their_other_selections():
    index = PackageList(dimension_rows(), TARGETS)
    results = select(index, requires='unmet', findings_only=True)
    assert (results['total'], results['result_count'], results['coverage_count']) == (3, 3, 3)
    assert results['requires_counts']['all'] == 3
    coverage = select(index, requires='unmet', check='ok')
    assert (coverage['total'], coverage['result_count'], coverage['coverage_count']) == (2, 2, 2)
    assert {k: v for k, v in coverage['check_statuses'].items() if v} == {'ok': 2}
    assert coverage['requires_counts'] == {'all': 2, 'changes': 1, 'unknown': 0, 'unmet': 2}
    empty = select(index, requires='unmet', query='pkg-b')
    assert empty['total'] == 0
    assert empty['requires_counts'] == {'all': 0, 'changes': 0, 'unknown': 0, 'unmet': 0}


@pytest.fixture
def linked_client(snapshot, tmp_path, monkeypatch):
    snapshot['monitor_catalog'] = {'requires': {'title': 'RuntimeDeps'}}
    rows, collection = view.project_monitors(snapshot)
    projected = {'binutils': ['unmet', 'changes', 'unmet'], 'foo3': ['changes'],
                 'foo4': ['unknown'], 'unknown': [], 'untracked': ['unmet']}
    for row in rows:
        result = row['monitors']['requires']
        check = {'unknown': 'not_configured', 'untracked': 'error'}.get(row['name'], 'ok')
        result['check'].update(status=check)
        result['dimensions'].update({
            'requires': projected[row['name']],
            'check:requires': [check],
            'findings:requires': ['yes'] if projected[row['name']] else [],
            'maintenance': ['Advisory'],
        })
        row['monitors']['source']['dimensions']['buildsystem'] = ['cmake']
    index = PackageList(rows, snapshot['targets'])
    app = create_app(tmp_path / 'unused.db')
    # Tests this boundary against a prepared projection; no provider/model rewrite.
    monkeypatch.setattr(app.state.projection, 'read', lambda: (snapshot, index, collection))
    return TestClient(app)


def parsed(link):
    return parse_qs(urlsplit(link).query)


def test_requires_filter_is_validated_only_on_monitored_api(linked_client):
    response = linked_client.get(query_url('/api/v2/packages', {'requires': 'unmet'}, monitor='requires', section='results'))
    assert response.status_code == 200
    result = response.json()
    assert [item['name'] for item in result['items']] == ['binutils', 'untracked']
    assert result['requires_counts'] == {'all': 2, 'changes': 1, 'unknown': 0, 'unmet': 2}
    for path in ('/api/v2/packages', '/api/ui/packages'):
        assert linked_client.get(path + '?requires=unknown').status_code == 422
        assert linked_client.get(path + '?requires=invalid').status_code == 422


def test_requires_navigation_is_not_added_to_other_monitors(linked_client):
    for query in ('', 'monitor=build', 'monitor=version'):
        document = linked_client.get('/api/ui/packages?' + query).json()
        assert not any(nav['label'] == 'Dependencies' for nav in document['controls']['navigation'])
