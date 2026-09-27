"""Locate, inspect or check one monitor without publishing observations."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from tracker import config as cfg, identity, state
from tracker.monitors.registry import REGISTRY
from tracker.monitors.runner import execute, plan, refresh_policy, settings
from tracker.monitors.version import compare
from tracker.package import package_location, rule_location
from tracker.providers.client import IO


def describe(config, snapshot, name, monitor):
    adapter = REGISTRY[monitor]
    observed = name in snapshot.get('sources', {}) or name in snapshot.get('specs', {})
    if not observed:
        # Configuration supplies identity, not observations. The ordinary planner
        # still reports missing source/version inputs; no synthetic success exists.
        snapshot = {**snapshot, 'native_ids': list(config['native']), 'bindings': config['packages']}
    version = compare.evaluate(snapshot, name)
    proposed = plan(config, snapshot, name, monitor, version=version)
    configured = config['packages'].get(name, {}).get('monitors', {})
    mode = 'explicit' if monitor in configured else 'derived' if proposed['inputs'] is not None else 'unconfigured'
    options = settings(config)
    previous = snapshot.get('monitors', {}).get(name, {}).get(monitor, {})
    policy = refresh_policy(monitor, proposed, previous, options)
    track = cfg.binding(config, name)['compare']
    source_release = version.release.public() if version.release else None
    context = identity.package_context({'source_release': source_release,
                                        'identity': cfg.public_source(config['native'].get(version.track, {}))})
    return {
        **proposed,
        'name': name,
        'monitor': monitor,
        'enabled': monitor in options['enabled'],
        'mapping': mode,
        'configuration': package_location(config, name, 'monitors', monitor),
        'version_rule': rule_location(config, version.track) if version.track else None,
        'configured_version_rule': rule_location(config, track) if track and track != version.track else None,
        'identity_context': context if mode == 'derived' else None,
        'source_release': source_release if mode == 'derived' and context['origin'] == 'source_release' else None,
        'module': adapter.__name__,
        'code': str(Path(adapter.__file__).resolve()),
        'observation_available': observed,
        'refresh': {**vars(policy), 'due': proposed['status'] == 'pending' and
                    policy.due(previous, proposed['fingerprint'], datetime.now(timezone.utc))},
        'read_only': True,
    }, policy


def human_result(action, result, config_path):
    if action == 'list':
        return '\n'.join(f"{name}: {'enabled' if item['enabled'] else 'disabled'} · {item['code']}"
                         for name, item in result.items())
    lines = [f"Package: {result['name']}",
             f"Monitor: {result['monitor']} ({'enabled' if result['enabled'] else 'disabled'})",
             'Mapping: ' + result['mapping']]
    if result['inputs'] is not None:
        lines.append('Inputs: ' + json.dumps(result['inputs'], ensure_ascii=False, sort_keys=True))
    context = result['identity_context']
    if context and context['origin'] == 'source_release':
        lines.append('Identity: observed Source0 · ' + result['source_release']['url'])
    elif context and context['origin'] == 'native' and result['version_rule']:
        rule = result['version_rule']
        lines.append(f"Identity: native rule {'.'.join(rule['table'])} · {rule['file']}:{rule['line'] or '?'}")
    if result['configured_version_rule']:
        rule = result['configured_version_rule']
        lines.append(f"Configured rule: {'.'.join(rule['table'])} · {rule['file']}:{rule['line'] or '?'} (not the snapshot binding)")
    location = result['configuration']
    if location:
        suffix = ':' + str(location['line']) if location['line'] else ' (add package monitor settings)'
        label = 'Edit: ' if result['mapping'] == 'explicit' else 'Optional override: '
        lines.append(label + location['file'] + suffix)
    elif result['mapping'] != 'derived':
        lines.append('Override: set packages_config in ' + str(Path(config_path).resolve()))
    lines.append('Code: ' + result['code'])
    if result['observation_available']:
        lines.append('Check: ' + result['status'])
    else:
        lines.append('Observation: none; configuration only')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('list', 'explain', 'check'))
    parser.add_argument('name', nargs='?')
    parser.add_argument('--monitor')
    parser.add_argument('--config', required=True)
    parser.add_argument('--db')
    parser.add_argument('--format', choices=('json', 'human'), default='json')
    args = parser.parse_args(argv)
    if args.action != 'list' and (not args.name or args.monitor not in REGISTRY):
        parser.error('explain/check require NAME --monitor REGISTERED_ID')
    try:
        config = cfg.load(args.config)
        options = settings(config)
        if args.action == 'list':
            result = {name: {'module': adapter.__name__, 'code': str(Path(adapter.__file__).resolve()),
                             'scope': getattr(adapter, 'SCOPE', 'current'),
                             'enabled': name in options['enabled'], 'adapter_version': adapter.VERSION}
                      for name, adapter in REGISTRY.items()}
        else:
            snapshot = state.read(args.db) if args.db else state.empty()
            result, policy = describe(config, snapshot, args.name, args.monitor)
            if args.action == 'check':
                if not args.db or not result['observation_available']:
                    message = 'check requires --db with an observed package'
                    print('Error: ' + message if args.format == 'human' else json.dumps({'error': message}))
                    return 2
                io = IO()  # Contributor checks never write production state/cache.
                try:
                    result.update(execute(args.monitor, result, io, schedule=policy))
                finally:
                    io.close()
        cfg.require_unchanged(config, args.config)
    except (OSError, ValueError, sqlite3.Error) as error:
        message = 'Cannot read or validate monitor inputs (' + type(error).__name__ + ')'
        print('Error: ' + message if args.format == 'human' else json.dumps({'error': message}))
        return 2
    print(human_result(args.action, result, args.config) if args.format == 'human'
          else json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if args.action == 'check' and result.get('status') in ('error', 'partial', 'unsupported') else 0


if __name__ == '__main__':
    raise SystemExit(main())
