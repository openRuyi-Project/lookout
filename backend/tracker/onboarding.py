"""Offline proposals from confined RPM facts; never a runtime version authority."""
from pathlib import Path
from . import config as cfg, discover_sources


def propose(config_path, name, snapshot, *, config=None):
    config = cfg.load(config_path) if config is None else config
    if cfg.binding(config, name)['compare']:
        raise ValueError('package already has a version rule; use explain/check')
    if name not in snapshot.get('sources', {}):
        raise ValueError('package is not present in the source inventory')
    spec = snapshot.get('specs', {}).get(name, {})
    meta = spec.get('metadata') or {}
    row = {'current': meta.get('version'), 'spec_sha256': spec.get('native_query', {}).get('spec_sha256')}
    hint = discover_sources.hints(row, spec)
    current = row['current']
    if hint.get('hint_error') or snapshot['sources'][name].get('error') or snapshot['sources'][name].get('version') != current:
        raise ValueError('matching native SPEC/source version evidence is required')
    source = hint.get('source_url', '')
    result = {'name': name, 'current': current, 'source0': source, 'spec_sha256': row['spec_sha256'],
              'rule_file': str(Path(config['nvpath']).resolve()), 'rule_table': name,
              'entry': None, 'review_required': True, 'state_writes': False,
              'reason': 'no safe automatic proposal; supply a reviewed native rule'}
    entry = discover_sources.registry_entry({**row, **hint})
    if entry:
        result.update(entry=entry, reason='exact crates.io source identity; compatibility line requires review')
    return result
