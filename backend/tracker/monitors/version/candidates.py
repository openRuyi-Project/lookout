# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Identity projection from saved confined-RPM facts, never a SPEC interpreter."""
import re
from urllib.parse import quote, urlsplit

from tracker.monitors.source import release as source_release


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
    if len(urls) != 1:
        return result
    source = urls[0].split('#', 1)[0]
    result['source_url'] = source
    if '%' in source:
        return result
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


def registry_entry(candidate):
    """Propose a native rule for an exact, version-matched registry Source0.

    Both onboarding entry points consume saved ``hints``; package names and
    homepages are not registry identities. The result still requires review and
    native verification before becoming a configured rule.
    """
    if candidate.get('hint_error'):
        return None
    current = candidate.get('current') or ''
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', current):
        return None
    source = candidate.get('source_url') or ''
    release = source_release.from_url(source)
    if not release or not source_release.matches_rpm(release, current):
        return None
    major, minor, _ = current.split('.')
    line = (re.escape(major + '.' + minor) + r'\.[0-9]+' if major == '0'
            else re.escape(major) + r'\.[0-9]+\.[0-9]+')
    return {'source': 'cratesio', 'cratesio': release.name, 'include_regex': '^' + line + '$'}


def go_source_check(candidate):
    """Hold contradictory submodule evidence; never repair a module by guessing.

    A successful query for a repository's root module cannot validate a Source0
    taken from an independently tagged component. Vanity paths need explicit
    review here because matching their last segment does not prove the mapping.
    """
    source = candidate.get('source_url') or ''
    if '%' in source:
        return {'reason': 'go_module_source_identity_unverified'}
    try:
        url = urlsplit(source)
    except ValueError:
        return {'reason': 'go_module_source_identity_unverified'}
    parts = url.path.split('/')
    tag = ''
    if url.hostname == 'github.com' and len(parts) > 4 and parts[3] == 'archive':
        tag = '/'.join(parts[4:])
    elif url.hostname == 'codeload.github.com' and len(parts) > 4 and parts[3] in ('tar.gz', 'zip'):
        tag = '/'.join(parts[4:])
    elif '/-/archive/' in url.path:
        tag = url.path.split('/-/archive/', 1)[1].rsplit('/', 1)[0]
    tag = tag.removeprefix('refs/tags/')
    tag = re.sub(r'\.(?:tar\.(?:gz|xz|bz2|zst)|tgz|zip)$', '', tag)
    component_tag = r'(.+)/v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?'
    found = re.fullmatch(component_tag, tag)
    if not found and '/' in tag:
        found = re.fullmatch(component_tag, tag.rsplit('/', 1)[0])
    if not found:
        return {}
    component = found[1]
    module = candidate['go_module']
    parent, _, last = module.rpartition('/')
    if re.fullmatch(r'v[0-9]+', last) and int(last[1:]) >= 2:
        module = parent
    repo = candidate.get('source_repository') or ''
    repo_path = repo.removeprefix('https://').removeprefix('http://')
    prefix = repo_path + '/'
    same_repository = module.startswith(prefix) or (
        repo_path.startswith('github.com/') and module[:len(prefix)].casefold() == prefix.casefold()
    )
    if repo_path and same_repository:
        relative = module[len(repo_path) + 1:]
        reason = None if relative == component else 'go_module_source_component_mismatch'
    else:
        reason = ('go_module_source_identity_unverified' if module.endswith('/' + component)
                  else 'go_module_source_component_mismatch')
    return {'source_component': component, 'reason': reason}


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
