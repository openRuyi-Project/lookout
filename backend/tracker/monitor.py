"""Internal monitor runner and contributor CLI. HTTP imports neither this nor adapters."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from typing import Protocol
from . import config as cfg, state, monitor_eol, monitor_security, monitor_license, version_status
from .monitor_io import IO, ProviderIO
from .monitor_model import CORE_IDS, fingerprint, validate_findings
from .schedule import Schedule

class Adapter(Protocol):
    """Module contract, without inheritance. Optional refresh(subject, inputs,
    previous) returns Schedule; modules without it use DEFAULT_REFRESH.
    Optional query_subject(subject, inputs) selects the subject fields used by
    check(). The safe default includes the complete source context.
    """
    VERSION: int
    HOSTS: set[str]

    def inputs(self, package: dict, configured: dict | None) -> dict | None: ...

    def check(self, subject: dict, inputs: dict, io: ProviderIO) -> dict: ...


# Explicit trusted modules, not dynamic imports from configuration. A new monitor
# adds one adapter here; scheduling, storage, API and frontend stay unchanged.
REGISTRY: dict[str, Adapter] = {'eol': monitor_eol, 'security': monitor_security, 'license': monitor_license}
DEFAULT_REFRESH = Schedule(interval_seconds=21600)


def settings(config):
    raw = config.get('monitors', {})
    if set(raw) - {'enabled', 'interval_seconds', 'stale_after_seconds', 'batch_size', 'workers', 'heartbeat_seconds', 'refresh'}:
        raise ValueError('unknown monitor runner setting')
    result = {'enabled': [], 'stale_after_seconds': 86400,
              'batch_size': 256, 'workers': 4, 'heartbeat_seconds': 30, 'refresh': {}, **raw}
    if not isinstance(result['enabled'], list) or len(set(result['enabled'])) != len(result['enabled']) or set(result['enabled']) - set(REGISTRY):
        raise ValueError('enabled monitor must be registered')
    if set(result['enabled']) & CORE_IDS:
        raise ValueError('monitor identity is reserved for a core observation')
    for key, maximum in [('interval_seconds', 604800), ('stale_after_seconds', 2592000), ('batch_size', 10000), ('workers', 8), ('heartbeat_seconds', 3600)]:
        if key not in result:
            continue
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError('invalid monitor setting: ' + key)
    if result['stale_after_seconds'] <= result.get('interval_seconds', 0):
        raise ValueError('monitor stale threshold must exceed interval')
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
    # Preserve explicit settings in older operational configurations.
    if 'interval_seconds' in options:
        policy = policy.override({'interval_seconds': options['interval_seconds']})
    policy = policy.override(options.get('refresh', {}).get(provider, {}))
    if policy.interval_seconds >= options.get('stale_after_seconds', 86400):
        raise ValueError('monitor stale threshold must exceed interval')
    return policy


def plan(config, snapshot, name, provider, *, version=None):
    adapter = REGISTRY[provider]
    version = version or version_status.evaluate(snapshot, name)
    scope = getattr(adapter, 'SCOPE', 'current')
    if scope not in ('current', 'upgrade'):
        raise ValueError('invalid monitor scope')
    current = version.subject
    if scope == 'upgrade':
        current['target_version'] = version.upstream.get('version')
    identity = cfg.public_source(config.get('native', {}).get(version.track, {}))
    configured = config.get('packages', {}).get(name, {}).get('monitors', {}).get(provider)
    # Adapters only see their package context, not snapshot/config/storage handles.
    try:
        inputs = adapter.inputs({**current, 'identity': identity}, configured)
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


def failed(proposed, previous, error, at):
    """A failed policy/check retains only evidence for this exact input."""
    old = previous if previous and previous.get('fingerprint') == proposed['fingerprint'] else {}
    return {**proposed, 'status': 'error', 'attempted_at': at,
            'failures': old.get('failures', 0) + 1,
            'checked_at': old.get('checked_at'), 'findings': old.get('findings', []),
            'evidence_revision': old.get('evidence_revision'), 'changed_at': old.get('changed_at'),
            'error': type(error).__name__, 'note': 'Check failed; matching prior findings are retained, not refreshed.'}


def execute(provider, proposed, io, previous=None, *, schedule=None):
    if proposed['status'] != 'pending':
        if (proposed['status'] == 'unsupported' and previous
                and previous.get('fingerprint') == proposed['fingerprint']
                and (previous.get('attempted_at') or previous.get('checked_at'))):
            # Eligibility is not a new provider result. Keep the dated result so
            # recovery of the same input can reuse it until its own refresh is due.
            status = previous['status']
            if (status == previous.get('input_status') == 'unsupported'
                    and 'input_note' not in previous):
                status = 'pending'  # Legacy gate records lost their provider status.
            return {**previous, 'status': status, 'subject': proposed['subject'],
                    'input_status': proposed['status'], 'input_note': proposed['note']}
        return proposed
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
        if set(output) != {'status', 'findings', 'note'} or output['status'] not in ('ok', 'partial', 'unsupported'):
            raise ValueError('invalid monitor result')
        validate_findings(output['findings'])
        for f in output['findings']:
            if f['scope'] != proposed['scope']:
                raise ValueError('monitor finding scope mismatch')
            if f['scope'] == 'upgrade' and f['target_version'] != proposed['subject'].get('target_version'):
                raise ValueError('monitor finding target mismatch')
        revision = evidence_revision(
            {'input_fingerprint': proposed['fingerprint']}, output['findings'])
        unchanged = old.get('evidence_revision') == revision
        if unchanged:
            output['findings'] = old['findings']
        return {**proposed, **output, 'attempted_at': at, 'checked_at': at,
                'failures': old.get('failures', 0) + 1 if output['status'] == 'partial' else 0,
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
    io = io or IO(Path(db).parent / 'monitor-cache')
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
                        lost_result = (previous.get('status') == 'pending' or
                                       previous.get('input_status') == previous.get('status') == 'unsupported'
                                       and 'input_note' not in previous)
                        observations[name][provider] = ({**previous, 'subject': proposed['subject'],
                                                         'status': 'pending' if lost_result else previous['status'],
                                                         'input_status': 'pending', 'input_note': None}
                                                        if same else proposed)
                        # Older records replaced the provider status with the gate;
                        # their lost result must be checked once, never guessed back.
                        if (policy.due(previous, proposed['fingerprint'], now)
                                or lost_result):
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
            def publish():
                active = cfg.load(config_path)
                if (active['config_digest'], active['nv_digest']) != (config['config_digest'], config['nv_digest']):
                    raise ValueError('monitor configuration changed during collection')
                with state.writer_lock(db, timeout=60):
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
                                valid[name][provider] = expected
                            elif expected['status'] != 'pending':
                                valid[name][provider] = execute(provider, expected, io, fact)
                            else:
                                # A packaging-only revision may change during the request.
                                # Keep the original check time; rebind only identical query inputs.
                                valid[name][provider] = {**fact, 'subject': expected['subject'],
                                                        'input_status': 'pending', 'input_note': None}
                    if (latest.get('monitors') == valid and latest.get('monitor_catalog') == catalog and
                            latest.get('monitor_stale_after_seconds') == options['stale_after_seconds']):
                        return latest
                    result = state.merge(latest, 'monitors', {'monitors': valid, 'monitor_catalog': catalog,
                        'monitor_stale_after_seconds': options['stale_after_seconds']})
                    state.commit(db, result)
                return result
            # Publish input invalidation and completed checks without waiting for
            # the slowest provider; coalesce writes just like upstream collection.
            publish()
            last_published = 0.0
            with ThreadPoolExecutor(max_workers=options['workers']) as pool:
                pending = {pool.submit(execute, provider, proposed, io, previous, schedule=policy): (name, provider)
                           for _, provider, name, proposed, previous, policy in selected_jobs}
                for future in as_completed(pending):
                    name, provider = pending[future]
                    observations[name][provider] = future.result()
                    if time.monotonic() - last_published >= 5:
                        publish(); last_published = time.monotonic()
            return publish()
    finally:
        if own_io:
            io.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('list', 'explain', 'check'))
    p.add_argument('name', nargs='?'); p.add_argument('--monitor')
    p.add_argument('--config', required=True); p.add_argument('--db', required=True)
    args = p.parse_args(argv)
    snapshot = state.read(args.db); config = cfg.load(args.config)
    options = settings(config)
    if args.action == 'list':
        result = {k: {'module': v.__name__, 'scope': getattr(v, 'SCOPE', 'current'),
                      'enabled': k in options['enabled'], 'adapter_version': v.VERSION} for k, v in REGISTRY.items()}
    else:
        if args.name not in snapshot.get('sources', {}) or args.monitor not in REGISTRY:
            p.error('explain/check require an observed NAME --monitor REGISTERED_ID')
        proposed = plan(config, snapshot, args.name, args.monitor)
        from .package import location
        result = {**proposed, 'configuration': location(args.config, ('packages', args.name)),
                  'module': REGISTRY[args.monitor].__name__, 'read_only': True}
        previous = snapshot.get('monitors', {}).get(args.name, {}).get(args.monitor, {})
        policy = refresh_policy(args.monitor, proposed, previous, options)
        result['refresh'] = {**vars(policy), 'due': proposed['status'] == 'pending' and
                            policy.due(previous, proposed['fingerprint'], datetime.now(timezone.utc))}
        if args.action == 'check':
            io = IO()  # contributor checks do not write production state/cache
            try:
                result.update(execute(args.monitor, proposed, io, schedule=policy))
            finally:
                io.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if isinstance(result, dict) and result.get('status') in ('error', 'partial', 'unsupported') else 0


if __name__ == '__main__':
    raise SystemExit(main())
