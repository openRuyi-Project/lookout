"""Source for reading documents; no collection or persistence."""
from tracker.presentation.model import Entry, Section
from tracker.presentation.values import field, stamp, text


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
