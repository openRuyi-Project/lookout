"""Release metadata changes only the image's project version."""
import importlib.util
import tomllib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'stamp_release_version', Path(__file__).resolve().parents[3] / 'scripts/stamp-release-version.py')
stamp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stamp)


@pytest.mark.parametrize('version', ['1.2.4', '2.0.0', '0.0.1'])
def test_stamp_preserves_other_metadata(tmp_path, version):
    path = tmp_path / 'pyproject.toml'
    original = '# Inputs\n[project]\nversion = "1.2.3" # release\ndependencies = ["example>=1"]\n'
    path.write_text(original)
    stamp.stamp(path, version)
    assert path.read_text() == original.replace('1.2.3', version)
    assert tomllib.loads(path.read_text())['project']['version'] == version
    stamp.stamp(path, version)
    assert path.read_text() == original.replace('1.2.3', version)


@pytest.mark.parametrize('version', ['development', '', 'v1.2.3', '01.2.3', '1.2.3\n', '1.2.3;echo bad'])
def test_no_write_for_development_or_invalid_version(tmp_path, version):
    path = tmp_path / 'pyproject.toml'
    original = b'[project]\nversion = "1.2.3"\n'
    path.write_bytes(original)
    if version == 'development':
        stamp.stamp(path, version)
    else:
        with pytest.raises(ValueError, match='MAJOR.MINOR.PATCH'):
            stamp.stamp(path, version)
    assert path.read_bytes() == original
