"""Pure reading adapters: facts -> documents. Never called by collectors.

Only this boundary interprets monitor fields for readers. The website consumes
bounded document primitives, not monitor IDs, OBS states or advisory fact codes.
Filtering remains PackageList's job; this module does not recalculate counts.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from urllib.parse import quote, urlencode, urlsplit
from typing import Callable

from .monitor_model import CHECK_GROUPS, canonical_label
from .presentation_model import (Cell, Choice, Column, Controls, DetailDocument,
    Entry, Facet, Field, ListingDocument, Navigation, Option, Parameter, Row,
    Section, Table, Text)


CHECK_LABELS = {
    'ok': 'Checked', 'partial': 'Partial evidence', 'error': 'Failed',
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


def module(pkg, kind):
    return next((m for m in pkg['monitors'].values() if m['data']['kind'] == kind), None)


def buildsystem(value, links):
    return text(value, kind='tag', appearance='buildsystem:' + value,
                href=links.to(buildsystem=value))


def short_commit(value, other=None):
    """Compact identity only; distinguish the displayed pair on prefix collision."""
    length = 6
    if other and value != other:
        while length < len(value) and value[:length] == other[:length]:
            length += 1
    return value[:length]


def dated_commit(value, date, other=None):
    prefix = date[:10].replace('-', '') + '.' if date else ''
    return prefix + short_commit(value, other)


def version_value(pkg, *, compact=True):
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
    revision = value.get('revision')
    if revision:
        if not compact:
            return [text(value['current'], kind='code', tone=tone, title=title)]
        current, latest = revision['current'], revision['latest']
        links = revision['links']
        values = [text(dated_commit(current, revision['packaged_date'], latest), kind='code', tone=tone, href=links['current'],
                       title=f"{value['current']} · {current}")]
        if value['relation'] == 'changed':
            values += [text('→', title='Tracked branch tip differs from packaged source'),
                       text(dated_commit(latest, revision['latest_committed_at'], current), kind='code', tone='positive', href=links['latest'],
                            title=revision['branch'] + ' · ' + latest)]
        return values
    values = [text(value['current'], kind='code', tone=tone, title=title)]
    release = value.get('source_release')
    if release and release['version'] != value['current']:
        prefix = str(value['current']) + '-'
        qualifier = release['version'].removeprefix(prefix)
        values.append(text('(' + qualifier + ')', kind='code', tone='muted',
                           href=release['url'], title='Exact upstream source release'))
    if value['relation'] == 'outdated':
        values += [text('→', title='Update available'), text(value['latest'], kind='code', tone='positive')]
    return values


def check_value(check):
    status = check['status']
    return text(CHECK_LABELS.get(status, status.replace('_', ' ')),
                tone='normal' if status == 'ok' else 'notice',
                title=check.get('error') or check.get('note'))


def build_reason(observation):
    """Additional OBS evidence, not a restatement of the displayed status."""
    reason = observation.get('details')
    if not observation.get('issue') and observation['raw_status'] != 'scheduled':
        return None
    if not isinstance(reason, str):
        return None
    reason = ' '.join(reason.split())
    labels = {observation['raw_status'].casefold(), observation['text'].casefold()}
    for label in labels:
        if reason.casefold().startswith(label + ':'):
            reason = reason[len(label) + 1:].strip()
            break
    return reason if reason and reason.rstrip('.:').casefold() not in labels else None


def build_reason_line(build, detail_url):
    observations = build.get('flavors') or [build]
    reasons = []
    for observation in observations:
        reason = build_reason(observation)
        if not reason:
            continue
        if len(observations) > 1:
            # The aggregate may be Failed while a different flavor is Blocked.
            # Keep that evidence attached to its own flavor and status.
            label = observation.get('package') or 'Unnamed flavor'
            if observation['raw_status'] != build['raw_status']:
                label += ' · ' + observation['text']
            reason = label + ': ' + reason
        reasons.append(reason)
    if not reasons:
        return []
    full = '; '.join(reasons)
    visible = full if len(full) <= 120 else full[:119].rstrip() + '…'
    return [text(visible, href=detail_url + '#build', tone='muted',
                 title='Full OBS build details' if visible != full else 'OBS build details')]


def build_cell(build, current, detail_url, *, show_reason=False):
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
    reason = build_reason_line(build, detail_url) if show_reason else []
    return cell(values, previous, reason)


def evidence_labels(result, links):
    return [text(label['label'] + (f" {label['count']}" if label['count'] > 1 else ''),
                 kind='tag', href=links.to(maintenance=label['label']),
                 tone='notice' if label['stale'] else 'normal',
                 title=CHECK_LABELS['expired'] if label['stale'] else None)
            for label in result['data']['labels']]


def retained_marker(href='#checks'):
    return text(CHECK_LABELS['expired'], tone='notice', href=href,
                title='Retained observation; not a fresh check result')


def fact_field(fact):
    status, value = fact['status'], fact['value']
    rendered = (status.replace('_', ' ') if status != 'observed' else
                'Yes' if value is True else 'No' if value is False else
                ', '.join(value) or '—' if isinstance(value, list) else value)
    if (status == 'observed' and fact.get('code') == 'epss_probability'
            and isinstance(value, (float, int)) and not isinstance(value, bool)):
        rendered = f'{value * 100:.3g}%'
    kind = 'code' if fact.get('code') == 'cvss_vector' else 'text'
    return field(fact['key'], text(rendered, kind=kind), text(fact['source'], href=fact['url'], tone='muted'))


def fact_fields(facts):
    """Show a repeated assertion once, retaining each distinct evidence link."""
    fields = {}
    references = {}
    for fact in facts:
        if fact.get('code') == 'reference' and fact['status'] == 'observed':
            # A citation is a link, not the URL repeated as both value and source.
            url = urlsplit(fact['url'])
            identity = url.path.rstrip('/').split('/')[-1][:12] if '/commit/' in url.path else url.hostname
            references.setdefault(fact['url'], text(f"{fact['key'].title()} · {identity}",
                                                   href=fact['url'], title=fact['url']))
            continue
        rendered = fact_field(fact)
        value = fact['value']
        identity = tuple(value) if isinstance(value, list) else value
        key = (rendered.label, type(value), identity, fact['status'], fact.get('code'))
        previous = fields.get(key)
        if previous is None:
            fields[key] = rendered
        elif rendered.values[1] not in previous.values[1:]:
            previous.values.append(rendered.values[1])
    result = list(fields.values())
    if references:
        result.append(field('References', *references.values()))
    return result


def evidence_section(result, links):
    findings = result['data']['findings']
    if not findings:
        return []
    # Common query facts belong once at group level, not once per advisory.
    shared = [f for f in findings[0]['facts'] if f.get('code') == 'query'
              and all(f in item['facts'] for item in findings)]
    security = result['id'] == 'security'
    all_stale = all(finding['stale'] for finding in findings)
    targets = {finding.get('target_version') for finding in findings}
    shared_target = (next(iter(targets)) if len(targets) == 1
                     and all(finding['scope'] == 'upgrade' for finding in findings) else None)
    entries = []
    for finding in findings:
        # The heading already identifies this subject. Keep qualifiers only
        # when they name a different subject (for example another CVE alias).
        facts = [{**f, 'key': f['key'].removesuffix(' · ' + finding['title'])}
                 for f in finding['facts'] if f not in shared]
        facts.sort(key=lambda f: {'summary': 0, 'fixed_events': 1, 'epss_probability': 2,
                                 'kev_added': 3, 'kev_ransomware': 4}.get(f.get('code'), 5))
        fields = fact_fields(facts)
        if finding['scope'] == 'upgrade' and not shared_target:
            fields.insert(0, field('Target', text(finding.get('target_version'), kind='code')))
        heading = [text(finding['title'], href=finding['evidence_url'])]
        heading += [text(tag, kind='tag', href=links.to(maintenance=tag)) for tag in finding['tags']]
        if finding['stale'] and not all_stale:
            heading.append(retained_marker())
        entries.append(Entry(heading=heading, fields=fields))
    notes = (['Queried source component only. Local patches, bundled dependencies and binary artifacts are not evaluated.']
             if security else [])
    fields = fact_fields(shared)
    if shared_target:
        fields.insert(0, field('Target', text(shared_target, kind='code')))
    title = f"{result['title']} · {len(findings)}"
    if all_stale:
        title += ' · ' + CHECK_LABELS['expired']
    return [Section(id=result['id'], title=title,
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
    revision = data.get('revision')
    if revision:
        links = revision['links']
        fields += [field('Branch', text(revision['branch'], href=links['branch'])),
                   field('Source commit', text(revision['current'], kind='code', href=links['current']))]
        if revision['latest'] != revision['current'] and revision['latest']:
            fields.append(field('Observed branch tip', text(revision['latest'], kind='code',
                                href=links['latest'])))
            if revision['latest_committed_at']:
                fields.append(field('Committed at', stamp(revision['latest_committed_at'])))
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
        observations = build.get('flavors') or [build]
        for observation in observations:
            last = observation.get('last_success') or {}
            identity = [text(build['label'])]
            if len(observations) > 1 and observation.get('package'):
                identity.append(text(observation['package'], kind='code', tone='muted'))
            status_lines = [build_cell(observation, None, '').lines[0]]
            reason = build_reason(observation)
            if reason:
                status_lines.append([text(reason, tone='muted')])
            rows.append(Row(key=f"{build['target']}:{observation.get('package', '')}", cells=[
                cell(identity), cell(*status_lines),
                cell([text(last.get('version'), kind='code')]), cell([stamp(last.get('time'))])]))
    return [Section(id=result['id'], title=result['title'],
        table=Table(label='Build status by target', columns=[Column(title='Target'),
            Column(title='Result'), Column(title='Last successful version'), Column(title='Succeeded at')], rows=rows))]


def requirement_status(requirement, *, target=False):
    status = requirement['target_satisfaction'] if target else requirement['satisfaction']
    reason = requirement.get('target_reason') if target else requirement.get('reason')
    mark, tone = {'satisfied': ('✓', 'positive'), 'unsatisfied': ('✗', 'negative'),
                  'unknown': ('?', 'muted')}[status]
    title = {'satisfied': 'Current source version satisfies the upstream declaration',
             'unsatisfied': 'Current source version does not satisfy the upstream declaration',
             'unknown': 'Cannot determine: ' + (reason or 'unknown').replace('_', ' ')}[status]
    return text(mark, tone=tone, title=title)


def requirement_constraint(requirement, side):
    constraint = requirement.get(side)
    if not constraint:
        return text('Not observed', tone='muted')
    expression = constraint['expression'] or 'any version'
    if requirement['scheme'] == 'numeric_minimum':
        expression = '≥ ' + expression
    return text(expression, kind='code', href=constraint['url'])


def requirement_values(requirement, *, compact=False):
    package = requirement['package']
    label = requirement['name']
    if requirement.get('extras'):
        label += '[' + ', '.join(requirement['extras']) + ']'
    mapped = requirement.get('mapping') in (None, 'mapped')
    name = text(label, href='/packages/' + quote(package, safe='') if package and mapped else None)
    values = [name, requirement_constraint(requirement, 'current'), requirement_status(requirement)]
    if requirement['changed']:
        values += [text('→'), requirement_constraint(requirement, 'target'), requirement_status(requirement, target=True)]
    elif requirement['current'] is None and requirement['target'] is not None:
        values = [name, text('Upgrade:'), requirement_constraint(requirement, 'target'), requirement_status(requirement, target=True)]
    mapping = requirement.get('mapping')
    if mapping in ('not_mapped', 'ambiguous', 'not_packaged'):
        values = [value for value in values if value.text != '?']
        values.append(text({'not_mapped': 'Not mapped', 'ambiguous': 'Ambiguous mapping',
                            'not_packaged': 'Not packaged'}[mapping], tone='muted'))
    if compact and not requirement['changed']:
        values = [value for value in values if value.text != 'any version']
    return values


def requirement_groups(requirements):
    """Fold only identical assessments under different applicability clauses.

    Conditions remain separate evidence, never evaluated or rewritten here.
    Identity, source URLs, extras, scope and assessment are all part of the key.
    """
    grouped = {}
    for requirement in requirements:
        key = (bool(requirement.get('condition')),
               json.dumps({key: value for key, value in requirement.items() if key != 'condition'}, sort_keys=True))
        group = grouped.setdefault(key, (requirement, []))
        condition = requirement.get('condition')
        if condition and condition not in group[1]:
            group[1].append(condition)
    return list(grouped.values())


def requires_sections(result, links):
    requirements = result['data']['requirements']
    if not requirements:
        return []
    sections, conditions = [], []
    for optional, title, suffix in [(False, 'Requires', ''), (True, 'Optional dependencies', '-optional')]:
        groups = requirement_groups([item for item in requirements if (item.get('optional') is True) == optional])
        rows = []
        for number, (requirement, clauses) in enumerate(groups):
            values = requirement_values(requirement)
            observed = requirement['observed'] or {}
            rows.append(Row(key=str(number) + ':' + requirement['dependency'], cells=[
                cell([values[0]]), cell(values[1:]), cell([text(observed.get('version'), kind='code')])]))
            if clauses:
                condition_fields = [field('Applies when (any)', *[text(clause, kind='code') for clause in clauses])]
                if requirement.get('optional') is None:
                    condition_fields.append(field('Optionality', text('Not observed', tone='muted')))
                conditions.append(Entry(heading=values, fields=condition_fields))
        if rows:
            sections.append(Section(id=result['id'] + suffix, title=title, table=Table(
                label='Upstream runtime requirements compared with current source versions',
                columns=[Column(title='Dependency'), Column(title='Required'), Column(title='Current source')], rows=rows)))
    if conditions:
        sections.append(Section(id=result['id'] + '-conditions', title='Dependency conditions',
                                collapsible=True, entries=conditions))
    return sections


def requires_cells(pkg, result, links):
    data = result['data']
    lines = []
    has_optional = any(item.get('optional') is True for item in data['requirements'])
    for optional, label in [(False, 'Runtime'), (True, 'Optional')]:
        groups = requirement_groups([item for item in data['requirements'] if (item.get('optional') is True) == optional])
        if not groups:
            continue
        if has_optional:
            lines.append([text(label, tone='muted')])
        lines.extend(requirement_values(item, compact=True) for item, _ in groups)
    if result.get('dimensions', {}).get('retained:' + result['id']):
        lines.insert(0, [retained_marker(links.to(monitor=result['id'], freshness='retained', check='', section='results'))])
    if any(requirement['changed'] or requirement['current'] is None for requirement in data['requirements']):
        lines.insert(0, version_value(pkg))
    return [cell(*lines)]


@dataclass(frozen=True)
class Presenter:
    """Package context is always composable; a results view is opt-in.

    A shared observation interface does not imply a separate navigation entry.
    Columns and cells must answer an independent list-reading question together.
    """
    sections: Callable[[dict, Links], list[Section]]
    columns: Callable[[str, list[dict]], list[Column]] | None = None
    cells: Callable[[dict, dict, Links], list[Cell]] | None = None
    filters: frozenset[str] = frozenset()

    def __post_init__(self):
        if (self.columns is None) != (self.cells is None):
            raise ValueError('a results view requires both columns and cells')

    @property
    def has_results(self):
        return self.columns is not None


def evidence_cells(pkg, result, links):
    data = result['data']
    entries = data.get('entries') or data.get('findings', [])
    checks_url = links.to(monitor=result['id'], freshness='retained', check='', section='results')
    if result['id'] == 'security':
        # One wrapping line of identifiers, not one tall row per advisory or a
        # count that repeats the section's identity. Keep KEV tied to its subject.
        lines = []
        for stale in (False, True):
            values = [text(entry['title'] + (' · KEV' if 'KEV' in entry.get('tags', []) else ''),
                           href=entry['evidence_url'], kind='code', tone='notice' if stale else 'normal')
                      for entry in entries if entry['stale'] == stale]
            if values:
                lines.append(([retained_marker(checks_url)] if stale else []) + values)
        return [cell(*lines)]
    lines = []
    for stale in (False, True):
        group = [[text(entry['title'], href=entry['evidence_url'], tone='notice' if stale else 'normal')]
                 for entry in entries if entry['stale'] == stale]
        if stale and group:
            group[0].insert(0, retained_marker(checks_url))
        lines.extend(group)
    if any(finding.get('scope') == 'upgrade' for finding in entries):
        lines.insert(0, version_value(pkg))
    return [cell(*lines)]


def single_column(title, targets):
    return [Column(title=title)]


def version_annotations(pkg, links=None):
    """Compact related evidence; expanded facts stay in their owning section."""
    result = module(pkg, 'version')
    if not result:
        return []
    data = result['data']
    values = []
    for annotation in data.get('annotations', []):
        label = annotation['label']
        if annotation['count'] > 1:
            label += f" {annotation['count']}"
        # A withdrawn current release does not mean the update is withdrawn.
        if annotation['monitor'] == 'yanked' and data['relation'] == 'outdated':
            label += ' · ' + str(data['current'])
        subject = (f"Target {annotation['target_version']}" if annotation['scope'] == 'upgrade'
                   else f"Current release {data['current']}")
        title = subject + ('; retained evidence, see Checks' if annotation['stale'] else '')
        if annotation['monitor'] == 'security':
            title += '; upstream advisory matches, local patches not evaluated'
        href = (links.to(monitor='version', signal=annotation['monitor'], check='', section='results')
                if links else '#' + annotation['monitor'])
        values.append(text(label, kind='tag', href=href, title=title,
                           tone='notice' if annotation['stale'] else 'normal'))
    return values


def version_cells(pkg, result, links):
    values = version_value(pkg) + version_annotations(pkg, links)
    if any(a['stale'] for a in result['data'].get('annotations', [])):
        values.append(retained_marker(links.to(monitor=result['id'], freshness='retained', check='', section='results')))
    return [cell(values)]


def build_columns(title, targets):
    return [Column(title=target['label'], role='status') for target in targets]


def build_cells(pkg, result, links):
    return [build_cell(build, result['data']['source_version'], pkg['detail_url'],
                       show_reason=links.query.get('monitor') == result['id'])
            for build in result['data']['targets']]


PRESENTERS = {
    'source': Presenter(source_sections),
    'version': Presenter(version_sections, single_column, version_cells, frozenset({'view', 'signal'})),
    'build': Presenter(build_sections, build_columns, build_cells, frozenset({'build'})),
    'evidence': Presenter(evidence_section, single_column, evidence_cells),
    'requires': Presenter(requires_sections, single_column, requires_cells, frozenset({'requires'})),
}

def presenter(descriptor):
    return PRESENTERS[descriptor['kind']]


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
    if result.get('maintenance'):
        result['maintenance'] = canonical_label(result['maintenance'])
    if 'build' in result:
        values = result['build'] if isinstance(result['build'], list) else [result['build']]
        result['build'] = [value for value in values if value.partition(':')[2] != 'issues']
    if result.get('view') not in ('all', 'updates'):
        result.pop('view', None)
    if focus:
        result.update(monitor=focus['id'], section=section)
        if check:
            result['check'] = check
    return result


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
        statuses = [item for item in payload['build_statuses'][tid] if item['value'] != 'issues']
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
