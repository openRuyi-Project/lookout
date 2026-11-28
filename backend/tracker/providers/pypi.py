"""Identity and release transport shared by independent PyPI monitors."""

from email import policy
from email.parser import Parser
from hashlib import sha256
from urllib.parse import quote, urlsplit

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from tracker.identity import from_package
from tracker.providers.model import Release, UnsupportedRelease

HOSTS = {"pypi.org", "files.pythonhosted.org"}


def project(settings):
    value = settings.get('pypi') if isinstance(settings, dict) and set(settings) == {'pypi'} else None
    name = value.get('name') if isinstance(value, dict) else value
    if not isinstance(name, str) or not 1 <= len(name) <= 512:
        raise ValueError("monitor requires a PyPI identity")
    return name


def inputs(package, configured):
    identity = from_package(package) if configured is None else None
    if configured is None and (not identity or identity['ecosystem'] != 'PyPI'):
        return None
    if configured is not None:
        name = project(configured)
    else:
        assert identity is not None
        name = identity['name']
    source = package.get('source_release')
    if (source and source.get('ecosystem') == 'PyPI'
            and canonicalize_name(source['name']) == canonicalize_name(name)):
        return {'pypi': {'name': name, 'version': source['version'], 'url': source['url']}}
    return configured if configured is not None else {'pypi': name}


def distribution(settings, version, io):
    source = settings['pypi']
    if not isinstance(source, dict) or Version(source['version']) != Version(version):
        return None
    url = source['url']
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'files.pythonhosted.org'
            or not parsed.path.startswith('/packages/') or parsed.query or parsed.fragment):
        raise ValueError('invalid PyPI distribution URL')
    name = canonicalize_name(project(settings))
    index = io.json('GET', f'https://pypi.org/simple/{quote(name, safe="")}/',
                    accept='application/vnd.pypi.simple.v1+json')
    filename = parsed.path.rsplit('/', 1)[-1]
    named_source = parsed.path.startswith('/packages/source/')
    matches = [row for row in index['files'] if row['filename'] == filename
               and (named_source or row['url'] == url)]
    if len(matches) != 1:
        raise UnsupportedRelease('Source0 does not identify one indexed PyPI file.')
    row = matches[0]
    selected = urlsplit(row['url'])
    if (selected.scheme != 'https' or selected.netloc != 'files.pythonhosted.org'
            or not selected.path.startswith('/packages/') or selected.query or selected.fragment
            or selected.path.rsplit('/', 1)[-1] != filename):
        raise ValueError('invalid indexed PyPI distribution URL')
    return row


def declarations(settings, version, io, fields):
    row = distribution(settings, version, io)
    if row is None:
        return release(project(settings), version, io)
    marker = row.get('core-metadata', row.get('dist-info-metadata'))
    if marker is not True and not isinstance(marker, dict):
        raise UnsupportedRelease('The Source0 file has no published Core Metadata.')
    url = row['url'] + '.metadata'
    body = io.text(url)
    if isinstance(marker, dict) and 'sha256' in marker and sha256(body.encode()).hexdigest() != marker['sha256']:
        raise ValueError('PyPI Core Metadata digest mismatch')
    msg = Parser(policy=policy.compat32).parsestr(body, headersonly=True)
    if (msg.defects or any(len(msg.get_all(field, [])) != 1 for field in ('Name', 'Version', 'Metadata-Version'))
            or canonicalize_name(msg['Name']) != canonicalize_name(project(settings))
            or Version(msg['Version']) != Version(version)):
        raise ValueError('PyPI Core Metadata identity mismatch')
    dynamic = {value.lower().strip() for value in msg.get_all('Dynamic', [])}
    level = Version(msg['Metadata-Version'])
    if level.major not in (1, 2):
        raise UnsupportedRelease('Unsupported PyPI Core Metadata version.')
    if not row['filename'].endswith('.whl') and (level < Version('2.2') or dynamic.intersection(fields)):
        raise UnsupportedRelease('The Source0 file does not establish static declarations.')
    if any(len(msg.get_all(field, [])) > 1 for field in fields if field != 'requires-dist'):
        raise ValueError('Duplicate PyPI Core Metadata declaration')
    return {field.replace('-', '_'): msg.get_all(field, []) if field == 'requires-dist' else msg.get(field)
            for field in fields}, url


def release(name, version, io):
    """Return release info and its evidence URL; shared IO owns request caching."""
    if not isinstance(version, str) or not 1 <= len(version) <= 512:
        raise ValueError("monitor requires an observed release version")
    try:
        local = Version(version).local
    except InvalidVersion:
        local = None
    if local is not None:
        # PyPI excludes local versions; stripping the suffix would query another build.
        raise UnsupportedRelease("A local build version does not identify a published PyPI release.")
    url = f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json"
    data = io.json("GET", url)
    if not isinstance(data, dict) or not isinstance(data.get("info"), dict):
        raise ValueError("PyPI release response has no metadata object")
    info = data["info"]
    observed_name, observed_version = info.get("name"), info.get("version")
    if (not isinstance(observed_name, str) or not 1 <= len(observed_name) <= 512
            or canonicalize_name(observed_name) != canonicalize_name(name)
            or not isinstance(observed_version, str) or not 1 <= len(observed_version) <= 512):
        raise ValueError("PyPI response identity does not match the query")
    if observed_version != version:
        try:
            same_version = Version(observed_version) == Version(version)
        except InvalidVersion:
            same_version = False
        if not same_version:
            raise ValueError("PyPI response identity does not match the query")
    return info, url


def metadata(settings, version, io):
    name = project(settings)
    info, url = declarations(settings, version, io, {'license-expression', 'license'})
    expression = info.get("license_expression")
    field = 'license_expression'
    if expression is None or expression == '':
        # Legacy License is free text. The license monitor still requires exact
        # valid SPDX; no classifier guessing or natural-language conversion.
        expression, field = info.get('license'), 'license'
    if expression is not None and not isinstance(expression, str):
        expression = None
    yanked, reason = info.get('yanked'), info.get('yanked_reason')
    if yanked is True and reason is not None and not isinstance(reason, str):
        raise ValueError('Registry yanked reason is not text')
    return Release(
        name=name, source="PyPI", url=url,
        license_expression=expression, license_declaration=expression,
        yanked=yanked if isinstance(yanked, bool) else None,
        yanked_reason=reason if isinstance(reason, str) else None,
        license_field=field,
    )


def withdrawal(settings, version, io):
    row = distribution(settings, version, io)
    if row is None:
        return metadata(settings, version, io)
    status = row.get('yanked')
    return Release(name=project(settings), source='PyPI', url=row['url'],
                   license_expression=None, license_declaration=None,
                   yanked=True if isinstance(status, str) else status,
                   yanked_reason=status if isinstance(status, str) else None,
                   withdrawal_field='file yanked')
