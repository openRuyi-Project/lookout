"""Evidence layout preserves subject identity and source attribution."""
from copy import deepcopy

from tracker.monitors.model import evidence, finding
from tracker.presentation.evidence import evidence_section
from tracker.presentation.navigation import Links


def section(facts):
    item = finding('CVE-2099-1000', 'Advisory', 'CVE-2099-1000', facts, 'https://example.org/advisory')
    item['stale'] = False
    return evidence_section({'id': 'security', 'title': 'Advisory', 'data': {'findings': [item]}}, Links())[0]


def test_repeated_advisory_subject_retains_its_provider_link():
    facts = [evidence('Renamed field', 'CVE-2099-1000', 'NVD', 'https://example.org/nvd', code='advisory_id'),
             evidence('Returned advisory', 'GHSA-other', 'OSV', 'https://example.org/osv', code='advisory_id')]
    before = deepcopy(facts)
    entry = section(facts).entries[0]
    assert [(value.text, value.href) for value in entry.metadata] == [('NVD', 'https://example.org/nvd')]
    assert [field.values[0].text for field in entry.fields] == ['GHSA-other']
    assert facts == before


def test_uncoded_or_unavailable_subject_facts_remain_visible():
    entry = section([
        evidence('Returned advisory', 'CVE-2099-1000', 'OSV', 'https://example.org/osv'),
        evidence('Returned advisory', None, 'NVD', 'https://example.org/nvd',
                 code='advisory_id', status='unavailable'),
    ]).entries[0]
    assert not entry.metadata
    assert [field.values[0].text for field in entry.fields] == ['CVE-2099-1000', 'unavailable']


def test_epss_dates_stay_next_to_their_subject_without_parsing_labels():
    facts = []
    for subject, probability, day in [('one', .004, '2099-01-01'), ('two', .01, '2099-01-02')]:
        url = 'https://example.org/epss?cve=' + subject
        facts += [evidence('Odds ' + subject, probability, 'FIRST', url, code='epss_probability'),
                  evidence('As of ' + subject, day, 'FIRST', url, code='epss_date'),
                  evidence('Other ' + subject, True, 'Provider', url)]
    fields = section(facts).entries[0].fields
    assert [(field.label, field.values[0].text) for field in fields[:4]] == [
        ('Odds one', '0.4%'), ('As of one', '2099-01-01'), ('Odds two', '1%'), ('As of two', '2099-01-02')]
    assert all(field.values[-1].href for field in fields)
