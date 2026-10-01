"""Topic composition renders secondary evidence once and keeps filter links true."""
from tests.helpers.query import conjunction, terms_in

from tracker.presentation import (
    navigation as presentation_navigation,
    pages as presentation_pages,
    version as presentation_version,
)


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
    links = presentation_navigation.Links({'filters': conjunction({'buildsystem': 'meson'}).model_dump(),'monitor': 'version'})
    row, = presentation_version.version_cells(pkg, pkg['monitors']['version'], links)
    values = row.lines[0]
    assert [v.text for v in values] == ['1.0', '→', '2.0', 'Yanked · 1.0', 'Advisory 2', 'LicenseDiff', 'DepChanges 3', 'Stale']
    assert 'CVE' not in row.model_dump_json()
    tags = [v for v in values if v.kind == 'tag']
    for tag, mid in zip(tags, ('yanked', 'security', 'license', 'requires')):
        assert terms_in(tag.href) == [('buildsystem', 'meson'), ('version_signal', mid)]
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
