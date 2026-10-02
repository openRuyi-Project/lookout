"""Build reading density: reasons belong to the focused view and their target."""
from copy import deepcopy
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest

from tracker import state
from tracker.api import create_app
from tracker.readmodel import snapshot as view
from tracker.readmodel.packages import PackageList


NOW = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
PACKAGE = 'fixture-app'


@pytest.fixture
def observed(config):
    snapshot = state.empty()
    snapshot.update(generation=1, mode='fixture', last_attempt=NOW.isoformat(),
                    obs=config['obs'], targets=config['targets'],
                    inventory={PACKAGE: PACKAGE}, stale_after_seconds=86400)
    snapshot['sources'][PACKAGE] = state.success(
        {}, {'version': '2.0', 'srcmd5': 'fixture-source'}, NOW.isoformat())
    snapshot['index'][PACKAGE] = {'srcmd5': 'fixture-source'}
    snapshot['builds'][PACKAGE] = {
        target['id']: state.success({}, {'raw_status': 'succeeded'}, NOW.isoformat())
        for target in config['targets']}
    return snapshot


@pytest.fixture
def client_for(monkeypatch, tmp_path):
    # Version ordering is unrelated to these read-model checks; the fixture has
    # no provider observations and must not need platform RPM bindings.
    monkeypatch.setattr(state, 'compare', lambda *args: 'unknown')

    def create(snapshot):
        rows, collection = view.project_monitors(snapshot, NOW)
        index = PackageList(rows, snapshot['targets'])
        app = create_app(tmp_path / 'unused.db')
        monkeypatch.setattr(app.state.projection, 'read', lambda: (snapshot, index, collection))
        return TestClient(app)

    return create


def row(document):
    return next(row for row in document['table']['rows'] if row['key'] == PACKAGE)


def values(cell):
    return [value for line in cell['lines'] for value in line]


@pytest.mark.parametrize('status', ['blocked', 'unresolvable', 'broken', 'scheduled', 'failed'])
def test_build_focus_shows_observed_reason_only_in_its_target(observed, client_for, status):
    target = observed['targets'][0]['id']
    observed['builds'][PACKAGE][target].update(
        raw_status=status, details='nothing provides fixture-dependency >= 7')
    before = deepcopy(observed)
    client = client_for(observed)
    response = client.get('/api/ui/packages?monitor=build')
    assert response.status_code == 200
    cells = row(response.json())['cells']
    reason = cells[1]['lines'][-1][0]
    assert reason['text'] == 'nothing provides fixture-dependency >= 7'
    assert reason['href'] == f'/packages/{PACKAGE}#build'
    assert reason['tone'] == 'muted'
    assert all('fixture-dependency' not in str(cell) for cell in cells[2:])
    overview = row(client.get('/api/ui/packages').json())
    assert 'fixture-dependency' not in str(overview['cells'])
    assert overview['notes'][0]['column'] == 2 and overview['notes'][0]['span'] == 3
    assert overview['notes'][0]['values'][0]['text'] == target + ':'
    # Keep the public list API compact; its detail endpoint already owns the
    # original OBS observations. The UI consumes the full prepared projection.
    assert 'fixture-dependency' not in client.get('/api/v2/packages?monitor=build').text
    assert 'fixture-dependency' in client.get(f'/api/v2/packages/{PACKAGE}').text
    assert observed == before


@pytest.mark.parametrize('status', ['succeeded', 'building', 'disabled', 'excluded'])
def test_normal_status_details_do_not_become_build_problem_messages(observed, client_for, status):
    target = observed['targets'][0]['id']
    observed['builds'][PACKAGE][target].update(raw_status=status, details='worker://fixture-host')
    client = client_for(observed)
    document = client.get('/api/ui/packages?monitor=build').json()
    assert len(row(document)['cells'][1]['lines']) == 1
    assert 'worker://fixture-host' not in str(document)


@pytest.mark.parametrize('reason', [None, '', ' \n ', 'blocked', 'Blocked.', 'Blocked: blocked'])
def test_missing_or_repeated_status_reason_adds_no_row(observed, client_for, reason):
    target = observed['targets'][0]['id']
    observed['builds'][PACKAGE][target].update(raw_status='blocked', details=reason)
    client = client_for(observed)
    document = client.get('/api/ui/packages?monitor=build').json()
    assert len(row(document)['cells'][1]['lines']) == 1


def test_status_prefix_is_not_repeated_and_last_success_is_preserved(observed, client_for):
    target = observed['targets'][0]['id']
    observed['builds'][PACKAGE][target].update(
        raw_status='unresolvable', details='Unresolvable:  nothing\n  provides fixture-lib',
        last_success={'version': '1.9', 'time': NOW.isoformat(), 'srcmd5': 'older-source'})
    client = client_for(observed)
    cell = row(client.get('/api/ui/packages?monitor=build').json())['cells'][1]
    assert [line[0]['text'] for line in cell['lines']] == [
        'Unresolvable', '1.9', 'nothing provides fixture-lib']
    assert cell['lines'][0][0]['href'].startswith('https://build.openruyi.cn/package/live_build_log/')
    assert cell['lines'][1][0]['href'] == f'/packages/{PACKAGE}#build'


