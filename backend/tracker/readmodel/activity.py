"""Package activity joins and bounded history; no collection on HTTP requests."""
import base64
import json
from datetime import datetime


def linked(snapshot, name):
    repositories = set(snapshot.get('github_repositories', []))
    for link in snapshot.get('github_links', {}).get(name, {}).values():
        repo = link['repository']
        if repo in repositories:
            item = snapshot.get('github_items', {}).get(repo, {}).get(link['id'])
            if item:
                yield item, link['evidence']


def order(item):
    return datetime.fromisoformat(item['updated_at']).timestamp(), item['repository'], item['id']


def page(snapshot, name, kind, cursor=None, per_page=20):
    if kind not in ('pr', 'issue') or not 1 <= per_page <= 50:
        raise ValueError('Activity requires kind=pr|issue and per_page=1..50')
    after = None
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')))
            if (not isinstance(decoded, list) or len(decoded) != 5 or decoded[:2] != [name, kind]
                    or not isinstance(decoded[2], (float, int))
                    or not all(isinstance(value, str) for value in decoded[3:])):
                raise ValueError
            after = tuple(decoded[2:])
        except (ValueError, UnicodeError, TypeError) as error:
            raise ValueError('Invalid activity cursor') from error
    items = sorted((dict(item, association=evidence) for item, evidence in linked(snapshot, name)
                    if item['kind'] == kind), key=order, reverse=True)
    total = len(items)
    if after is not None:
        items = [item for item in items if order(item) < after]
    selected = items[:per_page]
    next_cursor = None
    if len(items) > per_page:
        next_cursor = base64.urlsafe_b64encode(json.dumps([name, kind, *order(selected[-1])]).encode()).decode()
    # Bodies and changed-file lists are indexed server-side, not repeated in history responses.
    fields = ('id', 'repository', 'number', 'kind', 'title', 'url', 'status', 'updated_at',
              'available', 'paths_complete', 'association', 'author')
    return {'items': [{key: item.get(key) if key == 'author' else item[key] for key in fields} for item in selected],
            'total': total, 'next_cursor': next_cursor}


def project(context):
    from tracker.readmodel.monitors import check_state, component_ttl
    snapshot = context.snapshot
    repositories = snapshot.get('github_repositories', [])
    checks = [check_state([snapshot.get('components', {}).get('github:' + repo, {})], context.now,
                         component_ttl(snapshot, 'github:' + repo)) for repo in repositories]
    observations = list(linked(snapshot, context.name))
    partial = any(not item['paths_complete'] for item, _ in observations)
    pending = any(snapshot.get('components', {}).get('github:' + repo, {}).get('page') for repo in repositories)
    errors = [check['error'] for check in checks if check['error']]
    stale = any(check['stale'] for check in checks)
    labels = [{'label': label, 'count': sum(item['kind'] == kind for item, _ in observations), 'stale': stale}
              for kind, label in (('pr', 'PR'), ('issue', 'Issue'))]
    labels = [label for label in labels if label['count']]
    check = {'status': 'error' if errors else 'partial' if partial or pending else
             'expired' if stale else 'ok', 'stale': stale,
             'error': '; '.join(errors) or None,
             'note': 'Changed-file list incomplete.' if partial else 'Repository synchronization in progress.' if pending else None,
             'checked_at': min((c['checked_at'] for c in checks if c['checked_at']), default=None)}
    return {'check': check, 'dimensions': {'maintenance': [label['label'] for label in labels]},
            'data': {'kind': 'activity', 'labels': labels}}
