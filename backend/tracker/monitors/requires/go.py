"""Exact-release Go language minimum, not a binary runtime dependency."""
import re

from tracker.monitors.requires.compare import numeric_release
from tracker.monitors.requires.model import Requirement, UnsupportedRequirements
from tracker.providers.go import inputs as inputs
from tracker.providers.go import module_document, project
from tracker.providers.go_mod import directives

HOSTS = {'proxy.golang.org'}
__all__ = ['HOSTS', 'inputs', 'read']


def read(version, settings, io):
    document, url = module_document(settings, version, io)
    declarations = [values for directive, values in directives(document, project(settings))
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
    return [Requirement('go', 'Go', 'build', 'numeric_minimum', declaration,
                        normalized, 'Go module proxy', url)]
