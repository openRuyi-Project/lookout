"""Resumable repository sync; only changed activity records are written to SQLite."""
from copy import deepcopy
from datetime import datetime, timedelta
from urllib.parse import urlencode

from tracker import config as cfg
from tracker import state
from tracker.monitors.github.model import Matcher, settings
from tracker.monitors.model import fingerprint
from tracker.providers.github import BudgetExhausted, Client, RateLimited

PAGE_SIZE = 100


def normalize(raw, repo, previous, client, *, refresh=False, progress=None):
    number = raw['number']
    if type(number) is not int or number <= 0 or raw['state'] not in ('open', 'closed'):
        raise ValueError('Invalid GitHub activity identity/state')
    stamp = raw['updated_at']
    if datetime.fromisoformat(stamp).tzinfo is None:
        raise ValueError('GitHub activity timestamp requires timezone')
    kind = 'pr' if 'pull_request' in raw else 'issue'
    fields = {key: raw.get(key) for key in ('title', 'body', 'state', 'state_reason', 'updated_at', 'created_at', 'closed_at')}
    fields['author'] = (raw.get('user') or {}).get('login')
    labels = sorted(label['name'] for label in raw.get('labels', []))
    source_key = fingerprint({**fields, 'labels': labels, 'kind': kind})
    if previous and previous.get('source_key') == source_key and previous.get('paths_complete') and not refresh:
        return {**previous, 'available': True}
    item = {**fields, 'repository': repo, 'number': number, 'id': str(raw['id']), 'kind': kind,
            'url': f'https://github.com/{repo}/' + ('pull/' if kind == 'pr' else 'issues/') + str(number),
            'labels': labels, 'paths': [], 'paths_complete': True, 'source_key': source_key,
            'available': True}
    if kind == 'issue':
        item['status'] = ('open' if raw['state'] == 'open' else
                          {'completed': 'completed', 'not_planned': 'not_planned', 'duplicate': 'not_planned'}.get(raw.get('state_reason'), 'closed'))
        return item
    progress = progress if progress is not None else {}
    if progress.get('source_key') != source_key:
        progress.clear()
        progress['source_key'] = source_key
    if 'detail' not in progress:
        progress['detail'], _ = client.get(f'/repos/{repo}/pulls/{number}')
    detail = progress['detail']
    item['status'] = ('merged' if detail.get('merged_at') else 'closed' if detail['state'] == 'closed'
                      else 'draft' if detail.get('draft') else 'open')
    item['merged_at'] = detail.get('merged_at')
    item['diff_key'] = [detail['base']['sha'], detail['head']['sha']]
    if previous and previous.get('diff_key') == item['diff_key'] and previous.get('paths_complete'):
        item['paths'] = previous['paths']
    else:
        paths, count = set(progress.get('paths', [])), progress.get('count', 0)
        for page in (() if progress.get('files_done') else range(progress.get('file_page', 1), 31)):
            files, _ = client.get(f'/repos/{repo}/pulls/{number}/files?per_page=100&page={page}')
            if not isinstance(files, list):
                raise ValueError('Invalid GitHub file list')
            count += len(files)
            for file in files:
                paths.add(file['filename'])
                if file.get('previous_filename'):
                    paths.add(file['previous_filename'])
            progress.update(paths=sorted(paths), count=count, file_page=page + 1)
            if len(files) < 100 or count >= detail['changed_files']:
                break
        progress['files_done'] = True
        # File pages are not a GitHub snapshot. Publish only after the diff endpoints
        # still identify the same revisions; the completed pages survive budget pauses.
        confirmed, _ = client.get(f'/repos/{repo}/pulls/{number}')
        if ([confirmed['base']['sha'], confirmed['head']['sha']] != item['diff_key']
                or confirmed['changed_files'] != detail['changed_files']):
            progress.clear()
            raise BudgetExhausted
        item['paths'] = sorted(paths)
        item['paths_complete'] = count == detail['changed_files']
        fields.update({key: confirmed[key] for key in fields if key in confirmed})
        if 'user' in confirmed:
            fields['author'] = (confirmed['user'] or {}).get('login')
        if 'labels' in confirmed:
            labels = sorted(label['name'] for label in confirmed['labels'])
        item.update(fields, labels=labels, source_key=fingerprint({**fields, 'labels': labels, 'kind': kind}),
                    status=('merged' if confirmed.get('merged_at') else 'closed' if confirmed['state'] == 'closed'
                            else 'draft' if confirmed.get('draft') else 'open'), merged_at=confirmed.get('merged_at'))
    return item


