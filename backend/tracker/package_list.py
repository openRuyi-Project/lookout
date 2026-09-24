"""One set intersection defines rows, counts and linked filter choices.

A facet's choices apply every selection except its own. Counts describe packages,
not build flavors or findings. Pagination happens only after this calculation.
"""
from collections import defaultdict

from . import build_status


VIEWS = ('all', 'updates', 'problems', 'attention', 'untracked')


class PackageList:
    def __init__(self, rows, targets, query='', *, monitor=''):
        query = query.strip().casefold()
        self.rows = [row for row in rows if query in row['name'].casefold()]
        self.all = set(range(len(self.rows)))
        self.monitor = monitor
        self.index = defaultdict(lambda: defaultdict(set))
        for key in ('view', 'buildsystem', 'maintenance', *(f'build:{t["id"]}' for t in targets)):
            self.index[key]
        for number, row in enumerate(self.rows):
            self.index['view']['all'].add(number)
            for module in row['monitors'].values():
                for dimension, values in module['dimensions'].items():
                    for value in values:
                        self.index[dimension][value].add(number)

    def matching(self, selections, *, without=None):
        result = self.all.copy()
        for dimension, value in selections.items():
            if value and dimension != without:
                result.intersection_update(self.index[dimension].get(value, set()))
        return result

    def counts(self, dimension, selections, required=()):
        context = self.matching(selections, without=dimension)
        options = self.index[dimension]
        values = options.keys() | set(required)
        if selections.get(dimension):
            values.add(selections[dimension])
        counts = {value: len(context & options.get(value, set())) for value in sorted(values)}
        return {value: count for value, count in counts.items()
                if count or value in required or value == selections.get(dimension)}

    def select(self, *, view, buildsystem, maintenance, builds, page, per_page, check=''):
        selections = {'view': view, 'buildsystem': buildsystem, 'maintenance': maintenance,
                      **{f'build:{target}': status for target, status in builds.items()}}
        if self.monitor:
            selections['check:' + self.monitor] = check
        selected = sorted(self.matching(selections))
        total = len(selected)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        statuses = {}
        for dimension in self.index:
            if dimension.startswith('build:'):
                counts = self.counts(dimension, selections, required=('issues',))
                statuses[dimension.removeprefix('build:')] = [
                    {'value': value, 'label': build_status.label(value), 'count': count}
                    for value, count in counts.items()
                ]
        return {
            'items': [self.rows[number] for number in selected[(page - 1) * per_page:page * per_page]],
            'total': total, 'page': page, 'per_page': per_page, 'pages': pages,
            'counts': self.counts('view', selections, required=VIEWS),
            'buildsystems': self.counts('buildsystem', selections),
            'maintenance_labels': self.counts('maintenance', selections),
            'build_statuses': statuses,
            'check_statuses': self.counts('check:' + self.monitor, selections) if self.monitor else {},
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
