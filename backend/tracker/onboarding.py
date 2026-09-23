"""Offline proposals from confined RPM facts; never a runtime version authority."""
import re
from pathlib import Path
from urllib.parse import urlsplit
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
    # Registry Source0 encodes an exact component, unlike a shared homepage.
    crate = re.fullmatch(r'https://(?:static\.crates\.io/crates/|crates\.io/api/v1/crates/)([A-Za-z0-9_-]+)/(.+)', source)
    if crate and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', current or ''):
        tail = crate[2]
        if tail in (current + '/download', crate[1] + '-' + current + '.crate'):
            major, minor, _ = current.split('.')
            line = re.escape(major + '.' + minor) + r'\.[0-9]+' if major == '0' else re.escape(major) + r'\.[0-9]+\.[0-9]+'
            result.update(entry={'source': 'cratesio', 'cratesio': crate[1], 'include_regex': '^'+line+'$'},
                          reason='exact crates.io source identity; compatibility line requires review')
    return result
