"""Immutable edits to a pending condition sequence and sealed groups."""
from dataclasses import dataclass, replace

from tracker.readmodel.query import Condition, FilterQuery, Group, Logic


@dataclass(frozen=True)
class QueryEditor:
    query: FilterQuery
    next_logic: Logic = 'and'

    @property
    def current(self):
        return Group(conditions=self.query.tail)

    def mode(self, logic: Logic):
        return replace(self, next_logic=logic)

    def toggle(self, condition: Condition):
        if self.current.contains(condition):
            tail = tuple(term for term in self.query.tail if term.identity != condition.identity)
        else:
            tail = (*self.query.tail, condition.model_copy(update={'logic': self.next_logic}))
        return replace(self, query=FilterQuery(groups=self.query.groups, tail=tail))

    def group(self):
        if not self.query.tail:
            raise ValueError('Group requires preceding ungrouped conditions')
        return replace(self, query=FilterQuery(groups=(*self.query.groups,
            Group(logic=self.next_logic, conditions=self.query.tail))))

    def remove(self, group, condition=None):
        if not 0 <= group < len(self.query.groups):
            raise ValueError('Group does not exist')
        groups = list(self.query.groups)
        if condition is None:
            groups.pop(group)
        else:
            original = groups[group]
            groups[group] = Group(logic=original.logic,
                conditions=tuple(term for term in original.conditions if term.identity != condition.identity))
        return replace(self, query=FilterQuery(groups=tuple(groups), tail=self.query.tail))
