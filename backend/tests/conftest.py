from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import tomlkit
from tracker import state
from tracker.config import track_fingerprint

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
    path = tmp_path / 'tracker.toml'
    path.write_text(tomlkit.dumps({key: config[key]
                                  for key in ('obs', 'targets', 'collector', 'packages')}))
    config.update(cfg.load(path))
    return path
