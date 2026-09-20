"""Operator-owned configuration, never supplied by an HTTP request."""
from pathlib import Path
from . import version_rules
import hashlib
import os
import json
import re
import tomllib
from urllib.parse import urlsplit, urlunsplit, parse_qs, quote

def load(path, snapshot=None):
    path = Path(path).resolve()
    raw = path.read_bytes()
    config = tomllib.loads(raw.decode())
    config['config_digest'] = hashlib.sha256(raw).hexdigest()
    for key, default in (('obs_interval_seconds', 60), ('nvchecker_interval_seconds', 21600),
                         ('nvchecker_timeout_seconds', 7200), ('build_history_interval_seconds', 300)):
        value = config['collector'].setdefault(key, default)
        if type(value) is not int or value <= 0:
            raise ValueError(f'{key} must be a positive integer')
    for interval, stale, default in (('obs_interval_seconds', 'obs_stale_after_seconds', 300),
                                      ('nvchecker_interval_seconds', 'stale_after_seconds', 86400)):
        if config['collector'].get(stale, default) <= config['collector'][interval]:
            raise ValueError(f'{stale} must exceed {interval}')
    nvpath = path.parent / config['collector'].get('nvchecker_config', 'nvchecker.toml')
    text = nvpath.read_text()
    rules = version_rules.load(nvpath)
    native = rules.entries
    origins = {name: list(origin.table) for name, origin in rules.origins.items()}
    rule_files = {name: origin.file for name, origin in rules.origins.items()}
    overridden = sorted(name for name, origin in rules.origins.items() if origin.overrides_group)
    version_bindings, options = rules.bindings, rules.options
    # Native rule files own executable nvchecker entries. During migration, retain
    # only tracker-specific binding policy from the old generated source; it is
    # not read as a second executable rule source.
    if nvpath.name == 'nvchecker.toml':
        legacy = nvpath.parent / 'groups.toml'
        if legacy.is_file():
            _, _, version_bindings, _ = version_rules.expand(legacy.read_text())
    config['nvpath'] = str(nvpath)
    config['native'] = native
    config['rule_origins'] = origins
    config['rule_files'] = rule_files
    config['automatic_filters'] = version_rules.filters(text)
    config['exception_notes'] = {}
    for file in version_rules.files(nvpath):
        if file==nvpath:continue
        lines=[]
        for line in file.read_text().splitlines(keepends=True):
            if line.strip() and not line.lstrip().startswith('#'):break
            lines.append(line)
        if lines:config['exception_notes'][file.stem]=''.join(lines)
    config['group_native'], config['group_origins'], _, _ = version_rules.expand(text)
    config['exception_native'] = {n:e for n,e in native.items() if rule_files[n] != str(nvpath)}
    automatic, automatic_origins = version_rules.automatic(text,snapshot or {},native)
    native.update(automatic); origins.update(automatic_origins)
    rule_files.update({n:str(nvpath) for n in automatic})
    config['automatic_rules'] = sorted(automatic)
    config['overridden_rules'] = overridden
    config['compact_versions'] = 'schema' in tomllib.loads(text)
    config['native_options'] = options
    config['version_bindings'] = version_bindings
    config['nv_digest'] = version_rules.digest(nvpath)
    config.setdefault('packages', {})
    if config['compact_versions']:
        for name, entry in config['packages'].items():
            if set(entry) & version_rules.BINDING_KEYS:
                raise ValueError('version policy belongs only in the centralized rule file: ' + name)
    for name, entry in version_bindings.items():
        config['packages'].setdefault(name, {}).update(entry)
    # Distribution presentation data has one owner; the frontend knows no
    # BuildSystem categories. CSS values are deliberately limited to hex colors.
    config.setdefault('openruyi', {}).setdefault('buildsystems', {})
    for name, appearance in config['openruyi']['buildsystems'].items():
        if (not isinstance(name, str) or not name or len(name) > 100
                or not isinstance(appearance, dict) or set(appearance) != {'background', 'foreground'}
                or any(not isinstance(v, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', v)
                       for v in appearance.values())):
            raise ValueError('openruyi.buildsystems requires category names and background/foreground hex colors')
    ids = [t['id'] for t in config['targets']]
    identities = [(t['repository'], t['architecture']) for t in config['targets']]
    if len(ids) != 3 or len(set(ids)) != 3 or len(set(identities)) != 3:
        raise ValueError('exactly three distinct target identities are required')
    for binding in config['packages'].values():
        for track in [binding.get('compare'), *binding.get('watch', [])]:
            if track and track not in config['native']:
                raise ValueError(f'unknown configured track: {track}')
    # Optional git SPEC source (third external source, peer to OBS and nvchecker).
    # One managed full clone holds all history (changelogs) and every current SPEC
    # blob (read via cat-file). Absent config disables the source cleanly, so tests
    # and non-git deployments are unaffected.
    spec = config.get('spec', {})
    config['spec'] = {
        # The clone path is environment-specific (host vs container), so an env var may
        # override it without duplicating config; TRACKER_SPEC_REPO enables the source
        # even when no [spec] table is present (used by the container image).
        'repo': os.environ.get('TRACKER_SPEC_REPO') or spec.get('repo'),
        'url': spec.get('url', 'https://github.com/openRuyi-Project/openRuyi.git'),
        'branch': spec.get('branch', 'main'),
        'source_url_template': spec.get('source_url_template'),
        'macro_package': spec.get('macro_package', config['collector'].get('spec_macro_package')),
        'changelog_limit': spec.get('changelog_limit', 20),
        'fetch_timeout_seconds': spec.get('fetch_timeout_seconds', 300),
    }
    if type(config['spec']['changelog_limit']) is not int or not 1 <= config['spec']['changelog_limit'] <= 200:
        raise ValueError('spec.changelog_limit must be an integer in 1..200')
    timeout = config['spec']['fetch_timeout_seconds']
    if type(timeout) is not int or timeout <= 0:
        raise ValueError('spec.fetch_timeout_seconds must be a positive integer')
    interval = spec.get('interval_seconds', 21600)
    if type(interval) is not int or interval <= 0:
        raise ValueError('spec.interval_seconds must be a positive integer')
    config['spec']['interval_seconds'] = interval
    for key in ('url', 'branch'):
        value = config['spec'][key]
        if not isinstance(value, str) or not value or value.startswith('-') or any(c in value for c in '\r\n\x00'):
            raise ValueError(f'spec.{key} must be a non-empty safe string')
    repo = config['spec']['repo']
    if repo is not None and (not isinstance(repo, str) or not repo):
        raise ValueError('spec.repo must be a non-empty path or omitted')

    for value in (config['obs']['api_url'], config['obs']['web_url']):
        u = urlsplit(value)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('OBS base URL must not contain credentials or a query')
    template = config['spec']['source_url_template']
    if template is not None and not spec_source_url({'url': config['spec']['url'], 'source_url_template': template}, 'PACKAGE', 'REF'):
        raise ValueError('spec.source_url_template must be a public HTTP(S) URL containing {ref} and {path}')
    return config

def track_fingerprint(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def public_source(entry):
    """Keep secrets, executable commands, arbitrary config out of the public API."""
    result = {}
    for key in ('source', 'pypi', 'cratesio', 'cpan', 'anitya', 'github', 'git', 'url'):
        value = entry.get(key)
        if not isinstance(value, str):
            continue
        if key in ('url', 'git'):
            u = urlsplit(value)
            if u.scheme not in ('https', 'http') or not u.hostname:
                continue
            value = urlunsplit((u.scheme, u.hostname + (f':{u.port}' if u.port else ''), u.path, '', ''))
        result[key] = value
    # Expose a known public identity, not arbitrary query strings/credentials.
    if entry.get('source') == 'jq':
        try:
            u = urlsplit(entry.get('url', ''))
            ids = parse_qs(u.query).get('project_id', [])
            if (u.scheme == 'https' and u.hostname == 'release-monitoring.org'
                    and u.path.rstrip('/') == '/api/v2/versions'
                    and len(ids) == 1 and re.fullmatch(r'[1-9][0-9]{0,11}', ids[0])):
                result['project_id'] = int(ids[0])
                result['project_url'] = 'https://release-monitoring.org/project/' + ids[0] + '/'
        except (ValueError, TypeError):
            pass
    return result


# A release-line label is normally implicit in the package name: a trailing "-N[.N...]"
# means that maintenance line. Only genuine exceptions need an explicit binding.
_TRACK_LABEL = re.compile(r'-(\d+(?:\.\d+)*)$')

def derive_track_label(name):
    m = _TRACK_LABEL.search(name)
    return f'{m.group(1)}.x' if m else 'stable'

def binding(config, name):
    b = config.get('packages', {}).get(name, {})
    return {**b, 'compare': b.get('compare', name if name in config['native'] else None),
            'watch': b.get('watch', []), 'track_label': b.get('track_label') or derive_track_label(name)}


def spec_source_url(origin, name, ref):
    """Writer-owned provenance selects the URL; the frontend never guesses a forge.

    Known forge conventions are defaults. Other hosts require one explicit
    operator template, not a new code adapter. Never interpolate unescaped input.
    """
    try:
        u = urlsplit(origin.get('url', ''))
        if u.scheme not in ('https', 'http') or not u.hostname or u.username or u.password or u.query or u.fragment:
            return None
        template = origin.get('source_url_template')
        base = urlunsplit((u.scheme, u.netloc, u.path.rstrip('/').removesuffix('.git'), '', ''))
        if template is None:
            if u.hostname == 'github.com':
                template = base + '/tree/{ref}/{path}'
            elif u.hostname.startswith('gitlab.') or u.hostname == 'gitlab.com':
                template = base + '/-/tree/{ref}/{path}'
            else:
                return None
        if not isinstance(template, str) or '{ref}' not in template or '{path}' not in template:
            return None
        url = template.format(ref=quote(ref, safe=''), path='SPECS/' + quote(name, safe=''))
        result = urlsplit(url)
        if (result.scheme not in ('https', 'http') or not result.hostname or result.username
                or result.password or result.query or result.fragment or any(c in url for c in '\r\n{}')):
            return None
        return url
    except (ValueError, KeyError, TypeError, IndexError):
        return None
