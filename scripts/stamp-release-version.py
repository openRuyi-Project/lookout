#!/usr/bin/env python3
"""Set the application version in an image's copy of project metadata."""
import argparse
import re
from pathlib import Path

import tomlkit


def stamp(path, version):
    if version == 'development':
        return
    if re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', version) is None:
        raise ValueError('Release version must be MAJOR.MINOR.PATCH')
    document = tomlkit.parse(path.read_text())
    document['project']['version'] = version
    path.write_text(tomlkit.dumps(document))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('version')
    args = parser.parse_args()
    try:
        stamp(args.path, args.version)
    except (ValueError, KeyError, OSError) as error:
        parser.exit(2, f'{error}\n')


if __name__ == '__main__':
    main()
