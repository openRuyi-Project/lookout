"""Contributor edits resolve to real data/code locations without writing observations."""
import json
from pathlib import Path

import httpx
import pytest
import tomlkit

from tests.helpers.config import setup_config
from tracker import config as cfg, package, state
from tracker.monitors import __main__ as cli
from tracker.providers.client import IO


def configuration(tmp_path, policy=None):
    path = setup_config(tmp_path / 'config', version='upstream-widget')
    if policy is not None:
        (path.parent / 'packages.toml').write_text(policy)
    else:
        document = tomlkit.parse(path.read_text())
        del document['packages_config']
        path.write_text(tomlkit.dumps(document))
    with path.open('a') as stream:
        stream.write('\n[monitors]\nenabled = ["eol", "license"]\n')
    return path


def observed_snapshot(config):
    snapshot = state.empty()
    snapshot.update(generation=1, native_ids=list(config['native']), bindings=config['packages'])
    snapshot['sources']['widget'] = state.success({}, {'version': '1.2.3', 'srcmd5': 'revision'}, state.utcnow())
    return snapshot


@pytest.mark.parametrize('policy, line, table', [
    ('# purpose\n[widget]\nmonitors = { eol = { product = "widget", cycle_parts = 2 } }\n', 2, ['widget']),
    ('[widget.monitors]\neol = { product = "widget", cycle_parts = 2 }\n', 1, ['widget', 'monitors']),
    ('widget = { monitors = { eol = { product = "widget", cycle_parts = 2 } } }\n', 1, ['widget']),
    ('[widget.monitors.eol]\nproduct = "widget"\ncycle_parts = 2\n', 1, ['widget', 'monitors', 'eol']),
])
def test_explicit_monitor_locates_its_authored_parent_without_a_database(tmp_path, monkeypatch, capsys,
                                                                       policy, line, table):
    path = configuration(tmp_path, policy)
    monkeypatch.setattr(cli, 'IO', lambda: pytest.fail('explain must not create provider IO'))
    assert cli.main(['explain', 'widget', '--monitor', 'eol', '--config', str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['mapping'] == 'explicit'
    assert result['inputs'] == {'product': 'widget', 'cycle_parts': 2}
    assert result['configuration'] == {'file': str(path.parent / 'packages.toml'), 'table': table, 'line': line}
    assert result['module'] == 'tracker.monitors.eol'
    assert Path(result['code']).is_file()
    assert result['observation_available'] is False
    assert result['subject']['version'] is None and result['status'] != 'ok'
    assert result['refresh']['due'] is False


def test_package_binding_uses_the_policy_file_not_operator_config(tmp_path):
    path = configuration(tmp_path, '# line policy\n[widget]\ntrack_label = "1.x"\n')
    result = package.explain(path, 'widget')
    assert result['binding_location'] == {'file': str(path.parent / 'packages.toml'), 'table': ['widget'], 'line': 2}
    assert result['binding']['track_label'] == '1.x'
    assert result['rules'][0]['file'] == str(path.parent / 'native.toml')
    assert 'Policy: ' + str(path.parent / 'packages.toml') + ':2' in package.human_result('explain', result)


def test_derived_license_uses_the_native_identity_without_claiming_an_observation(tmp_path, capsys):
    path = configuration(tmp_path)
    missing = tmp_path / 'absent.sqlite3'
    args = ['explain', 'widget', '--monitor', 'license', '--config', str(path), '--db', str(missing)]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['mapping'] == 'derived' and result['inputs'] == {'pypi': 'upstream-widget'}
    assert result['configuration'] is None
    assert result['version_rule']['file'] == str(path.parent / 'native.toml')
    assert result['observation_available'] is False and result['status'] != 'ok'
    assert not missing.exists()
    assert cli.main([*args, '--format', 'human']) == 0
    output = capsys.readouterr().out
    assert 'Mapping: derived' in output and 'upstream-widget' in output
    assert 'Observation: none; configuration only' in output
    assert 'Identity: native rule widget' in output and str(path.parent / 'native.toml') in output
    assert 'Override:' not in output
    assert 'fingerprint' not in output and '"findings"' not in output


def test_source0_identity_is_resolved_by_the_existing_planner(tmp_path, capsys):
    path = configuration(tmp_path)
    config = cfg.load(path)
    snapshot = observed_snapshot(config)
    snapshot['specs']['widget'] = state.success({}, {
        'head': 'source-head',
        'native_query': {'spec_sha256': 'a' * 64, 'context': {'resolver': 6}},
        'metadata': {'version': '1.2.3', 'sources': [
            {'number': 0, 'url': 'https://static.crates.io/crates/source-widget/source-widget-1.2.3.crate'}]},
    }, state.utcnow())
    database = tmp_path / 'snapshot.sqlite3'
    state.commit(database, snapshot)
    before = database.read_bytes()
    assert cli.main(['explain', 'widget', '--monitor', 'license', '--config', str(path), '--db', str(database)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['mapping'] == 'derived' and result['inputs'] == {'cratesio': 'source-widget'}
    assert result['identity_context'] == {'identity': {'ecosystem': 'crates.io', 'name': 'source-widget'},
                                          'origin': 'source_release'}
    assert result['observation_available'] is True
    # The conflicting configured PyPI rule must not silently displace Source0.
    assert result['status'] == 'unsupported'
    assert database.read_bytes() == before
    assert cli.main(['explain', 'widget', '--monitor', 'license', '--config', str(path),
                     '--db', str(database), '--format', 'human']) == 0
    output = capsys.readouterr().out
    assert 'Identity: observed Source0' in output and 'source-widget-1.2.3.crate' in output
    assert database.read_bytes() == before


def test_changed_binding_distinguishes_planner_inputs_from_configured_rule(tmp_path, capsys):
    path = configuration(tmp_path, '[widget]\ncompare = "new-track"\n')
    native = path.parent / 'native.toml'
    with native.open('a') as stream:
        stream.write('\n[new-track]\nsource = "pypi"\npypi = "next-project"\n')
    config = cfg.load(path)
    snapshot = observed_snapshot(config)
    snapshot['bindings'] = {'widget': {'compare': 'widget'}}
    database = tmp_path / 'snapshot.sqlite3'
    state.commit(database, snapshot)
    args = ['explain', 'widget', '--monitor', 'license', '--config', str(path), '--db', str(database)]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['inputs'] == {'pypi': 'upstream-widget'}
    assert result['version_rule']['table'] == ['widget']
    assert result['configured_version_rule']['table'] == ['new-track']
    assert result['identity_context'] == {'identity': {'ecosystem': 'PyPI', 'name': 'upstream-widget'},
                                          'origin': 'native'}
    assert cli.main([*args, '--format', 'human']) == 0
    output = capsys.readouterr().out
    assert 'Identity: native rule widget' in output
    assert 'Configured rule: new-track' in output and 'not the snapshot binding' in output


def test_unconfigured_monitor_does_not_invent_an_identity_or_line(tmp_path, capsys):
    path = configuration(tmp_path, '[another]\nmonitors = { eol = { product = "another", cycle_parts = 1 } }\n')
    assert cli.main(['explain', 'new-package', '--monitor', 'eol', '--config', str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['mapping'] == 'unconfigured' and result['inputs'] is None
    assert result['configuration']['line'] is None
    assert result['configuration']['table'] == ['new-package', 'monitors', 'eol']
    assert result['version_rule'] is None


def test_monitor_list_needs_neither_database_nor_io(tmp_path, monkeypatch, capsys):
    path = configuration(tmp_path)
    monkeypatch.setattr(cli, 'IO', lambda: pytest.fail('list must not create provider IO'))
    assert cli.main(['list', '--config', str(path), '--format', 'human']) == 0
    output = capsys.readouterr().out
    assert 'license: enabled' in output and 'eol: enabled' in output
    assert 'monitors/license.py' in output and 'monitors/eol.py' in output


def test_check_requires_an_observation_before_opening_provider_io(tmp_path, monkeypatch, capsys):
    path = configuration(tmp_path)
    monkeypatch.setattr(cli, 'IO', lambda: pytest.fail('ineligible check must not create provider IO'))
    assert cli.main(['check', 'widget', '--monitor', 'license', '--config', str(path)]) == 2
    assert 'observed package' in json.loads(capsys.readouterr().out)['error']


def test_explicit_eol_check_leaves_database_and_config_unchanged(tmp_path, monkeypatch, capsys):
    path = configuration(tmp_path, '[widget]\nmonitors = { eol = { product = "widget", cycle_parts = 2 } }\n')
    config = cfg.load(path)
    database = tmp_path / 'snapshot.sqlite3'
    state.commit(database, observed_snapshot(config))
    before = database.read_bytes()
    inputs = {p: Path(p).read_bytes() for p in config['input_hashes']}
    requests = []

    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={'result': {'releases': [{'name': '1.2', 'isEol': False}]}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        io = IO(client=client)
        monkeypatch.setattr(cli, 'IO', lambda: io)
        assert cli.main(['check', 'widget', '--monitor', 'eol', '--config', str(path), '--db', str(database)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'ok' and result['read_only'] is True
    assert requests == ['https://endoflife.date/api/v1/products/widget/']
    assert database.read_bytes() == before
    assert {p: Path(p).read_bytes() for p in inputs} == inputs


def test_package_check_rejects_policy_changes_during_the_native_check(tmp_path, monkeypatch):
    path = configuration(tmp_path, '[widget]\ntrack_label = "1.x"\n')
    policy = path.parent / 'packages.toml'

    def run(*args, **kwargs):
        policy.write_text('[widget]\ntrack_label = "2.x"\n')
        return {'widget': {'version': '1.2.3', 'error': None}}, None

    monkeypatch.setattr(package.nv, 'run', run)
    with pytest.raises(ValueError, match='configuration changed'):
        package.check(path, 'widget')
