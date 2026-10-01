"""Two-level package predicates, with explicit AND/OR/NOT links and conjunction precedence."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Groups and conditions share one budget; the root is fixed, not user-recursive.
# See docs/design.md for the parse + selection + candidate-count benchmark.
MAX_QUERY_NODES = 128
Logic = Literal['and', 'or', 'not']


class QueryModel(BaseModel):
    model_config = ConfigDict(
        extra='forbid', frozen=True, json_schema_serialization_defaults_required=True,
    )


class Condition(QueryModel):
    dimension: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=100)
    logic: Logic = 'and'

    @property
    def identity(self):
        return self.dimension, self.value


class Group(QueryModel):
    logic: Logic = 'and'
    conditions: tuple[Condition, ...] = ()

    @model_validator(mode='after')
    def unique(self):
        seen = set()
        terms = []
        for condition in self.conditions:
            if condition.identity not in seen:
                seen.add(condition.identity)
                terms.append(condition)
        if terms:
            # A leading condition NOT is independent of the row connector.
            if terms[0].logic != 'not' or self.logic == 'not':
                terms[0] = terms[0].model_copy(update={'logic': self.logic})
        object.__setattr__(self, 'conditions', tuple(terms))
        return self

    def contains(self, condition):
        return any(c.identity == condition.identity for c in self.conditions)

    def append(self, condition, logic):
        if self.contains(condition):
            return self
        connector = self.logic if self.conditions or logic == 'not' else logic
        return Group(logic=connector,
            conditions=(*self.conditions, condition.model_copy(update={'logic': logic})))


class FilterQuery(QueryModel):
    groups: tuple[Group, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def bounded(cls, value):
        if isinstance(value, dict):
            groups = value.get('groups', ())
            if isinstance(groups, (list, tuple)):
                # Count before deduplication; repeated input cannot evade the budget.
                nodes = len(groups)
                for group in groups:
                    if isinstance(group, Group):
                        terms = group.conditions
                    elif isinstance(group, dict):
                        terms = group.get('conditions', ())
                    else:
                        terms = ()
                    if isinstance(terms, (list, tuple)):
                        nodes += len(terms)
                    if nodes > MAX_QUERY_NODES:
                        raise ValueError(f'Query exceeds {MAX_QUERY_NODES} nodes (groups + conditions)')
        return value

    @property
    def nodes(self):
        return sum(1 + len(group.conditions) for group in self.groups)

    @classmethod
    def decode(cls, value):
        if not isinstance(value, str):
            return cls.model_validate(value)
        if value.lstrip().startswith('{'):
            return cls.model_validate_json(value)
        dimension, separator, term = value.partition('=')
        if not separator:
            raise ValueError('Expected dimension=value or a JSON filter query')
        return cls(groups=(Group(conditions=(Condition(dimension=dimension, value=term),)),))

    def encode(self):
        return self.model_dump_json(exclude_defaults=True)

    def parameters(self):
        """A single row is an ordered list of links; multiple rows need brackets."""
        if not self.groups:
            return []
        if (len(self.groups) == 1 and self.groups[0].conditions
                and self.groups[0].logic != 'not'
                and not (self.groups[0].logic == 'or' and self.groups[0].conditions[0].logic == 'not')):
            return [(term.logic.upper() + '-' + term.dimension, term.value)
                    for term in self.groups[0].conditions]
        return [('filters', self.encode())]

    @classmethod
    def extract(cls, parameters):
        """Return the filter and non-filter parameters without collapsing repeats."""
        terms, remaining, serialized = [], [], []
        for key, value in parameters:
            if key.startswith(('AND-', 'OR-', 'NOT-')):
                logic, _, dimension = key.partition('-')
                terms.append({'dimension': dimension, 'value': value, 'logic': logic.lower()})
            elif key == 'filters':
                serialized.append(value)
            else:
                remaining.append((key, value))
        if len(serialized) > 1 or (terms and serialized):
            raise ValueError('Use ordered AND-/OR-/NOT- parameters or one filters parameter, not both')
        if terms:
            # Validate raw terms before Group deduplication, including its node cost.
            query = cls.model_validate({'groups': [{'logic': 'and' if terms[0]['logic'] == 'not' else terms[0]['logic'], 'conditions': terms}]})
        else:
            query = cls.decode(serialized[0]) if serialized else cls()
        return query, remaining


def segments(operands, universe):
    """Split a union of intersection/difference runs; leading NOT complements the scope."""
    alternatives = set()
    tail = None
    for logic, operand in operands:
        if tail is None:
            tail = set(universe).difference(operand) if logic == 'not' else set(operand)
        elif logic == 'and':
            tail.intersection_update(operand)
        elif logic == 'not':
            tail.difference_update(operand)
        else:
            alternatives.update(tail)
            tail = set(operand)
    return frozenset(alternatives), None if tail is None else frozenset(tail)


def combine(operands, universe):
    alternatives, tail = segments(operands, universe)
    return universe if tail is None else alternatives | tail


class Evaluation:
    """Results depend only on predicates; counts also use the next edit's context.

    Candidate counts add one condition idempotently to the active row.
    Counts are capped by that condition's own scoped population; matches
    always contains the full expression result. Empty rows are not ALL operands.
    """
    def __init__(self, index, scope, query, active=0, next_logic: Logic = 'and'):
        if active < 0 or active >= max(1, len(query.groups)):
            raise ValueError('Active group does not exist')
        self.index = index
        self.scope = frozenset(scope)
        self.query = query
        self.active = active
        self.next_logic = next_logic
        for group in query.groups:
            for condition in group.conditions:
                if condition.dimension not in index:
                    raise ValueError('Unknown filter dimension: ' + condition.dimension)
        self.groups = [self.group(group) if group.conditions else None for group in query.groups]
        self.matches = combine(((g.logic, result) for g, result in zip(query.groups, self.groups)
                                if result is not None), self.scope)
        self.current = query.groups[active] if query.groups else Group()
        self.alternatives, self.tail = segments(self.operands(self.current), self.scope)
        # The active row occurs once: F(X) = (X & F(all)) | (~X & F(empty)).
        # Evaluate fixed rows twice, not once per candidate in the whole palette.
        self.fixed_matches = self.substitute(frozenset())
        self.possible_matches = self.substitute(self.scope)

    def predicate(self, condition):
        return self.scope.intersection(self.index.get(condition.dimension, {}).get(condition.value, ()))

    def operands(self, group):
        # The row connector is applied outside its parentheses, exactly once.
        return ((c.logic if i or (c.logic == 'not' and group.logic != 'not') else 'and', self.predicate(c))
                for i, c in enumerate(group.conditions))

    def group(self, group):
        return combine(self.operands(group), self.scope)

    def count(self, condition):
        members = self.predicate(condition)
        if self.current.contains(condition):
            count = len(self.matches)
        else:
            candidate = (self.scope - members if self.tail is None
                         and self.next_logic == 'not' and self.current.logic != 'not' else members)
            if self.tail is not None:
                candidate = self.alternatives | (self.tail & members if self.next_logic == 'and'
                    else self.tail - members if self.next_logic == 'not' else self.tail | members)
            count = len((candidate & self.possible_matches) | ((self.scope - candidate) & self.fixed_matches))
        return min(count, len(members))

    def substitute(self, candidate):
        groups = list(zip((g.logic for g in self.query.groups), self.groups))
        replacement = (self.current.logic if self.current.conditions or self.next_logic == 'not' else self.next_logic, candidate)
        if groups:
            groups[self.active] = replacement
        else:
            groups.append(replacement)
        return combine(((logic, result) for logic, result in groups if result is not None), self.scope)
