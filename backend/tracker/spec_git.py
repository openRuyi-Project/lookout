"""Per-package SPEC metadata and changelog from one managed full clone of openRuyi.

A single external source, peer to OBS and nvchecker. One full bare clone holds the
entire history (for changelogs) and every current SPEC blob (read via cat-file), so
all reads are offline and only `git fetch` touches the network. git performs history
simplification (`-- PATH`) and trailer extraction (`%(trailers)`) itself. After
bootstrap, only commits since the saved ancestor are traversed. Rewritten history
or a changed parser environment requires a full reconciliation.
Following nv.py, subprocess calls and pure parsing are separate for testability.
"""
import hashlib
import subprocess
from .schedule import Schedule

FS, GS, RS = '\x1f', '\x1d', '\x1e'   # field / signed-off / record separators
_SIZE_LIMIT = 1024 * 1024


class MacroReadError(ValueError):
    """The configured macro set could not be read completely."""


def polling(spec):
    interval = spec['interval_seconds']
    return Schedule(interval, min(interval * 2, 900), max(interval, 900))


def head(repo, git='git'):
    value, error = _git_text(['-C', repo, 'rev-parse', 'HEAD'], git)
    return value.strip() if value else None, error


def is_ancestor(repo, previous, current, git='git'):
    if not previous:
        return False
    _, error = _git_text(['-C', repo, 'merge-base', '--is-ancestor', previous, current], git)
    return error is None


def _git_text(args, git='git', timeout=300):
    """Run git and capture text stdout; return (stdout, error). Never surface URLs,
    credentials, or raw stderr."""
    try:
        p = subprocess.run([git, *args], capture_output=True, text=True, timeout=timeout, check=False)
        if p.returncode != 0:
            return None, f'git {args[0]} exited {p.returncode}'
        return p.stdout, None
    except subprocess.TimeoutExpired:
        return None, f'git {args[0]} timeout'
    except OSError:
        return None, 'git executable unavailable'


def _git_bytes(args, git='git', timeout=60):
    """Run git and capture raw bytes stdout (for blob content), or None on failure."""
    try:
        p = subprocess.run([git, *args], capture_output=True, timeout=timeout, check=False)
        return p.stdout if p.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def fetch(repo, git='git', timeout=300, retries=2):
    """Advance the managed full clone to origin HEAD. The configured refspec moves the
    local branch; the clone is complete, so history and blobs remain local afterwards."""
    error = None
    for attempt in range(max(1, retries + 1)):
        _, error = _git_text(['-C', repo, 'fetch', '--quiet', 'origin'], git, timeout)
        if error is None:
            return True, None
    return False, error


def _bucket_log(stdout, limit):
    """Pure parser: turn a `git log --name-status` stream into per-package changelogs,
    newest first, capped at `limit`. Separated from the subprocess for testability."""
    result = {}
    for record in stdout.split(RS):
        if not record.strip('\n'):
            continue
        lines = record.split('\n')
        parts = lines[0].split(FS)
        if len(parts) != 5:
            continue                      # never guess on a malformed header
        commit, date, author, subject, sob = parts
        entry = {'commit': commit, 'date': date, 'author': author, 'subject': subject,
                 'signed_off_by': [s for s in sob.split(GS) if s]}
        touched = set()
        for line in lines[1:]:
            if not line.strip():
                continue
            # Renames affect both the removed directory and the new one.
            for path in line.split('\t')[1:]:
                seg = path.split('/', 2)
                if len(seg) >= 2 and seg[0] == 'SPECS':
                    touched.add(seg[1])
        for name in touched:
            bucket = result.setdefault(name, [])
            if len(bucket) < limit:
                bucket.append(entry)
    return result


def changelogs(repo, limit=20, git='git', *, since=None, names=None):
    """Bucket one Git traversal by package; optionally restrict commits or paths.

    Bootstrap/reconciliation walks the full history once. `since` limits ordinary
    polling to new commits. `names` fills history for newly observed packages in one
    batch. Each package's newest entry also identifies its metadata revision.
    """
    fmt = f'{RS}%H{FS}%aI{FS}%an{FS}%s{FS}%(trailers:key=Signed-off-by,valueonly,unfold,separator={GS})'
    paths = [f':(literal)SPECS/{name}' for name in names] if names is not None else ['SPECS']
    if not paths:
        return {}, None
    revisions = [f'{since}..HEAD'] if since else []
    stdout, error = _git_text(['-C', repo, 'log', '--no-show-signature',
                              f'--format={fmt}', '--name-status', *revisions, '--', *paths], git)
    if error is not None:
        return {}, error
    return _bucket_log(stdout, limit), None


def read_spec(repo, name, git='git'):
    """Current SPEC bytes for a package, read from the bare clone via cat-file (offline)."""
    listing, error = _git_text(['-C', repo, 'ls-tree', '--name-only', f'HEAD:SPECS/{name}'], git)
    if error is not None:
        return None
    specs = sorted(f for f in listing.splitlines() if f.endswith('.spec'))
    if not specs:
        return None
    chosen = f'{name}.spec' if f'{name}.spec' in specs else specs[0]
    data = _git_bytes(['-C', repo, 'cat-file', '-p', f'HEAD:SPECS/{name}/{chosen}'], git)
    if data is None or len(data) > _SIZE_LIMIT:
        return None
    return data


def read_macros(repo, macro_package, git='git'):
    """Every macro file shipped by the macro package at HEAD, as (provenance, bytes)
    pairs ready for native_spec's hash-checked parser. Never return a partial set."""
    if not macro_package:
        return []
    listing, error = _git_text(['-C', repo, 'ls-tree', '--name-only', f'HEAD:SPECS/{macro_package}'], git)
    if error is not None:
        raise MacroReadError('SPEC macro directory unavailable')
    macros = []
    for filename in sorted(listing.splitlines()):
        if not filename.startswith('macros'):
            continue
        data = _git_bytes(['-C', repo, 'cat-file', '-p', f'HEAD:SPECS/{macro_package}/{filename}'], git)
        if data is None:
            raise MacroReadError('SPEC macro file unavailable')
        if len(data) > _SIZE_LIMIT:
            raise MacroReadError('SPEC macro file exceeds size limit')
        macros.append(({'path': f'SPECS/{macro_package}/{filename}',
                        'sha256': hashlib.sha256(data).hexdigest()}, data))
    return macros
