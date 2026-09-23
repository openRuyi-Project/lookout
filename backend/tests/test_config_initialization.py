from pathlib import Path
import subprocess
import sys
from tracker import config

ROOT = Path(__file__).resolve().parents[2]


def test_documented_initializer_loads_and_refuses_overwrite(tmp_path):
    destination = tmp_path / "runtime-config"
    command = [sys.executable, str(ROOT / "deploy/init-config.py"), str(destination)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    loaded = config.load(destination / "tracker.toml")
    assert loaded["native"]["python-zmq"]["pypi"] == "pyzmq"
    assert {p.name for p in (destination / "versions").glob("*.toml")} == {"nvchecker.toml"}
    assert loaded["packages"]["openssl"]["track_label"] == "3.x"
    assert loaded["packages"]["openssl"]["monitors"]["eol"]["product"] == "openssl"
    before = {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2
    assert before == {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
