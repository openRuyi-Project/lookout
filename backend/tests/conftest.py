from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
import pytest
import tomlkit

from tracker import state
from tracker.config import track_fingerprint

pytest_plugins = ["tests.helpers.nvchecker"]


class ProjectedClient(TestClient):
    """Deterministic HTTP fixtures: explicitly prepare data before each request.

    Lifespan/concurrency tests use the unmodified TestClient and actual worker.
    Production requests only read; they never call this refresh hook.
    """
    def send(self, *args, **kwargs):
        self.app.state.projection.refresh()
        return super().send(*args, **kwargs)

@pytest.fixture
def config():
    return {'obs': {'api_url': 'http://fixture', 'web_url': 'https://build.openruyi.cn', 'project': 'openruyi'},
            'targets': [{'id': 'rva23', 'label': 'rva23', 'repository': 'riscv64', 'architecture': 'riscv64'},
                        {'id': 'rva20', 'label': 'rva20', 'repository': 'rva20', 'architecture': 'riscv64'},
                        {'id': 'x86_64', 'label': 'x86_64', 'repository': 'x86_64', 'architecture': 'x86_64'}],
            'native': {'binutils': {'source': 'manual'}, 'widget@3': {'source': 'manual'}, 'widget@4': {'source': 'manual'}, 'not-in-obs': {'source': 'manual'}},
            'packages': {'foo3': {'compare': 'widget@3', 'watch': ['widget@4'], 'track_label': '3.x'},
                         'foo4': {'compare': 'widget@4', 'track_label': '4.x'}},
            'collector': {'source_batch_size': 100, 'source_workers': 2, 'stale_after_seconds': 86400}}

def make_snapshot(config, now=None):
    now = now or state.utcnow()
    s = state.empty()
    s.update(generation=1, last_attempt=now, mode='fixture', targets=config['targets'],
             bindings=config['packages'], obs=config['obs'], native_ids=list(config['native']), stale_after_seconds=86400)
    s['inventory'] = {n: n for n in ['binutils', 'foo3', 'foo4', 'untracked', 'unknown']}
    s['inventory']['foo3:tools'] = 'foo3'
    for name, version in [('binutils','3.9.0'),('foo3','3.10.0'),('foo4','4.1.0'),('untracked','1.0'),('unknown',None)]:
        s['sources'][name] = state.success({}, {'version': version, 'srcmd5': 'h-'+name, 'rev': '1'}, now)
        s['index'][name] = {'srcmd5': 'h-'+name, 'rev': '1'}
    for name in s['inventory']:
        s['builds'][name] = {t['id']: state.success({}, {'raw_status': 'succeeded', 'matches_source': None}, now) for t in config['targets']}
    s['builds']['foo3:tools']['rva20']['raw_status'] = 'failed'
    s['builds']['foo4']['rva23']['raw_status'] = 'disabled'
    s['builds']['foo4']['rva20']['raw_status'] = 'excluded'
    for name, version in [('binutils','3.10.0'),('widget@3','3.10.0'),('widget@4','4.2.0')]:
        s['tracks'][name] = state.success({}, {'version': version, 'source_checked_at': None, 'source': {'source': 'fixture'}, 'configuration_fingerprint': track_fingerprint(config['native'][name])}, now)
    s['components'] = {key: state.success({}, {}, now) for key in ['inventory','source_index','targets','builds','nvchecker']}
    return s

@pytest.fixture
def snapshot(config):
    return make_snapshot(config)


@pytest.fixture
def configured_path(config, tmp_path):
    """The small in-memory fixture, validated through the real file loader."""
    from tracker import config as cfg
    native = tmp_path / 'native.toml'
    native.write_text(tomlkit.dumps(config['native']))
    config['collector']['nvchecker_config'] = native.name
    (tmp_path / 'packages.toml').write_text(tomlkit.dumps(config.get('packages', {})))
    path = tmp_path / 'tracker.toml'
    path.write_text(tomlkit.dumps({'packages_config': 'packages.toml', **{
        key: config[key] for key in ('obs', 'targets', 'collector')}}))
    config.update(cfg.load(path))
    return path


@pytest.fixture
def scoped_client(snapshot, tmp_path, monkeypatch):
    from tracker.readmodel import snapshot as view
    from tracker.readmodel.packages import PackageList
    from tracker.api import create_app
    monkeypatch.setattr(state, 'compare', lambda current, latest, *args:
                        'unknown' if not current or not latest else
                        'current' if current == latest else 'outdated')
    snapshot['monitor_catalog'] = {
        'requires': {'title': 'Requires'},
        'fixture_signature': {'title': 'Artifact signatures'},
    }
    rows, collection = view.project_monitors(snapshot)
    checks = {'binutils': 'ok', 'foo3': 'unsupported', 'foo4': 'error',
              'unknown': 'not_configured', 'untracked': 'ok'}
    requirement_choices = {'binutils': ['unmet'], 'untracked': ['changes']}
    for row in rows:
        name = row['name']
        system = 'meson' if name == 'foo4' else 'cmake'
        source = row['monitors']['source']
        source['data']['buildsystem'] = system
        source['dimensions']['buildsystem'] = [system]
        requires = row['monitors']['requires']
        requires['check']['status'] = checks[name]
        requires['dimensions'].update({
            'requires': requirement_choices.get(name, []),
            'check:requires': [checks[name]],
            'findings:requires': ['yes'] if name in requirement_choices else [],
            'maintenance': ['Requires'] if name in requirement_choices else [],
        })
        signature = row['monitors']['fixture_signature']
        signature['check']['status'] = checks[name]
        signature['dimensions'].update({
            'check:fixture_signature': [checks[name]],
            'findings:fixture_signature': ['yes'] if name in ('foo3', 'untracked') else [],
            'maintenance': ['Signature'] if name in ('foo3', 'untracked') else [],
        })
    index = PackageList(rows, snapshot['targets'])
    app = create_app(tmp_path / 'unused.sqlite3')
    monkeypatch.setattr(app.state.projection, 'read', lambda: (snapshot, index, collection))
    return TestClient(app)
