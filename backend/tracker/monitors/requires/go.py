"""Exact-release Go language minimum, not a binary runtime dependency."""
import re
from dataclasses import replace

from tracker.monitors.model import evidence
from tracker.monitors.requires.compare import numeric_release
from tracker.monitors.requires.model import Requirement, UnsupportedRequirements
from tracker.providers import go_components
from tracker.providers.go import inputs as module_inputs
from tracker.providers.go import module_document, project
from tracker.providers.go_mod import directives

HOSTS = {'proxy.golang.org', 'raw.githubusercontent.com'}
__all__ = ['HOSTS', 'inputs', 'read']


def minimum(document, module, url, source):
    declarations = [values for directive, values in directives(document, module)
                    if directive == 'go']
    if len(declarations) != 1 or len(declarations[0]) != 1:
        raise UnsupportedRequirements('An explicit Go language minimum is missing or invalid.')
    declaration = declarations[0][0]
    if not re.fullmatch(r'[1-9][0-9]*\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?', declaration):
        raise UnsupportedRequirements('The Go language minimum is not a stable numeric version.')
    try:
        normalized = numeric_release(declaration)
    except ValueError as error:
        raise UnsupportedRequirements('The Go language minimum is not a stable numeric version.') from error
    # toolchain is a main-module suggestion, not this module's required minimum.
    return Requirement('go', 'Go', 'build', 'numeric_minimum', declaration,
                       normalized, source, url)


def inputs(package, configured):
    return go_components.inputs(package, configured) or module_inputs(package, configured)


def read(version, settings, io):
    if isinstance(settings.get('go'), str):
        document, url = module_document(settings, version, io)
        return [minimum(document, project(settings), url, 'Go module proxy')]
    components = settings['go']
    if version != components['version']:
        raise UnsupportedRequirements('Component source archives describe only the current package.')
    requirements, facts = [], []
    for module, document, url, archive in go_components.documents(components, io):
        requirement = minimum(document, module, url, 'go.mod')
        requirements.append(requirement)
        facts.extend([evidence('Go minimum', [module, requirement.declaration], 'go.mod', url),
                      evidence('Source archive', module, 'SPEC', archive)])
    strongest = max(requirements, key=lambda item: numeric_release(item.declaration))
    return [replace(strongest, facts=tuple(facts))]
