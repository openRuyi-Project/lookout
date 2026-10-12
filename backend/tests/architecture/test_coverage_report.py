# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
import runpy
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

normalize = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts/coverage-report.py'))['normalize']


def report(sources, name):
    tree = ET.ElementTree(ET.Element('coverage'))
    parent = ET.SubElement(tree.getroot(), 'sources')
    for source in sources:
        ET.SubElement(parent, 'source').text = str(source)
    ET.SubElement(tree.getroot(), 'class', filename=name)
    return tree


def test_repository_paths(tmp_path):
    source = tmp_path / 'backend/tracker'
    source.mkdir(parents=True)
    (source / 'api.py').touch()
    tree = report([source], 'api.py')
    normalize(tree, tmp_path)
    assert tree.find('.//class').get('filename') == 'backend/tracker/api.py'
    assert tree.find('./sources/source').text == '.'


def test_reject_missing_ambiguous_and_external_paths(tmp_path):
    for subdir in ('one', 'two'):
        (tmp_path / subdir).mkdir()
        (tmp_path / subdir / 'api.py').touch()
    for sources, name in [([tmp_path], 'missing.py'),
                          ([tmp_path / 'one', tmp_path / 'two'], 'api.py'),
                          ([tmp_path.parent], 'external.py')]:
        with pytest.raises(ValueError):
            normalize(report(sources, name), tmp_path)
