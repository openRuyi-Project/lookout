"""Version for reading documents; no collection or persistence."""
from urllib.parse import quote

from tracker.presentation.model import Entry, Section
from tracker.presentation.labels import appearance, caption
from tracker.presentation.values import cell, field, module, retained_marker, stamp, text, version_value


def signal_title(monitor):
    return 'DepChanges' if monitor['kind'] == 'requires' else monitor['title']


def version_sections(result, links):
    data = result['data']
    entries = []
    for watch in data.get('watch', []):
        stale = watch.get('error') or watch['stale']
        fields = [field('Last observed' if stale else 'Observed', text(watch.get('version'), kind='code'))]
        if stale:
            fields.append(field('Check', text(watch.get('error') or 'Stale', tone='notice')))
        entries.append(Entry(
            heading=[text(watch['id'], href='/api/v2/tracks/' + quote(watch['id'], safe=''))], fields=fields))
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
    if data['relation'] in ('ahead', 'unknown', 'not_applicable'):
        fields.append(field('Comparison', text({'ahead': 'Ahead of tracked release',
            'unknown': 'Cannot compare current observations', 'not_applicable': 'Not applicable'}[data['relation']])))
    return [Section(id=result['id'], title=result['title'], fields=fields, entries=entries)] if fields or entries else []


def version_annotations(pkg, links=None):
    """Compact related evidence; expanded facts stay in their owning section."""
    result = module(pkg, 'version')
    if not result:
        return []
    data = result['data']
    values = []
    for annotation in data.get('annotations', []):
        label = caption(annotation['label'])
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
        values.append(text(label, kind='tag', href=href, title=title, appearance=appearance(annotation['label']),
                           tone='notice' if annotation['stale'] else 'normal'))
    return values


def version_cells(pkg, result, links):
    values = version_value(pkg, links=links) + version_annotations(pkg, links)
    if any(a['stale'] for a in result['data'].get('annotations', [])):
        values.append(retained_marker(links.to(monitor=result['id'], freshness='retained', check='', section='results')))
    return [cell(values)]