def completed(checkpoint, options, client, now, *, changed):
    interval = max(options.interval_seconds, getattr(client, 'interval_floor', 0))
    delay = interval if changed or not getattr(client, 'interval_floor', 600) else checkpoint.get('poll_delay', 0) + interval
    checkpoint.update(fetched_at=now, error=None, retry_at=None, poll_delay=delay,
                      next_poll_at=(datetime.fromisoformat(now) + timedelta(seconds=delay)).isoformat(),
                      stale_after_seconds=delay + options.stale_after_seconds)


def synchronize(repo, old_items, old_checkpoint, client, options, now):
    items, checkpoint = dict(old_items), deepcopy(old_checkpoint)
    checkpoint.update(attempted_at=now, stale_after_seconds=options.stale_after_seconds)
    if checkpoint.get('retry_at') and datetime.fromisoformat(now) < datetime.fromisoformat(checkpoint['retry_at']):
        return items, old_checkpoint
    try:
        full = not checkpoint.get('reconciled_at') or (
            datetime.fromisoformat(now) - datetime.fromisoformat(checkpoint['reconciled_at'])).total_seconds() >= options.reconcile_seconds
        if not checkpoint.get('page'):
            probe, etag = client.get(f'/repos/{repo}/issues?state=all&sort=updated&direction=desc&per_page=1',
                                     etag=None if full or checkpoint.get('error') else checkpoint.get('etag'))
            if probe is None:
                completed(checkpoint, options, client, now, changed=False)
                return items, checkpoint
            checkpoint.update(etag=etag, page=1, round_started=now, full=full, seen=[], changed=False,
                              round_watermark=probe[0]['updated_at'] if probe else None)
        while True:
            params = {'state': 'all', 'sort': 'created' if checkpoint['full'] else 'updated', 'direction': 'asc', 'per_page': PAGE_SIZE, 'page': checkpoint['page']}
            if not checkpoint['full'] and checkpoint.get('since'):
                params['since'] = checkpoint['since']
            if 'batch' not in checkpoint:
                checkpoint['batch'], _ = client.get(f'/repos/{repo}/issues?' + urlencode(params))
            batch = checkpoint['batch']
            if not isinstance(batch, list):
                raise ValueError('Invalid GitHub issue list')
            # Cheap issue associations precede PR detail/file requests within each page.
            for raw in sorted(batch, key=lambda item: 'pull_request' in item):
                identity = str(raw['id'])
                processed = checkpoint.setdefault('processed', [])
                if identity not in processed:
                    previous = items.get(identity)
                    item = normalize(raw, repo, previous, client, refresh=checkpoint['full'] and raw['state'] == 'open',
                                     progress=checkpoint.setdefault('pr_progress', {}))
                    checkpoint['changed'] = checkpoint.get('changed', False) or item != previous
                    items[identity] = item
                    checkpoint.pop('pr_progress', None)
                    processed.append(identity)
            stamps = [datetime.fromisoformat(raw['updated_at']) for raw in batch]
            if checkpoint.get('latest_updated'):
                stamps.append(datetime.fromisoformat(checkpoint['latest_updated']))
            if stamps:
                checkpoint['latest_updated'] = max(stamps).isoformat()
            checkpoint['seen'] = sorted(set(checkpoint['seen']) | {str(raw['id']) for raw in batch})
            if len(batch) < PAGE_SIZE:
                if checkpoint['full']:
                    seen = set(checkpoint['seen'])
                    checkpoint['changed'] = checkpoint.get('changed', False) or any(
                        key not in seen and item.get('available') for key, item in items.items())
                    items = {key: value if key in seen else {**value, 'available': False} for key, value in items.items()}
                    checkpoint['reconciled_at'] = now
                # Provider timestamps define the cursor; a fast host clock must not
                # advance it past updates that GitHub has not returned yet.
                if checkpoint.get('latest_updated'):
                    watermark = datetime.fromisoformat(checkpoint['latest_updated'])
                    if checkpoint.get('round_watermark'):
                        watermark = min(watermark, datetime.fromisoformat(checkpoint['round_watermark']))
                    checkpoint['since'] = (watermark - timedelta(seconds=120)).isoformat()
                completed(checkpoint, options, client, now, changed=checkpoint.get('changed', False))
                for key in ('page', 'seen', 'round_started', 'full', 'processed', 'latest_updated', 'batch', 'changed', 'pr_progress', 'round_watermark'):
                    checkpoint.pop(key, None)
                return items, checkpoint
            checkpoint['page'] += 1
            checkpoint.pop('batch', None)
            checkpoint.pop('processed', None)
    except BudgetExhausted:
        checkpoint['error'] = None
    except RateLimited as error:
        checkpoint.update(error=str(error) if error.rejected else None, retry_at=error.retry_at)
    except Exception as error:
        # Exception messages can include authenticated request details. Store only
        # the error class; HTTP clients do not publish tokens or response bodies.
        checkpoint['error'] = 'GitHub synchronization: ' + type(error).__name__
    return items, checkpoint


