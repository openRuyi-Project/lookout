"""Internal monitor runner and contributor CLI. HTTP imports neither this nor adapters."""
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from tracker import config as cfg, identity as package_identity, state
from tracker.monitors.model import CORE_IDS, fingerprint, validate_findings
from tracker.monitors.registry import REGISTRY
from tracker.monitors.schedule import Schedule
from tracker.monitors.version import compare as version_status
from tracker.providers.client import IO


DEFAULT_REFRESH = Schedule(interval_seconds=21600)


def settings(config):
    raw = config.get('monitors', {})
    if set(raw) - {'enabled', 'stale_after_seconds', 'batch_size', 'workers', 'heartbeat_seconds', 'refresh'}:
        raise ValueError('unknown monitor runner setting')
    result = {'enabled': [], 'stale_after_seconds': 86400,
              'batch_size': 256, 'workers': 4, 'heartbeat_seconds': 30, 'refresh': {}, **raw}
    if not isinstance(result['enabled'], list) or len(set(result['enabled'])) != len(result['enabled']) or set(result['enabled']) - set(REGISTRY):
        raise ValueError('enabled monitor must be registered')
    if set(result['enabled']) & CORE_IDS:
        raise ValueError('monitor identity is reserved for a core observation')
    for key, maximum in [('stale_after_seconds', 2592000), ('batch_size', 10000), ('workers', 8), ('heartbeat_seconds', 3600)]:
        if key not in result:
            continue
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError('invalid monitor setting: ' + key)
    if not isinstance(result['refresh'], dict) or set(result['refresh']) - set(REGISTRY):
        raise ValueError('refresh requires registered monitor IDs')
    for values in result['refresh'].values():
        policy = DEFAULT_REFRESH.override(values)
        if 'interval_seconds' in values and policy.interval_seconds >= result['stale_after_seconds']:
            raise ValueError('monitor stale threshold must exceed interval')
    return result


def polling(config):
    return Schedule(settings(config)['heartbeat_seconds'], 60, 300)


def refresh_policy(provider, proposed, previous, options):
    """Module code chooses policy; operator values override timing, not evidence."""
    refresh = getattr(REGISTRY[provider], 'refresh', None)
    policy = refresh(proposed['subject'], proposed['inputs'], previous) if refresh else DEFAULT_REFRESH
    if not isinstance(policy, Schedule):
        raise ValueError('monitor refresh() must return Schedule')
    policy = policy.override(options.get('refresh', {}).get(provider, {}))
    if policy.interval_seconds >= options.get('stale_after_seconds', 86400):
        raise ValueError('monitor stale threshold must exceed interval')
    return policy


