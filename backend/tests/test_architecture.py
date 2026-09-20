# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Runs the architecture guard inside the test suite, so the invariants are
enforced on every candidate build, not only when someone remembers the script."""
import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / 'scripts/check-architecture.py'


def _guard():
    spec = importlib.util.spec_from_file_location('archguard', GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repo_satisfies_all_invariants():
    guard = _guard()
    errors = (guard.check_read_write_separation()
              + guard.check_thin_config()
              + guard.check_no_pinned_versions()
              + guard.check_provider_order())
    assert errors == [], errors


def test_guard_detects_write_path_import():
    guard = _guard()
    tree = ast.parse('from . import state, collector')
    hit = set()
    for node in ast.walk(tree):
        hit |= guard._imported_submodules(node) & guard.WRITE_PATH
    assert hit == {'collector'}  # the guard would fail a read module importing the write path


def test_guard_detects_aliased_import():
    guard = _guard()
    tree = ast.parse('import tracker.obs as o')
    hit = set()
    for node in ast.walk(tree):
        hit |= guard._imported_submodules(node) & guard.WRITE_PATH
    assert hit == {'obs'}  # aliased plain imports are caught too


def test_guard_rejects_resorting_provider_history(tmp_path):
    guard = _guard()
    guard.ROOT = tmp_path
    (tmp_path / 'config/versions').mkdir(parents=True)
    path = tmp_path / 'config/versions/nvchecker.toml'
    text = ('[widget]\nsource="jq"\n'
            'url="https://release-monitoring.org/api/v2/versions/?project_id=1"\n'
            'filter=".stable_versions[]"\n')
    path.write_text(text)
    assert len(guard.check_provider_order()) == 1
    path.write_text(text.replace('.stable_versions[]', 'first(.stable_versions[])'))
    assert guard.check_provider_order() == []


def test_guard_detects_transitive_write_import(tmp_path):
    guard=_guard();guard.ROOT=tmp_path
    root=tmp_path/'backend/tracker';root.mkdir(parents=True)
    (root/'api.py').write_text('from . import innocent\n')
    (root/'view.py').write_text('')
    (root/'innocent.py').write_text('from . import discover_sources\n')
    assert guard.check_read_write_separation()==['read path reaches write module: api -> innocent -> discover_sources']
