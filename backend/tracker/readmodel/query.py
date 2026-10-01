"""Ordered set operations and sealed groups; every sequence starts from its scope."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# One budget covers conditions and Group operations, including raw repeats.
MAX_QUERY_NODES = 128
Logic = Literal['and', 'or', 'not']


class QueryModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, json_schema_serialization_defaults_required=True)


class Condition(QueryModel):
    dimension: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=100)
    logic: Logic = 'and'

    @property
    def identity(self):
        return self.dimension, self.value


def unique(conditions):
    seen = set()
    result = []
    for term in conditions:
        if term.identity not in seen:
            seen.add(term.identity)
            result.append(term)
    return tuple(result)


class Group(QueryModel):
    logic: Logic = 'and'
    conditions: tuple[Condition, ...] = ()

    @model_validator(mode='after')
    def deduplicate(self):
        object.__setattr__(self, 'conditions', unique(self.conditions))
        return self

    def contains(self, condition):
        return any(term.identity == condition.identity for term in self.conditions)


class FilterQuery(QueryModel):
    groups: tuple[Group, ...] = ()
    tail: tuple[Condition, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def bounded(cls, value):
        if isinstance(value, dict):
            groups, tail = value.get('groups', ()), value.get('tail', ())
            if isinstance(groups, (list, tuple)) and isinstance(tail, (list, tuple)):
                nodes = len(groups) + len(tail)
                for group in groups:
                    terms = group.conditions if isinstance(group, Group) else group.get('conditions', ()) if isinstance(group, dict) else ()
                    if isinstance(terms, (list, tuple)):
                        nodes += len(terms)
                if nodes > MAX_QUERY_NODES:
                    raise ValueError(f'Query exceeds {MAX_QUERY_NODES} nodes (groups + conditions)')
        return value

    @model_validator(mode='after')
    def normalize(self):
        object.__setattr__(self, 'groups', tuple(group for group in self.groups if group.conditions))
        object.__setattr__(self, 'tail', unique(self.tail))
        return self

    @property
    def conditions(self):
        return (*self.tail, *(term for group in self.groups for term in group.conditions))

    @property
    def nodes(self):
        return len(self.tail) + sum(1 + len(group.conditions) for group in self.groups)

    def parameters(self):
        parameters = []
        for group in self.groups:
            parameters.extend((term.logic.upper() + '-' + term.dimension, term.value) for term in group.conditions)
            parameters.append(('group', group.logic.upper()))
        parameters.extend((term.logic.upper() + '-' + term.dimension, term.value) for term in self.tail)
        return parameters

    @classmethod
    def extract(cls, parameters):
        groups, tail, remaining = [], [], []
        nodes = 0
        for key, value in parameters:
            if key.startswith(('AND-', 'OR-', 'NOT-')):
                logic, _, dimension = key.partition('-')
                tail.append({'dimension': dimension, 'value': value, 'logic': logic.lower()})
                nodes += 1
            elif key == 'group':
                if not tail:
                    raise ValueError('Group requires preceding ungrouped conditions')
                groups.append({'logic': value.lower(), 'conditions': tail})
                tail = []
                nodes += 1
            else:
                remaining.append((key, value))
            if nodes > MAX_QUERY_NODES:
                raise ValueError(f'Query exceeds {MAX_QUERY_NODES} nodes (groups + conditions)')
        return cls.model_validate({'groups': groups, 'tail': tail}), remaining



def apply(logic, current, operand):
    if logic == 'and':
        return current & operand
    if logic == 'not':
        return current - operand
    return current | operand


def combine(operands, universe):
    result = frozenset(universe)
    for logic, operand in operands:
        result = apply(logic, result, operand)
    return result


class Evaluation:
    """One expression drives matches and capped, idempotent next-condition counts."""
    def __init__(self, index, scope, query, next_logic: Logic = 'and'):
        self.index, self.scope, self.query, self.next_logic = index, frozenset(scope), query, next_logic
        for condition in query.conditions:
            if condition.dimension not in index:
                raise ValueError('Unknown filter dimension: ' + condition.dimension)
        operands = [(group.logic, self.conditions(group.conditions)) for group in query.groups]
        operands.extend(self.operands(query.tail))
        self.current = Group(conditions=query.tail)
        self.matches = combine(operands, self.scope)

    def predicate(self, condition):
        return self.scope.intersection(self.index.get(condition.dimension, {}).get(condition.value, ()))

    def operands(self, conditions):
        return ((term.logic, self.predicate(term)) for term in conditions)

    def conditions(self, conditions):
        return combine(self.operands(conditions), self.scope)

    def count(self, condition):
        members = self.predicate(condition)
        if self.current.contains(condition):
            return min(len(self.matches), len(members))
        candidate = apply(self.next_logic, self.matches, members)
        return min(len(candidate), len(members))
