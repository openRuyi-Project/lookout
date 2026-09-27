# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Exercise architectural boundaries, including nested imports and package initializers."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def guard(root=ROOT):
    spec = importlib.util.spec_from_file_location('archguard', ROOT / 'scripts/check-architecture.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = root
    return module


def source(root, path, text=''):
    destination = root / 'backend/tracker' / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def test_repo_satisfies_all_invariants():
    check = guard()
    assert (check.check_read_write_separation() + check.check_thin_config()
            + check.check_no_pinned_versions() + check.check_provider_order()) == []


@pytest.mark.parametrize('statement', [
    'from tracker.monitors.build import obs',
    'import tracker.monitors.build.obs as remote',
    'from ..monitors.build import obs',
])
def test_nested_read_import_cannot_reach_collection(tmp_path, statement):
    source(tmp_path, 'readmodel/__init__.py')
    source(tmp_path, 'readmodel/helper.py', statement)
    source(tmp_path, 'monitors/build/obs.py')
    errors = guard(tmp_path).check_read_write_separation()
    assert errors == ['read path reaches write module: tracker.readmodel.helper -> tracker.monitors.build.obs']


def test_package_initializer_is_part_of_the_import_graph(tmp_path):
    source(tmp_path, 'api.py', 'from . import innocent')
    source(tmp_path, 'innocent/__init__.py', 'from ..monitors import new_backend')
    source(tmp_path, 'monitors/new_backend.py')
    assert guard(tmp_path).check_read_write_separation() == [
        'read path reaches write module: tracker.api -> tracker.innocent -> tracker.monitors.new_backend'
    ]


def test_transitive_read_import_is_checked(tmp_path):
    source(tmp_path, 'api.py', 'from .readmodel import helper')
    source(tmp_path, 'readmodel/helper.py', 'from ..providers import new_registry')
    source(tmp_path, 'providers/new_registry.py')
    errors = guard(tmp_path).check_read_write_separation()
    assert any('tracker.api -> tracker.readmodel.helper -> tracker.providers.new_registry' in e for e in errors)


def test_pure_domain_types_and_annotation_only_imports_are_allowed(tmp_path):
    source(tmp_path, 'api.py', 'from .monitors.version import compare')
    source(tmp_path, 'monitors/version/compare.py')
    source(tmp_path, 'presentation/example.py',
           'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from tracker.providers import client\n')
    source(tmp_path, 'providers/client.py')
    assert guard(tmp_path).check_read_write_separation() == []


@pytest.mark.parametrize('statement', [
    'import httpx', 'from urllib.request import urlopen', 'import subprocess',
])
def test_read_module_cannot_bypass_adapters_with_direct_io(tmp_path, statement):
    source(tmp_path, 'presentation/example.py', statement)
    assert guard(tmp_path).check_read_write_separation()


def test_guard_rejects_resorting_provider_history(tmp_path):
    check = guard(tmp_path)
    path = tmp_path / 'config/versions/nvchecker.toml'
    path.parent.mkdir(parents=True)
    text = ('[widget]\nsource="jq"\n'
            'url="https://release-monitoring.org/api/v2/versions/?project_id=1"\n'
            'filter=".stable_versions[]"\n')
    path.write_text(text)
    assert len(check.check_provider_order()) == 1
    path.write_text(text.replace('.stable_versions[]', 'first(.stable_versions[])'))
    assert check.check_provider_order() == []
