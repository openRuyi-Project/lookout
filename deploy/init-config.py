#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Create private settings referring to the release catalog, without copying it."""

import argparse
from pathlib import Path
import shutil
import sys
import tempfile

import tomlkit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from tracker import config


def initialize(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("configuration destination already exists")
    destination = destination.resolve()
    if destination.is_relative_to(source):
        raise ValueError('configuration destination must be outside the source')
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in source.rglob("*")):
        raise ValueError("configuration source must contain regular files and directories")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=destination.parent, prefix=".init-config-"
    ) as temporary:
        prepared = Path(temporary) / "config"
        shutil.copytree(source, prepared)
        document = tomlkit.parse((prepared / 'tracker.toml').read_text())
        references = [(document['collector'], 'nvchecker_config', 'nvchecker.toml'),
                      (document, 'packages_config', None), (document, 'distribution_config', None)]
        for table, key, default in references:
            reference = table.get(key, default)
            if reference is None:
                continue
            if not isinstance(reference, str) or not reference:
                raise ValueError('catalog reference must name a configuration file')
            catalog_path = source / reference
            config.read_input(catalog_path)
            table[key] = str(catalog_path.resolve())
            copied = (prepared / reference).resolve()
            if copied.is_relative_to(prepared):
                copied.unlink()
        (prepared / 'tracker.toml').write_text(tomlkit.dumps(document))
        for directory in sorted(prepared.rglob('*'), reverse=True):
            if directory.is_dir() and not list(directory.iterdir()):
                directory.rmdir()
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
