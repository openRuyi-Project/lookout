"""Release numbering must fail before publishing any artifact."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'release_version', Path(__file__).resolve().parents[3] / 'scripts/release-version.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize('value', ['0.2.1', '0.3.0', '1.0.0'])
def test_new_release(value):
    release.validate(value, ['v0.2.0', 'unrelated', 'v0.3.0-rc1'], [])


@pytest.mark.parametrize('value', ['v0.2.1', '0.02.1', '0.2', '0.2.1;echo bad', '0.2.1\n', '0.2.1-rc1'])
def test_invalid_release(value):
    with pytest.raises(ValueError):
        release.validate(value, [], [])


def test_release_must_advance_and_not_retag_commit():
    for value in ['0.2.0', '0.1.9']:
        with pytest.raises(ValueError, match='exceed'):
            release.validate(value, ['v0.2.0'], [])
    with pytest.raises(ValueError, match='already'):
        release.validate('0.2.1', ['v0.2.0'], ['v0.2.0'])
