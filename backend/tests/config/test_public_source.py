"""Authoring shorthand cannot lose monitor identity or public provenance."""
import json

import pytest

from tests.helpers.config import setup_config
from tracker import config as cfg, config_change, package
from tracker.identity import from_native, request_url
from tracker.monitors import runner
from tracker.monitors.version.nvchecker import import_events


@pytest.mark.parametrize('name, path', [
    ('a', '1/a'), ('ab', '2/ab'), ('AbC', '3/a/abc'),
    ('Cargo', 'ca/rg/cargo'), ('x_y-z', 'x_/y-/x_y-z'),
])
def test_cargo_index_address_and_public_identity(name, path):
    compact = dict(source='crates_index', cratesio=name)
    explicit = dict(source='crates_index', url='https://index.crates.io/' + path)
    assert request_url(compact) == explicit['url']
    assert cfg.public_source(compact) == cfg.public_source(explicit)
    expected = {'ecosystem': 'crates.io', 'name': name.lower()}
    assert from_native(compact) == from_native(cfg.public_source(compact)) == expected


@pytest.mark.parametrize('source', ['jq', 'anitya_stable', 'regex'])
def test_anitya_url_keeps_public_identity_without_secrets(source):
    entry = dict(source=source, url='https://user:password@release-monitoring.org/api/v2/versions/'
                 '?project_id=37&token=SECRET', cmd='secret command')
    public = cfg.public_source(entry)
    assert public['project_id'] == 37
    assert public['project_url'] == 'https://release-monitoring.org/project/37/'
    assert all(secret not in json.dumps(public) for secret in ('password', 'SECRET', 'token=', 'user:', 'cmd'))


@pytest.mark.parametrize('url', [
    'https://other.example/api/v2/versions/?project_id=37',
    'https://release-monitoring.org.evil.example/api/v2/versions/?project_id=37',
    'https://release-monitoring.org:8443/api/v2/versions/?project_id=37',
    'https://release-monitoring.org/api/v2/versions/?project_id=7&project_id=8',
    'https://release-monitoring.org/api/v2/versions/?project_id=0',
    'https://release-monitoring.org/wrong/?project_id=37',
    'https://release-monitoring.org:invalid/api/v2/versions/?project_id=37',
])
def test_untrusted_endpoint_is_not_a_project_identity(url):
    assert 'project_id' not in cfg.public_source(dict(source='anitya_stable', url=url))


def test_compact_identity_survives_explain_and_event_import(tmp_path):
    path = setup_config(tmp_path / 'config')
    entry = dict(source='anitya_stable', anitya_id=37, prefix='v')
    native = path.parent / 'native.toml'
    native.write_text(config_change.edit_tables(native.read_text(), {'widget': entry}))
    expected = cfg.public_source(dict(source='jq', url=request_url(entry)))
    expected['source'] = 'anitya_stable'
    assert cfg.public_source(entry) == expected
    assert package.explain(path, 'widget')['rules'][0]['source'] == expected
    event = json.dumps(dict(name='widget', event='updated', version='2.0'))
    facts, error = import_events(event, {'widget': entry}, {}, '2026-01-01T00:00:00+00:00')
    assert error is None
    assert facts['widget']['source'] == expected
    assert facts['widget']['configuration_fingerprint'] == cfg.track_fingerprint(entry)


@pytest.mark.parametrize('monitor', ['security', 'requires', 'license', 'yanked'])
def test_adapter_inputs_survive_public_projection(config, snapshot, monitor):
    entry = dict(source='crates_index', cratesio='upstream-widget')
    config['native']['binutils'] = dict(source='crates_index', url=request_url(entry))
    before = runner.plan(config, snapshot, 'binutils', monitor)['inputs']
    config['native']['binutils'] = entry
    after = runner.plan(config, snapshot, 'binutils', monitor)['inputs']
    assert before is not None
    assert after == before