def plan(config, snapshot, name, provider, *, version=None):
    adapter = REGISTRY[provider]
    version = version or version_status.evaluate(snapshot, name)
    scope = getattr(adapter, 'SCOPE', 'current')
    if scope not in ('current', 'upgrade', 'current_and_upgrade'):
        raise ValueError('invalid monitor scope')
    current = version.subject
    if scope == 'upgrade':
        current['target_version'] = version.target_version
    if scope == 'current_and_upgrade':
        active = cfg.binding(config, name)
        fields = {'compare': None, 'comparable': True, 'not_applicable': False}
        policy_matches = all(active.get(k, v) == version.binding.get(k, v) for k, v in fields.items())
        current['target_version'] = version.target_version if version.upgrading and policy_matches else None
    identity = cfg.public_source(config.get('native', {}).get(version.track, {}))
    configured = config.get('packages', {}).get(name, {}).get('monitors', {}).get(provider)
    # Adapters only see their package context, not snapshot/config/storage handles.
    try:
        inputs = adapter.inputs({**current, 'identity': identity,
                                 'source_release': version.release.public() if version.release else None}, configured)
        if inputs is not None and not isinstance(inputs, dict):
            raise ValueError('monitor inputs must be a mapping or None')
        json.dumps(inputs, sort_keys=True, allow_nan=False)
        input_error = None
    except Exception as error:
        # A bad package identity must not abort checks for other packages/providers.
        inputs, input_error = None, type(error).__name__
    note, status = None, 'pending'
    if input_error:
        status, note = 'error', 'Monitor input configuration could not be prepared.'
    elif inputs is None:
        status, note = 'not_configured', 'No reliable monitor identity/configuration.'
    elif (version.identity_conflict or (version.release
          and package_identity.from_native(identity) not in (None, version.release.identity))):
        status, note = 'unsupported', 'Source0 and configured upstream identities disagree.'
    elif (not current['version'] or not current['revision']
          or version.source.get('error') or version.source_stale):
        status, note = 'unsupported', 'Current source version/revision is not established.'
    elif scope == 'upgrade':
        active = cfg.binding(config, name)
        fields = {'compare': None, 'comparable': True, 'not_applicable': False}
        if any(active.get(key, default) != version.binding.get(key, default) for key, default in fields.items()):
            status, note = 'unsupported', 'Version policy changed; waiting for upstream collection.'
        elif version.relation == 'unknown':
            status, note = 'unsupported', 'Version comparison is unavailable.'
        elif not version.upgrading:
            status, note = 'not_applicable', 'No confirmed version upgrade; this monitor is not run.'
    fingerprint_inputs = ({'invalid_configuration': repr(configured), 'identity': identity}
                          if input_error else inputs)
    query_subject = current
    try:
        select = getattr(adapter, 'query_subject', None)
        if select and inputs is not None and not input_error:
            query_subject = select(current, inputs)
        fp = fingerprint({'provider': provider, 'adapter_version': adapter.VERSION,
                          'subject': query_subject, 'inputs': fingerprint_inputs})
    except Exception as error:
        status, note, input_error = 'error', 'Monitor query input could not be prepared.', type(error).__name__
        fp = fingerprint({'provider': provider, 'subject': current, 'inputs': fingerprint_inputs,
                          'adapter_version': adapter.VERSION, 'query_error': input_error})
    return {'subject': current, 'scope': scope, 'fingerprint': fp, 'inputs': inputs,
            'adapter_version': adapter.VERSION,
            'status': status, 'input_status': status, 'note': note, 'findings': [], 'error': input_error}


def evidence_revision(subject, findings):
    # Findings/facts and identifier lists have no ranking semantics. Provider
    # response ordering must not turn the same evidence into a new revision.
    normalized = []
    for finding in findings:
        facts = [{**fact, 'value': sorted(set(fact['value']))
                  if isinstance(fact['value'], list) else fact['value']}
                 for fact in finding['facts']]
        normalized.append({**finding, 'facts': sorted(facts, key=fingerprint),
                           'tags': sorted(set(finding['tags']))})
    return fingerprint({'subject': subject, 'findings': sorted(normalized, key=lambda f: f['id'])})


def same_scope(proposed, previous, scope):
    """A target change is not a change to the current release query."""
    if (not previous or previous.get('scope') != 'current_and_upgrade'
            or previous.get('adapter_version') != proposed.get('adapter_version')
            or previous.get('inputs') != proposed['inputs']):
        return False
    fields = ('version',) if scope == 'current' else ('version', 'target_version')
    return all(previous['subject'].get(k) == proposed['subject'].get(k) for k in fields)


def retain_current(proposed, previous):
    if proposed['scope'] != 'current_and_upgrade' or not same_scope(proposed, previous, 'current'):
        return proposed
    check = previous.get('scope_checks', {}).get('current')
    if not check:
        return proposed
    return {**proposed, 'findings': [f for f in previous.get('findings', []) if f['scope'] == 'current'],
            'scope_checks': {'current': check}, 'checked_at': check.get('checked_at')}


def retain_scopes(output, proposed, previous, at):
    """Failure retains dated evidence for that release only, never refreshes it."""
    expected = {'current'} | ({'upgrade'} if proposed['subject'].get('target_version') else set())
    if set(output['scope_checks']) != expected:
        raise ValueError('monitor release checks mismatch')
    checks, findings = {}, list(output['findings'])
    for scope, check in output['scope_checks'].items():
        if set(check) != {'status', 'note'} or check['status'] not in ('ok', 'error', 'unsupported'):
            raise ValueError('invalid monitor release check')
        old = previous.get('scope_checks', {}).get(scope, {}) if same_scope(proposed, previous, scope) else {}
        checks[scope] = {**check, 'attempted_at': at,
                         'checked_at': at if check['status'] == 'ok' else old.get('checked_at')}
        if check['status'] != 'ok' and old:
            findings.extend(f for f in previous.get('findings', []) if f['scope'] == scope)
    return {**output, 'scope_checks': checks, 'findings': findings}


