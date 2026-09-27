"""Evidence for reading documents; no collection or persistence."""
from urllib.parse import urlsplit

from tracker.presentation.model import Entry, Section
from tracker.presentation.values import CHECK_LABELS, cell, field, retained_marker, text, version_value


def evidence_labels(result, links):
    return [text(label['label'] + (f" {label['count']}" if label['count'] > 1 else ''),
                 kind='tag', href=links.to(maintenance=label['label']),
                 tone='notice' if label['stale'] else 'normal',
                 title=CHECK_LABELS['expired'] if label['stale'] else None)
            for label in result['data']['labels']]


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
