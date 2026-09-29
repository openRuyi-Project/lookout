"""Navigation for reading documents; no collection or persistence."""
from urllib.parse import urlencode

from tracker.monitors.model import CHECK_GROUPS
from tracker.monitors.issues import Issue, VERSION_ISSUES
from tracker.monitors.build import status as build_status
from tracker.presentation.model import Choice, Controls, Navigation, Parameter
from tracker.presentation.registry import presenter
from tracker.presentation.values import CHECK_LABELS
from tracker.presentation.labels import appearance, caption, priority, facets
from tracker.presentation.version import signal_title


class Links:
    """Page links retain only filters supported by their destination."""
    def __init__(self, query=None, catalog=None):
        self.query = {key: value for key, value in (query or {}).items() if value}
        if isinstance(self.query.get('maintenance'), str):
            self.query['maintenance'] = [self.query['maintenance']]
        self.catalog = catalog

    def toggle(self, name, value, *, multiple=False):
        selected = self.query.get(name)
        if multiple:
            values = selected or []
            value = [item for item in values if item != value] if value in values else [*values, value]
        elif selected == value:
            value = ''
        return self.to(**{name: value})

    def choose(self, name, value, count, *, multiple=False):
        if count == 0:
            return self.only_filter(**{name: [value] if multiple else value})
        return self.toggle(name, value, multiple=multiple)

    def only_filter(self, **selection):
        context = {key: self.query[key] for key in ('q', 'per_page') if key in self.query}
        return Links(context, self.catalog).to(**selection)

    def to(self, **changes):
        query = {**self.query, 'page': 1, **changes}
        if self.catalog is not None:
            query = listing_query(query, self.catalog)
        query = {key: value for key, value in query.items() if value}
        return '/?' + urlencode(query, doseq=True)


def listing_filters(focus, section):
    """A filter is available only where its information is visible."""
    common = frozenset({'q', 'buildsystem', 'page', 'per_page'})
    if not focus:
        return common | {'view', 'maintenance', 'build', 'signal'}
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
    if result.get('view') not in ('all', *VERSION_ISSUES):
        result.pop('view', None)
    if focus:
        result.update(monitor=focus['id'], section=section)
        if check:
            result['check'] = check
    return result


def global_navigation(payload, query, links):
    counts = payload['buildsystems']
    styles = payload.get('presentation', {}).get('buildsystems', {})
    return [Navigation(label=caption('Build system'), choices=[
        Choice(label=caption('Not detected') if value == '_not_detected' else value,
                 count=count, selected=query.get('buildsystem') == value,
                 appearance='buildsystem:' + value if value != '_not_detected' else None,
                 icon=styles.get(value, {}).get('icon') if value != '_not_detected' else None,
                 href=links.choose('buildsystem', value, count))
        for value, count in sorted(counts.items(), key=lambda item: item[0] == '_not_detected')
    ])]


def filter_link(links, name, value):
    if name == 'build':
        target, _, status = value.partition(':')
        selected = [item for item in links.query.get('build', []) if item.partition(':')[0] != target]
        add = status and value not in links.query.get('build', [])
        return links.to(build=selected + ([value] if add else []))
    return links.to(**{name: value})


def maintenance_navigation(payload, query, links):
    choices = [Choice(label=caption(value), count=count, selected=value in links.query.get('maintenance', []),
                      appearance=appearance(value),
                      href=links.choose('maintenance', value, count, multiple=True))
               for value, count in (dict.fromkeys(facets(), 0) | payload['maintenance_labels']).items()]
    changes = payload.get('version_signals', {}).get('requires', 0)
    position = next((i + 1 for i, choice in enumerate(choices)
                     if choice.label == Issue.DEP_MISMATCH), len(choices))
    choices.insert(position, Choice(label='DepChanges', count=changes,
        selected=query.get('signal') == 'requires', appearance=appearance('DepChanges'),
        href=links.choose('signal', 'requires', changes)))
    return Navigation(label='Alerts', show_label=False,
                      choices=sorted(choices, key=lambda c: priority(c.label)))


def build_navigation(payload, query, links, *, inline=False):
    selected = {value.partition(':')[0]: value.partition(':')[2] for value in query.get('build', [])}
    columns = {item['value']: item['label']
               for statuses in payload['build_statuses'].values() for item in statuses}
    codes = build_status.ordered(columns)
    menus = {}
    for target in payload['targets']:
        tid = target['id']
        statuses = payload['build_statuses'][tid]
        if inline:
            # Keep alternatives visible across targets, including a clickable
            # zero that restarts the query rather than pretending it is unknown.
            by_code = {item['value']: item for item in statuses}
            statuses = [by_code.get(code, {'value': code, 'label': columns[code], 'count': 0})
                        for code in codes]
        if not inline and not any(item['count'] for item in statuses) and not selected.get(tid):
            continue
        choices = [Choice(label=item['label'], count=item['count'], appearance=appearance(item['label']),
                          selected=selected.get(tid) == item['value'],
                          href=(links.only_filter(build=[tid + ':' + item['value']]) if item['count'] == 0
                                else filter_link(links, 'build', tid + ':' + item['value']))) for item in statuses]
        if selected.get(tid) and not inline:
            choices.insert(0, Choice(label='Clear filter', href=filter_link(links, 'build', tid + ':')))
        menus[tid] = Navigation(label=target['label'], icon='⚙', choices=choices)
    return menus


