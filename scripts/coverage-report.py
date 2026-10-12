#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Validate Cobertura source paths and emit repository-relative filenames."""
import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def normalize(report, root):
    root = root.resolve()
    sources = [Path(node.text or '.') for node in report.findall('./sources/source')]
    for node in report.findall('.//class'):
        name = node.attrib['filename']
        matches = set()
        for source in sources:
            path = (source / name).resolve()
            if path.is_relative_to(root) and path.is_file():
                matches.add(path.relative_to(root).as_posix())
        if len(matches) != 1:
            raise ValueError(f'Coverage path must identify one repository file: {name}')
        node.set('filename', matches.pop())
    sources_node = report.find('sources')
    if sources_node is None:
        raise ValueError('Coverage report has no sources')
    sources_node.clear()
    ET.SubElement(sources_node, 'source').text = '.'


if __name__ == '__main__':
    document = ET.parse(sys.argv[1])
    normalize(document, Path(sys.argv[2]))
    document.write(sys.stdout, encoding='unicode', xml_declaration=True)
