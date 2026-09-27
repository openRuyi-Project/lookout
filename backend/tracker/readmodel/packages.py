"""One set intersection defines rows, counts and linked filter choices.

A facet's choices apply every selection except its own. Counts describe packages,
not build flavors or findings. Pagination happens only after this calculation.
"""
from collections import defaultdict
from types import MappingProxyType

from tracker.monitors import model as monitor_model
from tracker.monitors.build import status as build_status


VIEWS = ('all', 'updates', 'problems', 'attention', 'untracked')


def _search_values(value):
    """Index observation values, never field names or internal facet dimensions."""
    if isinstance(value, dict):
        for child in value.values():
            yield from _search_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _search_values(child)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        yield str(value).casefold()


class _Selection:
    """Request-local evaluation of fixed filters over an immutable index.

    Facets without a selection share the same context. Memoization lives only
    for this selection, never across requests or snapshot generations.
    """
    def __init__(self, index, scope, filters):
        self.index = index
        self.scope = scope
        self.filters = {key: value for key, value in filters.items() if value}
        self._contexts = {}

    def matching(self, *, without=None):
        key = without if without in self.filters else None
        if key not in self._contexts:
            members = set(self.scope)
            for dimension, value in self.filters.items():
                if dimension != key:
                    members.intersection_update(self.index.get(dimension, {}).get(value, ()))
            self._contexts[key] = frozenset(members)
        return self._contexts[key]

    def counts(self, dimension, required=()):
        context = self.matching(without=dimension)
        options = self.index.get(dimension, {})
        selected = self.filters.get(dimension)
        values = options.keys() | set(required) | ({selected} if selected else set())
        counts = {value: len(context.intersection(options.get(value, ()))) for value in sorted(values)}
        return {value: count for value, count in counts.items()
                if count or value in required or value == selected}


class PackageList:
    def __init__(self, rows, targets, query='', *, monitor=''):
        self.rows = tuple(rows)
        self.by_name = MappingProxyType({row['name']: row for row in self.rows})
        self.names = tuple(row['name'].casefold() for row in self.rows)
        # Built once with the projection; request handlers only search strings.
        self.observations = tuple({mid: '\n'.join(_search_values({
            'check': result.get('check', {}), 'data': result.get('data', {})}))
            for mid, result in row['monitors'].items()} for row in self.rows)
        self.all = frozenset(range(len(self.rows)))
        self.query = query
        self.monitor = monitor
        index = defaultdict(lambda: defaultdict(set))
        for key in ('view', 'buildsystem', 'maintenance', 'requires', 'version_signal', *(f'build:{t["id"]}' for t in targets)):
            index[key]
        for number, row in enumerate(self.rows):
            index['view']['all'].add(number)
            for module in row['monitors'].values():
                for dimension, values in module['dimensions'].items():
                    for value in values:
                        index[dimension][value].add(number)
        for dimension, options in index.items():
            if dimension.startswith('check:'):
                for group, statuses in monitor_model.CHECK_GROUPS.items():
                    options[group] = set().union(*(options.get(status, ()) for status in statuses))
        self.index = MappingProxyType({dimension: MappingProxyType({
            value: frozenset(members) for value, members in options.items()
        }) for dimension, options in index.items()})

    def monitor_coverage(self):
        """Package counts from the same indexed facts used by linked filters."""
        return {dimension.removeprefix('check:'): {
            status: len(members) for status, members in options.items()
            if status not in monitor_model.CHECK_GROUPS and members
        } for dimension, options in self.index.items() if dimension.startswith('check:')}

    def select(self, *, view, buildsystem, maintenance, builds, page, per_page, check='', findings_only=False,
               query=None, monitor=None, requires='', signal='', freshness='', search='name'):
        query = (self.query if query is None else query).strip().casefold()
        monitor = self.monitor if monitor is None else monitor
        scope = {number for number, name in enumerate(self.names) if query in name} if query else self.all
        if query and search == 'observations':
            scope.update(number for number, observations in enumerate(self.observations)
                         if any(query in text for mid, text in observations.items()
                                if not monitor or mid == monitor))
        selections = {'view': view, 'buildsystem': buildsystem, 'maintenance': maintenance, 'requires': requires, 'version_signal': signal,
                      **{f'build:{target}': status for target, status in builds.items()}}
        if monitor:
            selections['check:' + monitor] = check
            selections['retained:' + monitor] = 'yes' if freshness == 'retained' else ''
        coverage_selection = _Selection(self.index, scope, selections)
        coverage = coverage_selection.matching(without='check:' + monitor)
        results = coverage & self.index.get('findings:' + monitor, {}).get('yes', frozenset())
        check_statuses = coverage_selection.counts('check:' + monitor) if monitor else {}
        check_statuses = {status: count for status, count in check_statuses.items()
                          if status not in monitor_model.CHECK_GROUPS}
        selection = (_Selection(self.index, scope, {**selections, 'findings:' + monitor: 'yes'})
                     if findings_only else coverage_selection)
        retained = selection.matching(without='retained:' + monitor).intersection(
            self.index.get('retained:' + monitor, {}).get('yes', ()))
        selected = sorted(selection.matching())
        total = len(selected)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        statuses = {}
        for dimension in self.index:
            if dimension.startswith('build:'):
                counts = selection.counts(dimension)
                statuses[dimension.removeprefix('build:')] = [
                    {'value': value, 'label': build_status.label(value), 'count': count}
                    for value, count in counts.items()
                ]
        return {
            'items': [self.rows[number] for number in selected[(page - 1) * per_page:page * per_page]],
            'total': total, 'page': page, 'per_page': per_page, 'pages': pages,
            'counts': selection.counts('view', required=VIEWS),
            'buildsystems': selection.counts('buildsystem'),
            'maintenance_labels': selection.counts('maintenance'),
            'version_signals': selection.counts('version_signal'),
            'requires_counts': {
                'all': len(selection.matching(without='requires')),
                **selection.counts('requires', required=('unmet', 'changes')),
            },
            'build_statuses': statuses,
            'check_statuses': check_statuses,
            'result_count': len(results), 'coverage_count': len(coverage),
            'retained_count': len(retained),
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
