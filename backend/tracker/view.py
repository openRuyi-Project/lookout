"""Read-only package composition and the published v1 compatibility projection."""
from datetime import datetime, timezone
from urllib.parse import quote
from . import state, monitor_model, monitor_views, version_status
from .monitor_views import observed_at, component_ttl


def next_transition(snapshot, now):
    """Earliest freshness boundary for this snapshot, not an arbitrary cache TTL.

    The observation set is conservative (unused track/history facts may shorten
    caching, never prolong it). Deduplicate timestamps before date arithmetic:
    thousands of source/build entries commonly share a single poll timestamp.
    """
    ttl = component_ttl(snapshot, 'nvchecker')
    obs_ttl = component_ttl(snapshot, 'builds')
    history_ttl = component_ttl(snapshot, 'build_history:')
    checks = set()
    def observe(fact, lifetime):
        stamp = fact.get('checked_at') or fact.get('fetched_at')
        if isinstance(stamp, str) and stamp:
            checks.add((stamp, lifetime))
    for fact in snapshot['sources'].values():
        observe(fact, obs_ttl)
    for name in snapshot.get('specs', {}):
        fact = state.current_source(snapshot, name)
        observe(fact, fact['stale_after_seconds'])
    for fact in snapshot['tracks'].values():
        observe(fact, ttl)
    for targets in snapshot['builds'].values():
        for fact in targets.values():
            observe(fact, obs_ttl)
            observe({'fetched_at': fact.get('history_checked_at')}, history_ttl)
    for key, fact in snapshot['components'].items():
        observe(fact, component_ttl(snapshot, key))
    for providers in snapshot.get('monitors', {}).values():
        for fact in providers.values():
            observe(fact, snapshot.get('monitor_stale_after_seconds', 86400))
    changes = (state.next_stale_change({'fetched_at': stamp}, now, lifetime) for stamp, lifetime in checks)
    return min((change for change in changes if change is not None), default=None)

def project_monitors(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    flavors = {}
    for name, owner in snapshot['inventory'].items():
        flavors.setdefault(owner, []).append(name)
    modules = monitor_views.registry(snapshot)
    versions = version_status.evaluate_all(snapshot, now)
    rows = []
    for name in sorted(snapshot['sources'], key=lambda n: (n.casefold(), n)):
        version = versions[name]
        context = monitor_views.Context(snapshot, name, now, version, flavors.get(name, [name]),
                                        monitor_model.project(snapshot, name, now, version=version))
        results = {module.id: module.read(context) for module in modules}
        rows.append(dict(name=name, detail_url=f'/packages/{quote(name, safe="")}', monitors=results))
    return rows, collection(snapshot, rows, now)


def collection(snapshot, rows, now):
    errors = [v['error'] for v in snapshot['components'].values() if v.get('error')]
    if snapshot.get('last_attempt') and any(state.stale(v, now, component_ttl(snapshot, k))
                                          for k, v in snapshot['components'].items()):
        errors.append('collection observations are stale')
    components = snapshot['components']
    return dict(obs_updated_at=observed_at([components.get(k, {}) for k in
                                                ('targets', 'inventory', 'source_index', 'builds')]),
                      upstream_updated_at=observed_at([components.get('nvchecker', {})]),
                      last_attempt=snapshot['last_attempt'], mode=snapshot['mode'], errors=errors,
                      generation=snapshot['generation'], packages=len(rows),
                      tracked_packages=sum(bool(r['monitors']['version']['data']['track']) for r in rows))


def refresh_build_clock(snapshot, rows, now):
    """Only used before the cached projection's next semantic time boundary.

    Storage proved the entire successful status vector unchanged. Reuse source,
    version and evidence projections; their freshness deadlines remain enforced.
    """
    stamp = snapshot['components']['builds']['fetched_at']
    updated = []
    for row in rows:
        build = row['monitors']['build']
        targets = [{**target, 'updated_at': stamp,
                    'flavors': [{**fact, 'fetched_at': stamp, 'attempted_at': stamp, 'updated_at': stamp}
                                for fact in target['flavors']]} for target in build['data']['targets']]
        build = {**build, 'check': {**build['check'], 'checked_at': stamp, 'attempted_at': stamp},
                 'data': {**build['data'], 'targets': targets}}
        updated.append({**row, 'monitors': {**row['monitors'], 'build': build}})
    return updated, collection(snapshot, updated, now)


def legacy_package(row):
    """Published v1 clients retain their field names; all semantics come from monitors."""
    modules = row['monitors']
    source, version, build = (modules[k]['data'] for k in ('source', 'version', 'build'))
    evidence = [r for r in modules.values() if r['data']['kind'] == 'evidence']
    findings = [f for r in evidence for f in r['data']['findings']]
    return dict(name=row['name'], detail_url=row['detail_url'], current=source['version'],
                obs_version=source['obs'].get('version'), latest=version['latest'], relation=version['relation'],
                track=version['track'], track_label=version['track_label'], stale=version['stale'],
                current_build_success=build['source_success'], last_successful_version=build['last_successful_version'],
                upstream_updated_at=version['updated_at'], builds=build['targets'],
                needs_attention=any('attention' in r['dimensions'].get('view', []) for r in modules.values()),
                source=source['obs'], upstream=version['upstream'], version_error=version['error'],
                last_known_relation=version['last_known_relation'], watch=version['watch'],
                spec={k: source[k] for k in ('metadata', 'source_path', 'source_url', 'changelog', 'head', 'error')},
                buildsystem=source['buildsystem'], buildsystem_status=source['buildsystem_status'],
                maintenance=monitor_model.summarize(findings), maintenance_findings=findings,
                monitor_checks=[dict(monitor=r['id'], **{k: v for k, v in r['check'].items() if k != 'stale'})
                                for r in evidence if 'checked_at' in r['check']])


def project(snapshot, now=None):
    rows, collection = project_monitors(snapshot, now)
    return [legacy_package(row) for row in rows], collection


def summary(row):
    return {k: ([{bk: bv for bk, bv in b.items() if bk != 'flavors'} for b in v] if k == 'builds' else v)
            for k, v in row.items() if k not in ('source', 'upstream', 'version_error', 'watch',
                                                'last_known_relation', 'spec', 'maintenance_findings', 'monitor_checks')}


def monitor_summary(row):
    return {**row, 'monitors': {mid: monitor_views.summary(result) for mid, result in row['monitors'].items()}}


def upstream_failures(rows):
    """Group identical provider errors for triage, without claiming a common cause."""
    from urllib.parse import urlsplit
    groups = {}
    for row in rows:
        observation = row.get('upstream') or {}
        error = observation.get('error')
        if not error:
            continue
        source = observation.get('source') or {}
        provider = source.get('source') or 'unknown'
        url = source.get('url') or source.get('git')
        if isinstance(url, str):
            try:
                provider = urlsplit(url).hostname or provider
            except ValueError:
                pass  # Retain the provider kind if saved URL evidence is malformed.
        key = (provider, error)
        group = groups.setdefault(key, {'provider': provider, 'error': error, 'packages': []})
        group['packages'].append(row['name'])
    return [{**group, 'count': len(group['packages']), 'packages': sorted(group['packages'])}
            for _, group in sorted(groups.items())]
