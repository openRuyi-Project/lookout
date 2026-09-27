"""Inspect or check one monitor without publishing observations."""
import argparse
from datetime import datetime, timezone
import json

from tracker import config as cfg, state
from tracker.monitors.registry import REGISTRY
from tracker.monitors.runner import execute, plan, refresh_policy, settings
from tracker.providers.client import IO

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
        from tracker.package import location
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

if __name__ == "__main__":
    raise SystemExit(main())
