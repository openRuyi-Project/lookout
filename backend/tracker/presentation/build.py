"""Build for reading documents; no collection or persistence."""
from tracker.presentation.model import Column, Row, RowNote, Section, Table
from tracker.presentation.values import cell, stamp, text


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
    return [text(full, href=detail_url + '#build', tone='muted', title='OBS build details')]


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


def build_columns(title, targets):
    return [Column(title=target['label'], role='status') for target in targets]


def build_cells(pkg, result, links):
    return [build_cell(build, result['data']['source_version'], pkg['detail_url'],
                       show_reason=links.query.get('monitor') == result['id'])
            for build in result['data']['targets']]


def build_note(pkg, result, column):
    """Group identical reasons by target; preserve every distinct reason."""
    reasons = {}
    for build in result['data']['targets']:
        line = build_reason_line(build, pkg['detail_url'])
        if line:
            reasons.setdefault(line[0].text, []).append(build['label'])
    return [RowNote(column=column, span=len(result['data']['targets']), values=[
        text(', '.join(targets) + ':', kind='code', tone='muted'),
        text(reason, href=pkg['detail_url'] + '#build', tone='muted')])
        for reason, targets in reasons.items()]
