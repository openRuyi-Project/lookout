"""Pages for reading documents; no collection or persistence."""
from urllib.parse import quote

from tracker.monitors.model import CHECK_GROUPS
from tracker.presentation.evidence import evidence_labels
from tracker.presentation.model import Choice, Column, DetailDocument, ListingDocument, Navigation, Row, Section, Table
from tracker.presentation.navigation import Links, global_navigation, listing_controls, listing_query
from tracker.presentation.registry import PRESENTERS, presenter
from tracker.presentation.source import changelog_section
from tracker.presentation.values import (
    CHECK_LABELS,
    buildsystem,
    cell,
    check_value,
    field,
    module,
    stamp,
    text,
    version_value,
)
from tracker.presentation.version import version_annotations


def identity_cell(pkg, links, *, labels=True):
    identity = [text(pkg['name'], href=pkg['detail_url'], kind='code')]
    source = module(pkg, 'source')
    if source and source['data'].get('buildsystem'):
        identity.append(buildsystem(source['data']['buildsystem'], links))
    signals = []
    if labels:
        version = module(pkg, 'version')
        annotations = version['data'].get('annotations', []) if version else []
        related = {annotation['monitor'] for annotation in annotations}
        for result in pkg['monitors'].values():
            if result['data']['kind'] in ('evidence', 'requires') and result['id'] not in related:
                signals.extend(evidence_labels(result, links))
    return cell(identity, signals)


def listing(payload, query):
    query = listing_query(query, payload['monitors'])
    focus = next((m for m in payload['monitors'] if m['id'] == query.get('monitor')), None)
    payload = {**payload, 'section': query.get('section', 'results')}
    links = Links(query, payload['monitors'])
    coverage = bool(focus and payload['section'] == 'coverage')
    filtered_checks = coverage and bool(query.get('check'))
    reasons = {pkg['name']: pkg['monitors'][focus['id']]['check'].get('error') or
               pkg['monitors'][focus['id']]['check'].get('note')
               for pkg in payload['items']} if filtered_checks else {}
    show_reason = any(reasons.values())
    descriptors = ([focus] if focus else [m for m in payload['monitors'] if m['kind'] in ('version', 'build')])
    columns = [Column(title='Package', role='identity')]
    if filtered_checks:
        columns.append(Column(title='Version'))
        if show_reason:
            columns.append(Column(title='Reason'))
    elif coverage:
        columns += [Column(title='Check'), Column(title='Last checked')]
    else:
        for descriptor in descriptors:
            columns.extend(presenter(descriptor).columns(descriptor['title'], payload['targets']))
    rows = []
    for pkg in payload['items']:
        cells = [identity_cell(pkg, links, labels=not focus)]
        if filtered_checks:
            source = module(pkg, 'source')
            cells.append(cell([text(source['data'].get('version') if source else None, kind='code')]))
            if show_reason:
                cells.append(cell([text(reasons[pkg['name']], tone='notice')]))
        elif coverage:
            check = pkg['monitors'][focus['id']]['check']
            cells += [cell([check_value(check)]), cell([stamp(check.get('checked_at'))])]
        else:
            for descriptor in descriptors:
                result = pkg['monitors'][descriptor['id']]
                cells.extend(presenter(descriptor).cells(pkg, result, links))
        rows.append(Row(key=pkg['name'], cells=cells))
    title = focus['title'] if focus else 'Packages'
    if filtered_checks and query['check'] not in CHECK_GROUPS:
        title += ' · ' + CHECK_LABELS.get(query['check'], query['check'].replace('_', ' '))
    navigation = Navigation(label='Monitors', choices=[
        Choice(label='Overview', href=links.to(monitor='', check='', section=''), selected=not focus),
        *[Choice(label=m['title'], href=links.to(monitor=m['id'], check='', section='results'),
                 selected=m == focus) for m in sorted(payload['monitors'], key=lambda m: m['id'] == 'eol')
          if presenter(m).has_results and m['id'] != 'yanked']])
    controls = listing_controls(payload, query, focus, links)
    pagination = []
    if payload['page'] > 1:
        pagination.append(Choice(label='Previous', href=links.to(page=payload['page'] - 1)))
    if payload['page'] < payload['pages']:
        pagination.append(Choice(label='Next', href=links.to(page=payload['page'] + 1)))
    meta = ([] if focus else [field('OBS', stamp(payload['collection']['obs_updated_at'])),
                             field('Upstream', stamp(payload['collection']['upstream_updated_at']))])
    notices = ['Test fixture — not live openRuyi data.'] if payload['collection']['mode'] == 'fixture' else []
    if notice := payload['collection'].get('projection_notice'):
        notices.append(notice)
    return ListingDocument(title=title, navigation=navigation, global_navigation=global_navigation(payload, query, links), controls=controls,
        table=Table(label=title, columns=columns, rows=rows), total=payload['total'], page=payload['page'],
        pages=payload['pages'], pagination=pagination, meta=meta,
        notices=notices)


def detail(pkg):
    links = Links()
    source = module(pkg, 'source')
    data = source['data'] if source else {}
    meta = data.get('metadata') or {}
    identity = version_value(pkg, compact=False) + version_annotations(pkg)
    if data.get('buildsystem'):
        identity.append(buildsystem(data['buildsystem'], links))
    shortcuts = [text('/' + data['source_path'], href=data.get('source_url'))] if data.get('source_path') else []
    if meta.get('url'):
        shortcuts.append(text('Upstream', href=meta['url']))
    shortcuts.append(text('Raw data', href='/api/v2/packages/' + quote(pkg['name'], safe='')))
    # Composition order is a reader concern; collectors never encode it.
    results = sorted(pkg['monitors'].values(), key=lambda m: (m['id'] == 'eol', {'build': 0, 'evidence': 1, 'requires': 1, 'version': 2, 'source': 3}[m['data']['kind']]))
    sections, context = [], []
    for result in results:
        kind = result['data']['kind']
        adapter = PRESENTERS[kind]
        destination = sections if adapter.has_results else context
        destination.extend(adapter.sections(result, links))
    checks = []
    for result in pkg['monitors'].values():
        check = result['check']
        status_lines = [[check_value(check)]]
        status_lines.extend([text(check[key], tone='muted')] for key in ('note', 'error') if check.get(key))
        timestamps = [[stamp(check.get('checked_at'))]]
        if check.get('attempted_at') and check['attempted_at'] != check.get('checked_at'):
            timestamps.append([text('Last attempted', tone='muted'), stamp(check['attempted_at'])])
        checks.append(Row(key=result['id'], cells=[cell([text(result['title'],
            href=links.to(monitor=result['id'], section='coverage', check=check['status']))]),
            cell(*status_lines), cell(*timestamps)]))
    if source:
        sections.extend(changelog_section(source))
    sections.append(Section(id='checks', title='Checks', collapsible=True, table=Table(label='Collection checks', columns=[
        Column(title='Monitor'), Column(title='Observation'), Column(title='Last checked')], rows=checks)))
    return DetailDocument(title=pkg['name'], subtitle=meta.get('summary'), identity=identity,
                          links=shortcuts, context=context, sections=sections)


def theme(presentation):
    return {'appearances': {'buildsystem:' + key: value for key, value in presentation.get('buildsystems', {}).items()}}
