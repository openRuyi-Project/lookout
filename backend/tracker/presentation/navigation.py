"""Server-owned filter edits and links; the browser only renders/navigates."""
from urllib.parse import urlencode

from tracker.monitors.model import CHECK_GROUPS
from tracker.monitors.issues import Issue, VERSION_ISSUES
from tracker.monitors.build import status as build_status
from tracker.presentation.model import Choice, Controls, FilterEditor, FilterGroup, FilterCondition, Navigation, Parameter
from tracker.presentation.query_editor import QueryEditor
from tracker.presentation.registry import presenter
from tracker.presentation.values import CHECK_LABELS
from tracker.presentation.labels import appearance, caption, priority, facets
from tracker.presentation.version import signal_title
from tracker.readmodel.query import Condition, FilterQuery, MAX_QUERY_NODES


class Links:
    def __init__(self, query=None):
        self.query = {key: value for key, value in (query or {}).items() if value}
        raw = self.query.get('filters', {})
        filters = FilterQuery.decode(raw)
        self.query['filters'] = filters.model_dump()
        self.editor = QueryEditor(filters, self.query.get('active_group', 0), self.query.get('next_logic', 'and'))

    def to(self, **changes):
        query = {**self.query, 'page': None, **changes}
        if query.get('page') == 1:
            del query['page']
        filters = FilterQuery.decode(query.pop('filters', {}))
        parameters = [(key, value) for key, value in query.items() if value]
        return '/?' + urlencode(parameters + filters.parameters(), safe=':')

    def edited(self, editor, **changes):
        return self.to(filters=editor.query.model_dump(), active_group=editor.active,
                       next_logic=editor.next_logic if editor.next_logic != 'and' else '', **changes)

    def selected(self, dimension, value):
        return self.editor.current.contains(Condition(dimension=dimension, value=value))

    def condition(self, dimension, value, **changes):
        condition = Condition(dimension=dimension, value=value)
        if not self.editor.current.contains(condition) and self.editor.query.nodes >= MAX_QUERY_NODES:
            return None
        return self.edited(self.editor.toggle(condition), **changes)


def listing_query(query, catalog):
    # View selection changes columns, never silently drops an explicit predicate.
    focus = next((m for m in catalog if m['id'] == query.get('monitor')), None)
    if focus and not presenter(focus).has_results:
        return {**query, 'section': 'coverage'}
    return query


def condition_label(condition, payload):
    dimension, value = condition.dimension, condition.value
    family, _, owner = dimension.partition(':')
    if family == 'build':
        target = next((t['label'] for t in payload['targets'] if t['id'] == owner), owner)
        return target + ': ' + build_status.label(value)
    if family in ('check', 'retained', 'findings'):
        title = next((caption(m['title']) for m in payload['monitors'] if m['id'] == owner), owner)
        return title + ': ' + (CHECK_LABELS.get(value, value) if family == 'check' else
                              'Stale' if family == 'retained' else 'Results')
    if dimension == 'buildsystem':
        return caption('Build system') + ': ' + (caption('Custom') if value == '_not_detected' else value)
    if dimension == 'view':
        return caption(VERSION_ISSUES.get(value, value))
    if dimension == 'requires':
        return {'unmet': 'DepMismatch', 'changes': 'DepChanges'}.get(value, value)
    if dimension == 'version_signal':
        return next((caption(signal_title(m)) for m in payload['monitors'] if m['id'] == value), value)
    return caption(value)


def filter_editor(payload, links):
    editor = links.editor
    groups = []
    can_add = editor.query.nodes + 1 <= MAX_QUERY_NODES
    for number, group in enumerate(editor.query.groups):
        selected = editor.select(number)
        active = number == editor.active
        groups.append(FilterGroup(id=number, active=active, select=links.edited(selected),
            logic=group.logic,
            conditions=[FilterCondition(label=condition_label(c, payload), selected=active, logic=c.logic,
                href=links.edited(selected.toggle(c) if active else selected)) for c in group.conditions],
            clear=links.edited(editor.clear(number)),
            add=links.edited(selected.add()) if can_add else None))
    return FilterEditor(query=editor.query, active_group=editor.active, groups=groups,
        operators=[Choice(label=logic.upper(), selected=editor.next_logic == logic,
            href=links.edited(editor.mode(logic))) for logic in ('and', 'or')],
        clear=links.edited(QueryEditor(FilterQuery(), next_logic=editor.next_logic)))


