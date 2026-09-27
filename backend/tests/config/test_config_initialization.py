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
    locations = {'nvpath', 'packages_path', 'input_hashes'}
    assert {k: v for k, v in loaded.items() if k not in locations} == {
        k: v for k, v in source.items() if k not in locations}
    assert {str(Path(name).relative_to(destination)): value for name, value in loaded['input_hashes'].items()} == {
        str(Path(name).relative_to(ROOT / 'config')): value for name, value in source['input_hashes'].items()}
    assert Path(loaded['nvpath']).relative_to(destination) == Path(source['nvpath']).relative_to(ROOT / 'config')
    before = {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    assert before == {str(p.relative_to(ROOT / 'config')): p.read_bytes()
                      for p in (ROOT / 'config').rglob('*') if p.is_file()}
    assert destination.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in destination.rglob('*') if p.is_file())
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2
    assert before == {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
