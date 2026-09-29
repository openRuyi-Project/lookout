"""Topic composition renders secondary evidence once and keeps filter links true."""
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.helpers.listing import document, follow, modes, facet_destination
from tracker.presentation import (
    navigation as presentation_navigation,
    pages as presentation_pages,
    version as presentation_version,
)


def test_global_buildsystem_is_one_sidebar_with_link_equivalent_counts(scoped_client):
    for monitor in ('', 'version', 'requires', 'build', 'fixture_signature'):
        for check in ('', 'uncovered', 'failed'):
            page = document(scoped_client, {'monitor': monitor, 'check': check, 'buildsystem': 'cmake', 'q': 'u'})
            sidebar, = page['global_navigation']
            assert sidebar['label'] == 'BuildSystem'
            hidden = {p['name']: p['value'] for p in page['controls']['hidden']}
            assert hidden['buildsystem'] == 'cmake'
            for choice in sidebar['choices']:
                facet_destination(scoped_client, page, choice)
            for choice in page['navigation']['choices']:
                assert parse_qs(urlsplit(choice['href']).query)['buildsystem'] == ['cmake']


def package(relation='outdated'):
    return {'name': 'fixture', 'detail_url': '/packages/fixture', 'monitors': {
        'version': {'id': 'version', 'data': {'kind': 'version', 'current': '1.0', 'latest': '2.0',
            'track': 'fixture', 'relation': relation, 'annotations': [
                {'monitor': monitor, 'label': label, 'count': count, 'scope': scope,
                 'target_version': '2.0' if scope == 'upgrade' else None, 'stale': stale}
                for monitor, label, count, scope, stale in [
                    ('yanked', 'Yanked', 1, 'current', False),
                    ('security', 'Advisory', 2, 'current', False),
                    ('license', 'LicenseDiff', 1, 'upgrade', True),
                    ('requires', 'DepChanges', 3, 'upgrade', False)]]}},
    }}


def test_version_compacts_secondary_evidence_and_explains_yanked_subject():
    pkg = package()
    links = presentation_navigation.Links({'monitor': 'version', 'buildsystem': 'meson'})
    row, = presentation_version.version_cells(pkg, pkg['monitors']['version'], links)
    values = row.lines[0]
    assert [v.text for v in values] == ['1.0', '→', '2.0', 'Yanked · 1.0', 'Advisory 2', 'LicenseDiff', 'DepChanges 3', 'Stale']
    assert 'CVE' not in row.model_dump_json()
    tags = [v for v in values if v.kind == 'tag']
    for tag, mid in zip(tags, ('yanked', 'security', 'license', 'requires')):
        query = parse_qs(urlsplit(tag.href).query)
        assert query['signal'] == [mid] and query['buildsystem'] == ['meson']
    assert tags[0].title == 'Current release 1.0'
    assert 'local patches not evaluated' in tags[1].title
    assert tags[2].tone == 'notice' and 'retained' in tags[2].title


def test_current_version_keeps_security_label_without_claiming_upgrade():
    pkg = package('current')
    row, = presentation_version.version_cells(pkg, pkg['monitors']['version'], presentation_navigation.Links())
    assert '→' not in [v.text for v in row.lines[0]]
    assert 'Advisory 2' in [v.text for v in row.lines[0]]
    assert 'Yanked' in [v.text for v in row.lines[0]]


def test_detail_annotations_link_existing_fact_sections_not_aggregate_search():
    values = presentation_version.version_annotations(package())
    assert [v.href for v in values] == ['#yanked', '#security', '#license', '#requires']


def test_related_filters_are_scoped_to_version_and_discarded_in_coverage(scoped_client):
    for monitor in ('version', 'requires', 'build', 'fixture_signature'):
        for check in ('', 'uncovered'):
            query = {'monitor': monitor, 'signal': 'security', 'check': check}
            actual = document(scoped_client, query)
            expected_query = query if monitor == 'version' and not check else {k: v for k, v in query.items() if k != 'signal'}
            assert actual == document(scoped_client, expected_query)


def test_version_related_filter_links_match_rows_and_leave_with_topic(scoped_client):
    # The pure filter index test covers nonempty annotations. Here even a selected
    # missing value must remain visible/removable rather than silently ignored.
    page = document(scoped_client, {'monitor': 'version', 'signal': 'security', 'buildsystem': 'cmake'})
    row, = page['controls']['choice_rows']
    assert row['label'] == 'Related'
    assert next(o for o in row['choices'] if o['selected'])['count'] == 0
    for choice in modes(page):
        if choice['count'] is not None:
            assert follow(scoped_client, choice['href'])['total'] == choice['count']
    for choice in page['navigation']['choices']:
        query = parse_qs(urlsplit(choice['href']).query)
        if query.get('monitor') not in (['version'], None):
            assert 'signal' not in query


def test_navigation_deemphasizes_eol_and_omits_standalone_withdrawal():
    # Keep the navigation fixture independent of any production package/count.
    descriptors = [dict(id=mid, title=title, kind=kind) for mid, title, kind in [
        ('version', 'Version', 'version'), ('eol', 'EOL', 'evidence'),
        ('yanked', 'Yanked', 'evidence'), ('security', 'Advisory', 'evidence')]]
    payload = dict(monitors=descriptors, items=[], targets=[], section='results',
        page=1, pages=1, total=0, per_page=100, counts=dict(all=0, updates=0, untracked=0),
        buildsystems={}, maintenance_labels={}, build_statuses={},
        result_count=0, coverage_count=0, check_statuses={},
        collection=dict(mode='fixture', obs_updated_at=None, upstream_updated_at=None))
    doc = presentation_pages.listing(payload, {})
    assert [c.label for c in doc.navigation.choices] == ['Packages', 'Version', 'Advisory', 'EOL']
    assert doc.global_navigation[0].choices == []


def test_all_related_option_is_not_an_active_filter():
    payload = dict(section='results', monitors=[dict(id='security', title='Advisory', kind='evidence')],
        version_signals={'security': 1}, maintenance_labels={}, targets=[], build_statuses={},
        counts=dict(all=1, updates=0, untracked=0), check_statuses={}, check_groups={'failed': 0, 'uncovered': 0})
    controls = presentation_navigation.listing_controls(payload, {'monitor':'version'},
        dict(id='version', title='Version', kind='version'), presentation_navigation.Links())
    assert not controls.active
