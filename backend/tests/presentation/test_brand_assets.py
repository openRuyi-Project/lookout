"""Vendored artwork is local, attributable and inert; no provider-name guessing."""
import hashlib
import json
from pathlib import Path
import tomllib
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]


def test_declared_logos_are_complete_local_assets():
    directory = ROOT / 'frontend/src/assets/logos'
    catalog = json.loads((directory / 'catalog.json').read_text())
    assert set(catalog) == {path.stem for path in directory.glob('*.svg')}
    for name, entry in catalog.items():
        data = (directory / (name + '.svg')).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry['sha256']
        assert all(entry[field] for field in ('title', 'credit', 'source', 'asset_source', 'license', 'license_url'))
        assert entry['source'].startswith('https://') and entry['asset_source'].startswith('https://')
        assert b'<!ENTITY' not in data and b'<!DOCTYPE' not in data
        svg = ET.fromstring(data)
        assert svg.tag == '{http://www.w3.org/2000/svg}svg' and svg.get('viewBox')
        for element in svg.iter():
            assert element.tag.rsplit('}', 1)[-1] in {'svg', 'path', 'g', 'defs', 'clipPath', 'use', 'title'}
            for key, value in element.attrib.items():
                assert not key.startswith('on')
                if key.rsplit('}', 1)[-1] == 'href':
                    assert value.startswith('#')
    settings = tomllib.loads((ROOT / 'config/tracker.toml').read_text())
    for style in settings['openruyi']['buildsystems'].values():
        if style.get('icon'):
            assert style['icon'] in catalog
