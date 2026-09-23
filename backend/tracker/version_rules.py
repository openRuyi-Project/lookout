"""Load exactly one native nvchecker file. Package policy lives in tracker.toml."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class RuleSet:
    entries: dict
    options: dict
    digest: str


def files(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('native configuration changed: a regular file is required')
    return [path]


def digest(path):
    return hashlib.sha256(files(path)[0].read_bytes()).hexdigest()


def load(path):
    raw = files(path)[0].read_bytes()
    tables = tomllib.loads(raw.decode())
    options = tables.pop('__config__', {})
    if not isinstance(options, dict):
        raise ValueError('__config__ must be a native nvchecker options table')
    for name, entry in tables.items():
        if (not name or name.startswith('__') or not isinstance(entry, dict)
                or not isinstance(entry.get('source'), str) or not entry['source']):
            raise ValueError(f'{name}: expected a native nvchecker rule with source; migrate legacy group configuration first')
    return RuleSet(tables, options, hashlib.sha256(raw).hexdigest())
