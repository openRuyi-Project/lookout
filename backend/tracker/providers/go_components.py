"""Go submodule tag archives and explicit bundled-module associations."""
import re
from urllib.parse import quote, urlsplit

from tracker.monitors.source.release import semver
from tracker.providers.go import inputs as module_inputs
from tracker.providers.go import project
from tracker.providers.go_mod import directives

# Each component contributes two facts to a finding, whose contract permits 256.
MAX_COMPONENTS = 128


def source_tag(archive):
    if not isinstance(archive, str) or len(archive) > 8192:
        return None
    try:
        url = urlsplit(archive)
        if (url.scheme != 'https' or url.hostname != 'github.com' or url.username or url.password
                or url.port not in (None, 443) or url.query):
            return None
    except ValueError:
        return None
    match = re.fullmatch(r'/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/archive/refs/tags/'
                         r'([A-Za-z0-9_./+-]+)\.(?:tar\.gz|zip)', url.path)
    if not match or any(part in ('', '.', '..') for part in url.path.split('/')[1:]):
        return None
    path, separator, version = match[2].rpartition('/')
    if not separator or not version.startswith('v') or not semver(version[1:]):
        return None
    return {'archive': archive.split('#', 1)[0], 'repository': match[1], 'tag': match[2], 'path': path}


def source_path(path):
    if (not isinstance(path, str) or len(path) > 512
            or not re.fullmatch(r'[A-Za-z0-9_./+-]+', path)
            or any(part in ('', '.', '..') for part in path.split('/'))):
        raise ValueError('Invalid Go component path')
    return quote(path, safe='/')


def document_url(component, path):
    return ('https://raw.githubusercontent.com/' + component['repository'] + '/refs/tags/'
            + quote(component['tag'], safe='/') + '/' + source_path(path) + '/go.mod')


def inputs(package, configured):
    if configured is not None and (not isinstance(configured, dict) or not isinstance(configured.get('go'), dict)):
        return None
    includes = []
    if configured is not None:
        if set(configured) != {'go'} or set(configured['go']) != {'include'}:
            raise ValueError('Go bundled modules require include associations')
        includes = configured['go']['include']
        if not isinstance(includes, list) or not 1 <= len(includes) <= MAX_COMPONENTS:
            raise ValueError('Invalid Go bundled-module count')
        seen = set()
        for item in includes:
            if not isinstance(item, dict) or set(item) != {'module', 'from_module', 'path'}:
                raise ValueError('Go bundled module requires module, from_module and path')
            module = project({'go': item['module']})
            project({'go': item['from_module']})
            source_path(item['path'])
            if module in seen or module == item['from_module']:
                raise ValueError('Duplicate or self-referencing Go bundled module')
            seen.add(module)
    identity = module_inputs(package, None)
    if identity is None:
        if configured is not None:
            raise ValueError('Go source discovery requires an established Go identity')
        return None
    components = {}
    for source in package.get('source_archives') or []:
        if candidate := source_tag(source['url']):
            key = candidate['repository'], candidate['tag']
            previous = components.get(key)
            if previous is None or candidate['archive'] < previous['archive']:
                components[key] = candidate
    if not components:
        if includes:
            raise ValueError('Go bundled modules have no submodule source archives')
        return None
    if len(components) == 1 and not includes:
        path = next(iter(components.values()))['path']
        module_path = re.sub(r'/v(?:[2-9]|[1-9][0-9]+)$', '', identity['go'])
        # Keep exact proxy releases and upgrade support for established single modules.
        if module_path.endswith('/' + path):
            return None
    if len(components) + len(includes) > MAX_COMPONENTS:
        raise ValueError('Go components exceed the 256-fact evidence budget')
    return {'go': {'version': package['version'],
                   'components': sorted(components.values(), key=lambda item: item['archive']),
                   'include': sorted(includes, key=lambda item: item['module'])}}


def documents(settings, io):
    components = {}
    for component in settings['components']:
        url = document_url(component, component['path'])
        document = io.text(url, min_interval=0.25)
        module = next(values[0] for key, values in directives(document, None) if key == 'module')
        project({'go': module})
        if module in components:
            raise ValueError('Multiple source archives declare the same Go module')
        components[module] = component
        yield module, document, url, component['archive']
    # Only direct source modules can anchor an exception: no recursive include DSL.
    for include in settings['include']:
        component = components.get(include['from_module'])
        if component is None or include['module'] in components:
            raise ValueError('Go bundled-module anchor is missing or ambiguous')
        url = document_url(component, include['path'])
        yield include['module'], io.text(url, min_interval=0.25), url, component['archive']
