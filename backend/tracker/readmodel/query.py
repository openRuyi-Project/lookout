"""Facet selections: union within each facet, intersection between facets."""
from copy import copy
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tracker.identity import FACET_NAME_LIMIT, facet_name

# Bounds URL parsing and set operations, including repeated raw values.
MAX_QUERY_NODES = 128


class Condition(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    dimension: str = Field(min_length=1, max_length=FACET_NAME_LIMIT)
    value: str = Field(min_length=1, max_length=100)

    @field_validator('dimension')
    @classmethod
    def valid_dimension(cls, value):
        return facet_name(value)

    @property
    def identity(self):
        return self.dimension, self.value


class FilterQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    selections: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode='after')
    def bounded(self):
        if sum(max(1, len(values)) for values in self.selections.values()) > MAX_QUERY_NODES:
            raise ValueError(f'Query exceeds {MAX_QUERY_NODES} selections')
        for dimension, values in self.selections.items():
            Condition(dimension=dimension, value='validate')
            for value in values:
                Condition(dimension=dimension, value=value)
        object.__setattr__(self, 'selections', {key: tuple(sorted(set(values)))
            for key, values in sorted(self.selections.items()) if values})
        return self

    @property
    def conditions(self):
        return tuple(Condition(dimension=dimension, value=value)
            for dimension, values in self.selections.items() for value in values)

    def selected(self, dimension, value):
        return value in self.selections.get(dimension, ())

    def select(self, dimension, values):
        return FilterQuery(selections={**self.selections, dimension: tuple(values)})

    def toggle(self, dimension, value):
        selected = set(self.selections.get(dimension, ()))
        selected.symmetric_difference_update((value,))
        return self.select(dimension, selected)

    def parameters(self):
        return [('select', dimension + '=' + value)
            for dimension, values in self.selections.items() for value in values]

    @classmethod
    def extract(cls, parameters):
        selections, remaining = {}, []
        nodes = 0
        for key, value in parameters:
            if key != 'select':
                remaining.append((key, value))
                continue
            nodes += 1
            if nodes > MAX_QUERY_NODES:
                raise ValueError(f'Query exceeds {MAX_QUERY_NODES} selections')
            dimension, separator, item = value.partition('=')
            if not separator or not dimension or not item:
                raise ValueError('Use select=FACET=VALUE with a nonempty facet and value')
            values = selections.setdefault(dimension, [])
            values.append(item)
        return cls(selections=selections), remaining


def encode_parameters(parameters):
    return urlencode(parameters, safe=':=')


class Evaluation:
    """Counts ignore their own facet, not other selected facets or text search."""
    def __init__(self, index, scope, query):
        self.index, self.scope, self.query = index, frozenset(scope), query
        dimensions = list(query.selections)
        operands = []
        for dimension, values in query.selections.items():
            if dimension not in index:
                raise ValueError('Unknown filter dimension: ' + dimension)
            operands.append(frozenset().union(*(index[dimension].get(value, ()) for value in values)))
        prefixes = [self.scope]
        for operand in operands:
            prefixes.append(prefixes[-1] & operand)
        self.matches = prefixes[-1]
        self.facet_scopes = {}
        suffix = self.scope
        for i in range(len(dimensions) - 1, -1, -1):
            self.facet_scopes[dimensions[i]] = prefixes[i] & suffix
            suffix &= operands[i]

    def restrict(self, scope):
        """Intersect an additional scope without repeating the selected unions."""
        result = copy(self)
        result.scope = self.scope.intersection(scope)
        result.matches = self.matches.intersection(scope)
        result.facet_scopes = {dimension: members.intersection(scope)
                               for dimension, members in self.facet_scopes.items()}
        return result

    def count(self, condition):
        members = self.index.get(condition.dimension, {}).get(condition.value, ())
        return len(self.facet_scopes.get(condition.dimension, self.matches).intersection(members))
