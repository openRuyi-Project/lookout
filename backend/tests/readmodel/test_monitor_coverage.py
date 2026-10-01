from tests.helpers.query import conjunction, query_url
from itertools import product
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import ProjectedClient
from tracker import state
from tracker.api import create_app
from tracker.monitors import model as monitor_model
from tracker.readmodel.packages import PackageList


STATUSES = (
    'not_configured', 'unsupported', 'error', 'ok', 'not_applicable',
    'pending', 'partial', 'expired', 'input_unavailable', 'input_changed',
    'schema_changed',
)


def observed(snapshot, name, status, findings=()):
    return {
        'status': status,
        'subject': monitor_model.subject(snapshot, name),
        'checked_at': state.utcnow(),
        'findings': list(findings),
    }


def client_for(snapshot, tmp_path):
    db = tmp_path / 'coverage.sqlite3'
    state.commit(db, snapshot)
    return ProjectedClient(create_app(db))


@pytest.mark.parametrize('status', STATUSES)
def test_group_aliases_preserve_exact_check_states(snapshot, tmp_path, status):
    snapshot['monitor_catalog'] = {'fixture': {'title': 'Fixture'}}
    snapshot['monitors'] = {
        'binutils': {'fixture': observed(snapshot, 'binutils', status)},
    }
    client = client_for(snapshot, tmp_path)
    prefix = '/api/v2/packages?monitor=fixture&q=binutils'
    for group, accepted in (
        ('uncovered', {'not_configured', 'unsupported'}),
        ('failed', {'error', 'partial'}),
    ):
        result = client.get(query_url('/api/v2/packages', {'check:fixture': group}, monitor='fixture', section='coverage', q='binutils')).json()
        assert result['section'] == 'coverage'
        assert result['total'] == int(status in accepted)
        assert {k: v for k, v in result['check_statuses'].items() if v} == ({status: 1} if status in accepted else {})
        assert not {'uncovered', 'failed'} & result['check_statuses'].keys()
        assert all(row['monitors']['fixture']['check']['status'] == status
                   for row in result['items'])
    raw = client.get(query_url('/api/v2/packages', {'check:fixture': status}, monitor='fixture', section='coverage', q='binutils')).json()
    assert raw['total'] == 1
    assert raw['items'][0]['monitors']['fixture']['check']['status'] == status
    if status == 'ok':
        assert client.get(prefix + '&section=results').json()['total'] == 0


def test_check_groups_use_the_same_intersections_as_rows(config):
    first_target, second_target = (target['id'] for target in config['targets'][:2])
    rows, facts = [], []
    for number, status in enumerate((*STATUSES, 'error', 'unsupported')):
        alternate = STATUSES[(number + 3) % len(STATUSES)]
        system = ('meson', 'cmake')[number % 2]
        label = 'Signal' if number % 3 == 0 else 'Other'
        first = 'failed' if number % 3 == 0 else 'succeeded'
        second = 'blocked' if number % 2 == 0 else 'succeeded'
        name = f'package-{number:02d}'
        facts.append(dict(name=name, alpha=status, beta=alternate, system=system,
                          label=label, first=first, second=second))
        rows.append({'name': name, 'monitors': {
            'alpha': {'dimensions': {'check:alpha': [status], 'maintenance': [label]}},
            'beta': {'dimensions': {'check:beta': [alternate]}},
            'source': {'dimensions': {'buildsystem': [system]}},
            'build': {'dimensions': {f'build:{first_target}': [first],
                                     f'build:{second_target}': [second]}},
        }})
    index = PackageList(rows, config['targets'])
    for monitor, query, system, label, build in product(
        ('alpha', 'beta'), ('', 'package-0'), ('', 'meson'), ('', 'Signal'), ('', 'failed'),
    ):
        context = [fact for fact in facts
                   if query in fact['name']
                   and (not system or fact['system'] == system)
                   and (not label or fact['label'] == label)
                   and (not build or fact['first'] == build)]
        for group, accepted in (
            ('uncovered', {'not_configured', 'unsupported'}),
            ('failed', {'error'}),
        ):
            expected = [fact['name'] for fact in context if fact[monitor] in accepted]
            result = index.select(filters=conjunction({'buildsystem': system, 'maintenance': label, **{'build:' + target: value for target, value in ({first_target: build}).items()}, 'check:' + monitor: group}), monitor=monitor, query=query, page=1, per_page=2)
            assert result['total'] == len(expected)
            assert [row['name'] for row in result['items']] == expected[:2]
            assert result['check_statuses'] == {status: sum(f[monitor] == status and f[monitor] in accepted for f in context) for status in STATUSES}
            assert sum(result['check_statuses'].get(status, 0) for status in accepted) == result['total']
            if len(expected) > 2:
                later = index.select(filters=conjunction({'buildsystem': system, 'maintenance': label, **{'build:' + target: value for target, value in ({first_target: build}).items()}, 'check:' + monitor: group}), monitor=monitor, query=query, page=2, per_page=2)
                assert later['total'] == result['total']
                assert later['check_statuses'] == result['check_statuses']
                assert [row['name'] for row in later['items']] == expected[2:4]


