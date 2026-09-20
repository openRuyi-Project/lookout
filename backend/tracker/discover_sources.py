# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Identity projection from saved confined-RPM facts, never a SPEC interpreter."""
import re
from urllib.parse import quote, urlsplit


def hints(candidate, spec):
    """Old snapshots fail closed until the existing SPEC phase refreshes them.

    No clone read and no macro execution here: metadata and native_query belong
    to the same saved observation, with the exact SPEC hash and RPM target.
    """
    query = spec.get('native_query') or {}
    metadata = spec.get('metadata') or {}
    if (spec.get('error') or query.get('error')
            or not re.fullmatch(r'[a-f0-9]{64}', query.get('spec_sha256') or '')
            or query.get('spec_sha256') != candidate.get('spec_sha256')
            or query.get('context', {}).get('resolver', 0) < 6
            or metadata.get('version') != candidate.get('current')):
        return {'hint_error': 'native_identity_unverified'}
    result = {}
    module = metadata.get('go_module')
    if (isinstance(module, str)
            and re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z0-9.-]+/[A-Za-z0-9_./+~-]+', module)
            and not any(p in ('.', '..', '') for p in module.split('/'))):
        result['go_module'] = module
    # Source0 only: patches/auxiliary downloads cannot select the release repo.
    urls = [v['url'] for v in metadata.get('sources', []) if v.get('number') == 0]
    if len(urls) != 1 or '%' in urls[0]:
        return result
    source = urls[0].split('#', 1)[0]
    result['source_url'] = source
    repo = repository(source)
    if repo:
        result['source_repository'] = repo
    filename = urlsplit(source).path.rsplit('/', 1)[-1]
    found = re.fullmatch(r'(.+?)[_-](?:v)?' + re.escape(str(candidate['current']))
                        + r'(?:\.tar\.(?:gz|xz|bz2|zst)|\.tgz|\.zip)', filename)
    if found and re.fullmatch(r'[A-Za-z0-9_.+-]+', found[1]):
        result['archive_component'] = found[1]
    return result


def repository(url):
    """Recover only concrete forge repository paths, never arbitrary subpaths."""
    if not isinstance(url, str):
        return None
    try:
        u = urlsplit(url)
        if u.scheme not in ('https', 'http') or u.username or u.password or u.port not in (None,80,443):
            return None
    except ValueError:
        return None
    if u.hostname in ('github.com', 'codeload.github.com'):
        parts = u.path.split('/')
        if len(parts) >= 3 and all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) and p not in ('.','..') for p in parts[1:3]):
            return 'https://github.com/' + '/'.join(parts[1:3]).removesuffix('.git')
    if u.hostname and '/-/' in u.path:
        path = u.path.split('/-/',1)[0]
        if re.fullmatch(r'(?:/[A-Za-z0-9_.-]+){2,}',path) and not any(p in ('.','..') for p in path.split('/')[1:]):
            return 'https://' + u.hostname + path.removesuffix('.git')
    return None


def go_entry(module, base_url="https://proxy.golang.org"):
    u = urlsplit(base_url)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("Go proxy must be an operator-owned HTTPS URL")
    # Go proxy escape algorithm: uppercase becomes !lowercase, including vanity paths.
    escaped = ''.join('!'+c.lower() if c.isupper() else c for c in module)
    return {'source': 'jq', 'url': base_url.rstrip('/')+'/'+quote(escaped,safe='/!')+'/@latest',
            'filter': '.Version | select(test("^v[0-9]+([.][0-9]+){2}([+]incompatible)?$"))',
            'prefix': 'v', 'from_pattern': '[+]incompatible$', 'to_pattern': ''}


def release_directory(row):
    """A version-named release directory, not a generic downloads homepage."""
    value = row.get('source_url', '')
    if not row.get('archive_component') or '%' in value:
        return None
    u = urlsplit(value)
    if u.scheme != 'https' or u.query or u.username or u.password:
        return None
    directory = u.path.rsplit('/', 1)[0]
    if str(row['current']) not in directory.split('/'):
        return None
    return u.netloc.lower() + directory
