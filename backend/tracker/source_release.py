"""Exact upstream identities from saved, confined RPM Source0 evidence.

RPM Version remains the distribution/display version. This projection neither
creates nvchecker rules nor fetches metadata; consumers share the same identity.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
from urllib.parse import quote, urlsplit, urlunsplit


_NUMBER = r'(?:0|[1-9][0-9]*)'
_SEMVER = re.compile(
    rf'(?P<base>{_NUMBER}\.{_NUMBER}\.{_NUMBER})'
    r'(?:-(?P<preview>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
    r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
)


def semver(value):
    if not isinstance(value, str) or len(value) > 200:
        return None
    match = _SEMVER.fullmatch(value)
    if match and any(part.isdigit() and len(part) > 1 and part[0] == '0'
                     for part in (match['preview'] or '').split('.')):
        return None
    return match


@dataclass(frozen=True)
class Release:
    ecosystem: str
    name: str
    version: str
    url: str

    @property
    def identity(self):
        return {'ecosystem': self.ecosystem, 'name': self.name}

    def public(self):
        return asdict(self)


def from_url(value):
    """Recognize registry-owned archive protocols, not package-name conventions."""
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or url.hostname not in ('static.crates.io', 'crates.io')
                or url.username or url.password or url.port not in (None, 443) or url.query):
            return None
    except ValueError:
        return None
    prefix = '/crates/' if url.hostname == 'static.crates.io' else '/api/v1/crates/'
    if not url.path.startswith(prefix):
        return None
    name, separator, archive = url.path[len(prefix):].partition('/')
    if not separator or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
        return None
    if archive.endswith('/download'):
        version = archive.removesuffix('/download')
    elif archive.startswith(name + '-') and archive.endswith('.crate'):
        version = archive[len(name) + 1:-len('.crate')]
    else:
        return None
    if not semver(version):
        return None
    return Release('crates.io', name, version, urlunsplit((url.scheme, url.netloc, url.path, '', '')))


def matches_rpm(release, current):
    """Recognize the observed numeric-base/tilde packaging forms, not any mismatch."""
    parsed = semver(release.version)
    return current in (release.version, parsed['base'], release.version.replace('-', '~', 1))


def primary_source_url(source):
    query = source.get('native_query') or {}
    metadata = source.get('metadata') or {}
    if (source.get('origin') != 'spec' or source.get('error') or query.get('error')
            or not re.fullmatch(r'[a-f0-9]{64}', query.get('spec_sha256') or '')
            or query.get('context', {}).get('resolver', 0) < 6
            or metadata.get('version') != source.get('version')):
        return None
    sources = [item.get('url') for item in metadata.get('sources', []) if item.get('number') == 0]
    if len(sources) != 1:
        return None
    return sources[0]


def from_source(source):
    release = from_url(primary_source_url(source))
    return release if release and matches_rpm(release, source.get('version')) else None


def commit_hash(value):
    return isinstance(value, str) and bool(re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', value))


@dataclass(frozen=True)
class Revision:
    repository: str
    branch: str
    current: str
    packaged_date: str | None
    forge: str = 'forge'

    def public(self, latest, committed_at=None):
        latest = latest if commit_hash(latest) else None
        return {'repository': self.repository, 'branch': self.branch, 'current': self.current,
                'packaged_date': self.packaged_date, 'latest': latest,
                'latest_committed_at': revision_time(committed_at),
                'links': {'current': self.link('commit', self.current),
                          'latest': self.link('commit', latest) if latest else None,
                          'branch': self.link('tree', self.branch)}}

    def link(self, kind, value):
        value = quote(value, safe='')
        if self.forge == 'cgit':
            return f'{self.repository}/{kind}/?id={value}'
        if self.forge == 'gitlab':
            return f'{self.repository}/-/{kind}/{value}'
        if self.forge == 'gitea' and kind == 'tree':
            return f'{self.repository}/src/branch/{value}'
        return f'{self.repository}/{kind}/{value}'


def revision_time(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo else None
    except ValueError:
        return None


def tracks_commits(entry):
    if entry.get('source') == 'git':
        return entry.get('use_commit') is True
    # These native sources return branch commits when no tag/release mode is
    # selected. Branch selection is explicit configuration, never a runtime guess.
    return (entry.get('source') in ('github', 'gitlab', 'gitea') and bool(entry.get('branch'))
            and not any(entry.get(key) for key in
                        ('use_latest_tag', 'use_latest_release', 'use_max_tag', 'use_max_release')))


def observed_commit(upstream):
    entry = upstream.get('source') or {}
    value = upstream.get('version') if entry.get('source') == 'git' else upstream.get('revision')
    return value if tracks_commits(entry) and commit_hash(value) else None


def pinned_revision(source):
    """Recover a complete Source0 commit, corroborated by RPM's snapshot suffix.

    Archive protocols identify repositories, not package names or homepages.
    Local tarballs, abbreviated archive hashes and mixed-revision packages stay
    unresolved. This pure projection is shared by discovery and comparison.
    """
    value = primary_source_url(source)
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or not url.hostname or '.' not in url.hostname
                or url.username or url.password or url.port not in (None, 443)
                or url.query or '%' in url.path):
            return None
    except ValueError:
        return None
    sha = r'(?P<commit>[a-f0-9]{40}|[a-f0-9]{64})'
    extension = r'\.(?:tar\.(?:gz|xz|bz2)|zip)'
    filename = r'/[A-Za-z0-9_.+-]+' + extension
    host, forge = url.hostname, 'forge'
    if host == 'codeload.github.com':
        pattern = r'(?P<repo>/[^/]+/[^/]+)/(?:tar\.gz|zip)/' + sha
        host = 'github.com'
    elif '/-/archive/' in url.path:
        pattern = r'(?P<repo>/.+?)/-/archive/' + sha + filename
        forge = 'gitlab'
    elif '/snapshot/' in url.path:
        pattern = r'(?P<repo>/.+?)/snapshot/[A-Za-z0-9_.+-]+-' + sha + extension
        forge = 'cgit'
    else:
        pattern = r'(?P<repo>/.+?)/archive/' + sha + '(?:' + extension + '|' + filename + ')'
    match = re.fullmatch(pattern, url.path)
    if not match:
        return None
    parts = match['repo'].split('/')[1:]
    if (any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part) or part in ('.', '..') for part in parts)
            or (host == 'github.com' and len(parts) != 2)):
        return None
    repository = 'https://' + host + match['repo'].removesuffix('.git')
    commit = match['commit']
    # RPM's abbreviated hash must agree with the complete Source0 identity.
    # The date is packaging text, never a revision ordering key.
    version = source.get('version') or ''
    packaged = re.search(r'(?:^|[+~.-])git(?:([0-9]+)\.)?([a-f0-9]{7,64})$', version)
    if not packaged or not commit.startswith(packaged[2]):
        return None
    try:
        raw_date = packaged[1] or version.split('+git', 1)[0]
        date = datetime.strptime(raw_date, '%Y%m%d').date().isoformat() if len(raw_date) == 8 else None
    except ValueError:
        date = None
    return Revision(repository, '', commit, date, forge)


def revision(source, entry):
    """Bind exact source identity to a reviewed native branch rule, without I/O."""
    if not tracks_commits(entry) or entry.get('path'):
        return None
    branch = entry.get('branch')
    if not isinstance(branch, str) or not branch or re.search(r'[\s~^:?*\[\\]', branch):
        return None
    pinned = pinned_revision(source)
    if not pinned:
        return None
    provider = entry.get('source')
    configured = (entry.get('git', '') if provider == 'git' else
                  'https://' + entry.get('host', provider + '.com') + '/' + entry.get(provider, ''))
    configured = configured.rstrip('/').removesuffix('.git')
    same = (configured.casefold() == pinned.repository.casefold() if
            pinned.repository.startswith('https://github.com/') else configured == pinned.repository)
    if not same:
        return None
    forge = 'gitea' if provider == 'gitea' else pinned.forge
    return Revision(pinned.repository, branch, pinned.current, pinned.packaged_date, forge)
