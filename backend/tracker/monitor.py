"""Internal monitor runner and contributor CLI. HTTP imports neither this nor adapters."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from . import config as cfg, state, monitor_eol, monitor_security, monitor_license
from .monitor_io import IO
from .monitor_model import subject, fingerprint, validate_findings

# Explicit trusted modules, not dynamic imports from configuration. A new monitor
# adds one adapter here; scheduling, storage, API and frontend stay unchanged.
REGISTRY = {'eol': monitor_eol, 'security': monitor_security, 'license': monitor_license}


def settings(config):
    raw = config.get('monitors', {})
    if set(raw) - {'enabled', 'interval_seconds', 'stale_after_seconds', 'batch_size', 'workers', 'heartbeat_seconds'}:
        raise ValueError('unknown monitor runner setting')
    result = {'enabled': [], 'interval_seconds': 1800, 'stale_after_seconds': 86400,
              'batch_size': 256, 'workers': 4, 'heartbeat_seconds': 30, **raw}
    if not isinstance(result['enabled'], list) or len(set(result['enabled'])) != len(result['enabled']) or set(result['enabled']) - set(REGISTRY):
        raise ValueError('enabled monitor must be registered')
    for key, maximum in [('interval_seconds', 604800), ('stale_after_seconds', 2592000), ('batch_size', 10000), ('workers', 8), ('heartbeat_seconds', 3600)]:
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError('invalid monitor setting: ' + key)
    if result['stale_after_seconds'] <= result['interval_seconds']:
        raise ValueError('monitor stale threshold must exceed interval')
    return result


def plan(config, snapshot, name, provider):
    adapter = REGISTRY[provider]
    current = subject(snapshot, name)
    binding = cfg.binding(config, name)
    identity = cfg.public_source(config.get('native', {}).get(binding.get('compare'), {}))
    configured = config.get('packages', {}).get(name, {}).get('monitors', {}).get(provider)
    # Adapters receive a small derived package context, not the database schema,
    # entire snapshot, operator config or a writable state handle.
    inputs = adapter.inputs({**current, 'identity': identity}, configured)
    scope = getattr(adapter, 'SCOPE', 'current')
    if scope not in ('current', 'upgrade'):
        raise ValueError('invalid monitor scope')
    note, status = None, 'pending'
    if scope == 'upgrade':
        binding = cfg.binding(config, name)
        target = snapshot.get('tracks', {}).get(binding.get('compare'), {})
        latest = target.get('version')
        current['target_version'] = latest
        if (target.get('error') or state.stale(target, datetime.now(timezone.utc), snapshot.get('stale_after_seconds', 86400))
                or state.compare(current['version'], latest, binding.get('comparable', True)) != 'outdated'):
            status, note = 'not_applicable', 'No confirmed version upgrade; this monitor is not run.'
    if inputs is None:
        status, note = 'not_configured', 'No reliable monitor identity/configuration.'
    source = state.current_source(snapshot, name)
    if inputs is not None and (not state.usable_version(current['version']) or not current['revision'] or source.get('error')
                               or state.stale(source, datetime.now(timezone.utc), source['stale_after_seconds'])):
        status, note = 'unsupported', 'Current source version/revision is not established.'
    fp = fingerprint({'provider': provider, 'adapter_version': adapter.VERSION, 'subject': current, 'inputs': inputs})
    return {'subject': current, 'scope': scope, 'fingerprint': fp, 'inputs': inputs,
            'status': status, 'note': note, 'findings': [], 'error': None}


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


def execute(provider, proposed, io, previous=None):
    if proposed['status'] != 'pending':
        return proposed
    at = state.utcnow()
    try:
        output = REGISTRY[provider].check(proposed['subject'], proposed['inputs'], io.for_hosts(REGISTRY[provider].HOSTS))
        if set(output) != {'status', 'findings', 'note'} or output['status'] not in ('ok', 'partial', 'unsupported'):
            raise ValueError('invalid monitor result')
        validate_findings(output['findings'])
        for f in output['findings']:
            if f['scope'] != proposed['scope']:
                raise ValueError('monitor finding scope mismatch')
            if f['scope'] == 'upgrade' and f['target_version'] != proposed['subject'].get('target_version'):
                raise ValueError('monitor finding target mismatch')
        revision = evidence_revision(
            {'subject': proposed['subject'], 'input_fingerprint': proposed['fingerprint']}, output['findings'])
        old = previous if previous and previous.get('fingerprint') == proposed['fingerprint'] else {}
        unchanged = old.get('evidence_revision') == revision
        if unchanged:
            output['findings'] = old['findings']
        return {**proposed, **output, 'attempted_at': at, 'checked_at': at,
                'evidence_revision': revision,
                'changed_at': old.get('changed_at') if unchanged else at}
    except Exception as error:
        # Only retain observations belonging to this exact input and adapter.
        old = previous if previous and previous.get('fingerprint') == proposed['fingerprint'] else {}
        return {**proposed, 'status': 'error', 'attempted_at': at,
                'checked_at': old.get('checked_at'), 'findings': old.get('findings', []),
                'evidence_revision': old.get('evidence_revision'), 'changed_at': old.get('changed_at'),
                'error': type(error).__name__, 'note': 'Check failed; matching prior findings are retained, not refreshed.'}


def check(config, snapshot, name, provider, io):
    if provider not in REGISTRY or name not in snapshot.get('sources', {}):
        raise ValueError('unknown package or monitor')
    proposed = plan(config, snapshot, name, provider)
    return execute(provider, proposed, io)


def collect(config, config_path, db, *, io=None):
    options = settings(config)
    own_io = io is None
    io = io or IO(Path(db).parent / 'monitor-cache')
    try:
        with state.writer_lock(str(db) + '.monitors'):
            snapshot = state.read(db)
            observations, jobs = {}, []
            now = datetime.now(timezone.utc)
            for name in snapshot.get('sources', {}):
                observations[name] = {}
                for provider in options['enabled']:
                    proposed = plan(config, snapshot, name, provider)
                    previous = snapshot.get('monitors', {}).get(name, {}).get(provider, {})
                    same = previous.get('fingerprint') == proposed['fingerprint']
                    if proposed['status'] != 'pending':
                        observations[name][provider] = proposed
                    elif same and not state.stale(
                            {'fetched_at': previous.get('attempted_at') or previous.get('checked_at')},
                            now, options['interval_seconds']):
                        observations[name][provider] = previous
                    else:
                        observations[name][provider] = previous if same else proposed
                        jobs.append((previous.get('attempted_at', '') if same else '', provider, name, proposed, previous))
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
            def publish():
                active = cfg.load(config_path)
                if (active['config_digest'], active['nv_digest']) != (config['config_digest'], config['nv_digest']):
                    raise ValueError('monitor configuration changed during collection')
                with state.writer_lock(db, timeout=60):
                    latest = state.read(db)
                    if latest.get('obs') != snapshot.get('obs'):
                        raise ValueError('monitor source scope changed during collection')
                    valid = {}
                    for name, providers in observations.items():
                        if name not in latest.get('sources', {}):
                            continue
                        valid[name] = {}
                        for provider, fact in providers.items():
                            expected = plan(config, latest, name, provider)
                            valid[name][provider] = fact if expected['fingerprint'] == fact['fingerprint'] else expected
                    if (latest.get('monitors') == valid and
                            latest.get('monitor_stale_after_seconds') == options['stale_after_seconds']):
                        return latest
                    result = state.merge(latest, 'monitors', {'monitors': valid,
                        'monitor_stale_after_seconds': options['stale_after_seconds']})
                    state.commit(db, result)
                return result
            # Publish input invalidation and completed checks without waiting for
            # the slowest provider; coalesce writes just like upstream collection.
            publish()
            last_published = 0.0
            with ThreadPoolExecutor(max_workers=options['workers']) as pool:
                pending = {pool.submit(execute, provider, proposed, io, previous): (name, provider)
                           for _, provider, name, proposed, previous in selected_jobs}
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
    snapshot = state.read(args.db); config = cfg.load(args.config, snapshot=snapshot)
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
        if args.action == 'check':
            io = IO()  # contributor checks do not write production state/cache
            try:
                result.update(execute(args.monitor, proposed, io))
            finally:
                io.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if isinstance(result, dict) and result.get('status') in ('error', 'partial', 'unsupported') else 0


if __name__ == '__main__':
    raise SystemExit(main())
