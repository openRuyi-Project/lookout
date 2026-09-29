"""deps.dev license evidence for an exact Go module release, not build metadata."""
import re
from urllib.parse import quote

from tracker.identity import from_package
from tracker.monitors.source.release import semver
from tracker.providers.model import Release, UnsupportedRelease
from tracker.providers.go_mod import retractions, version_key


HOSTS = {'api.deps.dev', 'proxy.golang.org'}


def project(settings):
    if (not isinstance(settings, dict) or set(settings) != {'go'}
            or not isinstance(settings['go'], str) or len(settings['go']) > 512
            or not re.fullmatch(r'[A-Za-z0-9.-]+\.[A-Za-z0-9.-]+/[A-Za-z0-9_./+~-]+', settings['go'])
            or any(p in ('', '.', '..') for p in settings['go'].split('/'))):
        raise ValueError('monitor requires a Go module identity')
    return settings['go']


def inputs(package, configured):
    if configured is not None:
        project(configured)
        return configured
    identity = from_package(package)
    return {'go': identity['name']} if identity and identity['ecosystem'] == 'Go' else None


def metadata(settings, version, io):
    name = project(settings)
    if not isinstance(version, str) or not semver(version.removeprefix('v')):
        raise UnsupportedRelease('Current source version does not establish an exact Go release.')
    version = 'v' + version.removeprefix('v')
    url = f'https://api.deps.dev/v3/systems/go/packages/{quote(name, safe="")}/versions/{quote(version, safe="")}'
    data = io.json('GET', url, min_interval=0.25)
    if data.get('versionKey') != {'system': 'GO', 'name': name, 'version': version}:
        raise ValueError('deps.dev release identity does not match the query')
    licenses = data.get('licenses')
    # The API explicitly leaves the relationship between multiple licenses
    # unspecified. A single expression can be compared without inventing one.
    expression = licenses[0] if isinstance(licenses, list) and len(licenses) == 1 else None
    return Release(name, 'deps.dev', url, expression, expression, None, None, 'licenses (licensecheck)')


def withdrawal(settings, version, io):
    name = project(settings)
    try:
        version = 'v' + version.removeprefix('v')
        current = version_key(version)
    except (ValueError, AttributeError) as error:
        raise UnsupportedRelease('Current source version does not establish an exact Go release.') from error
    escaped = re.sub('[A-Z]', lambda match: '!' + match[0].lower(), name)
    base = 'https://proxy.golang.org/' + quote(escaped, safe='/!')
    versions = io.text(base + '/@v/list', min_interval=0.25).splitlines()
    if version not in versions:
        observed = io.json('GET', base + '/@v/' + quote(version, safe='') + '.info', min_interval=0.25)
        canonical = observed.get('Version')
        # +incompatible is proxy notation for the same pre-modules tag, not a
        # different release. Require the exact .info response, never append it speculatively.
        compatible = version + '+incompatible' if '+' not in version and current[0][0] >= 2 else version
        if canonical not in (version, compatible):
            raise ValueError('Go proxy release identity does not match the query')
        version = canonical
        current = version_key(version)
    # The list includes retracted tags. @latest alone can omit a release that
    # retracts itself, thereby hiding its other retractions (Go Modules §retract).
    versions = [v for v in versions if v and not re.search(r'[.-]\d{14}-[0-9a-f]{12}(?:\+incompatible)?$', v)]
    if versions:
        ordered = [(version_key(value), value) for value in versions]
        stable = [item for item in ordered if item[0][1]]
        latest = max(stable or ordered)[1]
    else:
        latest = io.json('GET', base + '/@latest', min_interval=0.25)['Version']
        version_key(latest)
    url = base + '/@v/' + quote(latest, safe='') + '.mod'
    document = io.text(url, min_interval=0.25)
    ranges = retractions(document, name)
    yanked = any(low <= current <= high for low, high in ranges)
    return Release(name, 'Go module proxy', url, None, None, yanked, None,
                   version=version, withdrawal_field='retracted')
