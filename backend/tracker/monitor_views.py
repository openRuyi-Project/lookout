"""Pure monitor projections. Collectors retain their batching and write ownership.

Every monitor supplies the same envelope and query dimensions. Payload kinds keep
source, version, build and evidence semantics distinct; no network imports here.
"""
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Callable
from urllib.parse import quote

from . import build_status, config as cfg, monitor_model, state, version_status


def observed_at(observations):
    """Old successful observations remain dated; missing/invalid ones are unknown.

    Never use attempted_at: a failed poll must not advertise a fresh update.
    All members are required when aggregating flavors or collection components.
    """
    return oldest_timestamp(observation.get('fetched_at') for observation in observations)


def oldest_timestamp(stamps):
    oldest, value = None, None
    for stamp in stamps:
        try:
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None:
                return None
        except (ValueError, TypeError):
            return None
        if oldest is None or parsed < oldest:
            oldest, value = parsed, stamp
    return value


def combined_success(entries):
    """Aggregate only unambiguous same-version/revision facts; details keep flavors."""
    facts = [entry.get('last_success') for entry in entries]
    if not facts or any(not fact for fact in facts):
        return None
    if len({(fact['version'], fact.get('srcmd5')) for fact in facts}) != 1:
        return None
    return max(facts, key=lambda fact: fact['time'])


def matching_success(fact, source, source_hash, now, history_ttl):
    if not source_hash or not source.get('version') or source.get('error') or fact.get('error'):
        return None
    last = fact.get('last_success')
    if last and last.get('srcmd5') == source_hash:
        return True
    # A stale/missing history or a newly succeeded result preceding history fetch
    # does not prove that the source has never succeeded. Keep this unknown.
    if (fact.get('raw_status') in ('succeeded', 'unknown', 'disabled', 'excluded')
            or fact.get('history_error') or fact.get('history_unresolved')
            or state.stale({'fetched_at': fact.get('history_checked_at')}, now, history_ttl)):
        return None
    return False


def combined_match(entries):
    relevant = [e for e in entries if e['raw_status'] not in ('disabled', 'excluded')]
    if not relevant:
        return None
    if any(e['matches_source'] is False for e in relevant):
        return False
    return True if all(e['matches_source'] is True for e in relevant) else None

def component_ttl(snapshot, key):
    """One freshness policy shared by projection and its cache deadline.

    A SPEC phase publishes its configured interval with its observation. Older
    snapshots without that identity keep their previous OBS-based semantics until
    the first SPEC collection. Two periods allow one scheduled refresh to finish;
    explicit fetch/parse failures remain errors regardless of this age allowance.
    """
    upstream = snapshot.get('stale_after_seconds', 86400)
    obs = snapshot.get('obs_stale_after_seconds', upstream)
    if key == 'nvchecker':
        return upstream
    if key.startswith('build_history:'):
        return max(obs, snapshot.get('build_history_interval_seconds', 300) * 2)
    if key == 'spec_git' and 'spec_interval_seconds' in snapshot:
        return max(obs, snapshot['spec_interval_seconds'] * 2)
    return obs


def check_state(observations, now, ttl):
    stamps = [f.get('checked_at') or f.get('fetched_at') for f in observations]
    attempts = [f.get('attempted_at') for f in observations if f.get('attempted_at')]
    errors = sorted({str(f['error']) for f in observations if f.get('error')})
    stale = any(state.stale(f, now, ttl) for f in observations)
    status = ('error' if errors else 'pending' if not stamps or not all(stamps)
              else 'expired' if stale else 'ok')
    return {
        'status': status,
        'stale': stale or bool(errors),
        'checked_at': oldest_timestamp(stamps),
        'attempted_at': max(attempts, default=None),
        'error': '; '.join(errors) or None,
    }


@dataclass(frozen=True)
class Context:
    snapshot: dict
    name: str
    now: datetime
    version: version_status.VersionStatus
    flavors: list[str]
    evidence: dict

    @property
    def obs_source(self):
        source = dict(self.snapshot['sources'][self.name])
        if source.get('version') and not state.usable_version(source['version']):
            source.update(raw_version=source['version'], version=None,
                          version_error='OBS source version is unresolved; see raw_version')
        return source


