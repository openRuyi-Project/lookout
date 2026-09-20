from copy import deepcopy
from tracker import view


def test_provider_groups_preserve_each_package_error():
    rows = [
        {'name': name, 'upstream': {'error': error, 'source': {'source': 'git', 'git': url}}}
        for name, error, url in [
            ('a', 'timeout', 'https://forge.example/a'),
            ('b', 'timeout', 'https://forge.example/b'),
            ('c', 'no matching version', 'https://forge.example/c'),
            ('d', 'timeout', 'https://other.example/d'),
            ('e', None, 'https://forge.example/e'),
        ]
    ]
    before = deepcopy(rows)
    result = view.upstream_failures(rows)
    group = next(g for g in result if g['provider'] == 'forge.example' and g['error'] == 'timeout')
    assert group['count'] == 2 and group['packages'] == ['a', 'b']
    assert len(result) == 3 and rows == before
