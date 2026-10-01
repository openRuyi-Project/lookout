"""Pure edits; active row and next operator are state, not query predicates."""
from dataclasses import dataclass, replace

from tracker.readmodel.query import Condition, FilterQuery, Group, Logic


@dataclass(frozen=True)
class QueryEditor:
    query: FilterQuery
    active: int = 0
    next_logic: Logic = 'and'

    def __post_init__(self):
        if self.active < 0 or self.active >= max(1, len(self.query.groups)):
            raise ValueError('Active group does not exist')

    @property
    def groups(self):
        return self.query.groups or (Group(),)

    @property
    def current(self):
        return self.groups[self.active]

    def _replace(self, groups, *, active=None):
        groups = tuple(groups)
        if not any(group.conditions for group in groups):
            groups = ()
            active = 0
        return replace(self, query=FilterQuery(groups=groups),
                       active=self.active if active is None else active)

    def select(self, group):
        return replace(self, active=group)

    def mode(self, logic: Logic):
        return replace(self, next_logic=logic)

    def toggle(self, condition: Condition):
        current = self.current
        if current.contains(condition):
            updated = Group(logic=current.logic,
                conditions=tuple(c for c in current.conditions if c.identity != condition.identity))
        else:
            updated = current.append(condition, self.next_logic)
        groups = list(self.groups)
        groups[self.active] = updated
        return self._replace(groups)

    def add(self):
        after = self.active + 1
        groups = self.groups[:after] + (Group(logic=self.next_logic),) + self.groups[after:]
        return self._replace(groups, active=after)

    def clear(self, group):
        if group < 0 or group >= len(self.groups):
            raise ValueError('Group does not exist')
        groups = list(self.groups)
        groups[group] = Group(logic=groups[group].logic)
        return self._replace(groups, active=group)
