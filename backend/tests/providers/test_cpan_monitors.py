import pytest

from tracker.monitors import license, yanked
from tracker.monitors.requires import cpan
from tracker.monitors.requires.compare import satisfies
from tracker.providers import cpan as provider


class IO:
    def __init__(self, **metadata):
        self.metadata = {'dynamic_config': 0, 'meta-spec': {'version': 2}, **metadata}
        self.hits = 1

    def json(self, method, url, body, **kwargs):
        assert method == 'POST' and body['size'] == 2
        terms = {k: v for row in body['query']['bool']['filter'] for k, v in row['term'].items()}
        return {'hits': {'hits': [{'_source': {
            'distribution': terms['distribution'], 'version': terms['version'], 'authorized': True,
            'name': 'Example-' + terms['version'], 'author': 'AUTHOR', 'metadata': self.metadata,
            'license': ['perl_5'] if terms['version'] == '1' else ['artistic_2'],
        }}] * self.hits}}


def test_shared_identity_and_licenses_do_not_imply_yanked_support():
    package = {'identity': {'source': 'cpan', 'cpan': 'Example'}}
    assert license.inputs(package, None) == {'cpan': 'Example'}
    assert yanked.inputs(package, None) is None
    result = license.check({'version': '1', 'target_version': '2'}, {'cpan': 'Example'}, IO())
    assert result['status'] == 'ok'
    assert result['findings'][0]['title'] == 'Artistic-1.0-Perl OR GPL-1.0-or-later → Artistic-2.0'
    assert result['findings'][0]['facts'][0]['value'] == 'perl_5'


def test_exact_release_missing_or_ambiguous_is_not_latest_fallback():
    io = IO()
    for count in (0, 2):
        io.hits = count
        with pytest.raises(ValueError, match='missing or ambiguous'):
            provider.release('Example', '1', io)


def test_only_static_runtime_prerequisites_are_observed():
    io = IO(prereqs={'runtime': {'requires': {'perl': '5.010', 'Some::Module': '>= 2, != 3'},
                                'recommends': {'Optional': '1'}},
                     'build': {'requires': {'Builder': '99'}}})
    rows = cpan.read('1', {'cpan': 'Example'}, io)
    assert [r.name for r in rows] == ['perl', 'Some::Module', 'Optional']
    assert rows[1].identity == {'ecosystem': 'CPANModule', 'name': 'Some::Module'}
    assert rows[2].optional is True
    assert rows[0].optional is False
    for dynamic in (1, '1', None):
        io.metadata['dynamic_config'] = dynamic
        with pytest.raises(cpan.UnsupportedRequirements, match='dynamic or unspecified'):
            cpan.read('1', {'cpan': 'Example'}, io)


def test_module_identity_is_not_mistaken_for_a_distribution():
    package = {'identity': {'source': 'cpan', 'cpan': 'Some::Module'}}
    assert provider.inputs(package, None) is None


def test_required_and_recommended_versions_remain_separate_declarations():
    from tracker.monitors.requires import monitor
    io = IO(prereqs={'runtime': {'requires': {'perl': '5.008'}, 'recommends': {'perl': '5.010'}}})
    result = monitor.check({'version': '1'}, {'cpan': 'Example'}, io)
    assert result['status'] == 'ok'
    assert len(result['findings']) == 2
    assert {r['requirement']['optional'] for r in result['findings']} == {False, True}


@pytest.mark.parametrize('required,observed,expected', [
    ('5.010', '5.40.0', True), ('5.041', '5.40.0', False),
    ('== 1.002003', 'v1.2.3', True), ('>= 1, != 2', '2', False),
    ('5.008001', '5.8.0', False), ('0', '5.40.0', True),
    ('1.2_01', '1.3', None), ('garbage', '1.3', None), ('> 9, invalid', '1.3', None),
])
def test_perl_constraint_syntax(required, observed, expected):
    assert satisfies('perl_version', required, observed)[0] is expected


def test_static_empty_prerequisites_are_known_not_dynamic():
    assert cpan.read('1', {'cpan': 'Example'}, IO()) == []


def test_provided_modules_keep_component_versions_and_ignore_unindexed():
    class IndexedIO(IO):
        def json(self, *args, **kwargs):
            data = super().json(*args, **kwargs)
            data['hits']['hits'][0]['_source']['provides'] = ['Some::Module', 'Unknown']
            return data
    io = IndexedIO(provides={'Some::Module': {'version': '0.9', 'file': 'lib/Some/Module.pm'},
                             'Unknown': {'file': 'lib/Unknown.pm'},
                             'Private': {'version': '99', 'file': 't/lib/Private.pm'}})
    provided = cpan.provides('12', {'cpan': 'Example'}, io)
    assert provided == [{'identity': {'ecosystem': 'CPANModule', 'name': 'Some::Module'},
                         'version': '0.9', 'source': 'MetaCPAN',
                         'url': 'https://metacpan.org/release/AUTHOR/Example-12'}]
