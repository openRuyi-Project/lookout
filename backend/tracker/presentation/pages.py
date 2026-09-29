"""Pages for reading documents; no collection or persistence."""
from urllib.parse import quote, urlsplit, urlunsplit

from tracker.monitors.model import CHECK_GROUPS
from tracker.monitors.issues import Issue
from tracker.presentation.evidence import evidence_labels
from tracker.presentation.build import build_note
from tracker.presentation.labels import appearance, caption, palettes, priority
from tracker.presentation.model import Choice, Column, DetailDocument, Field, ListingDocument, Navigation, Row, Section, Table
from tracker.presentation.navigation import (
    Links, global_navigation, listing_controls, listing_query,
)
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


def collection_meta(collection):
    result = [Field(label='BuildService', href=collection.get('build_service_url'),
                    values=[stamp(collection['obs_updated_at'])])]
    source = collection.get('source_repository')
    if not source:
        return result
    branch_url = commit_url = None
    try:
        url = urlsplit(source['url'])
        if (url.scheme in ('http', 'https') and url.hostname and not url.username
                and not url.password and not url.query and not url.fragment):
            base = urlunsplit((url.scheme, url.netloc, url.path.rstrip('/').removesuffix('.git'), '', ''))
            routes = ('tree', 'commit') if url.hostname == 'github.com' else (
                ('-/tree', '-/commit') if url.hostname.startswith('gitlab.') else None)
            if routes:
                branch_url = base + '/' + routes[0] + '/' + quote(source['branch'], safe='')
                commit_url = base + '/' + routes[1] + '/' + quote(source['revision'], safe='')
    except ValueError:
        pass
    result.append(Field(label=source['branch'], href=branch_url,
        values=[text(source['revision'][:6], kind='code', href=commit_url, title=source['revision'])]))
    return result


def identity_cell(pkg, links, *, labels=True):
    identity = [text(pkg['name'], href=pkg['detail_url'], kind='code')]
    signals = []
    if labels:
        version = module(pkg, 'version')
        if version and Issue.OUTDATED in version['dimensions'].get('maintenance', []):
            signals.append(text(Issue.OUTDATED, kind='tag', appearance=appearance(Issue.OUTDATED),
                                href=links.only_filter(maintenance=[Issue.OUTDATED])))
        signals.extend(check_failed_label(pkg, links))
        for result in pkg['monitors'].values():
            if result['data']['kind'] in ('evidence', 'requires'):
                signals.extend(evidence_labels(result, links, counts=False))
    source = module(pkg, 'source')
    if source and source['data'].get('buildsystem'):
        identity.append(buildsystem(source['data']['buildsystem'], links))
    identity.extend(sorted(signals, key=lambda value: priority(value.appearance)))
    return cell(identity)


def check_failed_label(pkg, links=None):
    errors = [result for result in pkg['monitors'].values() if result['check']['status'] == 'error']
    if not errors:
        return []
    anchor = 'check-' + errors[0]['id'] if len(errors) == 1 else 'checks'
    return [text(Issue.CHECK_FAILED, kind='tag', appearance=appearance(Issue.CHECK_FAILED),
                 title=', '.join(caption(result['title']) for result in errors),
                 href=links.only_filter(maintenance=[Issue.CHECK_FAILED]) if links else pkg['detail_url'] + '#' + anchor)]


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
            projected = presenter(descriptor).columns(caption(descriptor['title']), payload['targets'])
            columns.extend(projected)
    rows = []
    for pkg in payload['items']:
        cells = [identity_cell(pkg, links, labels=not focus)]
        notes = []
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
                column = len(cells)
                if not focus and descriptor['kind'] == 'version':
                    cells.append(cell(version_value(pkg, links=links)))
                    for related in sorted(pkg['monitors'].values(), key=lambda m: m['id'] == 'eol'):
                        preview = presenter({'kind': related['data']['kind']}).preview
                        if preview:
                            cells[column].lines.extend(preview(pkg, related, links))
                else:
                    cells.extend(presenter(descriptor).cells(pkg, result, links))
                if not focus and descriptor['kind'] == 'build':
                    notes.extend(build_note(pkg, result, column))
        rows.append(Row(key=pkg['name'], cells=cells, notes=notes))
    title = caption(focus['title']) if focus else 'Packages'
    if filtered_checks and query['check'] not in CHECK_GROUPS:
        title += ' · ' + CHECK_LABELS.get(query['check'], query['check'].replace('_', ' '))
    navigation = Navigation(label='Monitors', choices=[
        Choice(label='Packages', href=links.to(monitor='', check='', section=''), selected=not focus),
        *[Choice(label=caption(m['title']), href=links.to(monitor=m['id'], check='', section='results'),
                 selected=m == focus) for m in sorted(payload['monitors'], key=lambda m: m['id'] == 'eol')
          if presenter(m).has_results and m['id'] != 'yanked']])
    controls = listing_controls(payload, query, focus, links)
    pagination = []
    if payload['page'] > 1:
        pagination.append(Choice(label='Previous', href=links.to(page=payload['page'] - 1)))
    if payload['page'] < payload['pages']:
        pagination.append(Choice(label='Next', href=links.to(page=payload['page'] + 1)))
    meta = [] if focus else collection_meta(payload['collection'])
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
    identity = [buildsystem(data['buildsystem'], links)] if data.get('buildsystem') else []
    identity.extend(version_value(pkg, compact=False))
    signals = version_annotations(pkg) + check_failed_label(pkg)
    requirements = module(pkg, 'requires')
    if requirements:
        signals.extend(evidence_labels(requirements, links, counts=False))
    identity.extend(sorted(signals, key=lambda value: priority(value.appearance)))
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
        checks.append(Row(key=result['id'], id='check-' + result['id'], cells=[cell([text(caption(result['title']),
            href=links.to(monitor=result['id'], section='coverage', check=check['status']))]),
            cell(*status_lines), cell(*timestamps)]))
    if source:
        sections.extend(changelog_section(source))
    sections.append(Section(id='checks', title='Checks', collapsible=True, table=Table(label='Collection checks', columns=[
        Column(title='Monitor'), Column(title='Observation'), Column(title='Last checked')], rows=checks)))
    return DetailDocument(title=pkg['name'], subtitle=meta.get('summary'), identity=identity,
                          links=shortcuts, context=context, sections=sections)


def theme(presentation):
    return {'appearances': {**palettes(), **{'buildsystem:' + key: {k: value[k] for k in ('background', 'foreground')}
            for key, value in presentation.get('buildsystems', {}).items()}}}
