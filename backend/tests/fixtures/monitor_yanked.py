"""Executable porting example, registered only by tests; not production coverage."""
from urllib.parse import quote

from tracker.monitor_model import evidence, finding
from tracker.package_identity import from_native

VERSION = 1
HOSTS = {'pypi.org'}


def inputs(package, configured):
    identity = configured if configured is not None else from_native(package['identity'])
    if identity is None:
        return None
    if set(identity) != {'ecosystem', 'name'} or identity['ecosystem'] != 'PyPI' or not identity['name']:
        raise ValueError('requires a PyPI ecosystem/name identity')
    return identity


def check(subject, settings, io):
    url = 'https://pypi.org/pypi/{}/{}/json'.format(
        quote(settings['name'], safe=''), quote(subject['version'], safe=''))
    files = io.json('GET', url)['urls']
    if not files:
        return {'status': 'unsupported', 'findings': [], 'note': 'No release file observations.'}
    if any(type(file.get('yanked')) is not bool for file in files):
        raise ValueError('missing file yanked status')
    findings = []
    if all(file['yanked'] for file in files):
        fact = evidence('All release files yanked', True, 'PyPI', url)
        findings.append(finding('release-yanked', 'Yanked', settings['name'], [fact], url))
    return {'status': 'ok', 'findings': findings, 'note': None}
