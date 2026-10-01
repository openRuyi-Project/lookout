#!/usr/bin/env python3
"""Offline, deterministic query-budget measurement; no provider or database IO."""
import json
from pathlib import Path
import statistics
import sys
import time
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from tracker.readmodel.packages import PackageList
from tracker.readmodel.query import FilterQuery, MAX_QUERY_NODES


def main():
    # Dense facts stress union/count cost; snapshot construction is not timed.
    rows = [{'name': f'package-{i}', 'monitors': {'fixture': {'dimensions': {
        'maintenance': [f'label-{j}' for j in range(64) if (i + j) % 3],
        'buildsystem': [f'system-{i % 24}'],
        **{f'build:{target}': [f'state-{(i + target) % 12}'] for target in range(3)},
    }}}} for i in range(6000)]
    index = PackageList(rows, [{'id': str(i)} for i in range(3)])
    samples = []
    for nodes in (16, 32, 64, MAX_QUERY_NODES):
        for root in ('and', 'or'):
            groups = []
            remaining = nodes
            while remaining >= 2:
                count = min(15, remaining - 1)
                groups.append({'logic': root, 'conditions': [
                    {'dimension': 'maintenance', 'value': f'label-{(len(groups) * 7 + i) % 64}', 'logic': 'or'}
                    for i in range(count)]})
                remaining -= count + 1
            if remaining:
                groups.append({'logic': 'and', 'conditions': []})
            wire = json.dumps({'groups': groups}, separators=(',', ':'))
            durations = []
            for _ in range(31):
                start = time.perf_counter()
                query = FilterQuery.model_validate_json(wire)
                result = index.select(filters=query, active_group=len(groups) - 1, next_logic=root, per_page=100)
                durations.append((time.perf_counter() - start) * 1000)
            samples.append({'nodes': nodes, 'group_join_and_next': root, 'packages': len(rows),
                'candidate_counts': sum(len(result[key]) for key in (
                    'counts', 'buildsystems', 'maintenance_labels', 'version_signals', 'requires_counts'))
                    + sum(map(len, result['build_statuses'].values())),
                'url_bytes': len(quote(wire, safe='')), 'p50_ms': round(statistics.median(durations), 3),
                'p95_ms': round(sorted(durations)[29], 3)})
    print(json.dumps({'python': sys.version, 'node_limit': MAX_QUERY_NODES, 'samples': samples}, indent=2))


if __name__ == '__main__':
    main()
