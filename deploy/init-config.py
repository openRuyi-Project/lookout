#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Create a complete private runtime configuration; refuse existing destinations."""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from tracker import config


def initialize(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("configuration destination already exists")
    if any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("configuration source contains symlinks")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=destination.parent, prefix=".init-config-"
    ) as temporary:
        prepared = Path(temporary) / "config"
        shutil.copytree(source, prepared)
        prepared.chmod(0o700)
        for path in prepared.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        config.load(prepared / "tracker.toml")
        # mkdir is exclusive, including when another initializer wins the race.
        destination.mkdir(mode=0o700)
        try:
            for path in prepared.iterdir():
                shutil.move(str(path), destination / path.name)
        except Exception:
            shutil.rmtree(destination)
            raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", type=Path, default=ROOT / "config")
    args = parser.parse_args()
    try:
        print(initialize(args.source, args.destination))
    except (OSError, ValueError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
