"""Static CPAN runtime prerequisites; distribution versions are not module versions."""
import re

from tracker.monitors.requires.model import Requirement, UnsupportedRequirements
from tracker.providers.cpan import HOSTS, inputs, project, release


def read(version, settings, io):
    info, url = release(project(settings), version, io)
    metadata = info.get('metadata') or {}
    if metadata.get('dynamic_config') not in (False, 0, '0'):
        raise UnsupportedRequirements('CPAN prerequisite configuration is dynamic or unspecified.')
    if str(metadata.get('meta-spec', {}).get('version')) != '2':
        raise UnsupportedRequirements('CPAN metadata version is not supported.')
    runtime = metadata.get('prereqs', {}).get('runtime', {})
    if not isinstance(runtime, dict) or set(runtime) - {'requires', 'recommends', 'suggests', 'conflicts'}:
        raise UnsupportedRequirements('CPAN runtime prerequisite relationships are not supported.')
    # A conflict is not a positive dependency. Refuse an incomplete projection.
    if runtime.get('conflicts'):
        raise UnsupportedRequirements('CPAN runtime conflicts cannot be represented as dependencies.')
    result = []
    for relationship in ('requires', 'recommends', 'suggests'):
        declarations = runtime.get(relationship, {})
        if not isinstance(declarations, dict) or len(result) + len(declarations) > 256:
            raise UnsupportedRequirements('CPAN runtime prerequisites exceed the declaration budget.')
        for name, expression in declarations.items():
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_:]{0,79}', name)
                    or not isinstance(expression, str) or len(expression) > 1024):
                raise UnsupportedRequirements('CPAN runtime prerequisite is invalid.')
            # CPAN module identity is kept separate: a distribution can contain
            # several modules with versions unrelated to the archive version.
            dependency = 'perl' if name == 'perl' else 'cpanmodule.' + name.lower().replace('::', '.')
            result.append(Requirement(dependency, name, 'runtime', 'perl_version', expression, None,
                'MetaCPAN', url, identity={'ecosystem': 'CPANModule', 'name': name} if name != 'perl' else None,
                optional=relationship != 'requires', relationship=relationship,
                version_scope='release' if name == 'perl' else 'component'))
    return result


def provides(version, settings, io):
    info, url = release(project(settings), version, io)
    metadata = info.get('metadata') or {}
    modules = metadata.get('provides', {})
    if not isinstance(modules, dict):
        raise ValueError('CPAN provides must be a module map')
    indexed = set(info.get('provides', []))
    return [dict(identity={'ecosystem': 'CPANModule', 'name': name},
                 version=item['version'], source='MetaCPAN', url=url)
            for name, item in sorted(modules.items())
            if name in indexed and isinstance(item, dict)
            and isinstance(item.get('version'), str) and item['version']]
