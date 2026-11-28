"""Source for reading documents; no collection or persistence."""
from urllib.parse import quote, urlsplit, urlunsplit

from tracker.presentation.labels import caption
from tracker.presentation.model import Entry, Section
from tracker.presentation.values import field, stamp, text


def source_sections(result, links):
    data = result['data']
    meta = data.get('metadata') or {}
    fields = [field('License', text(meta['license']))] if meta.get('license') else []
    if not data.get('buildsystem'):
        fields.append(field(caption('Build system'), text(caption('Custom'),
            title='Not declared' if data['buildsystem_status'] == 'not_declared' else 'Not observed')))
    description = meta.get('description')
    notes = [description] if description and description != meta.get('summary') else []
    sections = [Section(placement='lead', id=result['id'], title='', fields=fields, notes=notes)] if fields or notes else []
    if evidence := data.get('tests_disabled'):
        reason = {
            'absent': 'No %check stage for riscv64.',
            'empty': 'The riscv64 %check section is empty after expansion.',
            'comments': 'The riscv64 %check section contains only comments.',
        }[evidence['reason']]
        if option := evidence.get('option'):
            reason += f" Enabling {option} at SPEC line {evidence['line']} restores commands."
        else:
            reason += f" The %check replacement at SPEC line {evidence['line']} suppresses the BuildSystem check."
        if evidence.get('x86_64') == 'nonempty':
            reason += ' The x86_64 section contains commands.'
        sections.append(Section(id='tests-disabled', title='Packaging tests', notes=[reason], excerpt=evidence.get('excerpt')))
    return sections


def repository_link(url, kind, ref):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            return None
        route = kind if parsed.hostname == 'github.com' else (
            '-/' + kind if parsed.hostname.startswith('gitlab.') else None)
        if route is None:
            return None
        base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/').removesuffix('.git'), '', ''))
        return base + '/' + route + '/' + quote(ref, safe='')
    except (ValueError, TypeError):
        return None


def changelog_section(source, repository=None):
    history = []
    for entry in source['data'].get('changelog', []):
        href = repository_link(repository['url'], 'commit', entry['commit']) if repository else None
        history.append(Entry(heading=[text(entry['subject'], href=href)], metadata=[
            text(entry['author'], tone='muted'),
            text(entry['commit'][:12], kind='code', href=href, title=entry['commit']),
            stamp(entry['date'])]))
    return [Section(id='changelog', title='Changelog', entries=history)] if history else []