def test_failed_build_is_not_a_failed_monitor_check(snapshot, tmp_path):
    client = client_for(snapshot, tmp_path)
    failed = client.get(query_url('/api/v2/packages', {'check:build': 'failed'}, monitor='build', section='coverage')).json()
    assert failed['total'] == 0
    assert failed['check_statuses'] == {'ok': 0}
    problems = client.get(query_url('/api/v2/packages', {'build:rva20': 'failed'}, monitor='build')).json()
    assert problems['total'] > 0
    assert all(row['monitors']['build']['check']['status'] == 'ok' for row in problems['items'])


@pytest.mark.parametrize('monitor', ['source', 'version', 'build', 'alpha', 'beta', 'requires'])
@pytest.mark.parametrize('logic', ['and', 'or'])
def test_navigation_counts_distinguish_results_from_or_candidates(snapshot, tmp_path, monitor, logic):
    from tracker.readmodel.query import FilterQuery, Group, Condition
    snapshot['monitor_catalog'] = {'alpha': {'title': 'First'}, 'beta': {'title': 'Second'}, 'requires': {'title': 'RuntimeDeps'}}
    snapshot['monitors'] = {name: {mid: observed(snapshot, name, status)
        for mid in ('alpha', 'beta', 'requires')} for name, status in
        zip(snapshot['sources'], ('not_configured', 'error', 'unsupported', 'ok', 'not_applicable'))}
    client = client_for(snapshot, tmp_path)
    query = FilterQuery(groups=(Group(logic=logic, conditions=(Condition(dimension='build:rva20', value='failed'),)),))
    response = client.get('/api/ui/packages', params={'monitor': monitor, 'section': 'coverage', 'filters': query.encode(), 'next_logic': logic})
    assert response.status_code == 200, response.text
    page = response.json()
    choices = [c for n in page['controls']['navigation'] for c in n['choices']]
    assert {'Uncovered', 'CheckFailed'} <= {c['label'] for c in choices}
    for choice in choices:
        destination = client.get('/api/ui/packages?' + urlsplit(choice['href']).query)
        assert destination.status_code == 200, destination.text
        expected = destination.json()['total']
        params = parse_qs(urlsplit(choice['href']).query)
        candidate_query = FilterQuery.model_validate_json(params['filters'][0])
        if logic == 'or' and candidate_query != query:
            candidate = candidate_query.groups[0].conditions[-1]
            params['filters'] = FilterQuery(groups=(Group(conditions=(candidate,)),)).encode()
            standalone = client.get('/api/ui/packages', params=params)
            assert standalone.status_code == 200, standalone.text
            expected = min(expected, standalone.json()['total'])
        assert choice['count'] == expected, choice


def test_checks_keep_exact_status_in_rows_and_detail(snapshot, tmp_path):
    snapshot['monitor_catalog'] = {'fixture': {'title': 'Fixture'}}
    snapshot['monitors'] = {'binutils': {'fixture': observed(snapshot, 'binutils', 'partial')}}
    client = client_for(snapshot, tmp_path)
    response = client.get(query_url('/api/ui/packages', {'check:fixture': 'partial'}, monitor='fixture', q='binutils', section='coverage'))
    assert response.status_code == 200
    page = response.json()
    assert page['total'] == 1
    assert page['title'] == 'Fixture'
    assert 'Partial evidence' not in str(page['table']['rows'])
    assert page['table']['rows'][0]['cells'][1]['lines'][0][0]['text'] == snapshot['sources']['binutils']['version']
    status_chip, = page['controls']['editor']['groups'][0]['conditions']
    assert status_chip['label'] == 'Fixture: Partial evidence'
    destination = parse_qs(urlsplit(status_chip['href']).query)
    assert 'check' not in destination and destination['monitor'] == ['fixture']
    assert destination['q'] == ['binutils']
    assert client.get('/api/ui/packages?' + urlsplit(status_chip['href']).query).json()['total'] == 1
    detail = client.get('/api/ui/packages/binutils').json()
    checks = next(section for section in detail['sections'] if section['id'] == 'checks')
    assert 'Partial evidence' in str(checks)
    assert checks['collapsible'] is True
