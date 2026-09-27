"""Validate the built distribution, not just imports from a source checkout."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

BACKEND = Path(__file__).resolve().parents[2]


def run(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=90,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': ''})
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_wheel_contains_subpackages_and_runs_without_checkout(tmp_path):
    source = tmp_path / 'source'
    def ignore(directory, names):
        excluded = {name for name in names if name == '__pycache__' or name.endswith(('.pyc', '.egg-info'))}
        # Only the backend-root build directory is an artifact; monitors/build is source.
        return excluded | ({'build'} if Path(directory) == BACKEND else set())
    shutil.copytree(BACKEND, source, ignore=ignore)
    wheels = tmp_path / 'wheels'
    run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--no-index',
         '--wheel-dir', str(wheels), str(source)], tmp_path)
    wheel, = wheels.glob('*.whl')
    installed = tmp_path / 'installed'
    expected = {p.relative_to(BACKEND).as_posix() for p in (BACKEND / 'tracker').rglob('*.py')}
    with zipfile.ZipFile(wheel) as archive:
        assert {n for n in archive.namelist() if n.endswith('.py')} == expected
        archive.extractall(installed)
    program = '''
import json, pathlib, runpy, sys
sys.path.insert(0, sys.argv[1])
from tracker.api import create_app
from tracker.monitors.registry import REGISTRY
from tracker.monitors.source import rpm
root = pathlib.Path(sys.argv[1]).resolve()
assert pathlib.Path(rpm.__file__).is_relative_to(root)
assert '/api/v2/packages' in create_app().openapi()['paths']
assert REGISTRY
spec = b'Name: layout-probe\\nVersion: 1.0\\nRelease: 1\\nSummary: Probe\\nLicense: MIT\\n\\n%description\\nProbe.\\n'
result = rpm.query(spec)
assert result['version'] == '1.0' and result['version_error'] is None, result
sandbox = result['native_query']['context']['sandbox']
assert sandbox['landlock_abi'] >= 6 and sandbox['seccomp'] == 'allow-list', sandbox
sys.argv = ['tracker.monitors', '--help']
runpy.run_module('tracker.monitors', run_name='__main__')
'''
    output = run([sys.executable, '-I', '-c', program, str(installed)], tmp_path)
    assert 'explain' in output and '--monitor' in output
