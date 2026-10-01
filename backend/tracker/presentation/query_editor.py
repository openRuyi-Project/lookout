"""Immutable edits to a pending condition sequence and sealed groups."""
from dataclasses import dataclass, replace

from tracker.readmodel.query import Condition, FilterQuery, Group, Logic, basic_buckets


@dataclass(frozen=True)
class QueryEditor:
    query: FilterQuery
    next_logic: Logic = 'and'

    @property
    def current(self):
        return Group(conditions=self.query.tail)

    def mode(self, logic: Logic):
        return replace(self, next_logic=logic)

    def switch_mode(self):
        if self.query.mode == 'advanced':
            return QueryEditor(FilterQuery(mode='basic'))
        buckets = basic_buckets(self.query.tail)
        return QueryEditor(FilterQuery(
            groups=tuple(Group(conditions=tuple(term.model_copy(update={'logic': 'and' if i == 0 else 'or'})
                for i, term in enumerate(bucket))) for bucket in buckets if len(bucket) > 1),
            tail=tuple(bucket[0] for bucket in buckets if len(bucket) == 1)))

    def toggle(self, condition: Condition):
        if self.current.contains(condition):
            tail = tuple(term for term in self.query.tail if term.identity != condition.identity)
        else:
            tail = (*self.query.tail, condition.model_copy(update={'logic': self.next_logic if self.query.mode == 'advanced' else 'and'}))
        return replace(self, query=FilterQuery(mode=self.query.mode, groups=self.query.groups, tail=tail))

    def group(self):
        if self.query.mode != 'advanced':
            raise ValueError('Grouping requires Advanced Search')
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