def test_long_reason_is_complete_in_both_listing_and_detail(observed, client_for):
    target = observed['targets'][0]['id']
    reason = 'nothing provides ' + ', '.join(f'fixture-library-{i}' for i in range(40))
    observed['builds'][PACKAGE][target].update(raw_status='unresolvable', details=reason)
    client = client_for(observed)
    cell = row(client.get('/api/ui/packages?monitor=build').json())['cells'][1]
    preview = cell['lines'][-1][0]
    assert preview['text'] == reason
    assert preview['href'] == f'/packages/{PACKAGE}#build'
    detail = client.get(f'/api/ui/packages/{PACKAGE}').json()
    build = next(section for section in detail['sections'] if section['id'] == 'build')
    assert any(value['text'] == reason for row in build['table']['rows']
               for cell in row['cells'] for value in values(cell))


def test_multiple_flavors_keep_reasons_attached_to_flavor_status(observed, client_for):
    target = observed['targets'][0]['id']
    flavor = PACKAGE + ':tools'
    observed['inventory'][flavor] = PACKAGE
    observed['builds'][flavor] = deepcopy(observed['builds'][PACKAGE])
    observed['builds'][PACKAGE][target].update(raw_status='failed', details='test fixture failed')
    observed['builds'][flavor][target].update(raw_status='blocked', details='waiting for fixture-toolkit')
    client = client_for(observed)
    document = client.get('/api/ui/packages?monitor=build').json()
    cells = row(document)['cells']
    assert cells[1]['lines'][0][0]['text'] == 'Failed'
    assert cells[1]['lines'][-1][0]['text'] == (
        'fixture-app: test fixture failed; fixture-app:tools · Blocked: waiting for fixture-toolkit')
    assert 'fixture-toolkit' not in str(cells[2:])
    detail = client.get(f'/api/ui/packages/{PACKAGE}').json()
    build = next(section for section in detail['sections'] if section['id'] == 'build')
    flavored = next(row for row in build['table']['rows'] if row['key'] == f'{target}:{flavor}')
    assert flavored['cells'][0]['lines'][0][1]['text'] == flavor
    assert [line[0]['text'] for line in flavored['cells'][1]['lines']] == [
        'Blocked', 'waiting for fixture-toolkit']


def test_all_build_status_icons_keep_accessible_names_and_log_links():
    from tracker.monitors.build.status import STATES, label
    from tracker.presentation.build import build_cell
    from tracker.presentation.labels import icon_only
    for code, state in {**STATES, 'future-status': STATES['unknown']}.items():
        observed = dict(raw_status=code, text=state.text, kind=state.kind,
                        log_url='https://example.org/log', updated_at=None)
        value = build_cell(observed, None, '/packages/example').lines[0][0]
        assert value.text == label(code)
        assert value.href == observed['log_url']
        assert code in value.title
        assert value.kind == ('icon' if code in STATES and icon_only(label(code)) else 'text')
        assert bool(value.icon) == (code in STATES)


def test_build_glyphs_follow_obs_semantics_and_declared_palette():
    from tracker.presentation.labels import icon, appearance, palettes
    from tracker.presentation.build import build_cell
    expected = {
        'succeeded': ('Succeeded', 'check'),
        'failed': ('Failed', 'circle-exclamation'),
        'unresolvable': ('Unresolvable', 'circle-exclamation'),
        'broken': ('Broken', 'circle-exclamation'),
        'blocked': ('Blocked', 'shield'),
        'scheduled': ('Scheduled', 'hourglass-half'),
        'dispatching': ('Dispatching', 'plane-departure'),
        'building': ('Building', 'gear'),
        'signing': ('Signing', 'signature'),
        'finished': ('Finishing', 'check'),
        'disabled': ('Disabled', 'ban'),
        'excluded': ('Excluded', 'ban'),
        'locked': ('Locked', 'lock'),
        'deleting': ('Deleting', 'eraser'),
        'unknown': ('No result', 'question'),
    }
    for code, (label, glyph) in expected.items():
        value = build_cell(dict(raw_status=code, text=label, kind='muted',
                                updated_at=None), None, '/packages/example').lines[0][0]
        assert value.icon == icon(label) == 'obs-' + glyph
        assert value.appearance == appearance(label)
        assert value.appearance in palettes()
    from collections import Counter
    shared = Counter(glyph for _, glyph in expected.values())
    for code, (label, glyph) in expected.items():
        value = build_cell(dict(raw_status=code, text=label, kind='muted', updated_at=None),
                           None, '/packages/example').lines[0][0]
        assert value.kind == ('icon' if code == 'succeeded' or shared[glyph] == 1 else 'text')


def test_success_is_icon_only_and_build_detail_retains_time(observed, client_for):
    from tracker.presentation.build import build_cell
    value = build_cell(dict(raw_status="succeeded", kind="ok", updated_at="2026-01-01T00:00:00Z"), None, "").lines[0][0]
    assert value.kind == "icon"
    assert value.text == "Succeeded"
    detail = client_for(observed).get(f"/api/ui/packages/{PACKAGE}").json()
    build = next(section for section in detail["sections"] if section["id"] == "build")
    assert [column["title"] for column in build["table"]["columns"]] == ["Target", "Result", "Last successful version", "Succeeded at"]