@dataclass(frozen=True)
class Monitor:
    id: str
    title: str
    kind: str
    project: Callable[[Context], dict]

    def describe(self):
        return {'id': self.id, 'title': self.title, 'kind': self.kind}

    def read(self, context):
        result = self.project(context)
        result['dimensions']['check:' + self.id] = [result['check']['status']]
        result['id'] = self.id
        result['title'] = self.title
        return result


def source(context):
    snapshot, name, current = context.snapshot, context.name, context.version.source
    spec = snapshot.get('specs', {}).get(name, {})
    metadata = spec.get('metadata')
    system = metadata.get('buildsystem') if metadata and not spec.get('error') else None
    system_status = (('declared' if system else 'not_declared')
                     if metadata and 'buildsystem' in metadata and not spec.get('error') else 'unknown')
    origin = spec.get('source_origin') or {}
    data = dict(kind='source', version=current.get('version'), revision=current.get('revision'),
                buildsystem=system, buildsystem_status=system_status,
                metadata=({k: metadata.get(k) for k in
                           ('name', 'version', 'summary', 'license', 'url', 'description', 'buildsystem')}
                          if metadata else None),
                source_path='SPECS/' + name,
                source_url=cfg.spec_source_url(origin, name, spec.get('head') or origin.get('branch') or ''),
                changelog=spec.get('changelog') or [], head=spec.get('head'), error=spec.get('error'),
                obs=context.obs_source)
    return dict(check=check_state([current], context.now, current['stale_after_seconds']),
                data=data, dimensions={'buildsystem': [system or '_not_detected']})


def version(context):
    value, snapshot = context.version, context.snapshot
    upstream = value.upstream
    ttl = component_ttl(snapshot, 'nvchecker')
    check = check_state([upstream], context.now, ttl)
    if value.binding.get('not_applicable'):
        check.update(status='not_applicable', stale=False)
    elif not value.track:
        check.update(status='not_configured', stale=False)
    views = []
    if value.upgrading:
        views.append('updates')
    if not value.track and value.relation != 'not_applicable':
        views.append('untracked')
    if value.relation in ('unknown', 'untracked') or value.stale:
        views.append('attention')
    data = dict(kind='version', current=value.source.get('version'), latest=upstream.get('version'),
                relation=value.relation, track=value.track,
                track_label=value.binding.get('track_label') or cfg.derive_track_label(context.name),
                stale=value.stale, error=value.error, last_known_relation=value.last_known_relation,
                upstream=upstream, updated_at=observed_at([upstream]) if value.track else None,
                watch=[dict(id=t, **snapshot['tracks'].get(t, {}),
                            stale=state.stale(snapshot['tracks'].get(t, {}), context.now, ttl))
                       for t in value.binding.get('watch', [])])
    return dict(check=check, data=data, dimensions={'view': views})


