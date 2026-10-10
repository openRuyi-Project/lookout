"""Configured CPEs select exact-version NVD queries without embedded CVE lists."""
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tracker.monitors.security import monitor, nvd

POLICIES = tomllib.loads((Path(__file__).resolve().parents[3] / 'config/packages.toml').read_text())
IDENTITIES = [(name, policy['monitors']['security']) for name, policy in POLICIES.items()
              if 'vendor' in policy.get('monitors', {}).get('security', {})]


@pytest.mark.parametrize('name,settings', IDENTITIES, ids=[name for name, _ in IDENTITIES])
def test_catalog_uses_provider_matching_for_each_observed_version(name, settings):
    selected = monitor.inputs({'name': name, 'version': '91.82.73'}, settings)
    assert selected['source'] == 'nvd'
    for version in ('91.82.73', '91.82.74'):
        params = parse_qs(urlsplit(nvd.query_url(selected, version)).query, keep_blank_values=True)
        assert params['cpeName'] == [f'cpe:2.3:{settings.get("part", "a")}:{settings["vendor"]}:{settings["product"]}:{version}:*:*:*:*:*:*:*']
        assert params['isVulnerable'] == ['']
    assert nvd.query_url(selected, '0+git20260101.abcdef') is None
