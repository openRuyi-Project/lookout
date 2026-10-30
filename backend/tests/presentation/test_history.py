"""History keeps source identity without repeating metadata field labels."""
from copy import deepcopy

import pytest

from tracker.presentation.activity import section
from tracker.presentation.source import changelog_section


def test_changelog_keeps_titles_and_provenance_without_mutating_observations():
    source = {'data': {'changelog': [{'commit': 'a' * 40, 'subject': 'Keep the original subject',
        'author': 'Author', 'date': '2026-01-02T03:04:05+00:00', 'signed_off_by': ['Reviewer <reviewer@example.org>']}]}}
    before = deepcopy(source)
    history, = changelog_section(source, {'url': 'https://gitlab.example.org/team/project.git'})
    entry, = history.entries
    assert entry.heading[0].text == 'Keep the original subject'
    assert entry.heading[0].href == 'https://gitlab.example.org/team/project/-/commit/' + 'a' * 40
    assert [v.text for v in entry.metadata] == ['Author', 'a' * 12, '2026-01-02 03:04:05 UTC']
    assert entry.metadata[1].title == 'a' * 40
    assert entry.metadata[2].datetime == '2026-01-02T03:04:05+00:00'
    assert not entry.fields
    assert source == before


@pytest.mark.parametrize('repository', [None, {'url': 'file:///repo'},
    {'url': 'https://unknown.example/repo'}, {'url': 'https://user:secret@github.com/team/repo'}])
def test_history_without_verified_forge_has_no_invented_link(repository):
    source = {'data': {'changelog': [{'commit': 'b' * 40, 'subject': 'Change', 'author': 'Author',
        'date': '2026-01-02T03:04:05Z', 'signed_off_by': []}]}}
    history, = changelog_section(source, repository)
    assert history.entries[0].heading[0].href is None
    assert history.entries[0].metadata[1].href is None


def test_activity_author_and_time_share_the_byline_but_failures_remain_visible():
    item = dict(kind='pr', status='merged', number=7, url='https://github.com/team/repo/pull/7',
        title='Change', author='Author', updated_at='2026-01-02T03:04:05Z', available=False, paths_complete=False)
    result = section('fixture', 'pr', {'items': [item], 'next_cursor': None, 'total': 1})
    entry, = result.entries
    assert entry.heading[0].title == 'merged'
    assert [v.text for v in entry.metadata] == ['Author', '2026-01-02 03:04:05 UTC']
    assert len(entry.fields) == 2
