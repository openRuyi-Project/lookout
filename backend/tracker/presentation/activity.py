"""Activity history uses reading primitives, not provider HTML or frontend rules."""
from urllib.parse import quote, urlencode

from tracker.presentation.model import Entry, Section
from tracker.presentation.values import field, stamp, text

# GitHub's issue closure and unmerged PR closure have different meanings/icons.
STATES = {
    ('issue', 'open'): ('issue-opened', 'open'),
    ('issue', 'completed'): ('issue-closed', 'merged'),
    ('issue', 'not_planned'): ('issue-closed', 'inactive'),
    ('issue', 'closed'): ('issue-closed', 'inactive'),
    ('pr', 'open'): ('git-pull-request', 'open'),
    ('pr', 'draft'): ('git-pull-request-draft', 'inactive'),
    ('pr', 'closed'): ('git-pull-request-closed', 'closed'),
    ('pr', 'merged'): ('git-merge', 'merged'),
}


def open_links(pkg, result):
    counts = {label['label']: label for label in result['data']['labels']}
    values = []
    for kind, label, title in (('pr', 'PR', 'Open pull requests'), ('issue', 'Issue', 'Open issues')):
        count = counts.get(label)
        if not count or not count['count']:
            continue
        icon, style = STATES[kind, 'open']
        description = f"{title}: {count['count']}" + (' (stale)' if count['stale'] else '')
        values.append(text(count['count'], icon=icon,
            appearance=None if count['stale'] else 'label:github-' + style,
            tone='notice' if count['stale'] else 'normal', title=description,
            aria_label=pkg['name'] + ': ' + description, href=pkg['detail_url'] + '#github-' + kind))
    return values


def section(name, kind, page):
    entries = []
    for item in page['items']:
        icon, style = STATES[item['kind'], item['status']]
        heading = [text('#' + str(item['number']), href=item['url'], icon=icon,
                        appearance='label:github-' + style, title=item['status']),
                   text(item['title'], href=item['url'])]
        byline = [text(item['author'], tone='muted')] if item.get('author') else []
        fields = []
        if not item['available']:
            fields.append(field('', text('No longer returned by repository', tone='muted')))
        if not item['paths_complete']:
            fields.append(field('', text('Changed-file list incomplete', tone='notice')))
        entries.append(Entry(heading=heading, metadata=[*byline, stamp(item['updated_at'])], fields=fields))
    more = None
    if page['next_cursor']:
        more = '/packages/' + quote(name, safe='') + '/activity?' + urlencode({'kind': kind, 'cursor': page['next_cursor']})
    return Section(id='github-' + kind, title=('PR' if kind == 'pr' else 'Issue') + f" · {page['total']}",
                   entries=entries, more=more)


def sections(result, links):
    # History is joined from the snapshot only for a requested detail page.
    return []
