"""Navigation for reading documents; no collection or persistence."""
from urllib.parse import urlencode

from tracker.monitors.model import CHECK_GROUPS
from tracker.presentation.model import Choice, Controls, Facet, Navigation, Option, Parameter
from tracker.presentation.registry import presenter
from tracker.presentation.values import CHECK_LABELS


class Links:
    """Page links retain only filters supported by their destination."""
    def __init__(self, query=None, catalog=None):
        self.query = {key: value for key, value in (query or {}).items() if value}
        self.catalog = catalog

    def to(self, **changes):
        query = {**self.query, 'page': 1, **changes}
        if self.catalog is not None:
            query = listing_query(query, self.catalog)
        query = {key: value for key, value in query.items() if value}
        return '/?' + urlencode(query, doseq=True)

    def without(self, key, value):
        current = self.query.get(key)
        return self.to(**{key: [v for v in current if v != value] if isinstance(current, list) else ''})


def listing_filters(focus, section):
    """A filter is available only where its information is visible."""
    common = frozenset({'q', 'buildsystem', 'page', 'per_page'})
    if not focus:
        return common | {'view', 'maintenance', 'build'}
    return common | (presenter(focus).filters | {'freshness'} if section == 'results' else frozenset())


def listing_query(query, catalog):
    """UI scope, applied before selection and when constructing every page link.

    Raw data API queries remain freely composable. Result subviews and check
    groups are mutually exclusive modes, not hidden cross-monitor constraints.
    """
    focus = next((m for m in catalog if m['id'] == query.get('monitor')), None)
    check = query.get('check', '') if focus else ''
    section = 'coverage' if focus and (check or not presenter(focus).has_results) else 'results'
    allowed = listing_filters(focus, section)
    result = {key: value for key, value in query.items() if key in allowed}
    if result.get('view') not in ('all', 'updates'):
        result.pop('view', None)
    if focus:
        result.update(monitor=focus['id'], section=section)
        if check:
            result['check'] = check
    return result


def global_navigation(payload, query, links):
    counts = payload['buildsystems']
    return [Navigation(label='Build system', choices=[
        Choice(label='All', href=links.to(buildsystem=''), selected=not query.get('buildsystem'),
               count=sum(counts.values())),
        *[Choice(label='Not detected' if value == '_not_detected' else value,
                 count=count, selected=query.get('buildsystem') == value,
                 appearance='buildsystem:' + value if value != '_not_detected' else None,
                 href=links.to(buildsystem=value)) for value, count in counts.items()],
    ])]


def filter_link(links, name, value):
    if name == 'build':
        target, _, status = value.partition(':')
        selected = [item for item in links.query.get('build', []) if item.partition(':')[0] != target]
        return links.to(build=selected + ([value] if status else []))
    return links.to(**{name: value})


def choice_row(facet, links):
    return Navigation(label=facet.label, choices=[
        Choice(label=option.label, href=filter_link(links, facet.name, option.value),
               selected=option.selected, count=option.count) for option in facet.options])


def build_facets(payload, query, *, include_empty=False):
    selected = {value.partition(':')[0]: value.partition(':')[2] for value in query.get('build', [])}
    facets = []
    for target in payload['targets']:
        tid = target['id']
        statuses = payload['build_statuses'][tid]
        if not include_empty and not any(item['count'] for item in statuses) and not selected.get(tid):
            continue
        facets.append(Facet(id='build-' + tid, name='build', label=target['label'], options=[
            Option(value=tid + ':', label='All', selected=not selected.get(tid),
                   count=sum(item['count'] for item in statuses)),
            *[Option(value=tid + ':' + item['value'], label=item['label'], count=item['count'],
                     selected=selected.get(tid) == item['value']) for item in statuses]]))
    return facets


