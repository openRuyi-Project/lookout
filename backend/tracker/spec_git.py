"""Per-package SPEC metadata and changelog from one managed full clone of openRuyi.

A single external source, peer to OBS and nvchecker. One full bare clone holds the
entire history (for changelogs) and every current SPEC blob (read via cat-file), so
all reads are offline and only `git fetch` touches the network. git performs history
simplification (`-- PATH`) and trailer extraction (`%(trailers)`) itself, and a diff
between the last processed commit and HEAD names exactly which package dirs changed.
Following nv.py, subprocess calls and pure parsing are separate for testability.
"""
import hashlib
import subprocess

FS, GS, RS = '\x1f', '\x1d', '\x1e'   # field / signed-off / record separators
_SIZE_LIMIT = 1024 * 1024


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


def fetch(repo, git='git', timeout=300):
    """Advance the managed full clone to origin HEAD. The configured refspec moves the
    local branch; the clone is complete, so history and blobs remain local afterwards."""
    _, error = _git_text(['-C', repo, 'fetch', '--quiet', 'origin'], git, timeout)
    return error is None, error


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
            path = line.split('\t')[-1]  # status\tpath, or Rxxx\told\tnew -> take the new path
            seg = path.split('/', 2)
            if len(seg) >= 2 and seg[0] == 'SPECS':
                touched.add(seg[1])
        for name in touched:
            bucket = result.setdefault(name, [])
            if len(bucket) < limit:
                bucket.append(entry)
    return result


def changelogs(repo, limit=20, git='git'):
    """ALL packages' changelogs in ONE history traversal, keyed by package name.

    `git log --name-status -- SPECS` walks the whole history once (~2s on a full local
    clone) and reports, per commit, which files changed. Python buckets each commit
    into every SPECS/<name> it touched. This replaces per-package `git log` (one
    subprocess each: ~40 min for the corpus). Each package's newest bucketed commit is
    also its head, so callers gate metadata re-parsing on it. Returns (dict, error)."""
    fmt = f'{RS}%H{FS}%aI{FS}%an{FS}%s{FS}%(trailers:key=Signed-off-by,valueonly,unfold,separator={GS})'
    stdout, error = _git_text(['-C', repo, 'log', '--no-show-signature',
                              f'--format={fmt}', '--name-status', '--', 'SPECS'], git)
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
    pairs ready for native_spec's hash-checked parser."""
    listing, error = _git_text(['-C', repo, 'ls-tree', '--name-only', f'HEAD:SPECS/{macro_package}'], git)
    if error is not None or not listing:
        return []
    macros = []
    for filename in sorted(listing.splitlines()):
        if not filename.startswith('macros'):
            continue
        data = _git_bytes(['-C', repo, 'cat-file', '-p', f'HEAD:SPECS/{macro_package}/{filename}'], git)
        if data is not None and len(data) <= _SIZE_LIMIT:
            macros.append(({'path': f'SPECS/{macro_package}/{filename}',
                            'sha256': hashlib.sha256(data).hexdigest()}, data))
    return macros
