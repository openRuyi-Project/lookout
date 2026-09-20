"""Pure identity projection from an already-reviewed native provider rule.

Recognize provider protocols, never RPM name prefixes or arbitrary forge names.
No observations, alternate package registry, network or version policy live here.
"""
import re
from urllib.parse import unquote, urlsplit


def from_native(entry):
    if entry.get('source') == 'pypi' and entry.get('pypi'):
        return {'ecosystem': 'PyPI', 'name': entry['pypi']}
    if entry.get('source') == 'cratesio' and entry.get('cratesio'):
        return {'ecosystem': 'crates.io', 'name': entry['cratesio']}
    try:
        url = urlsplit(entry.get('url', ''))
        if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443) or url.query or url.fragment:
            return None
    except ValueError:
        return None
    if entry.get('source') == 'regex' and url.hostname == 'index.crates.io':
        name = url.path.rsplit('/', 1)[-1]
        if not re.fullmatch(r'[a-z0-9_-]+', name):
            return None
        prefix = '1' if len(name) == 1 else '2' if len(name) == 2 else '3/' + name[0] if len(name) == 3 else name[:2] + '/' + name[2:4]
        if url.path == '/' + prefix + '/' + name:
            return {'ecosystem': 'crates.io', 'name': name}
    if (entry.get('source') == 'jq' and url.hostname in ('proxy.golang.org', 'proxy.golang.com.cn')
            and url.path.endswith('/@latest')):
        escaped = unquote(url.path[1:-len('/@latest')])
        if re.search(r'!(?![a-z])', escaped):
            return None
        module = re.sub(r'!([a-z])', lambda m: m[1].upper(), escaped)
        if (re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z0-9.-]+/[A-Za-z0-9_./+~-]+', module)
                and not any(p in ('.', '..', '') for p in module.split('/'))):
            return {'ecosystem': 'Go', 'name': module}
    return None
