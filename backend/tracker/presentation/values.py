"""Values for reading documents; no collection or persistence."""
from datetime import datetime, timezone

from tracker.monitors.issues import Issue
from tracker.presentation.model import Cell, Column, Field, Text
from tracker.presentation.labels import appearance, caption


CHECK_LABELS = {
    'ok': 'Checked', 'partial': 'Partial evidence', 'error': Issue.CHECK_FAILED,
    'failed': Issue.CHECK_FAILED,
    'pending': 'Not yet checked', 'not_configured': 'Not configured',
    'not_applicable': 'Not applicable', 'unsupported': 'Metadata unavailable',
    'input_unavailable': 'Input unavailable', 'expired': 'Stale',
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


def module(pkg, kind):
    return next((m for m in pkg['monitors'].values() if m['data']['kind'] == kind), None)


def buildsystem(value, links):
    return text(value, kind='tag', variant='solid', appearance='buildsystem:' + value,
                href=links.only_filter(buildsystem=value))


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


def version_value(pkg, *, compact=True, links=None):
    result = module(pkg, 'version')
    if not result:
        return []
    value = result['data']
    build = module(pkg, 'build')
    success = build['data']['source_success'] if build else None
    untracked = Issue.UNTRACKED in result.get('dimensions', {}).get('maintenance', [])
    tone = 'muted' if untracked or success is None else 'negative' if success is False else 'normal'
    title = ('Upstream is not tracked' if untracked else
             'Current source has not succeeded on every active build target' if success is False else
             'Current source build success is unknown' if success is None else
             'Current source succeeded on every active build target')
    if value['relation'] == 'unknown' and not untracked:
        title += '; source and upstream cannot currently be compared'
    annotation = ([text(Issue.UNTRACKED, kind='tag', appearance=appearance(Issue.UNTRACKED),
                        href=links.only_filter(maintenance=[Issue.UNTRACKED]) if links else '/?maintenance=Untracked')]
                  if untracked else [])
    decoration = 'dashed' if untracked and value['current'] else None
    revision = value.get('revision')
    if revision:
        if not compact:
            return [text(value['current'], kind='code', tone=tone, title=title, decoration=decoration), *annotation]
        current, latest = revision['current'], revision['latest']
        links = revision['links']
        values = [text(dated_commit(current, revision['packaged_date'], latest), kind='code', tone=tone, href=links['current'],
                       title=f"{value['current']} · {current}")]
        if value['relation'] == 'changed':
            values += [text('→', title='Tracked branch tip differs from packaged source'),
                       text(dated_commit(latest, revision['latest_committed_at'], current), kind='code', tone='positive', href=links['latest'],
                            title=revision['branch'] + ' · ' + latest)]
        return values + annotation
    values = [text(value['current'], kind='code', tone=tone, title=title, decoration=decoration)]
    release = value.get('source_release')
    if release and release['version'] != value['current']:
        prefix = str(value['current']) + '-'
        qualifier = release['version'].removeprefix(prefix)
        values.append(text('(' + qualifier + ')', kind='code', tone='muted',
                           href=release['url'], title='Exact upstream source release'))
    if value['relation'] == 'outdated':
        values += [text('→', title='Update available'), text(value['latest'], kind='code', tone='positive')]
    return values + annotation


def check_value(check):
    status = check['status']
    return text(CHECK_LABELS.get(status, status.replace('_', ' ')),
                tone='normal' if status == 'ok' else 'notice',
                title=check.get('error') or check.get('note'))


def retained_marker(href='#checks'):
    return text(CHECK_LABELS['expired'], tone='notice', href=href,
                title='Retained observation; not a fresh check result')


def single_column(title, targets):
    return [Column(title=title)]