def collect(config, db, *, client=None, now=None):
    options = settings(config)
    now = now or state.utcnow()
    with state.writer_lock(str(db) + '.github'):
        old = state.read(db)
        if not options.repositories and not old.get('github_repositories'):
            return old
        records = dict(old.get('github_items', {}))
        components = {}
        auth_retry_at = max((component.get('auth_retry_at', 0) for key, component in old['components'].items()
                             if key.startswith('github:')), default=0)
        owner = client or Client(options.request_budget, auth_retry_at=auth_retry_at)
        try:
            ordered = sorted(options.repositories, key=lambda repo: old['components'].get('github:' + repo, {}).get('attempted_at', ''))
            for repo in ordered:
                if getattr(owner, 'remaining', None) == 0 or getattr(owner, 'retry_at', None):
                    break
                key = 'github:' + repo
                checkpoint = old['components'].get(key, {})
                if checkpoint.get('next_poll_at') and datetime.fromisoformat(now) < datetime.fromisoformat(checkpoint['next_poll_at']):
                    components[key] = checkpoint
                    continue
                records[repo], component = synchronize(repo, records.get(repo, {}), checkpoint, owner, options, now)
                if component is not checkpoint and component.get('next_poll_at') == checkpoint.get('next_poll_at'):
                    interval = max(options.interval_seconds, getattr(owner, 'interval_floor', 0))
                    component['next_poll_at'] = (datetime.fromisoformat(now) + timedelta(seconds=interval)).isoformat()
                if getattr(owner, 'retry_at', None):
                    component['retry_at'] = owner.retry_at
                component['auth_retry_at'] = getattr(owner, 'auth_retry_at', 0)
                components[key] = component
        finally:
            if client is None:
                owner.close()
        with state.writer_lock(db, timeout=60):
            previous = state.read_cached(db)
            latest = previous[0]
            cfg.require_unchanged(config)
            associations = {}
            for repo, policy in options.repositories.items():
                matcher = Matcher(latest, policy)
                for identity, item in records.get(repo, {}).items():
                    for name, evidence in matcher.match(item).items():
                        associations.setdefault(name, {})[repo + '#' + identity] = {
                            'repository': repo, 'id': identity, 'evidence': evidence}
            snapshot = state.merge(latest, 'github', {'github_items': records, 'github_links': associations,
                'github_repositories': list(options.repositories)}, components)
            for key in list(snapshot['components']):
                if key.startswith('github:') and key.removeprefix('github:') not in options.repositories:
                    del snapshot['components'][key]
            state.commit(db, snapshot, previous=previous)
    return snapshot
