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


def same_values(left, right):
    """Compare TOML values without treating true, 1 and 1.0 as equivalent.

    Editors may pass TOML syntax nodes; their public unwrap method produces the
    same plain values as the runtime reader, without adding an editor dependency.
    """
    if hasattr(left, 'unwrap'):
        left = left.unwrap()
    if hasattr(right, 'unwrap'):
        right = right.unwrap()
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_values(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_values(a, b) for a, b in zip(left, right))
    return left == right


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
