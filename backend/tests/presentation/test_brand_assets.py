"""Vendored artwork is local, attributable and inert; no provider-name guessing."""
import hashlib
import json
from pathlib import Path
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
        assert svg.tag == '{http://www.w3.org/2000/svg}svg' and (svg.get('viewBox') or (svg.get('width') and svg.get('height')))
        for element in svg.iter():
            assert element.tag.rsplit('}', 1)[-1] in {'svg', 'path', 'g', 'defs', 'clipPath', 'use', 'title', 'metadata', 'RDF', 'Work', 'format', 'type', 'namedview', 'linearGradient', 'radialGradient', 'stop', 'ellipse', 'circle', 'mask', 'rect'}
            for key, value in element.attrib.items():
                assert not key.startswith('on')
                if key.rsplit('}', 1)[-1] == 'href':
                    assert value.startswith('#')
    from tracker import config
    settings = config.load(ROOT / 'config/tracker.toml')
    for style in settings['openruyi']['buildsystems'].values():
        if style.get('icon'):
            assert style['icon'] in catalog


def test_build_icons_are_local_inert_and_filters_keep_text():
    from tracker.presentation.labels import CATALOG
    from tracker.presentation.navigation import maintenance_navigation, Links
    from tracker.presentation.values import text
    for label, entry in CATALOG.items():
        if not entry.get('icon'):
            continue
        svg = ET.fromstring((ROOT / 'frontend/src/assets/icons' / (entry['icon'] + '.svg')).read_bytes())
        for element in svg.iter():
            assert element.tag.rsplit('}', 1)[-1] in {'svg', 'path', 'circle', 'g'}
            assert not any(key.startswith('on') or key.endswith('href') for key in element.attrib)
        assert text(label, kind='tag').icon is None
    from tracker.monitors.build.status import STATES, label
    assert all(CATALOG[label(code)].get('icon') for code in STATES)
    navigation = maintenance_navigation({'maintenance_labels': {}, 'version_signals': {}}, Links())
    for choice in navigation.choices:
        assert choice.icon is None
        assert choice.label and choice.href


def test_build_navigation_choices_are_text_only():
    from tracker.presentation.navigation import build_navigation, Links
    payload = {'targets': [{'id': 'fixture', 'label': 'Fixture'}],
               'build_statuses': {'fixture': [{'label': 'Failed', 'count': 1, 'value': 'failed'}]}}
    choices = build_navigation(payload, Links())['fixture'].choices
    assert choices[0].label == 'Failed'
    assert choices[0].icon is None
