"""One set intersection defines rows, counts and linked filter choices.

A facet's choices apply every selection except its own. Counts describe packages,
not build flavors or findings. Pagination happens only after this calculation.
"""
from collections import defaultdict
from types import MappingProxyType

from . import build_status


VIEWS = ('all', 'updates', 'problems', 'attention', 'untracked')


class PackageList:
    def __init__(self, rows, targets, query='', *, monitor=''):
        self.rows = tuple(rows)
        self.by_name = MappingProxyType({row['name']: row for row in self.rows})
        self.names = tuple(row['name'].casefold() for row in self.rows)
        self.all = frozenset(range(len(self.rows)))
        self.query = query
        self.monitor = monitor
        index = defaultdict(lambda: defaultdict(set))
        for key in ('view', 'buildsystem', 'maintenance', *(f'build:{t["id"]}' for t in targets)):
            index[key]
        for number, row in enumerate(self.rows):
            index['view']['all'].add(number)
            for module in row['monitors'].values():
                for dimension, values in module['dimensions'].items():
                    for value in values:
                        index[dimension][value].add(number)
        self.index = MappingProxyType({dimension: MappingProxyType({
            value: frozenset(members) for value, members in options.items()
        }) for dimension, options in index.items()})

    def matching(self, selections, *, without=None, scope=None):
        result = set(self.all if scope is None else scope)
        for dimension, value in selections.items():
            if value and dimension != without:
                result.intersection_update(self.index.get(dimension, {}).get(value, ()))
        return result

    def counts(self, dimension, selections, required=(), *, scope=None):
        context = self.matching(selections, without=dimension, scope=scope)
        options = self.index.get(dimension, {})
        values = options.keys() | set(required)
        if selections.get(dimension):
            values.add(selections[dimension])
        counts = {value: len(context & options.get(value, set())) for value in sorted(values)}
        return {value: count for value, count in counts.items()
                if count or value in required or value == selections.get(dimension)}

    def select(self, *, view, buildsystem, maintenance, builds, page, per_page, check='', findings_only=False,
               query=None, monitor=None):
        query = (self.query if query is None else query).strip().casefold()
        monitor = self.monitor if monitor is None else monitor
        scope = {number for number, name in enumerate(self.names) if query in name} if query else self.all
        selections = {'view': view, 'buildsystem': buildsystem, 'maintenance': maintenance,
                      **{f'build:{target}': status for target, status in builds.items()}}
        if monitor:
            selections['check:' + monitor] = check
        coverage = self.matching(selections, without='check:' + monitor, scope=scope)
        results = coverage & self.index.get('findings:' + monitor, {}).get('yes', frozenset())
        check_statuses = self.counts('check:' + monitor, selections, scope=scope) if monitor else {}
        if findings_only:
            selections['findings:' + monitor] = 'yes'
        selected = sorted(self.matching(selections, scope=scope))
        total = len(selected)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        statuses = {}
        for dimension in self.index:
            if dimension.startswith('build:'):
                counts = self.counts(dimension, selections, required=('issues',), scope=scope)
                statuses[dimension.removeprefix('build:')] = [
                    {'value': value, 'label': build_status.label(value), 'count': count}
                    for value, count in counts.items()
                ]
        return {
            'items': [self.rows[number] for number in selected[(page - 1) * per_page:page * per_page]],
            'total': total, 'page': page, 'per_page': per_page, 'pages': pages,
            'counts': self.counts('view', selections, required=VIEWS, scope=scope),
            'buildsystems': self.counts('buildsystem', selections, scope=scope),
            'maintenance_labels': self.counts('maintenance', selections, scope=scope),
            'build_statuses': statuses,
            'check_statuses': check_statuses,
            'result_count': len(results), 'coverage_count': len(coverage),
        }


def build_selections(values, targets):
    """Repeated build=TARGET:STATE parameters allow data-defined target identities."""
    known = {target['id'] for target in targets}
    selected = {}
    for value in values:
        target, separator, status = value.partition(':')
        if not separator or target not in known or target in selected or len(status) > 100:
            raise ValueError('build filters require one TARGET:STATE per configured target')
        selected[target] = status
    return selected
