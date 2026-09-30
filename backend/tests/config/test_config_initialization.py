from pathlib import Path
import subprocess
import sys

from tracker import config

ROOT = Path(__file__).resolve().parents[3]


def test_documented_initializer_loads_and_refuses_overwrite(tmp_path):
    destination = tmp_path / "runtime-config"
    command = [sys.executable, str(ROOT / "deploy/init-config.py"), str(destination)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    source = config.load(ROOT / 'config/tracker.toml')
    loaded = config.load(destination / "tracker.toml")
    for key in ('native', 'native_options', 'packages', 'openruyi', 'obs', 'spec', 'targets', 'monitors'):
        assert loaded[key] == source[key]
    for key in ('nvpath', 'packages_path', 'distribution_path'):
        assert loaded[key] == source[key]
        assert not Path(loaded[key]).is_relative_to(destination)
    assert loaded['collector']['nvchecker_config'] == source['nvpath']
    assert (loaded['collector'] | {'nvchecker_config': None}) == (source['collector'] | {'nvchecker_config': None})
    before = {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    assert not list(destination.rglob('nvchecker.toml'))
    assert not (destination / 'packages.toml').exists()
    assert not (destination / 'distribution.toml').exists()
    assert destination.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in destination.rglob('*') if p.is_file())
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2
    assert before == {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
