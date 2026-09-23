"""Small public projections. Only here do saved facts become display conclusions."""
from datetime import datetime, timezone
from urllib.parse import quote
from . import config as cfg, state, monitor_model, build_status, version_status


def observed_at(observations):
    """Old successful observations remain dated; missing/invalid ones are unknown.

    Never use attempted_at: a failed poll must not advertise a fresh update.
    All members are required when aggregating flavors or collection components.
    """
    stamps = []
    for observation in observations:
        stamp = observation.get('fetched_at')
        try:
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None:
                return None
        except (ValueError, TypeError):
            return None
        stamps.append((parsed, stamp))
    return min(stamps, key=lambda item: item[0])[1] if stamps else None

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

def project(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    ttl = component_ttl(snapshot, 'nvchecker')
    obs_ttl = component_ttl(snapshot, 'builds')
    history_ttl = component_ttl(snapshot, 'build_history:')
    targets = snapshot.get('targets', [])
    inv = snapshot['inventory']
    flavors = {}
    for name, owner in inv.items():
        flavors.setdefault(owner, []).append(name)
    rows = []
    versions = version_status.evaluate_all(snapshot, now)
    obs = snapshot.get('obs', {})
    base = obs.get('web_url', '').rstrip('/')
    project_name = quote(obs.get('project', ''), safe='')
    for name, source in sorted(snapshot['sources'].items(), key=lambda kv: (kv[0].casefold(), kv[0])):
        source = dict(source)
        if source.get('version') and not state.usable_version(source['version']):
            source['raw_version'] = source['version']
            source['version'] = None
            source['version_error'] = 'OBS source version is unresolved; see raw_version'
        version = versions[name]
        binding, track, upstream = version.binding, version.track, version.upstream
        source_stale = state.stale(source, now, obs_ttl)
        current, relation = version.source, version.relation
        builds = []
        all_entries = []
        for target in targets:
            entries = []
            for flavor in flavors.get(name, [name]):
                fact = snapshot['builds'].get(flavor, {}).get(target['id'], {})
                raw_status = fact.get('raw_status', 'unknown')
                # Preserve old status but mark it stale immediately after a failed observation.
                is_stale = state.stale(fact, now, obs_ttl) or bool(fact.get('error'))
                status = build_status.describe(raw_status)
                url = f'{base}/package/live_build_log/{project_name}/{quote(flavor, safe="")}/{quote(target["repository"], safe="")}/{quote(target["architecture"], safe="")}' if base else None
                source_hash = snapshot.get('index', {}).get(flavor, {}).get('srcmd5')
                # Flavor source identities must not be guessed from their owner.
                matched = None if source_stale or is_stale else matching_success(fact, source, source_hash, now, history_ttl)
                entries.append({**fact, 'updated_at': observed_at([fact]), 'last_success': fact.get('last_success'), 'package': flavor, 'raw_status': raw_status, 'text': status.text, 'kind': status.kind, 'issue': status.issue,
                                'stale': is_stale, 'log_url': url, 'rank': status.rank, 'matches_source': matched})
            all_entries.extend(entries)
            chosen = min(entries, key=lambda e: e['rank'])
            builds.append(dict(target=target['id'], label=target['label'], repository=target['repository'],
                               architecture=target['architecture'], raw_status=chosen['raw_status'],
                               text=chosen['text'], kind=chosen['kind'], issue=any(e['issue'] for e in entries), log_url=chosen['log_url'],
                               stale=any(e['stale'] for e in entries), matches_source=combined_match(entries),
                               last_success=combined_success(entries), updated_at=observed_at(entries), flavors=entries))
        old_data = version.stale
        attention = relation in ('unknown', 'untracked') or old_data or any(b['stale'] or b['text'] == 'No result' for b in builds)
        spec_fact = snapshot.get('specs', {}).get(name, {})
        metadata = spec_fact.get('metadata')
        spec = {'metadata': ({k: metadata.get(k) for k in ('name', 'version', 'summary', 'license', 'url', 'description', 'buildsystem')} if metadata else None),
                'source_path': 'SPECS/' + name,
                'source_url': cfg.spec_source_url(spec_fact.get('source_origin') or {}, name, spec_fact.get('head') or (spec_fact.get('source_origin') or {}).get('branch') or ''),
                'changelog': spec_fact.get('changelog') or [],
                'head': spec_fact.get('head'), 'error': spec_fact.get('error')}
        active_successes = [e.get('last_success') for e in all_entries if e['raw_status'] not in ('disabled', 'excluded')]
        successful_versions = {f['version'] for f in active_successes if f and f.get('version')}
        previous_version = (next(iter(successful_versions)) if active_successes and all(f and f.get('version') for f in active_successes) and len(successful_versions) == 1 else None)
        buildsystem = metadata.get('buildsystem') if metadata and not spec_fact.get('error') else None
        buildsystem_status = ('declared' if buildsystem else 'not_declared') if metadata and 'buildsystem' in metadata and not spec_fact.get('error') else 'unknown'
        maintenance = monitor_model.project(snapshot, name, now, version=version)
        # Package version is the SPEC-declared value.  OBS remains the build
        # evidence below (matches_source/last_success), so a source mismatch
        # cannot silently relabel the shipped artifact.
        rows.append(dict(upstream_updated_at=observed_at([upstream]) if track else None, current_build_success=combined_match(all_entries), last_successful_version=previous_version, name=name, current=current.get('version'), obs_version=source.get('version'), latest=upstream.get('version'), relation=relation,
                         track=track, track_label=binding.get('track_label') or cfg.derive_track_label(name), stale=old_data,
                         needs_attention=attention, builds=builds, detail_url=f'/packages/{quote(name, safe="")}',
                         source=source, upstream=upstream, version_error=version.error, last_known_relation=version.last_known_relation,
                         watch=[dict(id=t, **snapshot['tracks'].get(t, {}),
                                     stale=state.stale(snapshot['tracks'].get(t, {}), now, ttl))
                                for t in binding.get('watch', [])],
                         spec=spec, buildsystem=buildsystem, buildsystem_status=buildsystem_status,
                         maintenance=maintenance['summary'], maintenance_findings=maintenance['findings'],
                         monitor_checks=maintenance['checks']))
    errors = [v['error'] for v in snapshot['components'].values() if v.get('error')]
    if snapshot.get('last_attempt') and any(state.stale(v, now, component_ttl(snapshot, k)) for k, v in snapshot['components'].items()):
        errors.append('collection observations are stale')
    components = snapshot['components']
    obs_updated = observed_at([components.get(key, {}) for key in ('targets', 'inventory', 'source_index', 'builds')])
    return rows, dict(obs_updated_at=obs_updated, upstream_updated_at=observed_at([components.get('nvchecker', {})]), last_attempt=snapshot['last_attempt'], mode=snapshot['mode'], errors=errors, generation=snapshot['generation'], packages=len(rows), tracked_packages=sum(bool(r['track']) for r in rows))

def summary(row):
    return {k: ([{bk: bv for bk, bv in b.items() if bk != 'flavors'} for b in v] if k == 'builds' else v)
            for k, v in row.items() if k not in ('source', 'upstream', 'version_error', 'watch', 'last_known_relation', 'spec', 'maintenance_findings', 'monitor_checks')}


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