def listing_controls(payload, query, focus, links):
    allowed = listing_filters(focus, payload['section'])
    facets, rows = [], []
    if 'maintenance' in allowed and (any(payload['maintenance_labels'].values()) or query.get('maintenance')):
        facets.append(Facet(id='maintenance', name='maintenance', label='Maintenance', options=[
            Option(value='', label='All', selected=not query.get('maintenance')),
            *[Option(value=value, label=value, count=count, selected=query.get('maintenance') == value)
              for value, count in payload['maintenance_labels'].items()]]))
    if 'signal' in allowed:
        counts = payload.get('version_signals', {})
        if any(counts.values()) or query.get('signal'):
            titles = {monitor['id']: monitor['title'] for monitor in payload['monitors']}
            options = [Option(value='', label='All', selected=not query.get('signal')),
                *[Option(value=value, label=titles.get(value, value), count=count,
                         selected=query.get('signal') == value) for value, count in counts.items()]]
            rows.append(choice_row(Facet(id='signal', name='signal', label='Related', options=options), links))
    inline_build = bool(focus and focus['kind'] == 'build' and 'build' in allowed)
    if 'build' in allowed:
        choices = build_facets(payload, query, include_empty=inline_build)
        if inline_build:
            rows.extend(choice_row(facet, links) for facet in choices)
        else:
            facets.extend(choices)

    navigation = []
    counts = payload.get('navigation_counts', payload)
    result_mode = not query.get('check') and not query.get('freshness')
    if focus and focus['kind'] not in ('version', 'requires'):
        choices = []
        if presenter(focus).has_results:
            choices.append(Choice(label='Results',
                count=counts['result_count'] if focus['kind'] == 'evidence' else None,
                href=links.to(section='results', check='', freshness=''), selected=result_mode))
        navigation.append(Navigation(label='Views', choices=choices))
    if not focus or focus['kind'] == 'version':
        navigation.append(Navigation(label='Versions', choices=[Choice(label=label,
            count=counts['counts'][value], href=links.to(view=value, signal='', section='results', check='', freshness=''),
            selected=result_mode and query.get('view', 'all') == value)
            for value, label in [('all', 'All'), ('updates', 'Updates')]]))
    if focus and focus['kind'] == 'requires':
        navigation.append(Navigation(label='Requires', choices=[Choice(label=label,
            count=counts['requires_counts'][value or 'all'],
            href=links.to(requires=value, section='results', check='', freshness=''),
            selected=result_mode and query.get('requires', '') == value)
            for value, label in [('', 'All'), ('unmet', 'Unmet'), ('changes', 'Changes')]]))
    if focus:
        if counts.get('retained_count') or query.get('freshness') == 'retained':
            navigation[-1].choices.append(Choice(label=CHECK_LABELS['expired'], count=counts.get('retained_count', 0),
                href=links.to(section='results', check='', freshness='retained', view='', signal='', requires='', build=[]),
                selected=query.get('freshness') == 'retained'))
        navigation[-1].choices.extend([
            Choice(label=group.title(), count=sum(counts['check_statuses'].get(status, 0) for status in statuses),
                href=links.to(section='coverage', check=group), selected=query.get('check') == group)
            for group, statuses in CHECK_GROUPS.items()])
    active = []
    # Visible selectors already explain and clear themselves. Only a precise
    # check deep-link needs a separate removable indicator.
    if query.get('check') and query['check'] not in CHECK_GROUPS:
        active.append(Choice(label='Check: ' + CHECK_LABELS.get(query['check'], query['check']), href=links.to(check='')))
    hidden_keys = ('monitor', 'view', 'section', 'check', 'freshness', 'requires', 'signal', 'buildsystem', 'per_page')
    hidden = [Parameter(name=key, value=str(query[key])) for key in hidden_keys if query.get(key)]
    if inline_build:
        hidden.extend(Parameter(name='build', value=value) for value in query.get('build', []))
    return Controls(query=query.get('q', ''), hidden=hidden, facets=facets, choice_rows=rows,
                    active=active, navigation=navigation)
