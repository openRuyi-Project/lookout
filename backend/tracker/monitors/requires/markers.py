"""Partial PEP 508 evaluation against declared targets, never the collector host."""
from packaging.markers import InvalidMarker, Marker


def applies(expression, environment):
    if not expression:
        return True
    if len(expression) > 1024:
        return None
    try:
        # Packaging has no public partial-evaluation API. Isolate its private
        # tree here; unknown nodes/variables stay unknown rather than borrowing
        # default_environment(), which describes the monitor, not openRuyi.
        from packaging._parser import Variable
        from packaging.markers import _evaluate_markers

        budget = 256

        def evaluate(node, depth=0):
            nonlocal budget
            budget -= 1
            if budget < 0 or depth > 32:
                raise ValueError('marker budget exceeded')
            if isinstance(node, tuple) and len(node) == 3:
                variables = {part.value for part in (node[0], node[2]) if isinstance(part, Variable)}
                if not variables <= environment.keys():
                    return None
                return _evaluate_markers([node], environment)
            if not isinstance(node, list) or not node or len(node) % 2 != 1:
                raise ValueError('unknown marker tree')
            alternatives = [[]]
            for index, part in enumerate(node):
                if index % 2 == 0:
                    alternatives[-1].append(evaluate(part, depth + 1))
                elif part == 'or':
                    alternatives.append([])
                elif part != 'and':
                    raise ValueError('unknown marker operator')
            conjunctions = [False if False in group else None if None in group else True
                            for group in alternatives]
            return True if True in conjunctions else None if None in conjunctions else False

        return evaluate(Marker(expression)._markers)
    except (InvalidMarker, ValueError, TypeError, KeyError, AttributeError, ImportError, RecursionError):
        return None