def global_navigation(payload, links):
    styles = payload.get('presentation', {}).get('buildsystems', {})
    return [Navigation(label=caption('Build system'), choices=[
        Choice(label=caption('Custom') if value == '_not_detected' else value, count=count,
            selected=links.selected('buildsystem', value),
            appearance='buildsystem:' + value if value != '_not_detected' else None,
            icon=styles.get(value, {}).get('icon') if value != '_not_detected' else None,
            href=links.condition('buildsystem', value))
        for value, count in sorted(payload['buildsystems'].items(), key=lambda item: item[0] == '_not_detected')])]


def maintenance_navigation(payload, links):
    choices = [Choice(label=caption(value), count=count,
        selected=links.selected('maintenance', value), appearance=appearance(value),
        href=links.condition('maintenance', value))
        for value, count in (dict.fromkeys(facets(), 0) | payload['maintenance_labels']).items()]
    choices.append(Choice(label='DepChanges', count=payload.get('version_signals', {}).get('requires', 0),
        selected=links.selected('version_signal', 'requires'), appearance=appearance('DepChanges'),
        href=links.condition('version_signal', 'requires')))
    return Navigation(label='Alerts', show_label=False, choices=sorted(choices, key=lambda c: priority(c.label)))


def build_navigation(payload, links):
    return {target['id']: Navigation(label=target['label'], icon='⚙', choices=[
        Choice(label=item['label'], count=item['count'], appearance=appearance(item['label']),
            selected=links.selected('build:' + target['id'], item['value']),
            href=links.condition('build:' + target['id'], item['value']))
        for item in payload['build_statuses'][target['id']]]) for target in payload['targets']}


def listing_controls(payload, query, focus, links):
    rows = []
    navigation = []
    if not focus:
        rows.append(maintenance_navigation(payload, links))
    if not focus or (focus['kind'] == 'build' and payload['section'] == 'results'):
        rows.extend(row for row in build_navigation(payload, links).values() if row.choices)
    if focus:
        choices = []
        if focus['kind'] == 'version':
            choices.extend(Choice(label=caption(label), count=payload['counts'][value], appearance=appearance(label),
                selected=links.selected('view', value), href=links.condition('view', value))
                for value, label in VERSION_ISSUES.items())
            rows.append(Navigation(label='Related', choices=[Choice(label=caption(signal_title(m)),
                count=payload['version_signals'].get(m['id'], 0), selected=links.selected('version_signal', m['id']),
                href=links.condition('version_signal', m['id'])) for m in payload['monitors']
                if m['id'] in payload['version_signals']]))
        elif focus['kind'] == 'requires':
            choices.extend(Choice(label=caption(label), count=payload['requires_counts'][value],
                selected=links.selected('requires', value), href=links.condition('requires', value))
                for value, label in [('unmet', Issue.DEP_MISMATCH), ('changes', 'DepChanges')])
        if presenter(focus).has_results:
            choices.append(Choice(label='Results', count=payload['result_count'] if focus['kind'] in ('evidence', 'requires') else payload['coverage_count'],
                selected=payload['section'] == 'results', href=links.to(section='results')))
        choices.append(Choice(label='Coverage', count=payload['coverage_count'],
            selected=payload['section'] == 'coverage', href=links.to(section='coverage')))
        if payload.get('retained_count') or links.selected('retained:' + focus['id'], 'yes'):
            choices.append(Choice(label=CHECK_LABELS['expired'], count=payload['retained_count'],
                selected=links.selected('retained:' + focus['id'], 'yes'),
                href=links.condition('retained:' + focus['id'], 'yes')))
        choices.extend(Choice(label=CHECK_LABELS.get(group, group.title()), count=payload['check_groups'][group],
            selected=links.selected('check:' + focus['id'], group),
            href=links.condition('check:' + focus['id'], group, section='coverage')) for group in CHECK_GROUPS)
        navigation.append(Navigation(label=caption(focus['title']), choices=choices))
    hidden = [Parameter(name=key, value=str(query[key]))
              for key in ('monitor', 'section', 'active_group', 'next_logic', 'per_page') if query.get(key)]
    hidden.extend(Parameter(name=key, value=value) for key, value in links.editor.query.parameters())
    return Controls(query=query.get('q', ''), hidden=hidden, choice_rows=rows, navigation=navigation,
        active=[Choice(label='Search: ' + query['q'], href=links.to(q=''))] if query.get('q') else [],
        editor=filter_editor(payload, links))