def listing_controls(payload, query, focus, links):
    allowed = listing_filters(focus, payload['section'])
    rows = []
    if focus and 'signal' in allowed:
        counts = payload.get('version_signals', {})
        if any(counts.values()) or query.get('signal'):
            titles = {monitor['id']: caption(signal_title(monitor)) for monitor in payload['monitors']}
            rows.append(Navigation(label='Related', choices=[
                Choice(label=titles.get(value, value), count=count, selected=query.get('signal') == value,
                       href=links.to(signal=value)) for value, count in counts.items()]))
    if not focus and (maintenance := maintenance_navigation(payload, query, links)):
        rows.append(maintenance)
    if 'build' in allowed:
        build_rows = list(build_navigation(payload, query, links, inline=True).values())
        rows.extend(row for row in build_rows if row.choices)

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
    if focus and focus['kind'] == 'version':
        navigation.append(Navigation(label='Version', choices=[Choice(label=caption(label),
            count=counts['counts'][value], appearance=appearance(label),
            href=links.to(view=value, section='results', check='', freshness='', signal=''),
            selected=result_mode and query.get('view') == value) for value, label in VERSION_ISSUES.items()]))
    if focus and focus['kind'] == 'requires':
        navigation.append(Navigation(label='Dependencies', choices=[Choice(label=caption(label),
            count=counts['requires_counts'][value or 'all'],
            href=links.to(requires=value, section='results', check='', freshness=''),
            selected=result_mode and query.get('requires', '') == value)
            for value, label in [('unmet', Issue.DEP_MISMATCH), ('changes', 'DepChanges')]]))
    if focus:
        if counts.get('retained_count') or query.get('freshness') == 'retained':
            navigation[-1].choices.append(Choice(label=CHECK_LABELS['expired'], count=counts.get('retained_count', 0),
                href=links.to(section='results', check='', freshness='retained', view='', signal='', requires='', build=[]),
                selected=query.get('freshness') == 'retained'))
        navigation[-1].choices.extend([
            Choice(label=CHECK_LABELS.get(group, group.title()), count=counts['check_groups'][group],
                href=links.to(section='coverage', check=group), selected=query.get('check') == group)
            for group in CHECK_GROUPS])
    hidden_keys = ('monitor', 'view', 'section', 'check', 'freshness', 'requires', 'signal', 'buildsystem', 'per_page')
    hidden = [Parameter(name=key, value=str(query[key])) for key in hidden_keys if query.get(key)]
    hidden.extend(Parameter(name=key, value=value)
                  for key in ('build', 'maintenance') for value in links.query.get(key, []))
    return Controls(query=query.get('q', ''), hidden=hidden, choice_rows=rows,
                    active=active_filters(payload, query, links), navigation=navigation)


def active_filters(payload, query, links):
    titles = {monitor['id']: caption(signal_title(monitor)) for monitor in payload['monitors']}
    labels = {
        'q': 'Search: ' + query.get('q', ''),
        'buildsystem': caption('Build system') + ': ' + (caption('Not detected') if query.get('buildsystem') == '_not_detected'
                                        else query.get('buildsystem', '')),
        'signal': titles.get(query.get('signal'), query.get('signal', '')),
        'view': caption(VERSION_ISSUES.get(query.get('view'), '')),
        'requires': {'unmet': Issue.DEP_MISMATCH, 'changes': 'DepChanges'}.get(query.get('requires'), ''),
        'freshness': CHECK_LABELS['expired'] if query.get('freshness') == 'retained' else '',
        'check': 'Check: ' + CHECK_LABELS.get(query.get('check'), query.get('check', '').title()),
    }
    active = [Choice(label=label, href=links.to(**{key: ''}))
              for key, label in labels.items() if query.get(key) and label]
    active.extend(Choice(label=caption(value), href=links.to(maintenance=[
        item for item in links.query.get('maintenance', []) if item != value]))
        for value in dict.fromkeys(links.query.get('maintenance', [])))
    for target, menu in build_navigation(payload, query, links).items():
        for choice in menu.choices:
            if choice.selected:
                active.append(Choice(label=menu.label + ': ' + choice.label,
                                     href=filter_link(links, 'build', target + ':')))
    return active
