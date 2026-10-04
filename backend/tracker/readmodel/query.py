"""Ordered set operations and sealed groups; every sequence starts from its scope."""
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracker.monitors.build.status import STATES

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
    mode: Literal['basic', 'advanced'] = 'advanced'
    groups: tuple[Group, ...] = ()
    tail: tuple[Condition, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def bounded(cls, value):
        if isinstance(value, dict):
            groups, tail = value.get('groups', ()), value.get('tail', ())
            if isinstance(groups, (list, tuple)) and isinstance(tail, (list, tuple)):
                nodes = len(groups) + len(tail)
                if value.get('mode') == 'basic':
                    alternatives = {}
                    for term in tail:
                        fields = term.model_dump() if isinstance(term, Condition) else term
                        if isinstance(fields, dict):
                            dimension, item = fields.get('dimension'), fields.get('value')
                            if isinstance(dimension, str) and isinstance(item, str) and union_dimension(dimension):
                                alternatives.setdefault(dimension, set()).add(item)
                    nodes += sum(len(items) > 1 for items in alternatives.values())
                for group in groups:
                    terms = group.conditions if isinstance(group, Group) else group.get('conditions', ()) if isinstance(group, dict) else ()
                    if isinstance(terms, (list, tuple)):
                        nodes += len(terms)
                if nodes > MAX_QUERY_NODES:
                    raise ValueError(f'Query exceeds {MAX_QUERY_NODES} nodes (groups + conditions)')
        return value

    @model_validator(mode='after')
    def normalize(self):
        if self.mode == 'basic' and (self.groups or any(term.logic != 'and' for term in self.tail)):
            raise ValueError('Ordinary filters cannot contain explicit operators or groups')
        object.__setattr__(self, 'groups', tuple(group for group in self.groups if group.conditions))
        object.__setattr__(self, 'tail', unique(self.tail))
        if self.nodes > MAX_QUERY_NODES:
            raise ValueError(f'Query exceeds {MAX_QUERY_NODES} nodes (groups + conditions)')
        return self

    @property
    def conditions(self):
        return (*self.tail, *(term for group in self.groups for term in group.conditions))

    @property
    def nodes(self):
        implicit = sum(len(bucket) > 1 for bucket in basic_buckets(self.tail)) if self.mode == 'basic' else 0
        return implicit + len(self.tail) + sum(1 + len(group.conditions) for group in self.groups)

    def parameters(self):
        if self.mode == 'basic':
            return [('+'.join(token(term) for term in terms), '') for terms in basic_buckets(self.tail)]
        parameters = []
        for group in self.groups:
            parameters.extend((token(term), term.logic.upper()) for term in group.conditions)
            parameters.append(('Group', group.logic.upper()))
        parameters.extend((token(term), term.logic.upper()) for term in self.tail)
        return parameters or [('advanced', '1')]

    @classmethod
    def extract(cls, parameters):
        groups, tail, remaining = [], [], []
        mode, nodes = None, 0
        for key, value in parameters:
            if key in {'q', 'page', 'per_page', 'next_logic', 'monitor', 'section', 'include', 'search', 'detail', 'filters'}:
                remaining.append((key, value))
                continue
            if key == 'advanced':
                if value != '1' or mode == 'basic':
                    raise ValueError('Advanced Search cannot mix with ordinary filters')
                mode = 'advanced'
                continue
            if key == 'Group':
                if not tail or mode != 'advanced':
                    raise ValueError('Group requires preceding advanced conditions')
                groups.append({'logic': value.lower(), 'conditions': tail})
                tail = []
                nodes += 1
            elif value in ('AND', 'OR', 'NOT') or value == '':
                current = 'advanced' if value else 'basic'
                if mode and mode != current:
                    raise ValueError('Ordinary and advanced filters cannot be mixed')
                mode = current
                # HTML forms encode a literal plus as %2B; raw URL plus decodes to space.
                keys = key.replace(' ', '+').split('+')
                terms = [parse_token(part) for part in keys]
                if current == 'advanced' and len(terms) != 1:
                    raise ValueError('Advanced conditions require individual operators')
                if len(terms) > 1 and (not union_dimension(terms[0].dimension)
                        or len({term.dimension for term in terms}) != 1):
                    raise ValueError('Only one build target or BuildSystem can use +')
                tail.extend(term.model_copy(update={'logic': value.lower() if value else 'and'}) for term in terms)
                nodes += len(terms)
            else:
                remaining.append((key, value))
            if nodes > MAX_QUERY_NODES:
                raise ValueError(f'Query exceeds {MAX_QUERY_NODES} nodes (groups + conditions)')
        return cls.model_validate({'mode': mode or 'basic', 'groups': groups, 'tail': tail}), remaining


def union_dimension(dimension):
    return dimension == 'buildsystem' or dimension.startswith('build:')


def basic_buckets(conditions):
    buckets = {}
    for term in conditions:
        key = term.dimension if union_dimension(term.dimension) else term.identity
        buckets.setdefault(key, []).append(term)
    return tuple(buckets.values())


def token(condition):
    dimension, value = condition.identity
    if dimension == 'maintenance':
        return value
    if dimension == 'version_signal' and value == 'requires':
        return 'DepChanges'
    if dimension == 'buildsystem':
        return 'buildsystem_' + ('custom' if value == '_not_detected' else value)
    if dimension.startswith('build:') and value in STATES:
        return dimension.removeprefix('build:') + '_' + value
    return dimension + '/' + value


def parse_token(value):
    if not value or len(value) > 201 or any(c.isspace() or c in '&=+#?' for c in value):
        raise ValueError('Invalid filter token')
    if value.startswith('buildsystem_'):
        name = value.removeprefix('buildsystem_')
        return Condition(dimension='buildsystem', value='_not_detected' if name == 'custom' else name)
    if '/' in value:
        dimension, _, item = value.partition('/')
        return Condition(dimension=dimension, value=item)
    if value == 'DepChanges':
        return Condition(dimension='version_signal', value='requires')
    target, _, status = value.rpartition('_')
    if target and status in (*STATES, 'succeed'):
        return Condition(dimension='build:' + target, value='succeeded' if status == 'succeed' else status)
    return Condition(dimension='maintenance', value=value)


def encode_parameters(parameters):
    # Bare flags and literal + keep ordinary queries readable. Other values remain escaped.
    return '&'.join(quote(str(key), safe=':+/') + ('=' + quote(str(value), safe=':') if value != '' else '')
                    for key, value in parameters)



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
    """One expression drives matches and hypothetical next-condition totals."""
    def __init__(self, index, scope, query, next_logic: Logic = 'and'):
        self.index, self.scope, self.query, self.next_logic = index, frozenset(scope), query, next_logic
        for condition in query.conditions:
            if condition.dimension not in index:
                raise ValueError('Unknown filter dimension: ' + condition.dimension)
        operands = [(group.logic, self.conditions(group.conditions)) for group in query.groups]
        operands.extend(self.operands(query.tail))
        self.current = Group(conditions=query.tail)
        self.matches = self.basic_matches(query.tail) if query.mode == 'basic' else combine(operands, self.scope)

    def predicate(self, condition):
        return self.scope.intersection(self.index.get(condition.dimension, {}).get(condition.value, ()))

    def operands(self, conditions):
        return ((term.logic, self.predicate(term)) for term in conditions)

    def conditions(self, conditions):
        return combine(self.operands(conditions), self.scope)

    def basic_matches(self, conditions):
        buckets = basic_buckets(conditions)
        operands = [frozenset().union(*(self.predicate(term) for term in bucket)) for bucket in buckets]
        prefixes = [self.scope]
        for operand in operands:
            prefixes.append(prefixes[-1] & operand)
        suffix = self.scope
        self.alternative_scopes = {}
        for i in range(len(buckets) - 1, -1, -1):
            dimension = buckets[i][0].dimension
            if union_dimension(dimension):
                self.alternative_scopes[dimension] = prefixes[i] & suffix
            suffix = suffix & operands[i]
        return prefixes[-1]

    def count(self, condition):
        members = self.index.get(condition.dimension, {}).get(condition.value, frozenset())
        if self.query.mode == 'advanced':
            if self.next_logic == 'or':
                return len((self.scope & members) - self.matches)
            return len(self.matches & members)  # matches is already restricted to scope.
        if not self.current.contains(condition) and condition.dimension in self.alternative_scopes:
            return len((self.alternative_scopes[condition.dimension] & members) - self.matches)
        return len(self.matches & members)
