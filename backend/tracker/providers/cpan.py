"""Exact CPAN distribution releases, distinct from contained module versions."""
import re
from urllib.parse import quote

from tracker.identity import from_package
from tracker.providers.model import Release, UnsupportedRelease


HOSTS = {'fastapi.metacpan.org'}
# CPAN::Meta::Spec license codes, not guesses from license prose. Multiple
# codes have no specified AND/OR relationship and are deliberately not joined.
LICENSES = {'mit': 'MIT', 'apache_2_0': 'Apache-2.0', 'artistic_2': 'Artistic-2.0',
            'bsd': 'BSD-3-Clause', 'freebsd': 'BSD-2-Clause', 'zlib': 'Zlib',
            'perl_5': 'Artistic-1.0-Perl OR GPL-1.0-or-later'}


def project(settings):
    if (not isinstance(settings, dict) or set(settings) != {'cpan'}
            or not isinstance(settings['cpan'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_+-]{0,199}', settings['cpan'])):
        raise ValueError('monitor requires a CPAN distribution identity')
    return settings['cpan']


def inputs(package, configured):
    if configured is not None:
        project(configured)
        return configured
    identity = from_package(package)
    return {'cpan': identity['name']} if identity and identity['ecosystem'] == 'CPAN' else None


def release(name, version, io):
    project({'cpan': name})
    if not isinstance(version, str) or not re.fullmatch(r'[v0-9][A-Za-z0-9._-]{0,199}', version):
        raise ValueError('monitor requires an exact CPAN release version')
    data = io.json('POST', 'https://fastapi.metacpan.org/v1/release/_search', {
        'size': 2, 'query': {'bool': {'filter': [
            {'term': {'distribution': name}}, {'term': {'version': version}},
            {'term': {'authorized': True}},
        ]}},
    }, min_interval=0.25)
    hits = data['hits']['hits']
    if data.get('timed_out') or data.get('_shards', {}).get('failed'):
        raise ValueError('MetaCPAN search was incomplete')
    if len(hits) != 1:
        raise UnsupportedRelease('MetaCPAN exact release is missing or ambiguous.')
    info = hits[0]['_source']
    if (info.get('distribution') != name or str(info.get('version')) != version
            or info.get('authorized') is not True
            or not isinstance(info.get('author'), str) or not isinstance(info.get('name'), str)):
        raise ValueError('MetaCPAN release identity does not match the query')
    url = 'https://metacpan.org/release/' + quote(info['author'], safe='') + '/' + quote(info['name'], safe='')
    return info, url


def metadata(settings, version, io):
    name = project(settings)
    info, url = release(name, version, io)
    licenses = info.get('license')
    code = licenses[0] if isinstance(licenses, list) and len(licenses) == 1 else None
    expression = LICENSES.get(code) if isinstance(code, str) else None
    return Release(name, 'MetaCPAN', url, expression, code, None, None, 'license')
