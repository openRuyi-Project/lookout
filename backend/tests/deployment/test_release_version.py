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


@pytest.mark.parametrize('bump, project, tags, expected', [
    ('patch', '0.2.0', [], '0.2.1'),
    ('minor', '0.2.4', [], '0.3.0'),
    ('major', '0.2.4', [], '1.0.0'),
    ('patch', '0.2.0', ['v0.9.9', 'v0.10.2'], '0.10.3'),
    ('minor', '0.2.0', ['v1.4.9'], '1.5.0'),
    ('major', '0.2.0', ['v1.4.9'], '2.0.0'),
    ('patch', '2.0.0', ['v1.9.9'], '2.0.1'),
    ('patch', '0.2.0', ['unrelated', 'v9.0.0-rc1', 'v01.0.0'], '0.2.1'),
    ('patch', '0.0.0', [], '0.0.1'),
])
def test_select_next_version(bump, project, tags, expected):
    assert release.next_version(bump, project, tags, []) == expected


def test_selection_rejects_invalid_mode_and_released_commit():
    with pytest.raises(ValueError, match='Choose'):
        release.next_version('patch;echo bad', '0.2.0', [], [])
    with pytest.raises(ValueError, match='already'):
        release.next_version('patch', '0.2.0', ['v0.2.1'], ['v0.2.1'])


@pytest.fixture
def release_repository(tmp_path):
    import shutil
    import subprocess

    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()

    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'backend').mkdir()
    shutil.copy2(spec.origin, tmp_path / 'scripts/release-version.py')
    (tmp_path / 'backend/pyproject.toml').write_text('[project]\nversion = "1.2.3"\n')
    git('init', '-q')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    git('add', '.')
    git('commit', '-qm', 'Initial fixture')
    git('tag', 'v2.9.9')
    git('commit', '--allow-empty', '-qm', 'Next fixture')
    return tmp_path, git


@pytest.mark.parametrize('bump, expected', [('patch', '2.9.10'), ('minor', '2.10.0'), ('major', '3.0.0')])
def test_cli_uses_real_tags_without_creating_release(release_repository, bump, expected):
    import subprocess
    import sys

    root, git = release_repository
    before = git('tag', '--list'), git('rev-parse', 'HEAD'), git('status', '--porcelain')
    result = subprocess.run([sys.executable, 'scripts/release-version.py', bump],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected + '\n'
    assert result.stderr == ''
    assert (git('tag', '--list'), git('rev-parse', 'HEAD'), git('status', '--porcelain')) == before


def test_cli_refuses_released_commit(release_repository):
    import subprocess
    import sys

    root, git = release_repository
    git('tag', 'v3.0.0')
    result = subprocess.run([sys.executable, 'scripts/release-version.py', 'patch'],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout == ''
    assert 'already has a release tag' in result.stderr


@pytest.mark.parametrize('args', [[], ['0.2.1'], ['patch;echo bad'], ['patch', 'minor']])
def test_cli_rejects_invalid_input(release_repository, args):
    import subprocess
    import sys

    root, _ = release_repository
    result = subprocess.run([sys.executable, 'scripts/release-version.py', *args],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout == ''