def failed(proposed, previous, error, at):
    """A failed policy/check retains only evidence for this exact input."""
    old = previous if previous and previous.get('fingerprint') == proposed['fingerprint'] else {}
    result = {**proposed, 'status': 'error', 'attempted_at': at,
            'failures': old.get('failures', 0) + 1,
            'checked_at': old.get('checked_at'), 'findings': old.get('findings', []),
            'evidence_revision': old.get('evidence_revision'), 'changed_at': old.get('changed_at'),
            'error': type(error).__name__, 'note': 'Check failed; matching prior findings are retained, not refreshed.'}
    if proposed['scope'] == 'current_and_upgrade':
        scopes = [scope for scope in ('current', 'upgrade') if same_scope(proposed, previous, scope)]
        result['findings'] = [f for f in (previous or {}).get('findings', []) if f['scope'] in scopes]
        result['scope_checks'] = {scope: {**previous.get('scope_checks', {}).get(scope, {}),
            'status': 'error', 'note': result['note'], 'attempted_at': at} for scope in scopes}
    return result


def execute(provider, proposed, io, previous=None, *, schedule=None):
    if proposed['status'] != 'pending':
        if (proposed['status'] == 'unsupported' and previous
                and previous.get('fingerprint') == proposed['fingerprint']
                and (previous.get('attempted_at') or previous.get('checked_at'))):
            # Eligibility is not a new provider result. Keep the dated result so
            # recovery of the same input can reuse it until its own refresh is due.
            return {**previous, 'subject': proposed['subject'],
                    'input_status': proposed['status'], 'input_note': proposed['note']}
        # Source unavailability also gates the upgrade and changes its query.
        # Preserve only matching current evidence, dated and input-unavailable.
        return retain_current(proposed, previous) if proposed['status'] == 'unsupported' else proposed
    at = state.utcnow()
    old = previous if previous and previous.get('fingerprint') == proposed['fingerprint'] else {}
    try:
        policy = schedule or refresh_policy(provider, proposed, old, {})
        # A short module recheck must not silently reuse the transport's six-hour cache.
        max_age = policy.interval_seconds
        if old.get('status') in ('error', 'partial'):
            max_age = min(max_age, policy.retry_seconds)
        scoped_io = io.for_hosts(REGISTRY[provider].HOSTS, max_age=max_age)
        output = REGISTRY[provider].check(proposed['subject'], proposed['inputs'], scoped_io)
        expected_keys = {'status', 'findings', 'note'}
        combined = proposed['scope'] == 'current_and_upgrade'
        if combined:
            expected_keys.add('scope_checks')
        allowed_statuses = ('ok', 'partial', 'unsupported', 'error') if combined else ('ok', 'partial', 'unsupported')
        if set(output) != expected_keys or output['status'] not in allowed_statuses:
            raise ValueError('invalid monitor result')
        validate_findings(output['findings'])
        for f in output['findings']:
            if f['scope'] != proposed['scope'] and not (combined and f['scope'] in ('current', 'upgrade')):
                raise ValueError('monitor finding scope mismatch')
            if f['scope'] == 'upgrade' and f['target_version'] != proposed['subject'].get('target_version'):
                raise ValueError('monitor finding target mismatch')
        if combined:
            output = retain_scopes(output, proposed, previous, at)
        revision = evidence_revision(
            {'input_fingerprint': proposed['fingerprint']}, output['findings'])
        unchanged = old.get('evidence_revision') == revision
        if unchanged:
            output['findings'] = old['findings']
        checked_at = (max((c['checked_at'] for c in output['scope_checks'].values() if c.get('checked_at')), default=None)
                      if combined else at)
        return {**proposed, **output, 'attempted_at': at, 'checked_at': checked_at,
                'failures': old.get('failures', 0) + 1 if output['status'] in ('partial', 'error') else 0,
                'evidence_revision': revision,
                'changed_at': old.get('changed_at') if unchanged else at}
    except Exception as error:
        return failed(proposed, previous, error, at)


def check(config, snapshot, name, provider, io):
    if provider not in REGISTRY or name not in snapshot.get('sources', {}):
        raise ValueError('unknown package or monitor')
    proposed = plan(config, snapshot, name, provider)
    return execute(provider, proposed, io, schedule=refresh_policy(provider, proposed, {}, settings(config)))


