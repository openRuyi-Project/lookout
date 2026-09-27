"""Exact-release PyPI runtime declarations; never resolved on the monitor host."""

from dataclasses import replace

from packaging.requirements import InvalidRequirement, Requirement as PyPIRequirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

from .pypi_metadata import HOSTS, inputs, project, release
from .requirements import Requirement, UnsupportedRequirements


MAX_DECLARATION = 1024
MAX_DEPENDENCIES = 256
MAX_SPECIFIERS = 64


def _optional(marker):
    """Whether this clause requires an optional feature, not whether it applies here.

    Packaging exposes no public marker AST. Keep its private tree at this adapter
    boundary, with a bounded, conservative fallback for unrecognized forms. Only
    extra equality is interpreted; platform clauses are left unevaluated. A
    disjunction with a non-extra branch must not be labeled optional.
    """
    if marker is None:
        return False
    try:
        from packaging._parser import Value, Variable

        budget = 256

        def required(node, depth=0):
            nonlocal budget
            budget -= 1
            if budget < 0 or depth > 32:
                raise ValueError('marker analysis budget exceeded')
            if isinstance(node, tuple) and len(node) == 3:
                left, operator, right = node
                operands = (left, right)
                if not all(isinstance(item, (Variable, Value)) for item in operands):
                    raise ValueError('unknown marker operands')
                variables = [item.value for item in operands if isinstance(item, Variable)]
                if any(name in ('extras', 'dependency_groups') for name in variables):
                    return None  # Lock-file selectors are not PyPI metadata extras.
                if 'extra' not in variables:
                    return False
                if len(variables) != 1:
                    return None
                value = next(item.value for item in operands if isinstance(item, Value))
                if operator.value == '==':
                    return bool(value)
                if operator.value == '!=':
                    return not bool(value)
                return None
            if not isinstance(node, list) or not node or len(node) % 2 != 1:
                raise ValueError('unknown marker tree')
            alternatives = [[]]
            for index, item in enumerate(node):
                if index % 2 == 0:
                    alternatives[-1].append(required(item, depth + 1))
                elif item == 'or':
                    alternatives.append([])
                elif item != 'and':
                    raise ValueError('unknown marker operator')
            # One required feature gates a conjunction; every alternative must
            # be gated to make the entire declaration exclusively optional.
            gates = [True if True in group else None if None in group else False for group in alternatives]
            return False if False in gates else None if None in gates else True

        return required(marker._markers)
    except (ImportError, AttributeError, TypeError, ValueError, RecursionError):
        return None


def _python(declaration, url):
    if (not isinstance(declaration, str) or len(declaration) > MAX_DECLARATION
            or len(declaration.split(",")) > MAX_SPECIFIERS):
        raise UnsupportedRequirements("Provider Requires-Python declaration is invalid or exceeds the comparison budget.")
    try:
        normalized = SpecifierSet(declaration)
    except InvalidSpecifier as error:
        raise UnsupportedRequirements("Provider Requires-Python declaration is invalid.") from error
    return Requirement("python", "Python", "runtime", "pep440", declaration, normalized, "PyPI", url,
                       optional=False)


def _distribution(declaration, url):
    if not isinstance(declaration, str) or not declaration.strip() or len(declaration) > MAX_DECLARATION:
        raise UnsupportedRequirements("Provider Requires-Dist declaration is invalid or exceeds the comparison budget.")
    try:
        parsed = PyPIRequirement(declaration)
    except (InvalidRequirement, RecursionError) as error:
        raise UnsupportedRequirements("Provider Requires-Dist declaration is invalid.") from error
    if parsed.url is not None:
        raise UnsupportedRequirements("Provider Requires-Dist direct URL constraints are not comparable version declarations.")
    name = canonicalize_name(parsed.name)
    condition = str(parsed.marker) if parsed.marker is not None else None
    if (len(parsed.name) > 100 or len(name) > 95 or len(parsed.specifier) > MAX_SPECIFIERS
            or condition is not None and len(condition) > MAX_DECLARATION
            or len(parsed.extras) > 64 or any(len(extra) > 100 for extra in parsed.extras)):
        raise UnsupportedRequirements("Provider Requires-Dist declaration exceeds the comparison budget.")
    return Requirement(
        "pypi." + name, parsed.name, "runtime", "pep440", str(parsed.specifier), parsed.specifier, "PyPI", url,
        identity={"ecosystem": "PyPI", "name": name},
        condition=condition,
        extras=tuple(sorted(parsed.extras)),
        optional=_optional(parsed.marker),
    )


def read(version, settings, io):
    """Return observed clauses, not a claim that absent metadata means no dependencies.

    Every supplied Requires-Dist clause must be representable. In particular, a
    bad entry cannot turn a partial dependency list into a successful observation.
    Repeated declarations for one identity/marker/extras clause are conjunctive;
    combine their specifiers instead of silently dropping any constraint.
    Markers and extras stay attached to facts; optional-feature classification
    does not evaluate the monitor host or a target environment.
    """
    info, url = release(project(settings), version, io)
    result = []
    declaration = info.get("requires_python")
    if declaration is not None:
        if not isinstance(declaration, str) or len(declaration) > MAX_DECLARATION:
            raise UnsupportedRequirements("Provider Requires-Python declaration is invalid or exceeds the comparison budget.")
        if declaration.strip():
            result.append(_python(declaration, url))
    distributions = info.get("requires_dist")
    if distributions is not None:
        if not isinstance(distributions, list) or len(distributions) > MAX_DEPENDENCIES:
            raise UnsupportedRequirements("Provider Requires-Dist list is invalid or exceeds the comparison budget.")
        clauses = {}
        for item in distributions:
            parsed = _distribution(item, url)
            key = (parsed.dependency, parsed.condition, parsed.extras)
            if key in clauses:
                existing = clauses[key]
                combined = existing.comparison & parsed.comparison
                declaration = str(combined)
                if len(combined) > MAX_SPECIFIERS or len(declaration) > MAX_DECLARATION:
                    raise UnsupportedRequirements("Combined Requires-Dist clause exceeds the comparison budget.")
                parsed = replace(existing, declaration=declaration, comparison=combined)
            clauses[key] = parsed
        result.extend(clauses.values())
    if not result:
        raise UnsupportedRequirements("Comparable upstream runtime requirement declarations are missing.")
    return result
