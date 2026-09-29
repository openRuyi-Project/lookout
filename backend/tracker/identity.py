"""Pure identity projection from an already-reviewed native provider rule.

Recognize provider protocols, never RPM name prefixes or arbitrary forge names.
No observations, alternate package registry, network or version policy live here.
"""
import re
from urllib.parse import unquote, urlsplit


def request_url(entry):
    """Registry identity or explicit transport URL, never two authoring points.

    Shared by native source plugins and public provenance. This resolves one
    protocol address, not package membership, release policy or a config layout.
    """
    source = entry.get('source')
    field = {'crates_index': 'cratesio', 'anitya_stable': 'anitya_id'}.get(source)
    if field and field in entry:
        if 'url' in entry:
            raise ValueError(f'{source}: choose {field} or url, not both')
        value = entry[field]
        if source == 'crates_index':
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
                raise ValueError('crates_index: invalid cratesio name')
            name = value.lower()
            prefix = ('1' if len(name) == 1 else '2' if len(name) == 2
                      else '3/' + name[0] if len(name) == 3 else name[:2] + '/' + name[2:4])
            return f'https://index.crates.io/{prefix}/{name}'
        if type(value) is not int or not 1 <= value <= 999999999999:
            raise ValueError('anitya_stable: anitya_id must be a positive project ID')
        return f'https://release-monitoring.org/api/v2/versions/?project_id={value}'
    url = entry.get('url')
    if not isinstance(url, str) or not url:
        raise ValueError('source requires a URL or registry identity')
    return url


def from_package(package):
    return package_context(package)['identity']


def package_context(package):
    """Describe the same identity precedence used by registry monitors."""
    release = package.get('source_release')
    if release:
        return {'identity': {key: release[key] for key in ('ecosystem', 'name')}, 'origin': 'source_release'}
    identity = from_native(package.get('identity') or {})
    return {'identity': identity, 'origin': 'native' if identity is not None else None}


def from_native(entry):
    if entry.get('source') == 'cpan' and entry.get('cpan'):
        name = entry['cpan']
        return {'ecosystem': 'CPANModule' if '::' in name else 'CPAN', 'name': name}
    if entry.get('source') == 'pypi' and entry.get('pypi'):
        return {'ecosystem': 'PyPI', 'name': entry['pypi']}
    if entry.get('source') == 'cratesio' and entry.get('cratesio'):
        return {'ecosystem': 'crates.io', 'name': entry['cratesio']}
    try:
        url = urlsplit(request_url(entry))
        if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443) or url.query or url.fragment:
            return None
    except ValueError:
        return None
    if entry.get('source') in ('regex', 'crates_index') and url.hostname == 'index.crates.io':
        name = url.path.rsplit('/', 1)[-1]
        if not re.fullmatch(r'[a-z0-9_-]+', name):
            return None
        canonical = request_url({'source': 'crates_index', 'cratesio': name})
        if url.path == urlsplit(canonical).path:
            return {'ecosystem': 'crates.io', 'name': name}
    if (entry.get('source') in ('jq', 'go_proxy') and url.hostname in ('proxy.golang.org', 'proxy.golang.com.cn')
            and url.path.endswith('/@latest')):
        escaped = unquote(url.path[1:-len('/@latest')])
        if re.search(r'!(?![a-z])', escaped):
            return None
        module = re.sub(r'!([a-z])', lambda m: m[1].upper(), escaped)
        if (re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z0-9.-]+/[A-Za-z0-9_./+~-]+', module)
                and not any(p in ('.', '..', '') for p in module.split('/'))):
            return {'ecosystem': 'Go', 'name': module}
    return None
