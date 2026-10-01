"""Bounded PEP 508 tree analysis without using the collector environment."""
from collections.abc import Callable, Sequence
from typing import Any

from packaging.markers import InvalidMarker, Marker

type Truth = bool | None


def all_of(values: Sequence[Truth]) -> Truth:
    return False if False in values else None if None in values else True


def any_of(values: Sequence[Truth]) -> Truth:
    return True if True in values else None if None in values else False


def fold(marker: Marker, clause: Callable[[tuple[Any, ...]], Truth],
         conjunction: Callable[[Sequence[Truth]], Truth],
         disjunction: Callable[[Sequence[Truth]], Truth]) -> Truth:
    """Fold the private packaging tree; callers own leaf and operator semantics."""
    budget = 256

    def visit(node, depth=0):
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > 32:
            raise ValueError('marker analysis budget exceeded')
        if isinstance(node, tuple) and len(node) == 3:
            return clause(node)
        if not isinstance(node, list) or not node or len(node) % 2 != 1:
            raise ValueError('unknown marker tree')
        alternatives = [[]]
        for index, part in enumerate(node):
            if index % 2 == 0:
                alternatives[-1].append(visit(part, depth + 1))
            elif part == 'or':
                alternatives.append([])
            elif part != 'and':
                raise ValueError('unknown marker operator')
        return disjunction([conjunction(group) for group in alternatives])

    return visit(marker._markers)


def applies(expression, environment):
    if not expression:
        return True
    if len(expression) > 1024:
        return None
    try:
        # Packaging has no public partial-evaluation API. Unknown variables stay
        # unknown; default_environment() would describe the collector, not a target.
        from packaging._parser import Variable
        from packaging.markers import _evaluate_markers

        def evaluate(node):
            variables = {part.value for part in (node[0], node[2]) if isinstance(part, Variable)}
            if not variables <= environment.keys():
                return None
            return _evaluate_markers([node], environment)

        return fold(Marker(expression), evaluate, all_of, any_of)
    except (InvalidMarker, ValueError, TypeError, KeyError, AttributeError, ImportError, RecursionError):
        return None
