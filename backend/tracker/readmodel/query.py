"""Two-level package predicates, with explicit AND/OR links and AND precedence."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Groups and conditions share one budget; the root is fixed, not user-recursive.
# See docs/design.md for the parse + selection + candidate-count benchmark.
MAX_QUERY_NODES = 128
Logic = Literal['and', 'or']


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
            # The first link is the row's connector, not an implicit ALL operand.
            terms[0] = terms[0].model_copy(update={'logic': self.logic})
        object.__setattr__(self, 'conditions', tuple(terms))
        return self

    def contains(self, condition):
        return any(c.identity == condition.identity for c in self.conditions)

    def append(self, condition, logic):
        if self.contains(condition):
            return self
        return Group(logic=self.logic if self.conditions else logic,
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

    def encode(self):
        return self.model_dump_json()


def segments(operands):
    """Split an OR of AND runs into completed alternatives and its final run."""
    alternatives = set()
    tail = None
    for logic, operand in operands:
        if tail is None:
            tail = set(operand)
        elif logic == 'and':
            tail.intersection_update(operand)
        else:
            alternatives.update(tail)
            tail = set(operand)
    return frozenset(alternatives), None if tail is None else frozenset(tail)


def combine(operands, universe):
    alternatives, tail = segments(operands)
    return universe if tail is None else alternatives | tail


class Evaluation:
    """Results depend only on predicates; counts also use the next edit's context.

    Candidate counts add one condition idempotently to the active row.
    OR counts are capped by that condition's own scoped population; matches
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
        self.alternatives, self.tail = segments((c.logic, self.predicate(c)) for c in self.current.conditions)
        # With a single variable X, an AND/OR expression is F(empty) | (X & F(all)).
        # Evaluate fixed rows twice, not once per candidate in the whole palette.
        self.fixed_matches = self.substitute(frozenset())
        self.possible_matches = self.substitute(self.scope)

    def predicate(self, condition):
        return self.scope.intersection(self.index.get(condition.dimension, {}).get(condition.value, ()))

    def group(self, group):
        return combine(((c.logic, self.predicate(c)) for c in group.conditions), self.scope)

    def count(self, condition):
        members = self.predicate(condition)
        if self.current.contains(condition):
            count = len(self.matches)
        else:
            candidate = members
            if self.tail is not None:
                candidate = self.alternatives | (self.tail & members if self.next_logic == 'and'
                                                 else self.tail | members)
            count = len(self.fixed_matches | (candidate & self.possible_matches))
        return min(count, len(members)) if self.next_logic == 'or' else count

    def substitute(self, candidate):
        groups = list(zip((g.logic for g in self.query.groups), self.groups))
        replacement = (self.current.logic if self.current.conditions else self.next_logic, candidate)
        if groups:
            groups[self.active] = replacement
        else:
            groups.append(replacement)
        return combine(((logic, result) for logic, result in groups if result is not None), self.scope)
