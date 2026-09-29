from collections import Counter
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
        result = client.get(prefix + '&section=results&check=' + group).json()
        assert result['section'] == 'coverage'
        assert result['total'] == int(status in accepted)
        assert result['check_statuses'] == {status: 1}
        assert not {'uncovered', 'failed'} & result['check_statuses'].keys()
        assert all(row['monitors']['fixture']['check']['status'] == status
                   for row in result['items'])
    raw = client.get(prefix + '&check=' + status).json()
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
            result = index.select(
                monitor=monitor, query=query, view='all', buildsystem=system,
                maintenance=label, builds={first_target: build}, check=group,
                page=1, per_page=2,
            )
            assert result['total'] == len(expected)
            assert [row['name'] for row in result['items']] == expected[:2]
            assert result['check_statuses'] == dict(Counter(fact[monitor] for fact in context))
            assert sum(result['check_statuses'].get(status, 0) for status in accepted) == result['total']
            if len(expected) > 2:
                later = index.select(
                    monitor=monitor, query=query, view='all', buildsystem=system,
                    maintenance=label, builds={first_target: build}, check=group,
                    page=2, per_page=2,
                )
                assert later['total'] == result['total']
                assert later['check_statuses'] == result['check_statuses']
                assert [row['name'] for row in later['items']] == expected[2:4]


def test_failed_build_is_not_a_failed_monitor_check(snapshot, tmp_path):
    client = client_for(snapshot, tmp_path)
    failed = client.get('/api/v2/packages?monitor=build&check=failed').json()
    assert failed['total'] == 0
    assert failed['check_statuses'] == {'ok': len(snapshot['sources'])}
    problems = client.get('/api/v2/packages?monitor=build&build=rva20:failed').json()
    assert problems['total'] > 0
    assert all(row['monitors']['build']['check']['status'] == 'ok' for row in problems['items'])


@pytest.mark.parametrize('monitor', ['source', 'version', 'build', 'alpha', 'beta', 'requires'])
@pytest.mark.parametrize('filters', [
    '', '&q=foo', '&build=rva20:failed', '&maintenance=Signal',
    '&check=uncovered', '&check=failed', '&section=coverage',
])
def test_counted_navigation_links_select_the_advertised_packages(snapshot, tmp_path, monitor, filters):
    names = list(snapshot['sources'])
    snapshot['monitor_catalog'] = {
        'alpha': {'title': 'First observation'},
        'beta': {'title': 'Second observation'},
        'requires': {'title': 'RuntimeDeps'},
    }
    states = ('not_configured', 'error', 'unsupported', 'ok', 'not_applicable')
    for number, name in enumerate(names):
        findings = [monitor_model.finding('signal', 'Signal', 'Fixture signal', [],
                                         'https://example.org/evidence')] if number == 3 else []
        snapshot.setdefault('monitors', {})[name] = {
            'alpha': observed(snapshot, name, states[number], findings),
            'beta': observed(snapshot, name, states[-number - 1]),
            'requires': observed(snapshot, name, states[number]),
        }
    client = client_for(snapshot, tmp_path)
    response = client.get('/api/ui/packages?monitor=' + monitor + '&per_page=2' + filters)
    assert response.status_code == 200
    document = response.json()
    choices = [choice for navigation in document['controls']['navigation']
               for choice in navigation['choices']]
    by_label = {choice['label']: choice for choice in choices}
    assert {'Uncovered', 'CheckFailed'} <= by_label.keys()
    check_filters = {value for choice in choices
                     for value in parse_qs(urlsplit(choice['href']).query).get('check', [])}
    assert check_filters == {'uncovered', 'failed'}
    if monitor == 'version':
        assert parse_qs(urlsplit(by_label['Untracked']['href']).query)['view'] == ['untracked']
    for choice in choices:
        if choice['count'] is None:
            continue
        destination = client.get('/api/ui/packages?' + urlsplit(choice['href']).query)
        assert destination.status_code == 200
        assert destination.json()['total'] == choice['count'], choice
        if choice['label'] in {'Uncovered', 'CheckFailed'}:
            query = parse_qs(urlsplit(choice['href']).query)
            assert query['monitor'] == [monitor]
            assert query['check'] == [{'Uncovered': 'uncovered', 'CheckFailed': 'failed'}[choice['label']]]


def test_checks_keep_exact_status_in_rows_and_detail(snapshot, tmp_path):
    snapshot['monitor_catalog'] = {'fixture': {'title': 'Fixture'}}
    snapshot['monitors'] = {'binutils': {'fixture': observed(snapshot, 'binutils', 'partial')}}
    client = client_for(snapshot, tmp_path)
    response = client.get('/api/ui/packages?monitor=fixture&check=partial&q=binutils')
    assert response.status_code == 200
    page = response.json()
    assert page['total'] == 1
    assert 'Partial evidence' in page['title']
    assert 'Partial evidence' not in str(page['table']['rows'])
    assert page['table']['rows'][0]['cells'][1]['lines'][0][0]['text'] == snapshot['sources']['binutils']['version']
    status_chip, = [chip for chip in page['controls']['active'] if chip['label'].startswith('Check:')]
    assert status_chip['label'] == 'Check: Partial evidence'
    destination = parse_qs(urlsplit(status_chip['href']).query)
    assert 'check' not in destination and destination['monitor'] == ['fixture']
    assert destination['q'] == ['binutils']
    assert client.get('/api/ui/packages?' + urlsplit(status_chip['href']).query).json()['total'] == 0
    detail = client.get('/api/ui/packages/binutils').json()
    checks = next(section for section in detail['sections'] if section['id'] == 'checks')
    assert 'Partial evidence' in str(checks)
    assert checks['collapsible'] is True
