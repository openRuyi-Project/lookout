#!/usr/bin/env python3
# Guards this project's own invariants, in the spirit of openRuyi's pre-commit
# pygrep hooks. Pure standard library so it runs anywhere the repo is checked out.
#
# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Fail if a maintainability invariant drifts.

1. Read/write separation: the read path (api, view) must never import the
   write/network path (collector, obs, nv, native_spec).
2. Thin config: config/tracker.toml must hold no label-only binding whose label
   equals the value derived from the package name. Redundant bindings belong in
   the naming rule, not in the file.
3. No pinned versions: the native nvchecker config must not carry manual/hardcoded
   versions. Observations come from external sources, never from a checked-in value.
4. Preserve Anitya ordering: do not feed its already-sorted stable history back
   through nvchecker's different generic version comparator.
"""
import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from tracker.version_rules import load
WRITE_PATH = {'collector', 'obs', 'nv', 'native_spec', 'discover', 'discover_sources', 'package', 'config_change', 'spec_git', 'spec_worker', 'spec_sandbox', 'monitor', 'monitor_io', 'monitor_eol', 'monitor_security', 'monitor_cve', 'monitor_license'}
READ_PATH = ['api.py', 'view.py']


def _imported_submodules(node):
    """Every tracker submodule a node imports, across all import forms."""
    names = set()
    if isinstance(node, ast.Import):
        # import tracker.collector [as c]
        for alias in node.names:
            parts = alias.name.split('.')
            if parts[0] == 'tracker' and len(parts) > 1:
                names.add(parts[1])
    elif isinstance(node, ast.ImportFrom):
        if node.level and not node.module:
            # from . import collector, view
            names.update(alias.name for alias in node.names)
        elif node.module:
            # from .collector import x  /  from tracker.collector import x
            names.add(node.module.split('.')[-1])
    return names


def check_read_write_separation():
    errors = []
    root = ROOT / 'backend/tracker'
    graph = {}
    for path in root.glob('*.py'):
        graph[path.stem] = set().union(*(_imported_submodules(n) for n in ast.walk(ast.parse(path.read_text(), str(path)))))
    for filename in READ_PATH:
        start = Path(filename).stem
        todo, visited = [(start, [start])], set()
        while todo:
            name, chain = todo.pop()
            if name in visited:
                continue
            visited.add(name)
            for dependency in graph.get(name, ()):
                if dependency in WRITE_PATH or (dependency.startswith('monitor_') and dependency not in ('monitor_model', 'monitor_views')):
                    errors.append('read path reaches write module: ' + ' -> '.join([*chain, dependency]))
                elif dependency in graph:
                    todo.append((dependency, [*chain, dependency]))
    return errors


def check_thin_config():
    errors = []
    cfg = tomllib.loads((ROOT / 'config/tracker.toml').read_text())
    rule = re.compile(r'-(\d+(?:\.\d+)*)$')
    for name, binding in cfg.get('packages', {}).items():
        if set(binding) != {'track_label'}:
            continue
        m = rule.search(name)
        derived = f'{m.group(1)}.x' if m else 'stable'
        if binding['track_label'] == derived:
            errors.append(f'[packages."{name}"] label "{binding["track_label"]}" is derivable; drop it')
    return errors


def check_no_pinned_versions():
    errors = []
    native = load((ROOT / 'config/versions/nvchecker.toml')).entries
    for name, entry in native.items():
        if name == '__config__':
            continue
        if isinstance(entry, dict) and entry.get('source') == 'manual':
            errors.append(f'["{name}"] uses source="manual"; observations must come from an external source')
    return errors


def check_provider_order():
    errors = []
    native = load((ROOT / 'config/versions/nvchecker.toml')).entries
    for name, entry in native.items():
        if (entry.get('source') == 'jq'
                and entry.get('url', '').startswith('https://release-monitoring.org/api/v2/versions/')
                and re.fullmatch(r'\s*\.stable_versions\s*\[\s*\]\s*', entry.get('filter', ''))):
            errors.append(f'["{name}"] re-sorts Anitya history; select the first eligible provider-ordered version in jq')
    return errors


def main():
    errors = (check_read_write_separation() + check_thin_config()
              + check_no_pinned_versions() + check_provider_order())
    for e in errors:
        print(f'architecture: {e}', file=sys.stderr)
    if errors:
        print(f'{len(errors)} invariant violation(s)', file=sys.stderr)
        return 1
    print('architecture invariants: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
