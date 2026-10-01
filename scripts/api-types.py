#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Generate the UI contract with openapi-typescript; --check detects drift."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from tracker.api import create_app


def ui_contract(document):
    """Only reading documents cross the UI boundary; include their referenced schemas."""
    schemas = document['components']['schemas']
    selected = set()

    def visit(value):
        if isinstance(value, dict):
            reference = value.get('$ref')
            if reference:
                name = reference.rsplit('/', 1)[-1]
                if name not in selected:
                    selected.add(name)
                    visit(schemas[name])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for path, operations in document['paths'].items():
        if path.startswith('/api/ui/'):
            visit(operations['get']['responses']['200'])
    return {**document, 'paths': {}, 'components': {'schemas': {name: schemas[name] for name in sorted(selected)}}}


def render():
    return subprocess.run(
        ['node', str(ROOT / 'frontend/scripts/api-types.cjs')],
        input=json.dumps(ui_contract(create_app().openapi())), text=True,
        capture_output=True, check=True, timeout=30,
    ).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    output = ROOT / 'frontend/src/lib/api.generated.ts'
    try:
        expected = render()
    except (OSError, subprocess.SubprocessError):
        print('API generation failed: install Node and run npm ci in frontend; inspect the OpenAPI schema.', file=sys.stderr)
        return 1
    if args.check:
        if not output.is_file() or output.read_text() != expected:
            print('API contract drift: run python scripts/api-types.py', file=sys.stderr)
            return 1
        print('API contract matches OpenAPI')
        return 0
    output.write_text(expected)
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