def collect(config, config_path, db, *, io=None):
    options = settings(config)
    own_io = io is None
    io = io or IO(Path(db).parent / 'monitor-cache', workers=options['workers'])
    try:
        with state.writer_lock(str(db) + '.monitors'):
            snapshot = state.read(db)
            observations, jobs = {}, []
            now = datetime.now(timezone.utc)
            versions = version_status.evaluate_all(snapshot, now)
            for name, version in versions.items():
                observations[name] = {}
                for provider in options['enabled']:
                    proposed = plan(config, snapshot, name, provider, version=version)
                    previous = snapshot.get('monitors', {}).get(name, {}).get(provider, {})
                    same = previous.get('fingerprint') == proposed['fingerprint']
                    if proposed['status'] != 'pending':
                        observations[name][provider] = execute(provider, proposed, io, previous)
                    else:
                        try:
                            policy = refresh_policy(provider, proposed, previous, options)
                        except Exception as error:
                            observations[name][provider] = failed(proposed, previous, error, state.utcnow())
                            continue
                        pending = previous.get('status') == 'pending'
                        observations[name][provider] = ({**previous, 'subject': proposed['subject'],
                                                         'input_status': 'pending', 'input_note': None}
                                                        if same else retain_current(proposed, previous))
                        if (policy.due(previous, proposed['fingerprint'], now)
                                or pending):
                            jobs.append((previous.get('attempted_at', '') if same else '', provider, name, proposed, previous, policy))
            # Retry failures fairly; never let a bad first package monopolize batches.
            jobs.sort(key=lambda row: row[:3])
            queues = defaultdict(deque)
            for row in jobs:
                queues[row[1]].append(row)
            selected_jobs = []
            while queues and len(selected_jobs) < options['batch_size']:
                for provider in list(queues):
                    if len(selected_jobs) == options['batch_size']:
                        break
                    selected_jobs.append(queues[provider].popleft())
                    if not queues[provider]:
                        del queues[provider]
            catalog = {mid: {'title': getattr(REGISTRY[mid], 'TITLE', mid)} for mid in options['enabled']}
            dependency_packages = config.get('openruyi', {}).get('dependencies', {})
            def publish():
                with state.writer_lock(db, timeout=60):
                    cfg.require_unchanged(config, config_path)
                    latest = state.read(db)
                    if latest.get('obs') != snapshot.get('obs'):
                        raise ValueError('monitor source scope changed during collection')
                    valid = {}
                    versions = version_status.evaluate_all(latest)
                    for name, providers in observations.items():
                        if name not in latest.get('sources', {}):
                            continue
                        valid[name] = {}
                        for provider, fact in providers.items():
                            expected = plan(config, latest, name, provider, version=versions[name])
                            if expected['fingerprint'] != fact['fingerprint']:
                                valid[name][provider] = retain_current(expected, fact)
                            elif expected['status'] != 'pending':
                                valid[name][provider] = execute(provider, expected, io, fact)
                            else:
                                # A packaging-only revision may change during the request.
                                # Keep the original check time; rebind only identical query inputs.
                                valid[name][provider] = {**fact, 'subject': expected['subject'],
                                                        'input_status': 'pending', 'input_note': None}
                    if (latest.get('monitors') == valid and latest.get('monitor_catalog') == catalog and
                            latest.get('dependency_packages', {}) == dependency_packages and
                            latest.get('monitor_stale_after_seconds') == options['stale_after_seconds']):
                        return latest
                    result = state.merge(latest, 'monitors', {'monitors': valid, 'monitor_catalog': catalog,
                        'dependency_packages': dependency_packages,
                        'monitor_stale_after_seconds': options['stale_after_seconds']})
                    state.commit(db, result)
                return result
            # Publish input invalidation and completed checks without waiting for
            # the slowest provider; coalesce writes just like upstream collection.
            result = publish()
            if not selected_jobs:
                return result
            last_published = 0.0
            unpublished = False
            with ThreadPoolExecutor(max_workers=options['workers']) as pool:
                pending = {pool.submit(execute, provider, proposed, io, previous, schedule=policy): (name, provider)
                           for _, provider, name, proposed, previous, policy in selected_jobs}
                for future in as_completed(pending):
                    name, provider = pending[future]
                    observations[name][provider] = future.result()
                    unpublished = True
                    if time.monotonic() - last_published >= 5:
                        result = publish()
                        unpublished = False
                        last_published = time.monotonic()
            return publish() if unpublished else result
    finally:
        if own_io:
            io.close()
