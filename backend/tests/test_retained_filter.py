"""Out-of-date observation membership is separate from check failures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import product
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from tracker import monitor_model, monitor_views, requirements, state, view
from tracker.api import create_app
from tracker.package_list import PackageList


def observed(snapshot, name, findings=(), *, old=False, status='ok'):
    stamp = datetime.now(timezone.utc) - timedelta(days=3 if old else 0)
    return dict(subject=monitor_model.subject(snapshot, name), status=status,
                checked_at=stamp.isoformat(), findings=list(findings),
                error='Fixture collection failure' if status == 'error' else None)


def security_finding(identity):
    return monitor_model.finding(identity, 'Security', identity, [], 'https://example.org/' + identity)


@pytest.fixture
def retained_projection(snapshot):
    snapshot['monitor_catalog'] = {'security': {'title': 'Security'}}
    snapshot['monitors'] = {
        'binutils': {'security': observed(snapshot, 'binutils',
            [security_finding('OLD-1'), security_finding('OLD-2')], old=True)},
        'foo3': {'security': observed(snapshot, 'foo3', [security_finding('RETAINED')], status='error')},
        'foo4': {'security': observed(snapshot, 'foo4', status='error')},
        'untracked': {'security': observed(snapshot, 'untracked', [security_finding('FRESH')])},
    }
    before = deepcopy(snapshot)
    rows, collection = view.project_monitors(snapshot)
    assert snapshot == before
    return snapshot, rows, collection


def selected(index, **changes):
    filters = dict(view='all', buildsystem='', maintenance='', builds={}, page=1,
                   per_page=100, monitor='security')
    return index.select(**(filters | changes))


def names(result):
    return [row['name'] for row in result['items']]


def test_actual_projection_marks_retained_observations_not_failed_checks(retained_projection):
    snapshot, rows, _ = retained_projection
    index = PackageList(rows, snapshot['targets'])
    assert names(selected(index, freshness='retained')) == ['binutils', 'foo3']
    assert names(selected(index, check='failed')) == ['foo3', 'foo4']
    assert names(selected(index, freshness='retained', check='failed')) == ['foo3']
    assert selected(index)['retained_count'] == 2  # Packages, not three findings.
    assert selected(index, freshness='retained')['retained_count'] == 2
    assert selected(index, check='failed')['retained_count'] == 1
    for row in rows:
        expected = ['yes'] if row['name'] in ('binutils', 'foo3') else None
        assert row['monitors']['security']['dimensions'].get('retained:security') == expected
        assert row['monitors']['version']['dimensions'].get('retained:version') == expected
        for core in ('source', 'build'):
            assert 'retained:' + core not in row['monitors'][core]['dimensions']


def test_hidden_upstream_watch_and_old_build_values_are_not_retained_results(snapshot):
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    for observation in snapshot['tracks'].values():
        observation['fetched_at'] = old
    for targets in snapshot['builds'].values():
        for observation in targets.values():
            observation['fetched_at'] = old
    rows, _ = view.project_monitors(snapshot)
    assert any(row['monitors']['version']['data']['watch'] for row in rows)
    assert any(row['monitors']['version']['check']['stale'] for row in rows)
    assert any(row['monitors']['build']['check']['stale'] for row in rows)
    assert not any(dimension.startswith('retained:') for row in rows
                   for result in row['monitors'].values() for dimension in result['dimensions'])


def test_requires_needs_visible_assessments_not_only_old_provider_facts(snapshot):
    def finding(kind):
        declaration = requirements.Requirement('python', 'Python', kind, 'pep440',
            '>=3.8', '>=3.8', 'Fixture', 'https://example.org/python').fact()
        return monitor_model.finding(kind, 'Requires', 'Python', [], 'https://example.org/python',
                                     requirement=declaration)
    snapshot['monitors'] = {
        'binutils': {'requires': observed(snapshot, 'binutils', [finding('runtime')], old=True)},
        'foo3': {'requires': observed(snapshot, 'foo3', [finding('build')], old=True)},
        'foo4': {'requires': observed(snapshot, 'foo4', status='error')},
    }
    rows, _ = view.project_monitors(snapshot)
    by_name = {row['name']: row['monitors']['requires'] for row in rows}
    assert by_name['binutils']['data']['requirements']
    assert by_name['binutils']['dimensions']['retained:requires'] == ['yes']
    assert by_name['foo3']['data']['findings'][0]['stale']
    assert by_name['foo3']['data']['requirements'] == []
    assert 'retained:requires' not in by_name['foo3']['dimensions']
    assert 'retained:requires' not in by_name['foo4']['dimensions']


def test_retained_projection_is_repeatable_without_rewriting_observations():
    finding = {'stale': True}
    result = dict(data=dict(kind='evidence', findings=[finding]), dimensions={})
    monitor_views.retained_dimensions({'custom': result})
    assert result['dimensions'] == {'retained:custom': ['yes']}
    assert finding == {'stale': True}
    finding['stale'] = False
    monitor_views.retained_dimensions({'custom': result})
    assert result['dimensions'] == {}


def test_intersections_and_disjunctive_retained_count_match_independent_scan():
    examples = [
        ('pkg-a', True, 'error', 'cmake', 'Security', 'failed', ['security'], ['unmet']),
        ('pkg-b', True, 'expired', 'cmake', 'License', 'succeeded', ['license'], ['changes']),
        ('pkg-c', False, 'error', 'cmake', 'Security', 'failed', [], []),
        ('other', True, 'ok', 'meson', 'Security', 'failed', ['security'], ['unmet']),
    ]
    rows = [dict(name=name, monitors={'security': dict(dimensions={
        'retained:security': ['yes', 'yes'] if retained else [], 'check:security': [check],
        'findings:security': ['yes'] if signals else [], 'buildsystem': [system], 'maintenance': [label],
        'build:target': [build], 'version_signal': signals, 'requires': requires,
    })}) for name, retained, check, system, label, build, signals, requires in examples]
    index = PackageList(rows, [{'id': 'target'}])
    for query, freshness, check, system, signal, requires, findings in product(
        ('', 'PKG'), ('', 'retained'), ('', 'error'), ('', 'cmake'), ('', 'security'),
        ('', 'unmet'), (False, True),
    ):
        choices = {'check:security': check, 'buildsystem': system, 'version_signal': signal,
                   'requires': requires, 'findings:security': 'yes' if findings else ''}

        def matches(row, retained=False):
            dimensions = row['monitors']['security']['dimensions']
            return (query.casefold() in row['name'].casefold()
                    and all(not value or value in dimensions[dimension] for dimension, value in choices.items())
                    and (not retained or 'yes' in dimensions['retained:security']))

        result = selected(index, query=query, freshness=freshness, check=check, buildsystem=system,
                          signal=signal, requires=requires, findings_only=findings, per_page=1)
        expected = [row for row in rows if matches(row, bool(freshness))]
        assert result['total'] == len(expected)
        assert result['items'] == expected[:1]
        assert result['retained_count'] == sum(matches(row, True) for row in rows)
    assert names(selected(index, freshness='retained', maintenance='License',
                          builds={'target': 'succeeded'})) == ['pkg-b']
    assert selected(index, freshness='retained', maintenance='License',
                    builds={'target': 'failed'})['retained_count'] == 0


@pytest.fixture
def retained_client(retained_projection, tmp_path, monkeypatch):
    snapshot, rows, collection = retained_projection
    index = PackageList(rows, snapshot['targets'])
    app = create_app(tmp_path / 'unused.db')
    monkeypatch.setattr(app.state.projection, 'read', lambda: (snapshot, index, collection))
    return TestClient(app)


def test_raw_api_is_typed_composable_and_preserves_legacy(retained_client):
    base = '/api/v2/packages?monitor=security&freshness=retained'
    response = retained_client.get(base)
    assert response.status_code == 200
    result = response.json()
    assert names(result) == ['binutils', 'foo3'] and result['retained_count'] == 2
    assert (result['result_count'], result['coverage_count']) == (2, 2)
    failed = retained_client.get(base + '&check=failed').json()
    assert names(failed) == ['foo3'] and failed['retained_count'] == 1
    assert names(retained_client.get(base + '&build=rva20:failed').json()) == ['foo3']
    assert names(retained_client.get(base + '&build=rva20:issues').json()) == ['foo3']
    assert retained_client.get(base + '&signal=missing').json()['total'] == 0
    assert retained_client.get('/api/v2/packages?freshness=retained').status_code == 422
    for path in ('/api/v2/packages', '/api/ui/packages'):
        assert retained_client.get(path + '?monitor=security&freshness=expired').status_code == 422
    legacy = retained_client.get('/api/v1/packages?freshness=retained').json()
    assert legacy['total'] == 5 and 'retained_count' not in legacy
    schema = retained_client.get('/openapi.json').json()
    assert schema['components']['schemas']['MonitoredList']['properties']['retained_count']['type'] == 'integer'


@pytest.mark.parametrize('monitor', ['security', 'version'])
@pytest.mark.parametrize('mode', ['', '&freshness=retained', '&check=failed'])
def test_navigation_count_is_destination_count_and_modes_are_disjoint(retained_client, monitor, mode):
    document = retained_client.get('/api/ui/packages?monitor=' + monitor + mode).json()
    choices = [choice for navigation in document['controls']['navigation'] for choice in navigation['choices']]
    retained = next(choice for choice in choices if choice['label'] == 'Out of date')
    assert retained['count'] == 2
    for choice in choices:
        if choice['count'] is not None:
            destination = retained_client.get(choice['href'].replace('/?', '/api/ui/packages?')).json()
            assert destination['total'] == choice['count']
        query = parse_qs(urlsplit(choice['href']).query)
        if choice['label'] == 'Out of date':
            assert query['freshness'] == ['retained'] and 'check' not in query
        else:
            assert 'freshness' not in query
    assert retained['selected'] == (mode == '&freshness=retained')


def test_retained_count_follows_search_and_selected_zero_remains_visible(retained_client):
    document = retained_client.get('/api/ui/packages?monitor=security&q=foo').json()
    choices = [choice for navigation in document['controls']['navigation'] for choice in navigation['choices']]
    retained = next(choice for choice in choices if choice['label'] == 'Out of date')
    assert retained['count'] == 1
    destination = retained_client.get(retained['href'].replace('/?', '/api/ui/packages?')).json()
    assert [row['key'] for row in destination['table']['rows']] == ['foo3']
    empty = retained_client.get('/api/ui/packages?monitor=security&q=absent&freshness=retained').json()
    choices = [choice for navigation in empty['controls']['navigation'] for choice in navigation['choices']]
    retained = next(choice for choice in choices if choice['label'] == 'Out of date')
    assert retained['count'] == 0 and retained['selected'] and empty['total'] == 0
    inactive = retained_client.get('/api/ui/packages?monitor=security&q=foo4').json()
    assert not any(choice['label'] == 'Out of date'
                   for navigation in inactive['controls']['navigation'] for choice in navigation['choices'])


def test_row_markers_link_to_the_same_visible_retained_set(retained_client):
    for monitor in ('security', 'version'):
        document = retained_client.get('/api/ui/packages?monitor=' + monitor).json()
        for row in document['table']['rows']:
            markers = [value for cell in row['cells'] for line in cell['lines']
                       for value in line if value['text'] == 'Out of date']
            assert bool(markers) == (row['key'] in ('binutils', 'foo3'))
            for marker in markers:
                destination = retained_client.get(marker['href'].replace('/?', '/api/ui/packages?')).json()
                assert row['key'] in {item['key'] for item in destination['table']['rows']}
