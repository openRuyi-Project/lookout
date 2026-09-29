"""Build rows keep sparse alternatives and existing selection semantics."""
from urllib.parse import parse_qs, urlsplit

from tracker.presentation.navigation import Links, build_navigation


def test_inline_rows_keep_sparse_statuses_without_changing_existing_choices():
    payload = {
        'targets': [{'id': 'alpha', 'label': 'Target A'}, {'id': 'beta', 'label': 'Target B'}],
        'build_statuses': {
            'alpha': [{'value': 'failed', 'label': 'Failed', 'count': 2}],
            'beta': [{'value': 'succeeded', 'label': 'Succeeded', 'count': 3},
                     {'value': 'future-state', 'label': 'Future state', 'count': 1}],
        },
    }
    query = {'build': ['alpha:failed'], 'buildsystem': 'fixture', 'q': 'widget'}
    links = Links(query)
    existing = build_navigation(payload, query, links)
    aligned = build_navigation(payload, query, links, inline=True)
    expected = ['Succeeded', 'Failed', 'Future state']
    for target, row in zip(('alpha', 'beta'), aligned.values(), strict=True):
        assert [choice.label for choice in row.choices] == expected
        for choice in existing[target].choices:
            if choice.count is not None:
                assert choice in row.choices
    missing = aligned['beta'].choices[1]
    assert missing.count == 0 and not missing.selected
    assert parse_qs(urlsplit(missing.href).query) == {
        'q': ['widget'], 'page': ['1'], 'build': ['beta:failed']}