def build(context):
    snapshot, now, source = context.snapshot, context.now, context.obs_source
    ttl = component_ttl(snapshot, 'builds')
    history_ttl = component_ttl(snapshot, 'build_history:')
    source_stale = state.stale(source, now, ttl)
    base = snapshot.get('obs', {}).get('web_url', '').rstrip('/')
    project = quote(snapshot.get('obs', {}).get('project', ''), safe='')
    builds, all_entries, observations = [], [], []
    dimensions = {'view': []}
    for target in snapshot.get('targets', []):
        entries = []
        for flavor in context.flavors:
            fact = snapshot['builds'].get(flavor, {}).get(target['id'], {})
            observations.append(fact)
            raw_status = fact.get('raw_status', 'unknown')
            stale = state.stale(fact, now, ttl) or bool(fact.get('error'))
            status = build_status.describe(raw_status)
            url = (f'{base}/package/live_build_log/{project}/{quote(flavor, safe="")}/'
                   f'{quote(target["repository"], safe="")}/{quote(target["architecture"], safe="")}') if base else None
            source_hash = snapshot.get('index', {}).get(flavor, {}).get('srcmd5')
            matched = None if source_stale or stale else matching_success(fact, source, source_hash, now, history_ttl)
            entries.append({**fact, 'updated_at': observed_at([fact]), 'last_success': fact.get('last_success'),
                            'package': flavor, 'raw_status': raw_status, 'text': status.text, 'kind': status.kind,
                            'issue': status.issue, 'stale': stale, 'log_url': url, 'rank': status.rank,
                            'matches_source': matched})
        all_entries.extend(entries)
        chosen = min(entries, key=lambda entry: entry['rank'])
        item = dict(target=target['id'], label=target['label'], repository=target['repository'],
                    architecture=target['architecture'], raw_status=chosen['raw_status'], text=chosen['text'],
                    kind=chosen['kind'], issue=any(e['issue'] for e in entries), log_url=chosen['log_url'],
                    stale=any(e['stale'] for e in entries), matches_source=combined_match(entries),
                    last_success=combined_success(entries), updated_at=observed_at(entries), flavors=entries)
        builds.append(item)
        dimensions['build:' + target['id']] = [item['raw_status']] + (['issues'] if item['issue'] else [])
        if item['issue']:
            dimensions['view'].append('problems')
        if item['stale'] or item['raw_status'] == 'unknown':
            dimensions['view'].append('attention')
    successes = [e.get('last_success') for e in all_entries if e['raw_status'] not in ('disabled', 'excluded')]
    versions = {f['version'] for f in successes if f and f.get('version')}
    previous = (next(iter(versions)) if successes and all(f and f.get('version') for f in successes)
                and len(versions) == 1 else None)
    return dict(check=check_state(observations, now, ttl), dimensions=dimensions,
                data=dict(kind='build', targets=builds, source_version=context.version.source.get('version'),
                          source_success=combined_match(all_entries), last_successful_version=previous))


def evidence(context, monitor_id):
    facts = [f for f in context.evidence['findings'] if f['monitor'] == monitor_id]
    saved = next((c for c in context.evidence['checks'] if c['monitor'] == monitor_id), None)
    check = {k: v for k, v in (saved or {}).items() if k != 'monitor'}
    check.setdefault('status', 'pending')
    check['stale'] = any(f['stale'] for f in facts) or check['status'] in ('expired', 'input_changed', 'input_unavailable', 'schema_changed')
    labels = monitor_model.summarize(facts)
    return dict(check=check, dimensions={'maintenance': [label['label'] for label in labels]},
                data=dict(kind='evidence', findings=facts, labels=labels))


CORE = (
    Monitor('source', 'Source', 'source', source),
    Monitor('version', 'Version', 'version', version),
    Monitor('build', 'Build', 'build', build),
)


def registry(snapshot):
    saved = snapshot.get('monitor_catalog', {})
    ids = set(saved)
    for results in snapshot.get('monitors', {}).values():
        ids.update(results)
    # Old snapshots remain readable before the next collector publishes metadata.
    ids.difference_update(monitor_model.CORE_IDS)
    return (*CORE, *(Monitor(mid, saved.get(mid, {}).get('title') or mid, 'evidence',
                            partial(evidence, monitor_id=mid)) for mid in sorted(ids)))


def summary(result):
    data = result['data']
    if data['kind'] == 'source':
        data = {k: data[k] for k in ('kind', 'version', 'buildsystem', 'buildsystem_status')}
    elif data['kind'] == 'version':
        data = {k: v for k, v in data.items() if k not in ('watch', 'upstream')}
    elif data['kind'] == 'build':
        data = {**data, 'targets': [{k: v for k, v in b.items() if k != 'flavors'} for b in data['targets']]}
    else:
        data = {k: v for k, v in data.items() if k != 'findings'}
    return {k: (data if k == 'data' else v) for k, v in result.items() if k != 'dimensions'}
