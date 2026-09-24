"""Populate only the fresh volumes owned by deploy/smoke-image.py."""
import os
from pathlib import Path
import sys
import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tracker import config, state

PACKAGE = 'smoke-fixture'
VERSION = '1.2.3'


def prepare(mode, config_dir=Path('/config'), data_dir=Path('/data')):
    targets = [dict(id=name, label=name, repository=name, architecture=arch)
               for name, arch in [('rva23', 'riscv64'), ('rva20', 'riscv64'), ('x86_64', 'x86_64')]]
    cfg = {'obs': {'api_url': 'http://127.0.0.1:9', 'web_url': 'http://127.0.0.1:9', 'project': 'fixture'},
           'collector': {'obs_interval_seconds': 3600, 'obs_stale_after_seconds': 7200,
                         'nvchecker_interval_seconds': 3600, 'stale_after_seconds': 7200,
                         'nvchecker_timeout_seconds': 5, 'timeout_seconds': 1,
                         'native_spec_fallback': True, 'source_batch_size': 10, 'source_workers': 1}, 'monitors': {'enabled': []}}
    # Native nvchecker input, with no remote entries to fetch.
    (config_dir / 'nvchecker.toml').write_text('[__config__]\nmax_concurrency = 1\n')
    text = tomlkit.dumps({**cfg, 'targets': targets})
    path = config_dir / 'tracker.toml'
    path.write_text(text)
    config.load(path)
    if mode == 'invalid':
        path.write_text('broken = [')
    if mode == 'seeded':
        now = state.utcnow()
        snap = state.empty()
        snap.update(generation=1, targets=targets, obs=cfg['obs'], last_attempt=now,
                    inventory={PACKAGE: PACKAGE}, stale_after_seconds=7200,
                    obs_stale_after_seconds=7200)
        snap['sources'][PACKAGE] = state.success({}, {'version': VERSION, 'srcmd5': 'fixture'}, now)
        snap['builds'][PACKAGE] = {t['id']: state.success({}, {'raw_status': 'succeeded'}, now) for t in targets}
        state.commit(data_dir / 'state/tracker.sqlite3', snap)
    for root in (config_dir, data_dir):
        for path in [root, *root.rglob('*')]:
            os.chown(path, 10001, 10001)
            path.chmod(0o700 if path.is_dir() else 0o600)
    if mode == 'unwritable':
        os.chown(data_dir, 0, 0)
        data_dir.chmod(0o500)


if __name__ == '__main__':
    prepare(sys.argv[1])
