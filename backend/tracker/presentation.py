"""Pure reading adapters: facts -> documents. Never called by collectors.

Only this boundary interprets monitor fields for readers. The website consumes
bounded document primitives, not monitor IDs, OBS states or advisory fact codes.
Filtering remains PackageList's job; this module does not recalculate counts.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from typing import Callable

from .presentation_model import (Cell, Choice, Column, Controls, DetailDocument,
    Entry, Facet, Field, ListingDocument, Navigation, Option, Parameter, Row,
    Section, Table, Text)


CHECK_LABELS = {
    'ok': 'Checked', 'partial': 'Partial evidence', 'error': 'Check failed',
    'pending': 'Not yet checked', 'not_configured': 'Not configured',
    'not_applicable': 'Not applicable', 'unsupported': 'Metadata unavailable',
    'input_unavailable': 'Input unavailable', 'expired': 'Out of date',
    'input_changed': 'Input changed', 'schema_changed': 'Format changed',
}


def text(value, **kwargs):
    return Text(text=str(value) if value is not None else '—', **kwargs)


def cell(*lines):
    return Cell(lines=[line for line in lines if line])


def field(label, *values):
    return Field(label=label, values=list(values))


def stamp(value):
    try:
        date = datetime.fromisoformat(value).astimezone(timezone.utc)
        return text(date.strftime('%Y-%m-%d %H:%M:%S UTC'), kind='time', datetime=value)
    except (ValueError, TypeError):
        return text('—', tone='muted')


class Links:
    """Opaque navigation links preserve all selected dimensions, including repeats."""
    def __init__(self, query=None):
        self.query = {key: value for key, value in (query or {}).items() if value}

    def to(self, **changes):
        query = {**self.query, 'page': 1, **changes}
        query = {key: value for key, value in query.items() if value}
        return '/?' + urlencode(query, doseq=True)

    def without(self, key, value):
        current = self.query.get(key)
        return self.to(**{key: [v for v in current if v != value] if isinstance(current, list) else ''})


def module(pkg, kind):
    return next((m for m in pkg['monitors'].values() if m['data']['kind'] == kind), None)


def buildsystem(value, links):
    return text(value, kind='tag', appearance='buildsystem:' + value,
                href=links.to(buildsystem=value))


def version_value(pkg):
    result = module(pkg, 'version')
    if not result:
        return []
    value = result['data']
    build = module(pkg, 'build')
    success = build['data']['source_success'] if build else None
    untracked = not value['track'] and value['relation'] != 'not_applicable'
    tone = 'muted' if untracked or success is None else 'negative' if success is False else 'normal'
    title = ('Upstream is not tracked' if untracked else
             'Current source has not succeeded on every active build target' if success is False else
             'Current source build success is unknown' if success is None else
             'Current source succeeded on every active build target')
    if value['relation'] == 'unknown' and not untracked:
        title += '; source and upstream cannot currently be compared'
    values = [text(value['current'], kind='code', tone=tone, title=title)]
    if value['relation'] == 'outdated':
        values += [text('→', title='Update available'), text(value['latest'], kind='code', tone='positive')]
    return values


def check_value(check):
    status = check['status']
    return text(CHECK_LABELS.get(status, status.replace('_', ' ')),
                tone='normal' if status == 'ok' else 'notice',
                title=check.get('error') or check.get('note'))


def build_cell(build, current, detail_url):
    last = build.get('last_success')
    compact = (build['kind'] == 'ok' and build.get('matches_source') is True
               and last and last.get('version') and last['version'] == current)
    title = f"OBS {build['raw_status']}; observed {stamp(build.get('updated_at')).text}"
    if last:
        title += f"; last succeeded {last.get('version') or 'version not recorded'}, {stamp(last['time']).text}"
    values = [text(build['text'], href=build.get('log_url'), title=title,
                   tone={'error': 'negative', 'muted': 'muted', 'pending': 'notice'}.get(build['kind'], 'normal'))]
    previous = ([text(last['version'], kind='code', href=detail_url + '#build',
                      title='Last successful version: ' + stamp(last['time']).text)]
                if not compact and last and last.get('version') else [])
    return cell(values, previous)


def evidence_labels(result, links):
    return [text(label['label'] + (f" {label['count']}" if label['count'] > 1 else ''),
                 kind='tag', href=links.to(maintenance=label['label']),
                 title='Previous observation' if label['stale'] else None)
            for label in result['data']['labels']]


def fact_field(fact):
    status, value = fact['status'], fact['value']
    rendered = (status.replace('_', ' ') if status != 'observed' else
                'Yes' if value is True else 'No' if value is False else
                ', '.join(value) or '—' if isinstance(value, list) else value)
    if (status == 'observed' and fact.get('code') == 'epss_probability'
            and isinstance(value, (float, int)) and not isinstance(value, bool)):
        rendered = f'{value * 100:.3g}%'
    return field(fact['key'], text(rendered), text(fact['source'], href=fact['url'], tone='muted'))


def fact_fields(facts):
    """Show a repeated assertion once, retaining each distinct evidence link."""
    fields = {}
    for fact in facts:
        rendered = fact_field(fact)
        value = fact['value']
        identity = tuple(value) if isinstance(value, list) else value
        key = (rendered.label, type(value), identity, fact['status'], fact.get('code'))
        previous = fields.get(key)
        if previous is None:
            fields[key] = rendered
        elif rendered.values[1] not in previous.values[1:]:
            previous.values.append(rendered.values[1])
    return list(fields.values())


def evidence_section(result, links):
    findings = result['data']['findings']
    if not findings:
        return []
    # Common query facts belong once at group level, not once per advisory.
    shared = [f for f in findings[0]['facts'] if f.get('code') == 'query'
              and all(f in item['facts'] for item in findings)]
    security = result['id'] == 'security'
    all_stale = all(finding['stale'] for finding in findings)
    entries = []
    for finding in findings:
        # The heading already identifies this subject. Keep qualifiers only
        # when they name a different subject (for example another CVE alias).
        facts = [{**f, 'key': f['key'].removesuffix(' · ' + finding['title'])}
                 for f in finding['facts'] if f not in shared]
        facts.sort(key=lambda f: f.get('code') not in ('fixed_events', 'epss_probability'))
        fields = fact_fields(facts)
        if finding['scope'] == 'upgrade':
            fields.insert(0, field('Target', text(finding.get('target_version'), kind='code')))
        heading = [text(finding['title'], href=finding['evidence_url'])]
        heading += [text(tag, kind='tag', href=links.to(maintenance=tag)) for tag in finding['tags']]
        if finding['stale'] and not all_stale:
            heading.append(text('Previous observation', tone='notice'))
        entries.append(Entry(heading=heading, fields=fields))
    notes = (['Queried source component only. Local patches, bundled dependencies and binary artifacts are not evaluated.']
             if security else [])
    fields = fact_fields(shared)
    if all_stale:
        fields.append(field('Evidence', text('Previous observation', tone='notice')))
    return [Section(id=result['id'], title=f"{result['title']} · {len(findings)}",
                    fields=fields, entries=entries, notes=notes)]


def source_sections(result, links):
    data = result['data']
    meta = data.get('metadata') or {}
    fields = [field('License', text(meta['license']))] if meta.get('license') else []
    if not data.get('buildsystem'):
        fields.append(field('Build system', text(
            'Not declared' if data['buildsystem_status'] == 'not_declared' else 'Not observed')))
    description = meta.get('description')
    notes = [description] if description and description != meta.get('summary') else []
    return [Section(id=result['id'], title='Package information', fields=fields, notes=notes)] if fields or notes else []


def changelog_section(source):
    history = []
    for entry in source['data'].get('changelog', []):
        fields = [field('Commit', text(entry['commit'][:12], kind='code')),
                  field('Author', text(entry['author'])), field('Date', stamp(entry['date']))]
        if entry['signed_off_by']:
            fields.append(field('Signed-off-by', text(', '.join(entry['signed_off_by']))))
        history.append(Entry(heading=[text(entry['subject'])], fields=fields))
    return [Section(id='changelog', title='Changelog', entries=history)] if history else []


def version_sections(result, links):
    data = result['data']
    entries = []
    for watch in data.get('watch', []):
        stale = watch.get('error') or watch['stale']
        fields = [field('Last observed' if stale else 'Observed', text(watch.get('version'), kind='code'))]
        if stale:
            fields.append(field('Check', text(watch.get('error') or 'Out of date', tone='notice')))
        entries.append(Entry(
            heading=[text(watch['id'], href='/api/v1/tracks/' + quote(watch['id'], safe=''))], fields=fields))
    fields = []
    label = data.get('track_label')
    if label and label not in ('stable', data.get('track')):
        fields.append(field('Release line', text(label)))
    if data['relation'] in ('untracked', 'ahead', 'unknown', 'not_applicable'):
        fields.append(field('Comparison', text({'untracked': 'Untracked', 'ahead': 'Ahead of tracked release',
            'unknown': 'Cannot compare current observations', 'not_applicable': 'Not applicable'}[data['relation']])))
    return [Section(id=result['id'], title=result['title'], fields=fields, entries=entries)] if fields or entries else []


def build_sections(result, links):
    rows = []
    for build in result['data']['targets']:
        for observation in (build['flavors'] if len(build.get('flavors', [])) > 1 else [build]):
            last = observation.get('last_success') or {}
            identity = [text(build['label'])]
            if observation.get('package'):
                identity.append(text(observation['package'], kind='code', tone='muted'))
            rows.append(Row(key=f"{build['target']}:{observation.get('package', '')}", cells=[
                cell(identity), cell(build_cell(observation, None, '').lines[0]),
                cell([text(last.get('version'), kind='code')]), cell([stamp(last.get('time'))])]))
    return [Section(id=result['id'], title=result['title'],
        table=Table(label='Build status by target', columns=[Column(title='Target'),
            Column(title='Result'), Column(title='Last successful version'), Column(title='Succeeded at')], rows=rows))]


@dataclass(frozen=True)
class Presenter:
    """A read-side adapter, not a provider superclass or a frontend component."""
    columns: Callable[[str, list[dict]], list[Column]]
    cells: Callable[[dict, dict, Links], list[Cell]]
    sections: Callable[[dict, Links], list[Section]]


def evidence_cells(pkg, result, links):
    data = result['data']
    if result['id'] == 'security':
        count = data['finding_count']
        return [cell([text(f'{count} advisories', href=pkg['detail_url'] + '#' + result['id'])],
                     evidence_labels(result, links))] if count else [cell()]
    entries = data.get('entries') or data.get('findings', [])[:3]
    lines = [[text(entry['title'], href=entry['evidence_url'])] for entry in entries]
    if any(finding.get('scope') == 'upgrade' for finding in entries):
        lines.insert(0, version_value(pkg))
    if data['finding_count'] > len(entries):
        lines.append([text(f"+{data['finding_count'] - len(entries)} more", href=pkg['detail_url'] + '#' + result['id'])])
    return [cell(*lines)]


def single_column(title, targets):
    return [Column(title=title)]


def source_cells(pkg, result, links):
    data = result['data']
    system = [buildsystem(data['buildsystem'], links)] if data['buildsystem'] else []
    return [cell([text(data['version'], kind='code')], system)]


def version_cells(pkg, result, links):
    return [cell(version_value(pkg))]


def build_columns(title, targets):
    return [Column(title=target['label'], role='status') for target in targets]


def build_cells(pkg, result, links):
    return [build_cell(build, result['data']['source_version'], pkg['detail_url'])
            for build in result['data']['targets']]


PRESENTERS = {
    'source': Presenter(single_column, source_cells, source_sections),
    'version': Presenter(single_column, version_cells, version_sections),
    'build': Presenter(build_columns, build_cells, build_sections),
    'evidence': Presenter(single_column, evidence_cells, evidence_section),
}

def presenter(descriptor):
    return PRESENTERS[descriptor['kind']]


def identity_cell(pkg, links, *, labels=True, systems=True):
    identity = [text(pkg['name'], href=pkg['detail_url'], kind='code')]
    source = module(pkg, 'source')
    if systems and source and source['data'].get('buildsystem'):
        identity.append(buildsystem(source['data']['buildsystem'], links))
    signals = []
    if labels:
        for result in pkg['monitors'].values():
            if result['data']['kind'] == 'evidence':
                signals.extend(evidence_labels(result, links))
    return cell(identity, signals)


def listing(payload, query):
    links = Links(query)
    focus = next((m for m in payload['monitors'] if m['id'] == query.get('monitor')), None)
    coverage = bool(focus and payload['section'] == 'coverage')
    descriptors = ([focus] if focus else [m for m in payload['monitors'] if m['kind'] in ('version', 'build')])
    columns = [Column(title='Package', role='identity')]
    if coverage:
        columns += [Column(title='Check'), Column(title='Last checked')]
    else:
        for descriptor in descriptors:
            columns.extend(presenter(descriptor).columns(descriptor['title'], payload['targets']))
    rows = []
    for pkg in payload['items']:
        cells = [identity_cell(pkg, links, labels=not focus, systems=not focus or focus['kind'] != 'source')]
        if coverage:
            check = pkg['monitors'][focus['id']]['check']
            cells += [cell([check_value(check)]), cell([stamp(check.get('checked_at'))])]
        else:
            for descriptor in descriptors:
                result = pkg['monitors'][descriptor['id']]
                cells.extend(presenter(descriptor).cells(pkg, result, links))
        rows.append(Row(key=pkg['name'], cells=cells))
    title = focus['title'] if focus else 'Packages'
    navigation = Navigation(label='Monitors', choices=[
        Choice(label='Overview', href=links.to(monitor='', check='', section=''), selected=not focus),
        *[Choice(label=m['title'], href=links.to(monitor=m['id'], check='', section='results'),
                 selected=m == focus) for m in payload['monitors']]])
    controls = listing_controls(payload, query, focus, links)
    pagination = []
    if payload['page'] > 1:
        pagination.append(Choice(label='Previous', href=links.to(page=payload['page'] - 1)))
    if payload['page'] < payload['pages']:
        pagination.append(Choice(label='Next', href=links.to(page=payload['page'] + 1)))
    meta = ([] if focus else [field('OBS', stamp(payload['collection']['obs_updated_at'])),
                             field('Upstream', stamp(payload['collection']['upstream_updated_at']))])
    return ListingDocument(title=title, navigation=navigation, controls=controls,
        table=Table(label=title, columns=columns, rows=rows), total=payload['total'], page=payload['page'],
        pages=payload['pages'], pagination=pagination, meta=meta,
        notices=['Test fixture — not live openRuyi data.'] if payload['collection']['mode'] == 'fixture' else [])


def listing_controls(payload, query, focus, links):
    facets = []
    for name, label, counts in [('buildsystem', 'Build system', payload['buildsystems']),
                                ('maintenance', 'Maintenance', payload['maintenance_labels'])]:
        facets.append(Facet(id=name, name=name, label=label, options=[Option(value='', label='All'),
            *[Option(value=value, label='Not detected' if value == '_not_detected' else value,
                     count=count, selected=query.get(name) == value) for value, count in counts.items()]]))
    for target in payload['targets']:
        tid = target['id']
        facets.append(Facet(id='build-' + tid, name='build', label=target['label'], options=[Option(value=tid + ':', label='All'),
            *[Option(value=tid + ':' + choice['value'], label=choice['label'], count=choice['count'],
                     selected=tid + ':' + choice['value'] in query.get('build', []))
              for choice in payload['build_statuses'][tid]]]))
    navigation = []
    if focus:
        navigation.append(Navigation(label='Results and coverage', choices=[
            Choice(label='Results', count=payload['result_count'] if focus['kind'] == 'evidence' else None,
                   href=links.to(section='results', check=''), selected=payload['section'] == 'results'),
            Choice(label='Coverage', count=payload['coverage_count'], href=links.to(section='coverage', check=''),
                   selected=payload['section'] == 'coverage')]))
    if not focus or focus['kind'] == 'version':
        navigation.append(Navigation(label='Versions', choices=[Choice(label=label,
            count=payload['counts'][value], href=links.to(view=value), selected=query.get('view', 'all') == value)
            for value, label in [('all', 'All'), ('updates', 'Updates'), ('untracked', 'Untracked')]]))
    if focus and payload['section'] == 'coverage':
        navigation.append(Navigation(label='Check status', choices=[
            Choice(label='All', href=links.to(check=''), selected=not query.get('check')),
            *[Choice(label=CHECK_LABELS.get(key, key), count=count, href=links.to(check=key), selected=query.get('check') == key)
              for key, count in payload['check_statuses'].items()]]))
    active = []
    for facet in facets:
        for option in facet.options:
            if option.selected:
                active.append(Choice(label=f'{facet.label}: {option.label}', href=links.without(facet.name, option.value)))
    return Controls(query=query.get('q', ''),
        hidden=[Parameter(name=key, value=str(query[key])) for key in ('monitor', 'view', 'section', 'check', 'per_page') if query.get(key)],
        facets=facets,
        active=active, navigation=navigation)


def detail(pkg):
    links = Links()
    source = module(pkg, 'source')
    data = source['data'] if source else {}
    meta = data.get('metadata') or {}
    identity = version_value(pkg)
    if data.get('buildsystem'):
        identity.append(buildsystem(data['buildsystem'], links))
    shortcuts = [text('/' + data['source_path'], href=data.get('source_url'))] if data.get('source_path') else []
    if meta.get('url'):
        shortcuts.append(text('Upstream', href=meta['url']))
    shortcuts.append(text('Raw data', href='/api/v2/packages/' + quote(pkg['name'], safe='')))
    # Composition order is a reader concern; collectors never encode it.
    results = sorted(pkg['monitors'].values(), key=lambda m: {'build': 0, 'evidence': 1, 'version': 2, 'source': 3}[m['data']['kind']])
    sections, context = [], []
    for result in results:
        kind = result['data']['kind']
        destination = context if kind == 'source' else sections
        destination.extend(PRESENTERS[kind].sections(result, links))
    checks = []
    for result in pkg['monitors'].values():
        check = result['check']
        status_lines = [[check_value(check)]]
        status_lines.extend([text(check[key], tone='muted')] for key in ('note', 'error') if check.get(key))
        timestamps = [[stamp(check.get('checked_at'))]]
        if check.get('attempted_at') and check['attempted_at'] != check.get('checked_at'):
            timestamps.append([text('Last attempted', tone='muted'), stamp(check['attempted_at'])])
        checks.append(Row(key=result['id'], cells=[cell([text(result['title'])]), cell(*status_lines), cell(*timestamps)]))
    if source:
        sections.extend(changelog_section(source))
    sections.append(Section(id='checks', title='Checks', collapsible=True, table=Table(label='Collection checks', columns=[
        Column(title='Monitor'), Column(title='Observation'), Column(title='Last checked')], rows=checks)))
    return DetailDocument(title=pkg['name'], subtitle=meta.get('summary'), identity=identity,
                          links=shortcuts, context=context, sections=sections)


def theme(presentation):
    return {'appearances': {'buildsystem:' + key: value for key, value in presentation.get('buildsystems', {}).items()}}
